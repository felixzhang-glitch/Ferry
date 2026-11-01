"""Independent private device copies; one atomic index is the publication point."""
from __future__ import annotations

import copy
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from core.device_data_contract import (
    Batch, DeleteData, DeviceRecord, SyncCommit, SyncStart, canonical, strict_json,
    MAX_BATCH_BYTES, MAX_BATCHES, MAX_RECORDS, MAX_TRANSACTION_BYTES, TRANSACTION_TTL,
)

MAX_DEVICES = 32
MAX_RECEIPTS = 128
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 96 * 1024 * 1024
MAX_STORE_BYTES = 512 * 1024 * 1024
# Leave ample headroom below the gateway's 1MiB response limit.
MAX_PAGE_BYTES = 512 * 1024
MAX_OBJECT_FILES = MAX_DEVICES * MAX_BATCHES + MAX_DEVICES * 6 + 64
ID = re.compile(r"[a-f0-9]{32}\Z")
METRICS = {"activity": {"steps", "distance", "active_energy"}, "heart_rate": {"heart_rate"},
           "weight": {"weight"}, "oxygen": {"oxygen"}, "calendar": {"event"}, "reminders": {"reminder"}}


class DeviceDataError(ValueError):
    def __init__(self, code="store_unavailable", status=503):
        super().__init__(code)
        self.code, self.status = code, status


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise DeviceDataError("invalid_id", 422)
    return value


def validate_start(start: SyncStart):
    try:
        ZoneInfo(start.timezone)
    except (ValueError, KeyError):
        raise DeviceDataError("invalid_timezone", 422) from None
    if start.kind in {"calendar", "reminders"} and not start.sources:
        raise DeviceDataError("sources_required", 422)


def validate_record(record: DeviceRecord, start: SyncStart):
    if record.metric not in METRICS[start.kind]:
        raise DeviceDataError("invalid_metric", 422)
    if start.sources and record.source not in start.sources:
        raise DeviceDataError("invalid_source", 422)
    for timezone in (record.timezone, record.due.timezone if record.due else None):
        if timezone is not None:
            try:
                ZoneInfo(timezone)
            except (ValueError, KeyError):
                raise DeviceDataError("invalid_timezone", 422) from None
    if record.metric == "reminder":
        within = not record.completed or start.window_start <= record.completed_at < start.window_end
    else:
        within = (record.start < start.window_end and
                  (record.end > start.window_start or record.start == record.end == start.window_start))
    if not within:
        raise DeviceDataError("outside_window", 422)


