"""Read-only pi history ledger, separate from rolling/process telemetry.

Only allowlisted usage metadata crosses the checkpoint boundary. Records are
archived indefinitely; a changed source upserts identities, never adds deltas.
Queries use an immutable completed snapshot and never perform filesystem I/O.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = logging.getLogger(__name__)
TZ = ZoneInfo('Asia/Shanghai')
MAX_LINE_BYTES = 64 * 1024 * 1024
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,95}')
HASH_ID = re.compile(r'[0-9a-f]{64}')
PARTS = {'in': 'input', 'cr': 'cacheRead', 'cw': 'cacheWrite', 'out': 'output',
         'reason': 'reasoning', 'reportedTotal': 'totalTokens'}
REQUIRED = ('in', 'cr', 'cw', 'out', 'reportedTotal', 'reason')
SCAN_NUMBERS = ('files', 'changedFiles', 'sourceDirectories', 'failed')
WARNINGS = {
    'missingUsage': '部分响应未上报完整用量，汇总仅包含已知分项',
    'invalidLines': '存在畸形或超长记录，已跳过并保留此前用量',
    'pendingLines': '存在未完成尾行，将在后续扫描重试',
    'weakIdentities': '部分记录缺少稳定标识，复制或改写后可能无法可靠去重',
    'unattributedTurns': '部分助手响应无法关联用户回合，未虚构轮次',
    'totalMismatches': '上报总量与四项之和不一致，采用四项之和',
    'missingDates': '部分记录缺少有效时间，保留总量但不纳入日期筛选和日历',
    'invalidLabels': '部分模型或提供商标识无效，已替换为未知标识',
    'failed': '部分源目录或文件无法读取，已保留最后完成的数据',
    'cacheRecovered': '用量缓存失效或密钥变更，已隔离并尝试从源重建',
}


def _number(value):
    return (type(value) in (int, float) and 0 <= value <= 2**53
            and math.isfinite(value))


def _count(value):
    return type(value) is int and value >= 0


def _label(value, default='unknown'):
    return value if isinstance(value, str) and MODEL_ID.fullmatch(value) else default


def _timestamp(value):
    try:
        if _number(value):  # Native message.timestamp is milliseconds, not seconds.
            at = int(value)
            datetime.fromtimestamp(at / 1000, TZ)
            return at
        if isinstance(value, str) and len(value) <= 64:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                at = int(parsed.timestamp() * 1000)
                if at >= 0:
                    datetime.fromtimestamp(at / 1000, TZ)
                    return at
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _day(at):
    return datetime.fromtimestamp(at / 1000, TZ).date().isoformat() if at is not None else None


def _date_filter(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('日期必须为 YYYY-MM-DD')
    date.fromisoformat(value)
    return value


def _bucket():
    return {**dict.fromkeys(('in', 'cr', 'cw', 'out', 'reason', 'total', 'req',
                             'assistantReq', 'compactionReq'), 0), '_turns': set(), '_days': set()}


def _add(bucket, record, day):
    if not record['hasUsage']:
        return
    usage = record['usage']
    for key in ('in', 'cr', 'cw', 'out', 'reason'):
        bucket[key] += usage.get(key, 0)
    bucket['total'] += sum(usage.get(key, 0) for key in ('in', 'cr', 'cw', 'out'))
    bucket['req'] += 1
    bucket[record['kind'] + 'Req'] += 1
    if record['turn']:
        bucket['_turns'].add(record['turn'])
    if day:
        bucket['_days'].add(day)


def _finish(bucket):
    denominator = bucket['in'] + bucket['cr']
    return {key: value for key, value in bucket.items() if not key.startswith('_')} | {
        'turns': len(bucket['_turns']), 'activeDays': len(bucket['_days']),
        'cacheRate': bucket['cr'] / denominator if denominator else None}


class PiUsageLedger:
    def __init__(self, store_dir: str | Path, session_dirs: list[str | Path],
                 hmac_key: str, sync_interval_seconds=30):
        if not isinstance(hmac_key, str) or not hmac_key:
            raise ValueError('用量账本需要非空 HMAC 密钥')
        if not _number(sync_interval_seconds) or sync_interval_seconds <= 0:
            raise ValueError('扫描间隔必须为正数')
        self.root = Path(store_dir) / 'pi-usage'
        self._key = hmac_key.encode('utf-8')
        self._key_id = self._hash('checkpoint-key')
        paths = sorted({Path(os.path.abspath(Path(p).expanduser())) for p in session_dirs},
                       key=lambda p: (len(p.parts), str(p)))
        self.session_dirs: list[Path] = []
        for path in paths:
            if not any(path == root or root in path.parents for root in self.session_dirs):
                self.session_dirs.append(path)
        self.sync_interval_seconds = float(sync_interval_seconds)
        self._scan_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scanning = False
        self._sync_failed = False
        self._next_scan = 0.
        self._snapshot = self._empty_checkpoint()
        try:
            cached = self._load_checkpoint()
            if cached is not None:
                self._snapshot = cached
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            # Quarantine must wait for flock; constructors never write or scan.
            self._snapshot['cacheRecovered'] = True
            LOG.warning('pi 用量缓存不可用，将在同步时检查重建')

    def _hash(self, *parts):
        payload = json.dumps(parts, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def _empty_checkpoint(self):
        return {'schema_version': 2, 'keyId': self._key_id, 'records': {}, 'files': {},
                'cacheRecovered': False,
                'scan': {**dict.fromkeys(SCAN_NUMBERS, 0), 'lastScanAt': None}}

    def start(self) -> PiUsageLedger:
        with self._lifecycle_lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name='pi-usage-ledger', daemon=True)
                self._thread.start()
        return self

    def _run(self):
        self.sync(force=True)
        while not self._stop.wait(self.sync_interval_seconds):
            self.sync()

    def close(self):
        self._stop.set()
        with self._lifecycle_lock:
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5.)

    @contextlib.contextmanager
    def _process_lock(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise OSError('unsafe checkpoint directory')
        os.chmod(self.root, 0o700)
        fd = os.open(self.root / 'sync.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load_checkpoint(self):
        path = self.root / 'index.json'
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'r', encoding='utf-8') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError('invalid checkpoint')
            raw = json.load(handle)
        # Validate every persisted field; do not echo unknown keys from a cache.
        def require(condition):
            if not condition:
                raise ValueError('invalid checkpoint')

        require(isinstance(raw, dict) and type(raw.get('schema_version')) is int
                and raw['schema_version'] in (1, 2) and raw.get('keyId') == self._key_id)
        legacy = raw['schema_version'] == 1
        require(set(raw) == set(self._empty_checkpoint()))
        require(type(raw['cacheRecovered']) is bool)
        require(isinstance(raw['records'], dict) and isinstance(raw['files'], dict))
        scan = raw['scan']
        require(isinstance(scan, dict) and set(scan) == {*SCAN_NUMBERS, 'lastScanAt'})
        require(all(_count(scan[key]) for key in SCAN_NUMBERS))
        require(scan['lastScanAt'] is None or _timestamp(scan['lastScanAt']) == scan['lastScanAt'])
        record_keys = {'at', 'p', 'm', 'kind', 'session', 'turn', 'weak', 'usage', 'hasUsage', 'invalidLabels'}
        if not legacy:
            record_keys |= {'origin', 'aliases'}
        seen_aliases = set()
        for key, record in raw['records'].items():
            require(bool(HASH_ID.fullmatch(key)) and isinstance(record, dict) and set(record) == record_keys)
            require(record['at'] is None or (_count(record['at']) and _timestamp(record['at']) == record['at']))
            require(record['p'] == _label(record['p']) and record['m'] == _label(record['m']))
            require(record['kind'] in ('assistant', 'compaction'))
            require(isinstance(record['session'], str) and bool(HASH_ID.fullmatch(record['session'])))
            require(record['turn'] is None or (isinstance(record['turn'], str) and bool(HASH_ID.fullmatch(record['turn']))))
            require(record['kind'] != 'compaction' or record['turn'] is None)
            require(all(type(record[k]) is bool for k in ('weak', 'hasUsage', 'invalidLabels')))
            require(isinstance(record['usage'], dict) and set(record['usage']) <= set(PARTS))
            require(all(_number(value) for value in record['usage'].values()))
            require(record['hasUsage'] or not record['usage'])
            if not legacy:
                require(record['origin'] is None or (isinstance(record['origin'], str)
                        and bool(HASH_ID.fullmatch(record['origin'])) and record['origin'] in raw['files']))
                require(isinstance(record['aliases'], list) and bool(record['aliases']))
                require(all(isinstance(alias, str) and bool(HASH_ID.fullmatch(alias))
                            for alias in record['aliases']))
                aliases = set(record['aliases'])
                require(key in aliases and len(aliases) == len(record['aliases'])
                        and not seen_aliases.intersection(aliases))
                seen_aliases.update(aliases)
        for key, info in raw['files'].items():
            require(bool(HASH_ID.fullmatch(key)) and isinstance(info, dict))
            require(set(info) == {'stat', 'native', 'ids', 'invalidLines', 'pendingLines'})
            # Archived v1 sources may never reappear: keep their 3-part stat.
            require(isinstance(info['stat'], list) and len(info['stat']) in ((3,) if legacy else (3, 4))
                    and all(_count(n) for n in info['stat']))
            require(type(info['native']) is bool)
            require(all(_count(info[k]) for k in ('invalidLines', 'pendingLines')))
            require(isinstance(info['ids'], dict))
            require(all(k in raw['records'] and _count(n) and n > 0 for k, n in info['ids'].items()))
        if legacy:
            # Keep every old key as an alias. Only a real re-read can establish
            # provenance and additional aliases; never infer them from usage.
            raw['schema_version'] = 2
            for key, record in raw['records'].items():
                record.update(origin=None, aliases=[key], weak=record['weak'] or record['at'] is None)
        return raw

    def _quarantine(self):
        path = self.root / 'index.json'
        try:
            # No source data or exception text is logged, including on recovery.
            target = self.root / ('index.corrupt-' + uuid.uuid4().hex + '.json')
            os.replace(path, target)
            if not target.is_symlink():
                os.chmod(target, 0o600)
        except FileNotFoundError:
            pass
        LOG.warning('pi 用量缓存失效，已隔离并尝试重建')

    def _save_checkpoint(self, snapshot):
        fd, name = tempfile.mkstemp(prefix='.index-', suffix='.tmp', dir=self.root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(snapshot, handle, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.root / 'index.json')
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)

    def sync(self, force=False) -> dict:
        with self._scan_lock:
            if not force and time.monotonic() < self._next_scan:
                return self._status()
            with self._snapshot_lock:
                self._scanning = True
                previous = self._snapshot
            try:
                with self._process_lock():
                    try:
                        latest = self._load_checkpoint()
                    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
                        self._quarantine()
                        latest = None
                        previous = {**previous, 'cacheRecovered': True}
                    # Always reload after flock: a CLI may have imported since Web's last scan.
                    base = self._merge_snapshots(latest, previous) if latest is not None else previous
                    completed = self._scan(base)
                    self._save_checkpoint(completed)
                    with self._snapshot_lock:
                        self._snapshot = completed
                        self._sync_failed = False
            except Exception:
                # A failed scan/write must not replace the last completed snapshot.
                LOG.warning('pi 用量同步未完成，继续使用最后完成的数据')
                with self._snapshot_lock:
                    self._sync_failed = True
            finally:
                with self._snapshot_lock:
                    self._scanning = False
                self._next_scan = time.monotonic() + self.sync_interval_seconds
            return self._status()

    def _status(self):
        result = self.query()
        return {**result['scan'], 'state': result['quality']['state']}

    @staticmethod
    def _merge_record(previous, incoming, overwrite=False):
        """Keep known fields unless this is a new observation from the origin."""
        merged = {**previous, 'usage': {**incoming['usage'], **previous['usage']},
                  'aliases': list(dict.fromkeys([*previous['aliases'], *incoming['aliases']])),
                  'origin': previous['origin'] or incoming['origin'],
                  'hasUsage': previous['hasUsage'] or incoming['hasUsage'],
                  'weak': previous['weak'] and incoming['weak'],
                  'invalidLabels': previous['invalidLabels'] or incoming['invalidLabels']}
        if overwrite:
            merged['usage'].update(incoming['usage'])
            if incoming['p'] != 'unknown' and incoming['m'] != 'unknown':
                merged['invalidLabels'] = incoming['invalidLabels']
        for key in ('at', 'turn', 'p', 'm'):
            missing = (None, 'unknown')
            if key == 'm' and previous['kind'] == 'compaction':
                missing += ('compaction', 'branch_summary')
            if incoming[key] not in missing and (overwrite or previous[key] in missing):
                merged[key] = incoming[key]
        return merged

    @staticmethod
    def _alias_index(records):
        return {alias: key for key, record in records.items() for alias in record['aliases']}

    @staticmethod
    def _remap_ids(ids, aliases):
        remapped = {}
        for key, count in ids.items():
            canonical = aliases[key]
            remapped[canonical] = remapped.get(canonical, 0) + count
        return remapped

    def _upsert(self, records, aliases, files, key, incoming, *, from_source=False):
        # The first observed source owns corrections; copies cannot claim it by
        # being more complete or having larger token counts. v1 binds on reread.
        matches = list(dict.fromkeys(aliases[a] for a in incoming['aliases'] if a in aliases))
        if not matches:
            records[key] = incoming
            aliases.update(dict.fromkeys(incoming['aliases'], key))
            return
        # If a bridge joins formerly separate identities, keep the first
        # canonical already in this ledger (disk records precede memory-only
        # records). The bridging copy must not promote itself to origin.
        canonical = next(k for k in records if k in matches) if len(matches) > 1 else matches[0]
        merged = records[canonical]
        for other in matches:
            if other != canonical:
                merged = self._merge_record(merged, records.pop(other))
        overwrite = from_source and merged['origin'] in (None, incoming['origin'])
        merged = self._merge_record(merged, incoming, overwrite=overwrite)
        records[canonical] = merged
        aliases.update(dict.fromkeys(merged['aliases'], canonical))
        if len(matches) > 1:
            # Includes unchanged/archived files, not just the file being scanned.
            for file_key, info in files.items():
                if any(other in info['ids'] for other in matches if other != canonical):
                    files[file_key] = {**info, 'ids': self._remap_ids(info['ids'], aliases)}

    def _merge_snapshots(self, latest, previous):
        if latest['keyId'] != previous['keyId']:
            return latest
        records, files = dict(latest['records']), dict(latest['files'])
        aliases = self._alias_index(records)
        latest_keys = list(records)
        for key, record in previous['records'].items():
            # Even same-origin memory is stale: only fill gaps in disk values.
            self._upsert(records, aliases, files, key, record)
        latest_canonicals = {aliases[key] for key in latest_keys}
        for file_key, info in previous['files'].items():
            ids = self._remap_ids(info['ids'], aliases)
            if file_key not in files:
                files[file_key] = {**info, 'ids': ids}
            else:
                current = files[file_key]
                # Don't count two snapshots of the same file twice. Preserve
                # indexes for memory-only archives, but trust latest counts.
                extra = {k: n for k, n in ids.items() if k not in latest_canonicals}
                files[file_key] = {**current, 'ids': {**extra, **current['ids']},
                                   'native': current['native'] or info['native']}
        return {**latest, 'records': records, 'files': files,
                'cacheRecovered': latest['cacheRecovered'] or previous['cacheRecovered']}

    @staticmethod
    def _fingerprint(value):
        return [value.st_size, value.st_mtime_ns, value.st_ino, value.st_ctime_ns]

    @staticmethod
    def _open_root(path):
        """Open every component without following links, including ancestor races."""
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _scan(self, base):
        records, files = dict(base['records']), dict(base['files'])
        aliases = self._alias_index(records)
        scan = {**dict.fromkeys(SCAN_NUMBERS, 0), 'lastScanAt': None}
        visited = set()

        def walk(fd, path):
            directory = os.fstat(fd)
            identity = (directory.st_dev, directory.st_ino)
            if identity in visited:
                return
            visited.add(identity)
            try:
                with os.scandir(fd) as entries:
                    names = sorted(entry.name for entry in entries)
            except OSError:
                scan['failed'] += 1
                return
            for name in names:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            walk(child, path / name)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) and name.endswith('.jsonl'):
                        scan['files'] += 1
                        file_key = self._hash('file', str(path / name))
                        old = files.get(file_key)
                        if old and len(old['stat']) == 4 and old['stat'] == self._fingerprint(info):
                            continue
                        scan['changedFiles'] += 1
                        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                        with os.fdopen(source, 'rb') as handle:
                            before = os.fstat(handle.fileno())
                            if not stat.S_ISREG(before.st_mode):
                                raise OSError('source changed')
                            parsed, summary = self._parse_file(handle, file_key, before.st_size)
                            if self._fingerprint(before) != self._fingerprint(os.fstat(handle.fileno())):
                                raise OSError('source changed')
                        summary['stat'] = self._fingerprint(before)
                        summary['native'] = summary['native'] or bool(old and old['native'])
                        for key, record in parsed.items():
                            self._upsert(records, aliases, files, key, record, from_source=True)
                        summary['ids'] = self._remap_ids(summary['ids'], aliases)
                        files[file_key] = summary
                except OSError:
                    scan['failed'] += 1
        for root in self.session_dirs:
            try:
                fd = self._open_root(root)
                try:
                    scan['sourceDirectories'] += 1
                    walk(fd, root)
                finally:
                    os.close(fd)
            except OSError:
                scan['failed'] += 1
        if not self.session_dirs:
            scan['failed'] += 1
        scan['lastScanAt'] = int(time.time() * 1000)
        return {**base, 'records': records, 'files': files, 'scan': scan}

    def _record_aliases(self, raw, message, file_key, offset, kind):
        envelope_time = _timestamp(raw.get('timestamp'))
        message_time = _timestamp(message.get('timestamp'))
        timestamps = list(dict.fromkeys(t for t in (envelope_time, message_time) if t is not None))
        entry, response = raw.get('id'), message.get('responseId')
        aliases = []
        if isinstance(entry, str) and entry:
            # Same hash namespace as v1, for both available native timestamps.
            # Keep undated v1 identities addressable, but label them weak.
            aliases.extend(self._hash('entry', entry, at, kind) for at in (timestamps or [None]))
        if isinstance(response, str) and response and message_time is not None:
            aliases.append(self._hash('response', response, _label(message.get('provider')),
                                      _label(message.get('model')), message_time, kind))
        weak = not aliases or not timestamps
        if not aliases:
            aliases.append(self._hash('offset', file_key, offset, kind))
        return aliases, weak

    def _record_key(self, raw, message, file_key, offset, kind):
        aliases, weak = self._record_aliases(raw, message, file_key, offset, kind)
        return aliases[0], weak

    def _parse_file(self, handle, file_key, size):
        records, parents, nodes = {}, {}, {}
        summary = {'native': False, 'ids': {}, 'invalidLines': 0, 'pendingLines': 0}
        session = self._hash('session', file_key)
        remaining = size
        while remaining:
            offset = handle.tell()
            line = handle.readline(min(MAX_LINE_BYTES + 1, remaining))
            remaining -= len(line)
            if not line:
                break
            oversized = len(line) > MAX_LINE_BYTES
            while not line.endswith(b'\n') and remaining and oversized:
                line = handle.readline(min(MAX_LINE_BYTES + 1, remaining))
                remaining -= len(line)
                if not line:
                    break
            if not line.endswith(b'\n'):
                summary['pendingLines'] += 1
                break
            if oversized:
                summary['invalidLines'] += 1
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict) or not isinstance(raw.get('type'), str):
                    raise ValueError('invalid native entry')
            except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                summary['invalidLines'] += 1
                continue
            kind = raw['type']
            if kind == 'session':
                summary['native'] = True
                if isinstance(raw.get('id'), str):
                    session = self._hash('session', file_key, raw['id'])
                continue
            message = raw.get('message') if isinstance(raw.get('message'), dict) else {}
            role = message.get('role') if kind == 'message' else None
            parent = raw.get('parentId')
            parent = self._hash('node', parent) if isinstance(parent, str) and parent else None
            entry = raw.get('id')
            if isinstance(entry, str) and entry:
                turn = self._record_key(raw, message, file_key, offset, 'user')[0] if role == 'user' else None
                nodes[self._hash('node', entry)] = (parent, turn)
            if role != 'assistant' and kind not in ('compaction', 'branch_summary'):
                continue
            native_kind = kind
            kind = 'assistant' if role == 'assistant' else 'compaction'
            payload = raw if kind == 'compaction' else message
            raw_usage = payload.get('usage')
            has_usage = isinstance(raw_usage, dict)
            # A compaction without usage is not evidence of a model request.
            if kind == 'compaction' and not has_usage:
                continue
            identity_kind = native_kind if kind == 'compaction' else kind
            aliases, weak = self._record_aliases(raw, message, file_key, offset, identity_kind)
            key = aliases[0]
            usage = {key: raw_usage[value] for key, value in PARTS.items()
                     if _number(raw_usage.get(value))} if has_usage else {}
            if has_usage and 'reasoning' not in raw_usage:
                usage['reason'] = 0
            at = _timestamp(message.get('timestamp')) if kind == 'assistant' else None
            if at is None:
                at = _timestamp(raw.get('timestamp'))
            record = {'at': at, 'p': _label(payload.get('provider')),
                      'm': _label(payload.get('model'), native_kind if kind == 'compaction' else 'unknown'),
                      'kind': kind, 'session': session, 'turn': None, 'weak': weak,
                      'usage': usage, 'hasUsage': has_usage, 'origin': file_key, 'aliases': aliases,
                      'invalidLabels': any(payload.get(k) is not None and _label(payload[k], '') == ''
                                           for k in ('provider', 'model'))}
            records[key] = self._merge_record(records[key], record, overwrite=True) if key in records else record
            parents.setdefault(key, []).append(parent)
            summary['ids'][key] = summary['ids'].get(key, 0) + 1

        resolved = {}
        def nearest_user(node):
            trail, seen = [], set()
            turn = None
            while node is not None and node not in seen:
                if node in resolved:
                    turn = resolved[node]
                    break
                seen.add(node)
                trail.append(node)
                if node not in nodes:
                    break
                node, turn = nodes[node]
                if turn is not None:
                    break
            for visited in trail:
                resolved[visited] = turn
            return turn

        for key, record in records.items():
            if record['kind'] == 'assistant':
                record['turn'] = next((turn for node in reversed(parents[key])
                                       if (turn := nearest_user(node)) is not None), None)
        return records, summary

    def query(self, start: str | None = None, end: str | None = None) -> dict:
        start, end = _date_filter(start), _date_filter(end)
        if start and end and start > end:
            raise ValueError('开始日期不得晚于结束日期')
        with self._snapshot_lock:
            snapshot, scanning, failed = self._snapshot, self._scanning, self._sync_failed
        totals, by_day, by_model, by_day_model, calendar = _bucket(), {}, {}, {}, {}
        quality = dict.fromkeys(('missingUsage', 'invalidLines', 'pendingLines', 'duplicateRecords',
                                 'weakIdentities', 'unattributedTurns', 'totalMismatches',
                                 'missingDates', 'invalidLabels'), 0)
        occurrences = {}
        for info in snapshot['files'].values():
            for key in ('invalidLines', 'pendingLines'):
                quality[key] += info[key]
            for key, count in info['ids'].items():
                occurrences[key] = occurrences.get(key, 0) + count
        quality['duplicateRecords'] = sum(n - 1 for n in occurrences.values())
        for record in snapshot['records'].values():
            usage, day = record['usage'], _day(record['at'])
            quality['missingUsage'] += not record['hasUsage'] or any(k not in usage for k in REQUIRED)
            quality['weakIdentities'] += record['weak']
            quality['unattributedTurns'] += record['kind'] == 'assistant' and record['turn'] is None
            quality['totalMismatches'] += (all(k in usage for k in ('in', 'cr', 'cw', 'out', 'reportedTotal'))
                                          and sum(usage[k] for k in ('in', 'cr', 'cw', 'out')) != usage['reportedTotal'])
            quality['missingDates'] += day is None
            quality['invalidLabels'] += record['invalidLabels']
            if not record['hasUsage']:
                continue
            if day:
                _add(calendar.setdefault(day, _bucket()), record, day)
            if (start or end) and (day is None or (start and day < start) or (end and day > end)):
                continue
            _add(totals, record, day)
            model = (record['p'], record['m'])
            _add(by_model.setdefault(model, _bucket()), record, day)
            if day:
                _add(by_day.setdefault(day, _bucket()), record, day)
                _add(by_day_model.setdefault((day, *model), _bucket()), record, day)
        now = int(time.time() * 1000)
        scan = {**snapshot['scan'], 'scanning': scanning,
                'failed': snapshot['scan']['failed'] + int(failed),
                'records': sum(r['hasUsage'] for r in snapshot['records'].values()),
                'firstDay': min(calendar, default=None), 'lastDay': max(calendar, default=None)}
        warnings = [text for key, text in WARNINGS.items()
                    if (quality.get(key) or (key == 'failed' and scan['failed'])
                        or (key == 'cacheRecovered' and snapshot['cacheRecovered']))]
        last = scan['lastScanAt']
        stale = failed or (last is not None and now - last > max(90., self.sync_interval_seconds * 3) * 1000)
        if stale:
            warnings.append('采集未完成或已过期，当前展示最后完成的历史快照')
        state = ('scanning' if scanning else 'stale' if stale else 'partial' if warnings
                 else 'empty' if not scan['records'] else 'ok')
        quality.update(state=state, warnings=warnings, scope="all_scanned_history")
        return {'schema_version': 1, 'source': 'pi', 'timezone': 'Asia/Shanghai', 'generatedAt': now,
                'lastScanAt': last, 'range': {'start': start, 'end': end},
                # Native source files, not header IDs. Archived sources remain represented.
                'sessions': sum(info['native'] for info in snapshot['files'].values()),
                'failed': scan['failed'], 'totals': _finish(totals),
                'byDay': [{'d': d, **_finish(b)} for d, b in sorted(by_day.items())],
                'byModel': sorted([{'p': p, 'm': m, **_finish(b)} for (p, m), b in by_model.items()],
                                  key=lambda b: (-b['total'], b['p'], b['m'])),
                'byDayModel': [{'d': d, 'p': p, 'm': m, **_finish(b)}
                               for (d, p, m), b in sorted(by_day_model.items())],
                'calendar': [{'d': d, **_finish(b)} for d, b in sorted(calendar.items())],
                'quality': quality, 'scan': scan}

    def prometheus(self) -> str:
        result = self.query()
        lines, declared = [], set()
        def sample(name, value, labels=None):
            name = 'ferry_pi_usage_' + name
            if name not in declared:
                lines.extend([f'# HELP {name} Archived pi usage snapshot, not a process counter.',
                              f'# TYPE {name} gauge'])
                declared.add(name)
            suffix = '{' + ','.join(f'{k}={json.dumps(str(v), ensure_ascii=True)}'
                                    for k, v in sorted(labels.items())) + '}' if labels else ''
            lines.append(f'{name}{suffix} {value}')

        for key in ('in', 'cr', 'cw', 'out', 'reason', 'total'):
            sample('history_tokens', result['totals'][key], {'type': key})
        for key in ('req', 'assistantReq', 'compactionReq', 'turns'):
            sample('history_requests', result['totals'][key], {'type': key})
        sample('history_sessions', result['sessions'])
        sample('scan_files', result['scan']['files'])
        sample('scan_changed_files', result['scan']['changedFiles'])
        sample('scan_failed', result['failed'])
        sample('scanning', int(result['scan']['scanning']))
        sample('last_scan_timestamp_seconds', (result['lastScanAt'] or 0) / 1000)
        for state in ('ok', 'partial', 'empty', 'stale', 'scanning'):
            sample('collection_state', int(result['quality']['state'] == state), {'state': state})
        for key, value in result['quality'].items():
            if type(value) is int:
                sample('quality_records', value, {'issue': key})
        return '\n'.join(lines) + '\n'
