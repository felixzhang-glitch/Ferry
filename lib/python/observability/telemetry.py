"""Bounded, best-effort telemetry. Never serialize messages or exception text.

The main process is the sole writer of its journal. The independent web service
reads journals, and writes sidecar receipts to a separate producer journal.
Prometheus counters are process-lifetime snapshots, not rolling-window totals.
"""
from __future__ import annotations

import asyncio
import contextvars
import contextlib
import functools
import hashlib
import hmac
import inspect
import json
import math
import os
import queue
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BUCKETS = (.05, .1, .25, .5, 1., 2.5, 5., 10., 20., 30., 60., 120., 300., 600., 1800.)
ENUMS = {
    'channel': {'feishu', 'wechat', 'system'},
    'source': {'interactive', 'scheduled', 'push', 'system'},
    'status': {'received', 'accepted', 'duplicate', 'rejected', 'dropped', 'success', 'error',
               'cancelled', 'partial', 'unknown', 'start', 'end'},
    'stage': {'queue_wait', 'delivery', 'first_delivery', 'pipeline', 'agent', 'attempt',
              'first_text', 'tool', 'worker_wait', 'worker_setup', 'task_generation', 'task_delivery', 'channel_io'},
    'operation': {'reply', 'card_create', 'card_update', 'send', 'upload', 'download',
                  'reminder', 'daily', 'reset', 'other'},
    'tool': {'bash', 'read', 'write', 'edit', 'grep', 'find', 'ls', 'web', 'other'},
    'stop_reason': {'stop', 'toolUse', 'length', 'error', 'aborted', 'unknown'},
    'error_type': {'timeout', 'cancelled', 'transport', 'agent', 'unknown'},
}
KINDS = {'turn_start', 'turn', 'agent_call', 'attempt', 'model', 'tool', 'stage', 'message',
         'queue', 'delivery', 'task', 'session', 'retry', 'compaction', 'heartbeat', 'usage_gap', 'transport'}
NUMBERS = {'duration_seconds', 'ttft_seconds', 'started_at', 'tokens_input', 'tokens_output',
           'tokens_total', 'cache_read', 'cache_write', 'usage_missing', 'tool_calls',
           'attempts', 'count', 'model_responses'}
IDENTIFIER = re.compile(r'^[a-f0-9]{32}$')
MODEL_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,95}$')
GAUGES = {'queue_feishu', 'queue_wechat', 'active_tasks', 'worker_total', 'worker_busy',
          'worker_starting', 'circuit_open', 'session_mappings', 'daily_tasks', 'reminders',
          'rss_bytes', 'cpu_seconds', 'disk_free_bytes', 'event_loop_lag_seconds', 'pi_active', 'runtime_sampled_at'}


def clean_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """An explicit allowlist, used on both sides of the persistence boundary."""
    out: dict[str, Any] = {}
    for key, allowed in ENUMS.items():
        value = fields.get(key)
        if isinstance(value, str):
            out[key] = value if value in allowed else ('other' if key in {'tool', 'operation'} else 'unknown')
    for key in ('model', 'provider'):
        value = fields.get(key)
        if isinstance(value, str) and MODEL_ID.fullmatch(value):
            out[key] = value
    for key in ('run_id', 'session'):
        value = fields.get(key)
        if isinstance(value, str) and IDENTIFIER.fullmatch(value):
            out[key] = value
    for key in NUMBERS:
        value = fields.get(key)
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 2**53:
            out[key] = value
    return out


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        handle.flush()
    os.replace(tmp, path)