class DeviceDataStore:
    def __init__(self, root: str | Path):
        self.root = Path(os.path.abspath(root))
        self.lock = threading.RLock()
        self._mkdir(self.root)
        self._mkdir(self.root / "objects")
        with self.lock:
            if not (self.root / "index.json").exists():
                self._safe(self.root / "index.json")
                # An existing nonempty store without its authority must not be reset.
                if list((self.root / "objects").iterdir()):
                    raise DeviceDataError()
                self._write(self.root / "index.json", {"version": 1, "slots": {}, "syncs": {}, "receipts": {}})
            index = self._load()
            self._verify(index)
            self._maintain(index)

    def _safe(self, path):
        for part in [*reversed(path.parents), path]:
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (part != path and not stat.S_ISDIR(info.st_mode)):
                raise DeviceDataError()
            if part == path and not stat.S_ISDIR(info.st_mode):
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise DeviceDataError()

    def _mkdir(self, path):
        self._safe(path)
        if not path.exists():
            if not path.parent.exists():
                self._mkdir(path.parent)
            path.mkdir(mode=0o700)
        if not path.is_dir():
            raise DeviceDataError()
        os.chmod(path, 0o700)

    def _read(self, path, limit):
        self._safe(path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                    raise DeviceDataError()
                raw = source.read(limit + 1)
                if len(raw) > limit:
                    raise DeviceDataError()
                os.fchmod(source.fileno(), 0o600)
                return strict_json(raw)
        except (OSError, ValueError, RecursionError):
            raise DeviceDataError() from None

    def _fsync(self, directory):
        self._safe(directory)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write(self, path, value, limit=MAX_INDEX_BYTES):
        self._safe(path)
        raw = canonical(value)
        if len(raw) > limit:
            raise DeviceDataError("storage_budget", 409)
        temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            self._safe(path)
            os.replace(temporary, path)
            self._fsync(path.parent)
        except OSError:
            raise DeviceDataError() from None
        finally:
            self._safe(temporary)
            temporary.unlink(missing_ok=True)

    def _object(self, name):
        identifier(name)
        return self.root / "objects" / (name + ".json")

    def _load(self):
        index = self._read(self.root / "index.json", MAX_INDEX_BYTES)
        try:
            if set(index) != {"version", "slots", "syncs", "receipts"} or type(index["version"]) is not int or index["version"] != 1:
                raise ValueError()
            slots, syncs, receipts = index["slots"], index["syncs"], index["receipts"]
            if not all(isinstance(x, dict) for x in (slots, syncs, receipts)):
                raise ValueError()
            if len(slots) > MAX_DEVICES * 6 or len(syncs) > MAX_DEVICES or len(receipts) > MAX_RECEIPTS:
                raise ValueError()
            devices = set()
            for key, slot in slots.items():
                device, kind = key.split(":")
                identifier(device)
                devices.add(device)
                if kind not in METRICS or set(slot) != {"generation", "snapshot"}:
                    raise ValueError()
                if type(slot["generation"]) is not int or slot["generation"] < 0:
                    raise ValueError()
                if slot["snapshot"] is not None:
                    identifier(slot["snapshot"])
            if len(devices) > MAX_DEVICES:
                raise ValueError()
            active = set()
            for sid, tx in syncs.items():
                identifier(sid)
                if set(tx) != {"start", "created", "generation", "batches"}:
                    raise ValueError()
                start = SyncStart.model_validate(tx["start"])
                validate_start(start)
                key = start.device_id + ":" + start.kind
                if start.device_id in active or tx["generation"] != slots[key]["generation"]:
                    raise ValueError()
                active.add(start.device_id)
                if type(tx["created"]) not in {int, float} or not 0 <= tx["created"] <= 4102444800:
                    raise ValueError()
                if not isinstance(tx["batches"], list) or len(tx["batches"]) > MAX_BATCHES:
                    raise ValueError()
                for batch in tx["batches"]:
                    if set(batch) != {"object", "hash", "bytes"}:
                        raise ValueError()
                    identifier(batch["object"])
                    if not re.fullmatch(r"[a-f0-9]{64}", batch["hash"]) or type(batch["bytes"]) is not int or not 1 <= batch["bytes"] <= MAX_BATCH_BYTES:
                        raise ValueError()
                if sum(b["bytes"] for b in tx["batches"]) > MAX_TRANSACTION_BYTES:
                    raise ValueError()
            for sid, receipt in receipts.items():
                identifier(sid)
                if set(receipt) != {"key", "generation", "created", "commit"} or receipt["key"] not in slots:
                    raise ValueError()
                SyncCommit.model_validate(receipt["commit"])
                if type(receipt["created"]) not in {int, float} or not 0 <= receipt["created"] <= 4102444800 or type(receipt["generation"]) is not int:
                    raise ValueError()
            return index
        except (ValueError, TypeError, KeyError, AttributeError):
            raise DeviceDataError() from None

    def _snapshot(self, name):
        value = self._read(self._object(name), MAX_SNAPSHOT_BYTES)
        try:
            if set(value) != {"version", "start", "commit", "last_sync", "records", "last_seen"} or type(value["version"]) is not int or value["version"] != 1:
                raise ValueError()
            start = SyncStart.model_validate(value["start"])
            validate_start(start)
            SyncCommit.model_validate(value["commit"])
            if not isinstance(value["records"], list) or len(value["records"]) > MAX_RECORDS:
                raise ValueError()
            if not isinstance(value["last_seen"], list) or len(value["last_seen"]) != len(value["records"]):
                raise ValueError()
            if type(value["last_sync"]) not in {int, float} or not 0 <= value["last_sync"] <= 4102444800:
                raise ValueError()
            seen = set()
            for record, last_seen in zip(value["records"], value["last_seen"]):
                model = DeviceRecord.model_validate(record)
                key = (model.source, model.id)
                if key in seen or model.metric not in METRICS[start.kind]:
                    raise ValueError()
                seen.add(key)
                if type(last_seen) not in {int, float} or not 0 <= last_seen <= value["last_sync"]:
                    raise ValueError()
            return value
        except (ValueError, TypeError, KeyError):
            raise DeviceDataError() from None

    def _verify(self, index):
        for key, slot in index["slots"].items():
            if slot["snapshot"]:
                snap = self._snapshot(slot["snapshot"])
                if snap["start"]["device_id"] + ":" + snap["start"]["kind"] != key:
                    raise DeviceDataError()
        for tx in index["syncs"].values():
            for entry in tx["batches"]:
                raw = self._read(self._object(entry["object"]), MAX_BATCH_BYTES)
                if hashlib.sha256(canonical(raw)).hexdigest() != entry["hash"]:
                    raise DeviceDataError()
                batch = Batch.model_validate(raw)
                for record in batch.records:
                    validate_record(record, SyncStart.model_validate(tx["start"]))

    def _cleanup(self, index):
        keep = {s["snapshot"] + ".json" for s in index["slots"].values() if s["snapshot"]}
        keep.update(b["object"] + ".json" for tx in index["syncs"].values() for b in tx["batches"])
        paths = []
        for path in (self.root / "objects").iterdir():
            if len(paths) >= MAX_OBJECT_FILES:
                raise DeviceDataError()
            self._safe(path)
            if not (re.fullmatch(r"[a-f0-9]{32}\.json", path.name) or re.fullmatch(r"\.tmp-[a-f0-9]{32}", path.name)) or not path.is_file():
                raise DeviceDataError()
            paths.append(path)
        for path in paths:
            if path.name not in keep:
                path.unlink()
        self._fsync(self.root / "objects")
        for path in self.root.glob(".tmp-*"):
            self._safe(path)
            if not re.fullmatch(r"\.tmp-[a-f0-9]{32}", path.name):
                raise DeviceDataError()
            path.unlink()

    def _maintain(self, index):
        now = time.time()
        changed = False
        for section in ("syncs", "receipts"):
            for key, value in list(index[section].items()):
                if now - value["created"] >= TRANSACTION_TTL:
                    del index[section][key]
                    changed = True
        if changed:
            self._write(self.root / "index.json", index)
        self._cleanup(index)
        return index

    def _publish(self, index):
        self._write(self.root / "index.json", index)
        self._cleanup(index)

    def _confirm_publication(self):
        # A prior replace may have succeeded while its directory fsync failed.
        # An idempotent retry must not acknowledge that state before durability.
        try:
            self._fsync(self.root)
        except OSError:
            raise DeviceDataError() from None

    def _new_object(self, value, limit):
        size = len(canonical(value))
        total = sum(p.stat().st_size for p in (self.root / "objects").iterdir())
        if total + size > MAX_STORE_BYTES:
            raise DeviceDataError("storage_budget", 409)
        name = uuid.uuid4().hex
        self._write(self._object(name), value, limit)
        return name

    def start(self, start: SyncStart):
        validate_start(start)
        with self.lock:
            index = self._maintain(self._load())
            for sid, tx in index["syncs"].items():
                if tx["start"]["device_id"] == start.device_id:
                    # Recover the ID if an identical opening response was lost.
                    # Different scopes still cannot share an active transaction.
                    if tx["start"] == start.model_dump():
                        self._confirm_publication()
                        return {"sync_id": sid}
                    raise DeviceDataError("sync_active", 409)
            devices = {key.split(":")[0] for key in index["slots"]}
            if start.device_id not in devices and len(devices) >= MAX_DEVICES:
                raise DeviceDataError("device_limit", 409)
            key = start.device_id + ":" + start.kind
            slot = index["slots"].setdefault(key, {"generation": 0, "snapshot": None})
            sid = uuid.uuid4().hex
            index["syncs"][sid] = {"start": start.model_dump(), "created": time.time(), "generation": slot["generation"], "batches": []}
            self._publish(index)
            return {"sync_id": sid}

    def _transaction(self, index, sid):
        identifier(sid)
        if sid not in index["syncs"]:
            raise DeviceDataError("sync_not_found", 404)
        return index["syncs"][sid]

    def batch(self, sid: str, bid: int, batch: Batch, actual_bytes: int):
        if type(bid) is not int or not 0 <= bid < MAX_BATCHES:
            raise DeviceDataError("invalid_batch_id", 422)
        # Swift's Codable omits nil fields. Do not inflate a valid wire batch
        # above the byte limit merely by adding absent optional null defaults.
        payload = batch.model_dump(exclude_none=True)
        raw = canonical(payload)
        if not 0 < actual_bytes <= MAX_BATCH_BYTES or len(raw) > MAX_BATCH_BYTES:
            raise DeviceDataError("body_limit", 413)
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock:
            index = self._maintain(self._load())
            tx = self._transaction(index, sid)
            start = SyncStart.model_validate(tx["start"])
            for record in batch.records:
                validate_record(record, start)
            if bid < len(tx["batches"]):
                if tx["batches"][bid]["hash"] != digest:
                    raise DeviceDataError("batch_conflict", 409)
                self._confirm_publication()
                return {"ok": True}
            if bid != len(tx["batches"]):
                raise DeviceDataError("batch_gap", 409)
            if sum(b["bytes"] for b in tx["batches"]) + actual_bytes > MAX_TRANSACTION_BYTES:
                raise DeviceDataError("transaction_limit", 413)
            name = self._new_object(payload, MAX_BATCH_BYTES)
            tx["batches"].append({"object": name, "hash": digest, "bytes": actual_bytes})
            self._publish(index)
            return {"ok": True}

    def commit(self, sid: str, commit: SyncCommit):
        identifier(sid)
        with self.lock:
            index = self._maintain(self._load())
            if sid in index["receipts"]:
                receipt = index["receipts"][sid]
                if receipt["commit"] != commit.model_dump() or receipt["generation"] != index["slots"][receipt["key"]]["generation"]:
                    raise DeviceDataError("commit_conflict", 409)
                self._confirm_publication()
                return {"ok": True}
            tx = self._transaction(index, sid)
            if len(tx["batches"]) != commit.batch_count:
                raise DeviceDataError("count_mismatch", 409)
            start = SyncStart.model_validate(tx["start"])
            fresh = {}
            count = 0
            for entry in tx["batches"]:
                raw = self._read(self._object(entry["object"]), MAX_BATCH_BYTES)
                if hashlib.sha256(canonical(raw)).hexdigest() != entry["hash"]:
                    raise DeviceDataError()
                batch = Batch.model_validate(raw)
                for record in batch.records:
                    validate_record(record, start)
                    key = (record.source, record.id)
                    if key in fresh:
                        raise DeviceDataError("duplicate_record", 409)
                    fresh[key] = record.model_dump()
                    count += 1
            if count != commit.record_count or sum(b["bytes"] for b in tx["batches"]) > MAX_TRANSACTION_BYTES:
                raise DeviceDataError("count_mismatch", 409)
            key = start.device_id + ":" + start.kind
            slot = index["slots"][key]
            old = self._snapshot(slot["snapshot"]) if slot["snapshot"] else {"records": [], "last_seen": []}
            merged = {(r["source"], r["id"]): (r, seen) for r, seen in zip(old["records"], old["last_seen"])}
            # Wall-clock adjustments must not invalidate retained timestamps or
            # make retained rows appear confirmed in a new sync at the same instant.
            previous = old.get("last_sync", 0.)
            now = max(time.time(), math.nextafter(previous, math.inf))
            if not math.isfinite(now) or now > 4102444800:
                raise DeviceDataError("invalid_server_clock", 503)
            merged.update({key: (record, now) for key, record in fresh.items()})
            if len(merged) > MAX_RECORDS:
                raise DeviceDataError("record_budget", 409)
            rows = [merged[key] for key in sorted(merged)]
            snapshot = {"version": 1, "start": start.model_dump(), "commit": commit.model_dump(), "last_sync": now,
                        "records": [r for r, _ in rows], "last_seen": [seen for _, seen in rows]}
            slot["snapshot"] = self._new_object(snapshot, MAX_SNAPSHOT_BYTES)
            del index["syncs"][sid]
            index["receipts"][sid] = {"key": key, "generation": slot["generation"], "created": now, "commit": commit.model_dump()}
            while len(index["receipts"]) > MAX_RECEIPTS:
                del index["receipts"][min(index["receipts"], key=lambda x: index["receipts"][x]["created"])]
            self._publish(index)
            return {"ok": True}

    def cancel(self, sid):
        identifier(sid)
        with self.lock:
            index = self._maintain(self._load())
            index["syncs"].pop(sid, None)
            self._publish(index)
            return {"ok": True}

    def delete(self, data: DeleteData):
        with self.lock:
            index = self._maintain(self._load())
            key = data.device_id + ":" + data.kind
            if key in index["slots"]:
                slot = index["slots"][key]
                slot["generation"] += 1
                slot["snapshot"] = None
                index["syncs"] = {sid: tx for sid, tx in index["syncs"].items() if tx["start"]["device_id"] + ":" + tx["start"]["kind"] != key}
                index["receipts"] = {sid: r for sid, r in index["receipts"].items() if r["key"] != key}
            self._publish(index)
            return {"ok": True}

    def summary(self, device_id):
        identifier(device_id)
        with self.lock:
            index = self._maintain(self._load())
            items = []
            for key, slot in sorted(index["slots"].items()):
                if key.split(":")[0] != device_id or not slot["snapshot"]:
                    continue
                snap = self._snapshot(slot["snapshot"])
                start = snap["start"]
                items.append({"device_id": device_id, "kind": start["kind"], "record_count": len(snap["records"]),
                              "last_sync": snap["last_sync"], "last_read_count": snap["commit"]["record_count"],
                              **{k: snap["commit"][k] for k in ("completeness", "reason")},
                              **{k: start[k] for k in ("version", "window_start", "window_end", "timezone", "sources")}, "retained_history": True})
            return {"items": items}

    def records(self, device_id, kind, offset=0, limit=100):
        identifier(device_id)
        if kind not in METRICS or type(offset) is not int or not 0 <= offset <= MAX_RECORDS or type(limit) is not int or not 1 <= limit <= 200:
            raise DeviceDataError("invalid_page", 422)
        with self.lock:
            index = self._maintain(self._load())
            slot = index["slots"].get(device_id + ":" + kind)
            snap = self._snapshot(slot["snapshot"]) if slot and slot["snapshot"] else {"records": [], "last_seen": [], "last_sync": None}
            def page(count):
                end = offset + count
                return {"records": snap["records"][offset:end], "offset": offset,
                        "next_offset": end if end < len(snap["records"]) else None,
                        "metadata": {"last_seen": snap["last_seen"][offset:end], "last_sync": snap["last_sync"],
                                     "confirmed_in_last_sync": [seen == snap["last_sync"] for seen in snap["last_seen"][offset:end]],
                                     "sync": {**snap.get("start", {}), **snap.get("commit", {})},
                                     "retained_history": True, "snapshot": slot["snapshot"] if slot else None}}

            # Respect both the row limit and encoded bytes, including metadata.
            # Binary search keeps Unicode-heavy pages bounded without skipping rows.
            available = min(limit, max(0, len(snap["records"]) - offset))
            low, high = 0, available
            while low < high:
                middle = (low + high + 1) // 2
                if len(canonical(page(middle))) <= MAX_PAGE_BYTES:
                    low = middle
                else:
                    high = middle - 1
            result = page(low)
            if (available and low == 0) or len(canonical(result)) > MAX_PAGE_BYTES:
                raise DeviceDataError("page_budget", 503)
            return copy.deepcopy(result)
