"""Incremental, bounded readers for content-free journals; no native history reads."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from observability.telemetry import BUCKETS, KINDS, clean_fields

WINDOWS = {'1h': 3600, '24h': 86400, '7d': 604800, '30d': 2592000}
JOURNAL = re.compile(r'^events-\d{4}-\d{2}-\d{2}-(main|sidecar)-[0-9a-f]{32}\.jsonl$')


def _finite(value: Any, default=0):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else default


def _distribution(values: list[float]) -> dict:
    values.sort()
    size = len(values)
    def percentile(q):
        return values[max(0, math.ceil(size * q) - 1)] if size else None
    return {'count': size, 'p50': percentile(.5), 'p95': percentile(.95), 'p99': percentile(.99),
            'mean': sum(values) / size if size else None}


class MetricReader:
    """At most 200k events in RAM. Truncation is explicit, never a silent zero.

    Historical queries use event time within the retained journal window. Start
    events make unfinished runs visible; terminal events are counted once.
    """
    def __init__(self, store_dir: str | Path, retention_days: int = 30):
        self.root = Path(store_dir)
        self.retention_days = max(1, min(90, retention_days))
        self.events: deque[dict] = deque(maxlen=200_000)
        self.offsets: dict[str, tuple[int, int]] = {}
        self._seen: set[tuple] = set()
        self.invalid_lines = 0
        self.truncated = False
        self._lock = threading.RLock()
        self._refresh_at = 0.
        self._snapshots: dict[str, dict] = {}

    def _refresh(self) -> None:
        if time.monotonic() < self._refresh_at:
            return
        cutoff = time.time() - self.retention_days * 86400
        cutoff_day = time.strftime('%Y-%m-%d', time.gmtime(cutoff))
        paths = sorted(p for p in self.root.glob('events-*.jsonl') if JOURNAL.fullmatch(p.name) and p.name[7:17] >= cutoff_day)
        present = {p.name for p in paths}
        for name in list(self.offsets):
            if name not in present:
                self.offsets.pop(name, None)
        for path in paths:
            try:
                stat = path.stat()
                inode, offset = self.offsets.get(path.name, (stat.st_ino, 0))
                if stat.st_ino != inode or stat.st_size < offset:
                    offset = 0
                with path.open('rb') as handle:
                    handle.seek(offset)
                    while True:
                        begin = handle.tell()
                        line = handle.readline(8193)
                        if not line:
                            break
                        if not line.endswith(b'\n'):
                            if len(line) > 8192:
                                while line and not line.endswith(b'\n'):
                                    line = handle.readline(8193)
                                self.invalid_lines += 1
                                continue
                            handle.seek(begin)  # Writer may still be appending the tail.
                            break
                        try:
                            raw = json.loads(line)
                            if not isinstance(raw, dict) or raw.get('v') != 1 or raw.get('kind') not in KINDS:
                                raise ValueError('invalid telemetry record')
                            at = _finite(raw.get('at'), None)
                            seq, boot = raw.get('seq'), raw.get('boot_id')
                            if at is None or type(seq) is not int or not isinstance(boot, str) or not re.fullmatch('[0-9a-f]{32}', boot):
                                raise ValueError('invalid event identity')
                            identity = (boot, seq)
                            if identity in self._seen or at < cutoff:
                                continue
                            event = {'kind': raw['kind'], 'at': at, 'boot_id': boot, 'seq': seq,
                                     'producer': 'sidecar' if raw.get('producer') == 'sidecar' else 'main', **clean_fields(raw)}
                            if len(self.events) == self.events.maxlen:
                                evicted = self.events[0]
                                self._seen.discard((evicted['boot_id'], evicted['seq']))
                                self.truncated = True
                            self.events.append(event)
                            self._seen.add(identity)
                        except (ValueError, TypeError, UnicodeError, OverflowError):
                            self.invalid_lines += 1
                    self.offsets[path.name] = (stat.st_ino, handle.tell())
            except OSError:
                self.invalid_lines += 1
        # Producers can append out of timestamp order, so do not just pop left.
        retained = [e for e in self.events if e['at'] >= cutoff]
        if len(retained) != len(self.events):
            self.events = deque(retained, maxlen=200_000)
            self._seen = {(e['boot_id'], e['seq']) for e in retained}
        snapshots = {}
        for producer in ('main', 'sidecar'):
            try:
                path = self.root / f'{producer}-current.json'
                if path.stat().st_size > 5_000_000:
                    continue
                value = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(value, dict) and value.get('v') == 1 and value.get('producer') == producer:
                    snapshots[producer] = value
            except (OSError, ValueError):
                pass
        self._snapshots = snapshots
        self._refresh_at = time.monotonic() + 1.

    def query(self, window: str = '24h', channel: str = 'all', model: str = 'all') -> dict:
        if window not in WINDOWS or channel not in {'all', 'feishu', 'wechat', 'system'}:
            raise ValueError('unsupported query window or channel')
        if not isinstance(model, str) or len(model) > 96:
            raise ValueError('invalid model filter')
        with self._lock:
            self._refresh()
            now = time.time()
            seconds = WINDOWS[window]
            start = now - seconds
            selected_runs = {e.get('run_id') for e in self.events if e.get('model') == model and e.get('run_id')} if model != 'all' else set()
            events = [e for e in self.events if start <= e['at'] < now and
                      (channel == 'all' or e.get('channel', 'system') == channel) and
                      (model == 'all' or (e.get('model') == model if e['kind'] == 'model' else e.get('run_id') in selected_runs))]
            snapshots = dict(self._snapshots)
            main = snapshots.get('main', {})
            summary = {key: 0 for key in ('turns', 'success', 'errors', 'cancelled', 'attempts', 'model_responses',
                'tokens_input', 'tokens_output', 'tokens_total', 'usage_missing', 'tool_calls', 'retries', 'messages',
                'duplicates', 'queue_rejected', 'delivery_success', 'delivery_error', 'active_sessions')}
            summary.update(success_rate=None, cost=None)
            stages: dict[str, list] = defaultdict(list)
            buckets: dict[int, dict] = {}
            step = {'1h': 60, '24h': 1800, '7d': 21600, '30d': 86400}[window]
            # Do not fill unobserved periods with synthetic zeroes.
            first_data = min((e['at'] for e in events), default=now)
            for at in range(int(max(start, first_data)) // step * step, int(now) // step * step + 1, step):
                buckets[at] = {'at': at, 'turns': 0, 'errors': 0, 'tokens': 0}
            groups = {key: defaultdict(lambda: {'count': 0, 'tokens': 0, 'errors': 0}) for key in ('channels', 'models', 'statuses', 'tools')}
            sessions = set()
            runs: dict[str, dict] = {}
            ended = set()
            for event in events:
                kind = event['kind']
                status = event.get('status', 'unknown')
                duration = event.get('duration_seconds')
                stage = event.get('stage')
                if duration is not None and stage:
                    stages[stage].append(duration)
                bucket = buckets.get(int(event['at']) // step * step)
                if kind in {'turn_start', 'turn'} and event.get('session'):
                    sessions.add(event['session'])
                if kind in {'turn_start', 'turn'}:
                    run_id = event.get('run_id')
                    if run_id:
                        run = runs.setdefault(run_id, {'id': run_id, 'channel': event.get('channel', 'system'),
                            'source': event.get('source', 'system'), 'model': event.get('model', 'unknown'),
                            'started_at': event.get('started_at', event['at']), 'duration_seconds': None,
                            'status': 'running' if event.get('boot_id') == main.get('boot_id') and now - _finite(main.get('timestamp')) < 15 else 'unknown',
                            'ttft_seconds': None, 'tokens_total': None, 'tool_calls': 0, 'attempts': 0, 'usage_missing': 0})
                        if kind == 'turn':
                            if run_id in ended:
                                continue
                            ended.add(run_id)
                            for key in ('model', 'duration_seconds', 'ttft_seconds', 'tokens_total', 'tool_calls', 'attempts', 'usage_missing'):
                                if key in event:
                                    run[key] = event[key]
                            run['status'] = status
                if kind == 'turn':
                    summary['turns'] += 1
                    summary['success'] += status == 'success'
                    summary['errors'] += status == 'error'
                    summary['cancelled'] += status == 'cancelled'
                    if bucket:
                        bucket['turns'] += 1
                        bucket['errors'] += status == 'error'
                    for category, name in (('channels', event.get('channel', 'system')), ('statuses', status)):
                        group = groups[category][name]
                        group['count'] += 1
                        group['tokens'] += event.get('tokens_total', 0)
                        group['errors'] += status == 'error'
                elif kind == 'model':
                    summary['model_responses'] += 1
                    for key in ('tokens_input', 'tokens_output', 'tokens_total', 'usage_missing'):
                        summary[key] += event.get(key, 0)
                    name = event.get('model', 'unknown')
                    group = groups['models'][name]
                    group['count'] += 1
                    group['tokens'] += event.get('tokens_total', 0)
                    group['errors'] += event.get('stop_reason') in {'error', 'aborted'}
                    if bucket:
                        bucket['tokens'] += event.get('tokens_total', 0)
                elif kind == 'usage_gap':
                    summary['usage_missing'] += event.get('count', 1)
                elif kind == 'attempt':
                    summary['attempts'] += 1
                elif kind == 'tool':
                    summary['tool_calls'] += 1
                    group = groups['tools'][event.get('tool', 'other')]
                    group['count'] += 1
                    group['errors'] += status == 'error'
                elif kind == 'retry':
                    summary['retries'] += 1
                elif kind == 'message':
                    summary['messages'] += event.get('count', 1) if status == 'accepted' else 0
                    summary['duplicates'] += event.get('count', 1) if status == 'duplicate' else 0
                elif kind == 'queue' and status == 'rejected':
                    summary['queue_rejected'] += event.get('count', 1)
                elif kind == 'delivery':
                    summary['delivery_success'] += status == 'success'
                    summary['delivery_error'] += status in {'error', 'partial'}
            summary['active_sessions'] = len(sessions)
            denominator = summary['success'] + summary['errors']
            summary['success_rate'] = summary['success'] / denominator if denominator else None
            summary['success_rate_denominator'] = denominator
            age = max(0., now - main['timestamp']) if type(main.get('timestamp')) in (int, float) else None
            errors = sum(_finite(s.get('write_errors')) for s in snapshots.values())
            dropped = sum(_finite(s.get('dropped_events')) for s in snapshots.values())
            warnings = ['Token 仅覆盖可见 pi 响应；底层 HTTP 重试与隐藏压缩用量未验证',
                        '费用及缓存计费语义未验证，费用不估算',
                        '渠道成功仅指平台接受，不代表用户已读；渠道操作数不是对话轮次',
                        '历史按事件发生时间统计，跨窗口的开始/完成可能分属不同窗口']
            if model != 'all':
                warnings.append('模型筛选：轮次/阶段耗时按涉及该模型的轮次归因，Token 仅统计该模型；未关联轮次的队列样本不归因')
            state = 'ok'
            if not main:
                state = 'empty' if not events else 'stale'
                warnings.append('尚未收到主服务采集心跳，请确认主服务已启用可观测并重启')
            elif age is None or age > 15:
                state = 'stale'
                warnings.append('主服务采集心跳已过期，运行状态不可判定')
            runtime_sample = _finite(main.get('gauges', {}).get('runtime_sampled_at'))
            if runtime_sample and now - runtime_sample > 15:
                state = 'stale'
                warnings.append('运行时采样已过期，事件循环可能阻塞；写入器存活不等于业务健康')
            incomplete_runs = sum(r['usage_missing'] > 0 or r['status'] == 'unknown' for r in runs.values())
            if errors or dropped or self.invalid_lines or summary['usage_missing'] or incomplete_runs or self.truncated:
                if state == 'ok':
                    state = 'partial'
                warnings.append('存在缺失或未完成采集，已知用量不代表完整账单')
            if self.truncated:
                warnings.append('查询缓存达到 200000 条上限，本次历史聚合不完整')
            if seconds > self.retention_days * 86400:
                warnings.append('请求窗口超过配置的历史保留期')
                state = 'partial' if state == 'ok' else state
            quality = {'state': state, 'age_seconds': age, 'dropped_events': dropped, 'write_errors': errors,
                       'invalid_lines': self.invalid_lines, 'usage_missing': summary['usage_missing'],
                       'incomplete_runs': incomplete_runs, 'retention_days': self.retention_days,
                       'truncated': self.truncated, 'warnings': warnings,
                       'measurement_scope': 'visible_pi_events', 'first_event_at': first_data if events else None}
            runtime = {key: main[key] for key in ('timestamp', 'started_at', 'mode', 'gauges', 'pending_events') if key in main}
            return {'schema_version': 1, 'window': {'start': start, 'end': now, 'seconds': seconds},
                    'generated_at': now, 'data_until': main.get('timestamp'), 'quality': quality,
                    'summary': summary, 'performance': {stage: _distribution(values) for stage, values in stages.items()},
                    'timeseries': list(buckets.values()) if events else [],
                    'breakdown': {category: sorted([{'name': name, **value} for name, value in entries.items()], key=lambda x: -x['count'])
                                  for category, entries in groups.items()},
                    'runs': sorted(runs.values(), key=lambda x: x['started_at'], reverse=True)[:100], 'runtime': runtime}

    def prometheus(self) -> str:
        with self._lock:
            self._refresh()
            lines = []
            declared = set()
            now = time.time()
            def sample(name, value, labels=None, metric_type='gauge', family=None):
                family = family or name
                if family not in declared:
                    lines.extend([f'# HELP {family} Ferry content-free telemetry; process-lifetime counters reset on producer restart.',
                                  f'# TYPE {family} {metric_type}'])
                    declared.add(family)
                suffix = '{' + ','.join(f'{key}={json.dumps(str(val), ensure_ascii=True)}' for key, val in sorted((labels or {}).items())) + '}' if labels else ''
                lines.append(f'{name}{suffix} {_finite(value)}')
            for producer in ('main', 'sidecar'):
                snapshot = self._snapshots.get(producer, {})
                timestamp = _finite(snapshot.get('timestamp'))
                fresh = bool(timestamp and 0 <= now - timestamp <= 15)
                labels = {'producer': producer}
                sample('ferry_telemetry_available', int(fresh), labels)
                sample('ferry_telemetry_last_flush_timestamp_seconds', timestamp, labels)
                if not snapshot:
                    continue
                sample('ferry_telemetry_dropped_events_total', snapshot.get('dropped_events'), labels, 'counter')
                sample('ferry_telemetry_write_errors_total', snapshot.get('write_errors'), labels, 'counter')
                sample('ferry_telemetry_pending_events', snapshot.get('pending_events'), labels)
                sample('ferry_telemetry_process_start_time_seconds', snapshot.get('started_at'), labels)
                if not fresh:
                    continue  # Never export stale runtime gauges as healthy.
                for counter in snapshot.get('counters', []):
                    if not isinstance(counter, dict) or not re.fullmatch(r'ferry_[a-z_]+_total', str(counter.get('name'))):
                        continue
                    raw_labels = counter.get('labels', {})
                    safe = {k: v for k, v in clean_fields(raw_labels).items() if k not in {'session', 'run_id'}}
                    # Token type is a special bounded dimension, not an event field.
                    if raw_labels.get('type') in {'input', 'output', 'total'}:
                        safe['type'] = raw_labels['type']
                    sample(counter['name'], counter.get('value'), {**labels, **safe}, 'counter')
                for hist in snapshot.get('histograms', []):
                    if not isinstance(hist, dict) or len(hist.get('buckets', [])) != len(BUCKETS):
                        continue
                    dimensions = {**labels, **clean_fields({'stage': hist.get('stage'), 'channel': hist.get('channel')})}
                    family = 'ferry_stage_duration_seconds'
                    for boundary, count in zip(BUCKETS, hist['buckets']):
                        sample(f'{family}_bucket', count, {**dimensions, 'le': str(boundary)}, 'histogram', family)
                    sample(f'{family}_bucket', hist.get('count'), {**dimensions, 'le': '+Inf'}, 'histogram', family)
                    sample(f'{family}_count', hist.get('count'), dimensions, 'histogram', family)
                    sample(f'{family}_sum', hist.get('sum'), dimensions, 'histogram', family)
                from observability.telemetry import GAUGES
                gauges = snapshot.get('gauges', {})
                runtime_sample = _finite(gauges.get('runtime_sampled_at'))
                sample('ferry_runtime_sample_fresh', int(bool(runtime_sample and now - runtime_sample <= 15)), labels)
                for key, value in gauges.items():
                    if key in GAUGES and (key == 'runtime_sampled_at' or runtime_sample and now - runtime_sample <= 15):
                        sample(f'ferry_runtime_{key}', value, labels)
            return '\n'.join(lines) + '\n'