def counter_updates(event: dict[str, Any]):
    """Metric names and dimensions are bounded; IDs never become labels."""
    kind = event['kind']
    labels = {'channel': event.get('channel', 'system')}
    status = event.get('status', 'unknown')
    if kind == 'turn':
        yield 'ferry_turns_total', {**labels, 'source': event.get('source', 'system'), 'status': status}, 1
    elif kind == 'attempt':
        yield 'ferry_agent_attempts_total', {**labels, 'status': status}, 1
    elif kind == 'model':
        model_labels = {'provider': event.get('provider', 'unknown'), 'model': event.get('model', 'unknown')}
        yield 'ferry_model_responses_total', {**model_labels, 'stop_reason': event.get('stop_reason', 'unknown')}, 1
        for key in ('input', 'output', 'total'):
            value = event.get(f'tokens_{key}')
            if value is not None:
                yield 'ferry_tokens_total', {**model_labels, 'type': key}, value
        yield 'ferry_usage_missing_total', model_labels, event.get('usage_missing', 0)
    elif kind == 'usage_gap':
        yield 'ferry_usage_missing_total', {'provider': 'unknown', 'model': event.get('model', 'unknown')}, event.get('count', 1)
    elif kind == 'tool':
        yield 'ferry_tool_calls_total', {'tool': event.get('tool', 'other'), 'status': status}, 1
    elif kind in {'message', 'queue', 'delivery', 'transport', 'task', 'retry', 'compaction', 'session'}:
        if kind in {'delivery', 'transport', 'task'}:
            labels['operation'] = event.get('operation', 'other')
        if kind == 'task':
            labels['stage'] = event.get('stage', 'unknown')
        labels['status'] = status
        yield f'ferry_{kind}_events_total', labels, 1 if kind == 'delivery' else event.get('count', 1)


class Collector:
    def __init__(self, store_dir: str | Path, producer: str = 'main', retention_days: int = 30):
        if producer not in {'main', 'sidecar'}:
            raise ValueError('invalid telemetry producer')
        self.root = Path(store_dir)
        self.producer = producer
        self.retention_days = max(1, min(90, retention_days))
        self.boot_id = uuid.uuid4().hex
        self.started_at = time.time()
        self.pending: queue.Queue = queue.Queue(maxsize=4096)
        self.dropped = 0
        self.write_errors = 0
        self.persisted = 0
        self.last_event_at: float | None = None
        self.sequence = 0
        self.counters: Counter = Counter()
        self.histograms: dict[tuple, dict] = {}
        self.gauges: dict[str, float] = {}
        self.mode = 'unknown'
        self._models: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._file = None
        self._day = ''
        self._last_prune = 0.

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f'observability-{self.producer}', daemon=True)
            self._thread.start()
        return self

    def emit(self, kind: str, **fields) -> None:
        if kind not in KINDS or self._stop.is_set():
            return
        try:
            event = {'kind': kind, 'at': time.time(), **clean_fields(fields)}
            self.pending.put_nowait(event)
        except queue.Full:
            self.dropped += 1
        except Exception:
            self.dropped += 1

    def update_runtime(self, *, mode: str | None = None, **gauges) -> None:
        with self._lock:
            self.gauges['runtime_sampled_at'] = time.time()
            if mode in {'cli', 'worker'}:
                self.mode = mode
            for key, value in gauges.items():
                if key in GAUGES and type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    self.gauges[key] = value

    def flush(self, timeout: float = 3.) -> bool:
        deadline = time.monotonic() + timeout
        while self.pending.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        return self.pending.unfinished_tasks == 0

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.)

    def _record(self, event: dict) -> None:
        model = event.get('model')
        if model:
            pair = (event.get('provider', 'unknown'), model)
            if pair not in self._models and len(self._models) >= 64:
                event['model'] = 'other'
                event['provider'] = 'other'
                pair = ('other', 'other')
            self._models.add(pair)
        for name, labels, value in counter_updates(event):
            self.counters[(name, tuple(sorted(labels.items())))] += value
        duration = event.get('duration_seconds')
        stage = event.get('stage')
        if duration is not None and stage in ENUMS['stage']:
            key = (stage, event.get('channel', 'system'))
            histogram = self.histograms.setdefault(key, {'count': 0, 'sum': 0., 'buckets': [0] * len(BUCKETS)})
            histogram['count'] += 1
            histogram['sum'] += duration
            for index, boundary in enumerate(BUCKETS):
                if duration <= boundary:
                    histogram['buckets'][index] += 1
        self.last_event_at = event['at']

    def _append(self, event: dict) -> None:
        self.sequence += 1
        event.update(v=1, boot_id=self.boot_id, seq=self.sequence, producer=self.producer)
        self._record(event)
        day = time.strftime('%Y-%m-%d', time.gmtime(event['at']))
        if self._file is None or self._day != day:
            if self._file:
                self._file.close()
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.root / f'events-{day}-{self.producer}-{self.boot_id}.jsonl'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._file = os.fdopen(fd, 'a', encoding='utf-8', buffering=1)
            self._day = day
        self._file.write(json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n')
        self.persisted += 1

    def _snapshot(self) -> None:
        with self._lock:
            gauges, mode = dict(self.gauges), self.mode
        snapshot = {
            'v': 1, 'producer': self.producer, 'boot_id': self.boot_id,
            'timestamp': time.time(), 'started_at': self.started_at, 'pid': os.getpid(),
            'mode': mode, 'gauges': gauges, 'seq': self.sequence, 'persisted': self.persisted,
            'last_event_at': self.last_event_at, 'dropped_events': self.dropped,
            'write_errors': self.write_errors, 'pending_events': self.pending.qsize(),
            'counters': [{'name': name, 'labels': dict(labels), 'value': value}
                         for (name, labels), value in self.counters.items()],
            'histograms': [{'stage': stage, 'channel': channel, **value}
                           for (stage, channel), value in self.histograms.items()],
        }
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_json(self.root / f'{self.producer}-current.json', snapshot)
        # Only our own dated journals; never traverse native transcripts or logs.
        if time.monotonic() - self._last_prune > 3600:
            cutoff = time.strftime('%Y-%m-%d', time.gmtime(time.time() - self.retention_days * 86400))
            for path in self.root.glob(f'events-????-??-??-{self.producer}-*.jsonl'):
                if path.name[7:17] < cutoff:
                    path.unlink(missing_ok=True)
            self._last_prune = time.monotonic()

    def _run(self) -> None:
        next_snapshot = 0.
        while not self._stop.is_set() or not self.pending.empty():
            try:
                event = self.pending.get(timeout=.25)
            except queue.Empty:
                event = None
            if event is not None:
                try:
                    self._append(event)
                except Exception:
                    self.write_errors += 1
                    if self._file:
                        try:
                            self._file.close()
                        except OSError:
                            pass
                    self._file = None
                finally:
                    self.pending.task_done()
            if time.monotonic() >= next_snapshot:
                try:
                    self._snapshot()
                except Exception:
                    self.write_errors += 1
                next_snapshot = time.monotonic() + 2.
        try:
            if self._file:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
            self._snapshot()
        except Exception:
            self.write_errors += 1


@dataclass
class Run:
    id: str
    channel: str
    source: str
    session: str = ''
    started_at: float = field(default_factory=time.time)
    started: float = field(default_factory=time.monotonic)
    status: str = 'unknown'
    model: str = ''
    attempts: int = 0
    responses: int = 0
    tools: int = 0
    tokens: int = 0
    missing: int = 0
    ttft: float | None = None
    attempt_serial: int = 0
    tool_started: dict = field(default_factory=dict)
    seen_responses: set = field(default_factory=set)
    open_responses: int = 0


_collector: Collector | None = None
_hmac_key: bytes = b''
_trace_runs: dict[str, str] = {}
_current: contextvars.ContextVar[Run | None] = contextvars.ContextVar('observability_run', default=None)


def configure(settings) -> None:
    global _collector, _hmac_key
    if not settings.enabled or _collector is not None:
        return
    _hmac_key = settings.hmac_key.encode('utf-8')
    _collector = Collector(settings.store_dir, retention_days=settings.retention_days).start()


def shutdown() -> None:
    global _collector
    collector, _collector = _collector, None
    if collector:
        collector.close()


def update_runtime(**gauges) -> None:
    if _collector:
        _collector.update_runtime(**gauges)


def emit(kind: str, **fields) -> None:
    if _collector is None:
        return
    run = _current.get()
    values = {}
    if run:
        values.update(run_id=run.id, channel=run.channel, source=run.source, session=run.session, model=run.model)
    values.update(fields)
    _collector.emit(kind, **values)


def current_run_id() -> str:
    run = _current.get()
    return run.id if run else ''


def set_status(status: str) -> None:
    run = _current.get()
    if run and status in {'success', 'error', 'cancelled', 'rejected', 'partial', 'unknown'}:
        run.status = status


def take_run_id(trace_id: str) -> str:
    """One-use transport correlation; external traces are never run identities."""
    return _trace_runs.pop(trace_id, '')


def _begin(channel: str, source: str, kwargs: dict) -> tuple[Run, Any]:
    run_id = uuid.uuid4().hex
    trace = kwargs.get('trace_id', '')
    if isinstance(trace, str) and IDENTIFIER.fullmatch(trace):
        if len(_trace_runs) >= 2048:
            _trace_runs.pop(next(iter(_trace_runs)))
        _trace_runs[trace] = run_id
    session_key = kwargs.get('session_key')
    session = hmac.new(_hmac_key, str(session_key).encode(), hashlib.sha256).hexdigest()[:32] if session_key and _hmac_key else ''
    run = Run(run_id, channel, source, session)
    token = _current.set(run)
    emit('turn_start', status='start', started_at=run.started_at)
    return run, token


def _finish(run: Run, token=None) -> None:
    try:
        emit('turn', status=run.status, stage='pipeline', started_at=run.started_at,
             duration_seconds=time.monotonic() - run.started, ttft_seconds=run.ttft,
             model=run.model, attempts=run.attempts, model_responses=run.responses,
             tool_calls=run.tools, tokens_total=run.tokens, usage_missing=run.missing)
    finally:
        if token is not None:
            _current.reset(token)


def _error_status(exc: BaseException) -> str:
    return 'cancelled' if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) or type(exc).__name__ == 'AgentClientCancelled' else 'error'


def observe_run(channel: str, source: str = 'interactive'):
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            if _collector is None:
                return await fn(*args, **kwargs)
            actual_channel, actual_source = channel, source
            if channel == 'scheduled':
                task = kwargs.get('task') or (args[1] if len(args) > 1 else None)
                actual_channel, actual_source = getattr(task, 'channel', 'system'), 'scheduled'
            run, token = _begin(actual_channel, actual_source, kwargs)
            try:
                return await fn(*args, **kwargs)
            except BaseException as exc:
                run.status = _error_status(exc)
                raise
            finally:
                _finish(run, token)
        return wrapped
    return decorate


def observe_agent(stream: bool = False):
    def decorate(fn):
        if stream:
            @functools.wraps(fn)
            async def stream_wrapped(*args, **kwargs):
                owner = _collector is not None and _current.get() is None
                run, begin_token = _begin('system', 'system', kwargs) if owner else (_current.get(), None)
                if begin_token is not None:
                    _current.reset(begin_token)
                start = time.monotonic()
                status = 'unknown'
                iterator = fn(*args, **kwargs)
                try:
                    while True:
                        token = _current.set(run)
                        try:
                            piece = await iterator.__anext__()
                        except StopAsyncIteration:
                            status = 'success'
                            break
                        finally:
                            _current.reset(token)
                        # A caller may consume each chunk in a different Task.
                        # No ContextVar token may remain live across this yield.
                        yield piece
                except BaseException as exc:
                    status = _error_status(exc)
                    if run:
                        run.status = status
                    raise
                finally:
                    token = _current.set(run)
                    try:
                        await iterator.aclose()
                        emit('agent_call', status=status, stage='agent', duration_seconds=time.monotonic() - start)
                        if owner:
                            run.status = status
                            _finish(run)
                    finally:
                        _current.reset(token)
            return stream_wrapped
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            owner = _collector is not None and _current.get() is None
            run, token = _begin('system', 'system', kwargs) if owner else (_current.get(), None)
            start = time.monotonic()
            status = 'unknown'
            try:
                result = await fn(*args, **kwargs)
                status = 'success'
                return result
            except BaseException as exc:
                status = _error_status(exc)
                if run:
                    run.status = status
                raise
            finally:
                emit('agent_call', status=status, stage='agent', duration_seconds=time.monotonic() - start)
                if owner:
                    run.status = status
                    _finish(run, token)
        return wrapped
    return decorate


def observe_attempt(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        run = _current.get()
        start = time.monotonic()
        status = 'unknown'
        responses_before = run.responses if run else 0
        if run:
            run.attempts += 1
            run.attempt_serial += 1
            run.tool_started.clear()
            run.seen_responses.clear()
            run.open_responses = 0
            if run.attempts > 1:
                emit('retry', status='start')
        iterator = fn(*args, **kwargs)
        try:
            while True:
                token = _current.set(run)
                try:
                    piece = await iterator.__anext__()
                except StopAsyncIteration:
                    status = 'success'
                    break
                finally:
                    _current.reset(token)
                yield piece
        except BaseException as exc:
            status = _error_status(exc)
            raise
        finally:
            token = _current.set(run)
            try:
                await iterator.aclose()
                emit('attempt', status=status, stage='attempt', duration_seconds=time.monotonic() - start)
                if run:
                    missing = max(run.open_responses, int(run.responses == responses_before))
                    if missing:
                        run.missing += missing
                        emit('usage_gap', count=missing, status='unknown')
                    for tool, began in run.tool_started.values():
                        emit('tool', tool=tool, status='unknown', stage='tool', duration_seconds=time.monotonic() - began)
                    run.tool_started.clear()
            finally:
                _current.reset(token)
    return wrapped


def record_pi_event(event: dict) -> None:
    """Project a pi event onto numeric metadata. Full events never enter the queue."""
    if _collector is None:
        return
    try:
        run = _current.get()
        if run is None:
            return
        kind = event.get('type')
        if kind == 'message_start':
            message = event.get('message')
            if isinstance(message, dict) and message.get('role') == 'assistant':
                run.open_responses += 1
        elif kind == 'message_end':
            message = event.get('message')
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                return
            response_id = message.get('responseId')
            if isinstance(response_id, str) and response_id:
                # IDs stay in memory only, per attempt. Never hash message content.
                identity = (response_id, message.get('timestamp'))
                if identity in run.seen_responses:
                    return
                run.seen_responses.add(identity)
            run.open_responses = max(0, run.open_responses - 1)
            values = clean_fields({'model': message.get('model'), 'provider': message.get('provider'),
                                   'stop_reason': message.get('stopReason', 'unknown')})
            usage = message.get('usage')
            usage = usage if isinstance(usage, dict) else {}
            for raw, normalized in (('input', 'tokens_input'), ('output', 'tokens_output'), ('totalTokens', 'tokens_total'),
                                    ('cacheRead', 'cache_read'), ('cacheWrite', 'cache_write')):
                value = usage.get(raw)
                if type(value) is int and 0 <= value <= 2**53:
                    values[normalized] = value
            values['usage_missing'] = int(any(key not in values for key in ('tokens_input', 'tokens_output', 'tokens_total')))
            run.model = values.get('model', run.model)
            run.responses += 1
            run.tokens += values.get('tokens_total', 0)
            run.missing += values['usage_missing']
            emit('model', **values)
        elif kind == 'message_update':
            inner = event.get('assistantMessageEvent')
            if isinstance(inner, dict) and inner.get('type') == 'text_delta' and inner.get('delta') and run.ttft is None:
                run.ttft = time.monotonic() - run.started
                emit('stage', stage='first_text', duration_seconds=run.ttft, status='success')
        elif kind in {'tool_execution_start', 'tool_execution_end'}:
            tool = event.get('toolName')
            tool = tool if isinstance(tool, str) and tool in ENUMS['tool'] else 'other'
            call_id = event.get('toolCallId')
            if not isinstance(call_id, str) or len(call_id) > 256:
                return
            if kind == 'tool_execution_start':
                if call_id not in run.tool_started:
                    run.tools += 1
                    run.tool_started[call_id] = (tool, time.monotonic())
            else:
                entry = run.tool_started.pop(call_id, None)
                if entry:
                    emit('tool', tool=entry[0], status='error' if event.get('isError') else 'success',
                         stage='tool', duration_seconds=time.monotonic() - entry[1])
        elif kind == 'auto_retry_start':
            emit('retry', status='start')
        elif kind == 'auto_compaction_start':
            emit('compaction', status='start')
        elif kind == 'auto_compaction_end':
            emit('compaction', status='end')
    except Exception:
        # Protocol evolution and malformed optional usage cannot break an IM turn.
        if _collector:
            _collector.dropped += 1
