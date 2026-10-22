from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import math
import os
import re
import stat
import struct
import threading
import time
import unicodedata
import uuid
import zipfile
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MAX_INPUT_CHARS = 32000
MAX_OUTPUT_BYTES = 131072
MAX_UPLOAD_BYTES = 10485760
MAX_TURN_FILES = 5
MAX_TURN_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_THREADS = 100
MAX_MESSAGES = 500
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_UNBOUND = 10
UNBOUND_TTL = 24 * 60 * 60
# Keep retired keys after pruning so repeated requests cannot execute twice.
MAX_TURN_IDS = 10000
MAX_LEDGER_BYTES = 16 * 1024 * 1024
MAX_ZIP_ENTRIES = 512
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_PENDING_NOTIFICATIONS = 256
MAX_SCHEDULED_IDS = 10000
_PLACEHOLDER_TITLES = {"个人助手", "新对话"}
_TITLE_SOURCES = {"placeholder", "auto", "custom"}

_ID = re.compile(r"[0-9a-f]{32}\Z")
_TERMINAL = {"complete", "completed", "failed", "error", "cancelled", "interrupted", "truncated"}
_RETRYABLE = {"failed", "error", "cancelled", "interrupted", "truncated"}
_PUBLIC_ATTACHMENT = ("id", "name", "mime", "size", "kind")
_PUBLIC_TURN = (
    "id", "client_turn_id", "user_message_id", "assistant_message_id", "status",
    "generation", "retry_of", "created_at", "updated_at",
)
_TYPES = {
    "png": ("png", "image", {"image/png"}),
    "jpg": ("jpg", "image", {"image/jpeg"}),
    "jpeg": ("jpg", "image", {"image/jpeg"}),
    "gif": ("gif", "image", {"image/gif"}),
    "webp": ("webp", "image", {"image/webp"}),
    "pdf": ("pdf", "file", {"application/pdf"}),
    "txt": ("txt", "file", {"text/plain"}),
    "md": ("md", "file", {"text/markdown", "text/plain"}),
    "json": ("json", "file", {"application/json", "text/json"}),
    "csv": ("csv", "file", {"text/csv", "text/plain"}),
    "docx": ("docx", "file", {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    "xlsx": ("xlsx", "file", {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    "pptx": ("pptx", "file", {"application/vnd.openxmlformats-officedocument.presentationml.presentation"}),
}
_EXTENSIONS = {item[0] for item in _TYPES.values()}
_OFFICE = {
    "docx": ("word/document.xml", "document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"),
    "xlsx": ("xl/workbook.xml", "workbook", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"),
    "pptx": ("ppt/presentation.xml", "presentation", "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"),
}


class ChatError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def validate_id(value: str) -> str:
    """Accept only lowercase, 32-character UUID hex, never paths or UUID dashes."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ChatError("invalid_id", "标识必须是小写 UUID hex")
    return value


def _client_id(value: str) -> str:
    # Browser idempotency keys are dashed UUIDs; resource IDs are filename-safe hex.
    if not isinstance(value, str):
        raise ChatError("invalid_client_turn_id", "客户端轮次标识必须是 UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ChatError("invalid_client_turn_id", "客户端轮次标识必须是 UUID") from exc
    if value not in {parsed.hex, str(parsed)}:
        raise ChatError("invalid_client_turn_id", "客户端轮次标识须为小写 UUID hex 或标准 UUID")
    return parsed.hex


def _public_attachment(record: dict) -> dict:
    return {key: record[key] for key in _PUBLIC_ATTACHMENT}


def _public_turn(record: dict) -> dict:
    return {key: record[key] for key in _PUBLIC_TURN}


def _public_message(record: dict) -> dict:
    keys = ("id", "role", "text", "status", "turn_id", "created_at", "updated_at", "attachments", "generation", "truncated", "tool_calls", "tools_truncated", "kind", "event_id")
    return copy.deepcopy({key: record[key] for key in keys if key in record})


def _event_id(value: str) -> str:
    if (not _text(value) or len(value.encode("utf-8")) > 256 or not value.strip()
            or any(unicodedata.category(c).startswith("C") for c in value)):
        raise ChatError("invalid_event_id", "事件标识须为 1 至 256 字节的可显示文本")
    return value


def _notification(value: dict, thread_id: str) -> dict:
    if (not isinstance(value, dict) or not isinstance(value.get("kind"), str)
            or value["kind"] not in {"reply", "reminder", "daily"}):
        raise ChatError("invalid_notification", "通知元数据无效")
    keys = {"event_id", "thread_id", "message_id", "kind"}
    if value["kind"] == "reply":
        keys.add("owner")
    if set(value) != keys or value["thread_id"] != thread_id:
        raise ChatError("invalid_notification", "通知元数据字段无效")
    validate_id(value["thread_id"])
    validate_id(value["message_id"])
    _event_id(value["event_id"])
    if "owner" in value:
        _event_id(value["owner"])
    return dict(value)


def _validate_tools(tools: list[dict]) -> None:
    if not isinstance(tools, list) or len(tools) > 30:
        raise ChatError("invalid_tools", "工具活动超出限制")
    ids = set()
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {"id", "name", "summary", "status", "started_at", "duration_seconds"}:
            raise ChatError("invalid_tools", "工具活动字段无效")
        validate_id(tool["id"])
        if (tool["id"] in ids or not isinstance(tool["name"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", tool["name"])
                or not isinstance(tool["summary"], str) or len(tool["summary"]) > 160
                or tool["status"] not in {"running", "completed", "error", "cancelled", "interrupted"}
                or type(tool["started_at"]) not in (int, float) or not math.isfinite(tool["started_at"])
                or not 0 <= tool["started_at"] < 1e12):
            raise ChatError("invalid_tools", "工具活动内容无效")
        _text(tool["summary"])
        duration = tool["duration_seconds"]
        if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or not 0 <= duration <= 86400):
            raise ChatError("invalid_tools", "工具活动耗时无效")
        ids.add(tool["id"])


def _message_bytes(message: dict) -> int:
    tools = message.get("tool_calls", [])
    return len(message["text"].encode("utf-8")) + (len(json.dumps(tools, ensure_ascii=False).encode("utf-8")) if tools else 0)


def _json_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _text(value: str) -> bytes:
    if not isinstance(value, str):
        raise ChatError("invalid_text", "文本必须是字符串")
    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise ChatError("invalid_text", "文本不是有效 UTF-8") from exc


def _title(value: str) -> str:
    _text(value)
    value = value.strip()
    if not value or len(value) > 200 or any(unicodedata.category(c).startswith("C") for c in value):
        raise ChatError("invalid_title", "标题须为 1 至 200 字的可显示文本")
    return value


def _filename(value: str) -> str:
    _text(value)
    if "/" in value or "\\" in value or value.strip() in {"", ".", ".."}:
        raise ChatError("invalid_filename", "文件名不得包含路径")
    # Strip control and directional formatting characters, not Unicode letters.
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C")).strip()
    if not value or value.startswith(".") or len(value) > 200:
        raise ChatError("invalid_filename", "文件名无效或过长")
    return value


def _xml(data: bytes) -> ElementTree.Element:
    # UTF-8 only: scanning bytes alone would miss a UTF-16 encoded DTD.
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ChatError("invalid_upload", "文档 XML 须为 UTF-8") from exc
    if "<!doctype" in text.lower() or "<!entity" in text.lower():
        raise ChatError("invalid_upload", "文档 XML 不允许实体或 DTD")
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ChatError("invalid_upload", "文档 XML 无效") from exc


def _validate_office(data: bytes, extension: str) -> None:
    main, root_tag, content_type = _OFFICE[extension]
    # Bound the central directory before ZipFile allocates its entry objects.
    end = data.rfind(b"PK\x05\x06")
    if end < 0 or len(data) - end < 22:
        raise ChatError("invalid_upload", "文档 ZIP 无效")
    disk, directory_disk, disk_entries, entries, size, offset, comment = struct.unpack_from("<4H2IH", data, end + 4)
    if (disk or directory_disk or disk_entries != entries or not 0 < entries <= MAX_ZIP_ENTRIES
            or offset + size != end or end + 22 + comment != len(data)
            or data[offset:end].count(b"PK\x01\x02") != entries):
        raise ChatError("invalid_upload", "文档 ZIP 中央目录无效或超限")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ZIP_ENTRIES:
                raise ChatError("invalid_upload", "文档 ZIP 条目过多")
            expanded = 0
            names: set[str] = set()
            required: dict[str, bytes] = {}
            for entry in entries:
                name = entry.filename
                parts = name.rstrip("/").split("/")
                lower = name.lower()
                if (name != entry.orig_filename or name in names or not name or name.startswith("/") or "\\" in name
                        or any(p in {"", ".", ".."} or ":" in p for p in parts)
                        or any(unicodedata.category(c).startswith("C") for c in name)
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or entry.flag_bits & 1
                        or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or re.search(r"(?:vbaproject|activex|embeddings)", lower)
                        or Path(lower).suffix in {".exe", ".dll", ".js", ".vbs", ".sh", ".zip", ".html", ".svg"}):
                    raise ChatError("invalid_upload", "文档包含不安全 ZIP 条目")
                names.add(name)
                expanded += entry.file_size
                if (expanded > MAX_ZIP_BYTES or entry.file_size > 20 * 1024 * 1024
                        or entry.file_size > max(1, entry.compress_size) * 100):
                    raise ChatError("invalid_upload", "文档解压大小或压缩比超限")
                # Stream every member to check CRC without unbounded decompression.
                with archive.open(entry) as source:
                    read = 0
                    chunks = []
                    while chunk := source.read(65536):
                        read += len(chunk)
                        if read > entry.file_size or read > MAX_ZIP_BYTES:
                            raise ChatError("invalid_upload", "文档解压大小超限")
                        if name in {main, "[Content_Types].xml", "_rels/.rels"}:
                            chunks.append(chunk)
                    if read != entry.file_size:
                        raise ChatError("invalid_upload", "文档 ZIP 大小不符")
                    if chunks:
                        required[name] = b"".join(chunks)
            if not {main, "[Content_Types].xml", "_rels/.rels"}.issubset(required):
                raise ChatError("invalid_upload", "不是允许的 Office 文档结构")
            types = _xml(required["[Content_Types].xml"])
            if types.tag != "{http://schemas.openxmlformats.org/package/2006/content-types}Types":
                raise ChatError("invalid_upload", "文档内容类型无效")
            if any("macro" in node.get("ContentType", "").lower() for node in types):
                raise ChatError("invalid_upload", "不允许宏文档")
            if not any(node.tag.endswith("}Override") and node.get("PartName") == "/" + main
                       and node.get("ContentType") == content_type for node in types):
                raise ChatError("mime_mismatch", "Office 类型与文件类型不符")
            if _xml(required[main]).tag.rsplit("}", 1)[-1] != root_tag:
                raise ChatError("invalid_upload", "文档主条目无效")
            relationships = _xml(required["_rels/.rels"])
            if relationships.tag != "{http://schemas.openxmlformats.org/package/2006/relationships}Relationships":
                raise ChatError("invalid_upload", "文档关系结构无效")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, zlib.error) as exc:
        raise ChatError("invalid_upload", "文档 ZIP 无效") from exc


def _validate_content(data: bytes, extension: str) -> None:
    detected = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        detected = "jpg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        detected = "gif"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        detected = "webp"
    elif data.startswith(b"%PDF-"):
        detected = "pdf"
    elif data.startswith(b"PK"):
        detected = "office"
    if detected:
        if detected == "office" and extension in _OFFICE:
            _validate_office(data, extension)
        elif detected != extension:
            raise ChatError("mime_mismatch", "文件内容与声明类型不符")
        return
    if extension not in {"txt", "md", "json", "csv"}:
        raise ChatError("mime_mismatch", "文件内容与声明类型不符")
    if data.startswith((b"MZ", b"\x7fELF", b"#!", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe")):
        raise ChatError("invalid_upload", "不允许可执行文件")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ChatError("invalid_upload", "文本文件须为 UTF-8") from exc
    if any(ord(c) < 32 and c not in "\t\n\r\f" for c in text):
        raise ChatError("invalid_upload", "文本包含二进制控制字符")
    if re.search(r"<(?:!doctype\s+html|(?:[\w.-]+:)?(?:html|svg)\b|script\b|body\b|head\b|iframe\b)", text.lower()):
        raise ChatError("invalid_upload", "不允许 HTML 或 SVG")
    if extension == "json":
        try:
            json.loads(text, parse_constant=_invalid_constant)
        except (ValueError, RecursionError) as exc:
            raise ChatError("invalid_upload", "JSON 无效") from exc


class WebChatStore:
    """Bounded atomic storage beneath the explicitly supplied private root."""

    validate_id = staticmethod(validate_id)

    def __init__(self, root: str | Path):
        self.root = Path(os.path.abspath(root))
        self._lock = threading.RLock()
        self._uploads: set[tuple[str, str]] = set()
        self._index: dict[str, dict] = {}
        self._mkdir(self.root)
        self._mkdir(self.root / "threads")
        self._mkdir(self.root / "attachments")
        with self._lock:
            self._check_index()
            # Validate everything before recovering or replacing any JSON file.
            ledgers = []
            for path in (self.root / "threads").iterdir():
                self._safe(path)
                if path.name.startswith(".tmp-") and path.is_file():
                    continue
                if path.suffix != ".json" or not _ID.fullmatch(path.stem):
                    self._corrupt()
                ledgers.append(self._ledger(path.stem))
            if len(ledgers) > MAX_THREADS:
                self._corrupt()
            for ledger in ledgers:
                self._check_files(ledger)
            for directory in (self.root / "attachments").iterdir():
                self._safe(directory)
                if not _ID.fullmatch(directory.name) or not directory.is_dir():
                    self._corrupt()
                self._check_directory(directory)
            self._index = {ledger["id"]: self._summary(ledger) for ledger in ledgers}
            for ledger in ledgers:
                changed = self._migrate(ledger)
                for turn in ledger["turns"]:
                    if turn["status"] == "running":
                        self._finish_record(ledger, turn, "", "interrupted")
                        changed = True
                for attachment in ledger["attachments"].values():
                    if attachment["state"] == "uploading":
                        attachment["state"] = "reserved"
                        changed = True
                changed = self._expire(ledger) or changed
                if changed:
                    self._save(ledger, write_index=False)
                self._cleanup_files(ledger)
            for directory in (self.root / "attachments").iterdir():
                if directory.name not in self._index:
                    self._remove_directory(directory)
            for parent in (self.root, self.root / "threads"):
                for path in parent.iterdir():
                    if re.fullmatch(r"\.tmp-[0-9a-f]{32}", path.name):
                        self._safe(path)
                        if not path.is_file() or path.stat().st_nlink != 1:
                            self._corrupt()
                        path.unlink()
            self._write_index()

    @staticmethod
    def _corrupt() -> None:
        raise ChatError("store_corrupt", "聊天存储损坏或包含不安全路径；未覆盖原数据", 503)

    @staticmethod
    def _timestamp(value: Any) -> bool:
        return type(value) in {int, float} and 0 <= value < 1e12

    def _safe(self, path: Path) -> None:
        # Never resolve() a supplied symlink. Inspect ancestors instead.
        for part in [*reversed(path.parents), path]:
            try:
                mode = part.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                self._corrupt()
            if part != path and not stat.S_ISDIR(mode):
                self._corrupt()

    def _mkdir(self, path: Path) -> None:
        self._safe(path)
        if not path.exists():
            if not path.parent.exists():
                self._mkdir(path.parent)
            path.mkdir(mode=0o700)
        if not path.is_dir():
            self._corrupt()
        os.chmod(path, 0o700)

    def _json(self, path: Path) -> dict:
        self._safe(path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_LEDGER_BYTES:
                    self._corrupt()
                data = source.read(MAX_LEDGER_BYTES + 1)
                if len(data) > MAX_LEDGER_BYTES:
                    self._corrupt()
                result = json.loads(data.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_invalid_constant)
                os.fchmod(source.fileno(), 0o600)
                return result
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, ChatError):
                raise
            raise ChatError("store_corrupt", "聊天存储无法安全读取；未覆盖原数据", 503) from exc

    def _atomic_json(self, path: Path, value: dict) -> None:
        self._safe(path)
        temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
        try:
            data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(data) > MAX_LEDGER_BYTES:
                raise ChatError("store_limit", "聊天台账大小超限", 409)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            self._safe(path)
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        except OSError as exc:
            raise ChatError("storage_error", "聊天存储写入失败", 503) from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _ledger(self, thread_id: str) -> dict:
        ledger = self._json(self.root / "threads" / (thread_id + ".json"))
        try:
            if (ledger["version"] != 1 or ledger["id"] != thread_id
                    or _title(ledger["title"]) != ledger["title"]
                    or type(ledger["context_generation"]) is not int or ledger["context_generation"] < 0
                    or type(ledger["history_truncated"]) is not bool
                    or not all(self._timestamp(ledger[key]) for key in ("created_at", "updated_at"))
                    or not isinstance(ledger["messages"], list) or len(ledger["messages"]) > MAX_MESSAGES
                    or not isinstance(ledger["turns"], list) or not isinstance(ledger["attachments"], dict)
                    or not isinstance(ledger["retired_client_turn_ids"], list)
                    or len(ledger["turns"]) + len(ledger["retired_client_turn_ids"]) > MAX_TURN_IDS):
                self._corrupt()
            if "title_source" in ledger:
                source = ledger["title_source"]
                if (not isinstance(source, str) or source not in _TITLE_SOURCES
                        or (source == "placeholder" and ledger["title"] not in _PLACEHOLDER_TITLES)
                        or (source == "auto" and len(ledger["title"]) > 32)):
                    self._corrupt()
            retired_scheduled = ledger.get("retired_scheduled_event_ids", [])
            pending = ledger.get("pending_notifications", [])
            if (not isinstance(retired_scheduled, list) or len(retired_scheduled) > MAX_SCHEDULED_IDS
                    or not isinstance(pending, list) or len(pending) > MAX_PENDING_NOTIFICATIONS):
                self._corrupt()
            scheduled = set()
            for event_id in retired_scheduled:
                _event_id(event_id)
                if event_id in scheduled:
                    self._corrupt()
                scheduled.add(event_id)
            messages = {}
            text_bytes = 0
            for message in ledger["messages"]:
                validate_id(message["id"])
                if (message["id"] in messages or message["role"] not in {"user", "assistant", "system"}
                        or message["status"] not in _TERMINAL | {"running"}
                        or not isinstance(message["attachments"], list)
                        or len(message["attachments"]) > MAX_TURN_FILES
                        or not all(self._timestamp(message[key]) for key in ("created_at", "updated_at"))):
                    self._corrupt()
                encoded = _text(message["text"])
                tools = message.get("tool_calls", [])
                _validate_tools(tools)
                if ((message["role"] != "assistant" and (tools or "tools_truncated" in message))
                        or ("tools_truncated" in message and type(message["tools_truncated"]) is not bool)):
                    self._corrupt()
                text_bytes += _message_bytes(message)
                if ((message["role"] == "user" and len(message["text"]) > MAX_INPUT_CHARS)
                        or (message["role"] == "assistant" and (len(encoded) > MAX_OUTPUT_BYTES or message["attachments"]))
                        or ("truncated" in message and type(message["truncated"]) is not bool)):
                    self._corrupt()
                if message["role"] == "assistant" and message["turn_id"] is None:
                    event_id = _event_id(message["event_id"])
                    if (event_id in scheduled or message["kind"] not in {"reminder", "daily"}
                            or message["status"] != "complete" or tools
                            or type(message["generation"]) is not int
                            or not 0 <= message["generation"] <= ledger["context_generation"]):
                        self._corrupt()
                    scheduled.add(event_id)
                elif "kind" in message or "event_id" in message:
                    self._corrupt()
                elif message["role"] == "system":
                    if message["turn_id"] is not None or message["attachments"]:
                        self._corrupt()
                else:
                    validate_id(message["turn_id"])
                for attachment in message["attachments"]:
                    if set(attachment) != set(_PUBLIC_ATTACHMENT):
                        self._corrupt()
                messages[message["id"]] = message
            if text_bytes > MAX_TEXT_BYTES:
                self._corrupt()
            clients = set()
            turns = {}
            referenced = set()
            running = 0
            for turn in ledger["turns"]:
                for key in ("id", "user_message_id", "assistant_message_id"):
                    validate_id(turn[key])
                client_key = _client_id(turn["client_turn_id"])
                user = messages[turn["user_message_id"]]
                assistant = messages[turn["assistant_message_id"]]
                if (turn["id"] in turns or client_key in clients
                        or user["role"] != "user" or assistant["role"] != "assistant"
                        or user["turn_id"] != turn["id"] or assistant["turn_id"] != turn["id"]
                        or turn["status"] != assistant["status"]
                        or type(turn["generation"]) is not int or not 0 <= turn["generation"] <= ledger["context_generation"]
                        or not all(self._timestamp(turn[key]) for key in ("created_at", "updated_at"))
                        or type(turn.get("seq", 0)) is not int or turn.get("seq", 0) < 0
                        or not re.fullmatch(r"[0-9a-f]{64}", turn["request_hash"])):
                    self._corrupt()
                if turn["retry_of"] is not None:
                    validate_id(turn["retry_of"])
                running += turn["status"] == "running"
                if turn["status"] == "running" and turn["generation"] != ledger["context_generation"]:
                    self._corrupt()
                referenced.update((user["id"], assistant["id"]))
                turns[turn["id"]] = turn
                clients.add(client_key)
            if (running > 1 or referenced != {m["id"] for m in messages.values() if m["turn_id"] is not None}
                    or len(scheduled) > MAX_SCHEDULED_IDS or scheduled.intersection(turns)):
                self._corrupt()
            notification_ids = set()
            for item in pending:
                notification = _notification(item, thread_id)
                event_id = notification["event_id"]
                message = messages.get(notification["message_id"])
                if event_id in notification_ids:
                    self._corrupt()
                if message is None:
                    if not ledger["history_truncated"]:
                        self._corrupt()
                    notification_ids.add(event_id)
                    continue
                if message["role"] != "assistant":
                    self._corrupt()
                if notification["kind"] == "reply":
                    if (message["turn_id"] != event_id or message["status"] not in {"complete", "completed"}
                            or messages[turns[event_id]["user_message_id"]]["text"].lstrip().startswith("/")):
                        self._corrupt()
                elif message.get("event_id") != event_id or message.get("kind") != notification["kind"]:
                    self._corrupt()
                notification_ids.add(event_id)
            for client in ledger["retired_client_turn_ids"]:
                validate_id(client)
                if client in clients:
                    self._corrupt()
                clients.add(client)
            unbound = 0
            for key, attachment in ledger["attachments"].items():
                validate_id(key)
                if (attachment["id"] != key or _filename(attachment["name"]) != attachment["name"]
                        or attachment["extension"] not in _EXTENSIONS
                        or attachment["kind"] not in {"image", "file"}
                        or type(attachment["size"]) is not int or not 0 < attachment["size"] <= MAX_UPLOAD_BYTES
                        or attachment["state"] not in {"reserved", "uploading", "ready"}
                        or type(attachment["generation"]) is not int
                        or not 0 <= attachment["generation"] <= ledger["context_generation"]
                        or not all(self._timestamp(attachment[k]) for k in ("created_at", "updated_at"))
                        or not isinstance(attachment["bound_turn_ids"], list)):
                    self._corrupt()
                extension, kind, mimes = _TYPES[Path(attachment["name"]).suffix[1:].lower()]
                if extension != attachment["extension"] or kind != attachment["kind"] or attachment["mime"] not in mimes:
                    self._corrupt()
                bindings = attachment["bound_turn_ids"]
                if len(set(bindings)) != len(bindings) or (bindings and attachment["state"] != "ready"):
                    self._corrupt()
                for binding in bindings:
                    turn = turns[binding]
                    if turn["generation"] != attachment["generation"]:
                        self._corrupt()
                    user = messages[turn["user_message_id"]]
                    if _public_attachment(attachment) not in user["attachments"]:
                        self._corrupt()
                unbound += not bindings
            if unbound > MAX_UNBOUND:
                self._corrupt()
            for message in messages.values():
                if len({a["id"] for a in message["attachments"]}) != len(message["attachments"]):
                    self._corrupt()
                if sum(a["size"] for a in message["attachments"]) > MAX_TURN_UPLOAD_BYTES:
                    self._corrupt()
                for attachment in message["attachments"]:
                    record = ledger["attachments"][attachment["id"]]
                    if attachment != _public_attachment(record) or message["turn_id"] not in record["bound_turn_ids"]:
                        self._corrupt()
            return ledger
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            if isinstance(exc, ChatError) and exc.code == "store_corrupt":
                raise
            raise ChatError("store_corrupt", "聊天台账结构无效；未覆盖原数据", 503) from exc

    @staticmethod
    def _auto_title(ledger: dict) -> None:
        if ledger["title_source"] != "placeholder":
            return
        for message in ledger["messages"]:
            if message["role"] != "user" or message["text"].lstrip().startswith("/"):
                continue
            text = " ".join(message["text"].split())
            text = "".join(c for c in text if not unicodedata.category(c).startswith("C"))
            if not text and message["attachments"]:
                text = message["attachments"][0]["name"]
            if text:
                ledger["title"], ledger["title_source"] = text[:32], "auto"
                return

    def _migrate(self, ledger: dict) -> bool:
        changed = False
        if "title_source" not in ledger:
            ledger["title_source"] = "placeholder" if ledger["title"] in _PLACEHOLDER_TITLES else "custom"
            self._auto_title(ledger)
            changed = True
        for key in ("pending_notifications", "retired_scheduled_event_ids"):
            if key not in ledger:
                ledger[key] = []
                changed = True
        return changed

    def _summary(self, ledger: dict) -> dict:
        return {"id": ledger["id"], "title": ledger["title"], "title_source": ledger.get("title_source", "placeholder"),
                "message_count": len(ledger["messages"]), "history_truncated": ledger["history_truncated"],
                "created_at": ledger["created_at"], "updated_at": ledger["updated_at"], "time": ledger["updated_at"]}

    def _check_index(self) -> None:
        path = self.root / "index.json"
        if not path.exists() and not path.is_symlink():
            return
        index = self._json(path)
        if (not isinstance(index, dict) or index.get("version") != 1
                or not isinstance(index.get("threads"), list) or len(index["threads"]) > MAX_THREADS):
            self._corrupt()
        seen = set()
        for summary in index["threads"]:
            if (not isinstance(summary, dict) or not _ID.fullmatch(str(summary.get("id", "")))
                    or summary["id"] in seen or not isinstance(summary.get("title"), str)
                    or ("title_source" in summary and (not isinstance(summary["title_source"], str)
                                                      or summary["title_source"] not in _TITLE_SOURCES))
                    or type(summary.get("message_count")) is not int or not 0 <= summary["message_count"] <= MAX_MESSAGES
                    or type(summary.get("history_truncated")) is not bool
                    or not all(self._timestamp(summary.get(key)) for key in ("time", "created_at", "updated_at"))):
                self._corrupt()
            seen.add(summary["id"])

    def _write_index(self) -> None:
        self._check_index()
        self._atomic_json(self.root / "index.json", {"version": 1, "threads": self._sorted_summaries()})

    def _sorted_summaries(self) -> list[dict]:
        return copy.deepcopy(sorted(self._index.values(), key=lambda s: (s["time"], s["id"]), reverse=True))

    def _save(self, ledger: dict, *, write_index: bool = True) -> None:
        self._check_index()
        directory = self.root / "attachments" / ledger["id"]
        self._safe(directory)
        if directory.exists():
            self._check_directory(directory)
        self._atomic_json(self.root / "threads" / (ledger["id"] + ".json"), ledger)
        self._index[ledger["id"]] = self._summary(ledger)
        if write_index:
            self._write_index()
        self._cleanup_files(ledger)

    def _read(self, thread_id: str) -> dict:
        validate_id(thread_id)
        if thread_id not in self._index:
            raise ChatError("thread_not_found", "对话不存在", 404)
        ledger = self._ledger(thread_id)
        self._check_files(ledger)
        changed = self._migrate(ledger)
        if self._expire(ledger) or changed:
            self._save(ledger)
        return ledger

    def _expire(self, ledger: dict) -> bool:
        expired = [key for key, a in ledger["attachments"].items()
                   if not a["bound_turn_ids"] and time.time() - a["created_at"] > UNBOUND_TTL]
        for key in expired:
            del ledger["attachments"][key]
        return bool(expired)

    def _attachment_path(self, thread_id: str, attachment: dict) -> Path:
        return self.root / "attachments" / thread_id / (attachment["id"] + "." + attachment["extension"])

    def _check_directory(self, directory: Path) -> None:
        self._safe(directory)
        os.chmod(directory, 0o700)
        for path in directory.iterdir():
            self._safe(path)
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or not (re.fullmatch(r"\.upload-[0-9a-f]{32}", path.name)
                            or (_ID.fullmatch(path.stem) and path.suffix[1:] in _EXTENSIONS))):
                self._corrupt()
            os.chmod(path, 0o600)

    def _check_files(self, ledger: dict) -> None:
        self._safe(self.root / "attachments" / ledger["id"])
        for attachment in ledger["attachments"].values():
            path = self._attachment_path(ledger["id"], attachment)
            self._safe(path)
            if attachment["state"] == "ready":
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    self._corrupt()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != attachment["size"]:
                    self._corrupt()

    def _cleanup_files(self, ledger: dict) -> None:
        directory = self.root / "attachments" / ledger["id"]
        self._safe(directory)
        if not directory.exists():
            return
        self._check_directory(directory)
        keep = {self._attachment_path(ledger["id"], a).name for a in ledger["attachments"].values() if a["state"] == "ready"}
        keep.update(".upload-" + a for thread, a in self._uploads if thread == ledger["id"]
                    and a in ledger["attachments"] and ledger["attachments"][a]["state"] == "uploading")
        for path in directory.iterdir():
            if path.name not in keep:
                path.unlink()

    def _remove_directory(self, directory: Path) -> None:
        self._safe(directory)
        if directory.exists():
            self._check_directory(directory)
            for path in directory.iterdir():
                path.unlink()
            directory.rmdir()

    def list_threads(self) -> list[dict]:
        with self._lock:
            for thread_id in list(self._index):
                ledger = self._read(thread_id)
                self._index[thread_id] = self._summary(ledger)
            return self._sorted_summaries()

    def create_thread(self, title: str = "新对话") -> dict:
        title = _title(title)
        with self._lock:
            if len(self._index) >= MAX_THREADS:
                raise ChatError("thread_limit", "最多保留 100 个对话", 409)
            thread_id = uuid.uuid4().hex
            now = time.time()
            ledger = {"version": 1, "id": thread_id, "title": title, "context_generation": 0,
                      "title_source": "placeholder" if title in _PLACEHOLDER_TITLES else "custom",
                      "created_at": now, "updated_at": now, "history_truncated": False,
                      "messages": [], "turns": [], "attachments": {}, "retired_client_turn_ids": [],
                      "pending_notifications": [], "retired_scheduled_event_ids": []}
            self._save(ledger)
            return self._summary(ledger)

    def get_thread(self, thread_id: str) -> dict:
        """PRIVATE: complete independent ledger copy, not an HTTP response."""
        with self._lock:
            return copy.deepcopy(self._read(thread_id))

    def messages(self, thread_id: str, limit: int = 50, before: str | None = None) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ChatError("invalid_limit", "分页条数须为 1 至 100")
        if before is not None:
            validate_id(before)
        with self._lock:
            ledger = self._read(thread_id)
            end = len(ledger["messages"])
            if before is not None:
                end = next((i for i, m in enumerate(ledger["messages"]) if m["id"] == before), -1)
                if end == -1:
                    raise ChatError("message_not_found", "分页消息不存在", 404)
            start = max(0, end - limit)
            return {"thread": self._summary(ledger), "messages": [_public_message(m) for m in ledger["messages"][start:end]],
                    "has_more": start > 0}

    def rename_thread(self, thread_id: str, title: str) -> dict:
        title = _title(title)
        with self._lock:
            ledger = self._read(thread_id)
            ledger["title"] = title
            ledger["title_source"] = "custom"
            ledger["updated_at"] = time.time()
            self._save(ledger)
            return self._summary(ledger)

    def reset_thread(self, thread_id: str) -> dict:
        with self._lock:
            ledger = self._read(thread_id)
            for turn in ledger["turns"]:
                if turn["status"] == "running":
                    self._finish_record(ledger, turn, "", "interrupted")
            ledger["context_generation"] += 1
            ledger["attachments"] = {key: a for key, a in ledger["attachments"].items() if a["bound_turn_ids"]}
            now = time.time()
            ledger["messages"].append({"id": uuid.uuid4().hex, "role": "system", "text": "上下文已重置",
                                       "status": "completed", "turn_id": None, "created_at": now,
                                       "updated_at": now, "attachments": [], "generation": ledger["context_generation"]})
            ledger["updated_at"] = now
            self._prune(ledger)
            self._save(ledger)
            return self._summary(ledger)

    def delete_thread(self, thread_id: str) -> dict:
        with self._lock:
            ledger = self._read(thread_id)
            directory = self.root / "attachments" / thread_id
            if directory.exists():
                self._check_directory(directory)
            replacement = self.create_thread() if len(self._index) == 1 else None
            path = self.root / "threads" / (ledger["id"] + ".json")
            self._safe(path)
            path.unlink()
            self._fsync_directory(path.parent)
            del self._index[thread_id]
            self._write_index()
            self._remove_directory(directory)
            summaries = self._sorted_summaries()
            return {"threads": summaries, "replacement": replacement or (summaries[0] if summaries else None)}

    def reserve_attachment(self, thread_id: str, name: str, mime: str, size: int) -> dict:
        name = _filename(name)
        suffix = Path(name).suffix[1:].lower()
        if suffix not in _TYPES:
            raise ChatError("unsupported_type", "不允许此文件类型", 415)
        extension, kind, mimes = _TYPES[suffix]
        if not isinstance(mime, str) or mime.lower().strip() not in mimes:
            raise ChatError("mime_mismatch", "文件 MIME 与文件名类型不符", 415)
        mime = mime.lower().strip()
        if type(size) is not int or not 0 < size <= MAX_UPLOAD_BYTES:
            raise ChatError("upload_size", "文件须为 1 字节至 10 MiB", 413)
        with self._lock:
            ledger = self._read(thread_id)
            if sum(not a["bound_turn_ids"] for a in ledger["attachments"].values()) >= MAX_UNBOUND:
                raise ChatError("attachment_limit", "每个对话最多 10 个未发送附件", 409)
            attachment_id = uuid.uuid4().hex
            now = time.time()
            attachment = {"id": attachment_id, "name": name, "mime": mime, "size": size, "kind": kind,
                          "extension": extension, "state": "reserved", "created_at": now, "updated_at": now,
                          "generation": ledger["context_generation"], "bound_turn_ids": []}
            self._mkdir(self.root / "attachments" / thread_id)
            ledger["attachments"][attachment_id] = attachment
            self._save(ledger)
            return _public_attachment(attachment)

    @staticmethod
    def _find_attachment(ledger: dict, attachment_id: str) -> dict:
        validate_id(attachment_id)
        attachment = ledger["attachments"].get(attachment_id)
        if attachment is None:
            raise ChatError("attachment_not_found", "附件不存在或已过期", 404)
        return attachment

    async def write_attachment(self, thread_id: str, attachment_id: str, chunks: AsyncIterator[bytes]) -> dict:
        validate_id(thread_id)
        validate_id(attachment_id)
        key = (thread_id, attachment_id)
        temporary = self.root / "attachments" / thread_id / (".upload-" + attachment_id)
        with self._lock:
            ledger = self._read(thread_id)
            attachment = self._find_attachment(ledger, attachment_id)
            if key in self._uploads or attachment["state"] != "reserved":
                raise ChatError("upload_conflict", "附件已上传或正在上传", 409)
            self._uploads.add(key)
            attachment["state"] = "uploading"
            attachment["updated_at"] = time.time()
            try:
                self._save(ledger)
            except BaseException:
                self._uploads.discard(key)
                # A failed index write can leave a persisted upload claim to release.
                try:
                    persisted = self._ledger(thread_id)
                    claimed = persisted["attachments"].get(attachment_id)
                    if claimed and claimed["state"] == "uploading":
                        claimed["state"] = "reserved"
                        self._atomic_json(self.root / "threads" / (thread_id + ".json"), persisted)
                except ChatError:
                    pass  # A disk outage still recovers on the next startup.
                raise
            expected_size = attachment["size"]
            generation = attachment["generation"]
            extension = attachment["extension"]
        try:
            self._safe(temporary)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            data = bytearray()
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                async for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise ChatError("invalid_upload", "上传块必须是字节")
                    if len(data) + len(chunk) > min(expected_size, MAX_UPLOAD_BYTES):
                        raise ChatError("upload_size", "上传大小与声明不符或超限", 413)
                    data.extend(chunk)
                    target.write(chunk)
                if len(data) != expected_size:
                    raise ChatError("upload_size", "上传大小与声明不符", 413)
                await asyncio.to_thread(_validate_content, bytes(data), extension)
                target.flush()
                os.fsync(target.fileno())
            with self._lock:
                ledger = self._read(thread_id)
                attachment = self._find_attachment(ledger, attachment_id)
                if (attachment["state"] != "uploading" or attachment["generation"] != generation
                        or ledger["context_generation"] != generation):
                    raise ChatError("upload_conflict", "上传已被重置或取消", 409)
                path = self._attachment_path(thread_id, attachment)
                self._safe(path)
                if path.exists():
                    self._corrupt()
                os.replace(temporary, path)
                self._fsync_directory(path.parent)
                attachment["state"] = "ready"
                attachment["updated_at"] = time.time()
                self._save(ledger)
                return _public_attachment(attachment)
        except OSError as exc:
            raise ChatError("storage_error", "附件存储失败", 503) from exc
        finally:
            with self._lock:
                self._uploads.discard(key)
                self._safe(temporary)
                temporary.unlink(missing_ok=True)
                if thread_id in self._index:
                    ledger = self._ledger(thread_id)
                    attachment = ledger["attachments"].get(attachment_id)
                    if attachment and attachment["state"] == "uploading":
                        attachment["state"] = "reserved"
                        attachment["updated_at"] = time.time()
                        self._save(ledger)

    def attachment(self, thread_id: str, attachment_id: str) -> tuple[Path, dict]:
        with self._lock:
            ledger = self._read(thread_id)
            attachment = self._find_attachment(ledger, attachment_id)
            if attachment["state"] != "ready" or not attachment["bound_turn_ids"]:
                raise ChatError("attachment_not_bound", "附件尚未发送", 409)
            self._check_files(ledger)
            return self._attachment_path(thread_id, attachment), _public_attachment(attachment)

    def delete_attachment(self, thread_id: str, attachment_id: str) -> None:
        with self._lock:
            ledger = self._read(thread_id)
            attachment = self._find_attachment(ledger, attachment_id)
            if attachment["bound_turn_ids"]:
                raise ChatError("attachment_bound", "已发送附件不能单独删除", 409)
            del ledger["attachments"][attachment_id]
            self._save(ledger)

    @staticmethod
    def _request(client_turn_id: str, text: str, attachment_ids: list[str], retry_of: str | None) -> tuple[str, str]:
        client_key = _client_id(client_turn_id)
        if not isinstance(text, str):
            raise ChatError("invalid_text", "文本必须是字符串")
        if len(text) > MAX_INPUT_CHARS:
            raise ChatError("input_too_long", "输入最多 32000 字", 413)
        _text(text)
        if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_TURN_FILES:
            raise ChatError("attachment_limit", "每轮最多 5 个附件")
        for attachment_id in attachment_ids:
            validate_id(attachment_id)
        if len(set(attachment_ids)) != len(attachment_ids):
            raise ChatError("duplicate_attachment", "同一轮不能重复引用附件")
        if not text.strip() and not attachment_ids and retry_of is None:
            raise ChatError("empty_input", "请输入文本或添加附件")
        if retry_of is not None:
            validate_id(retry_of)
        request_hash = hashlib.sha256(json.dumps([text, attachment_ids, retry_of], ensure_ascii=False).encode("utf-8")).hexdigest()
        return client_key, request_hash

    def _duplicate(self, ledger: dict, client_key: str, request_hash: str) -> dict | None:
        for turn in ledger["turns"]:
            if _client_id(turn["client_turn_id"]) == client_key:
                if turn["request_hash"] != request_hash:
                    raise ChatError("turn_conflict", "同一客户端轮次标识不能用于不同请求", 409)
                return self._turn_result(ledger, turn, True)
        if client_key in ledger["retired_client_turn_ids"]:
            raise ChatError("turn_expired", "该轮次已处理且历史已裁剪；不会再次执行", 409)
        return None

    def lookup_turn(self, thread_id: str, client_turn_id: str, text: str,
                    attachment_ids: list[str], retry_of: str | None = None) -> dict | None:
        """Check idempotency without reserving a new generation."""
        request = self._request(client_turn_id, text, attachment_ids, retry_of)
        with self._lock:
            return self._duplicate(self._read(thread_id), *request)

    def get_turn(self, thread_id: str, turn_id: str | None = None) -> dict | None:
        """Public replay projection; omit the ID to select the latest turn."""
        if turn_id is not None:
            validate_id(turn_id)
        with self._lock:
            ledger = self._read(thread_id)
            turn = next((t for t in reversed(ledger["turns"]) if turn_id is None or t["id"] == turn_id), None)
            if turn is None:
                if turn_id is not None:
                    raise ChatError("turn_not_found", "轮次不存在或已裁剪", 404)
                return None
            return self._turn_result(ledger, turn, True)

    def begin_turn(self, thread_id: str, client_turn_id: str, text: str,
                   attachment_ids: list[str], retry_of: str | None = None) -> dict:
        client_key, request_hash = self._request(client_turn_id, text, attachment_ids, retry_of)
        with self._lock:
            ledger = self._read(thread_id)
            duplicate = self._duplicate(ledger, client_key, request_hash)
            if duplicate is not None:
                return duplicate
            if any(turn["status"] == "running" for turn in ledger["turns"]):
                raise ChatError("turn_running", "此对话已有进行中的轮次", 409)
            if len(ledger["turns"]) + len(ledger["retired_client_turn_ids"]) >= MAX_TURN_IDS:
                raise ChatError("turn_limit", "对话生命周期轮次已达上限，请新建对话", 409)
            previous = ledger["turns"][-1] if ledger["turns"] else None
            if retry_of is not None:
                if (previous is None or previous["id"] != retry_of or previous["status"] not in _RETRYABLE
                        or previous["generation"] != ledger["context_generation"]):
                    raise ChatError("invalid_retry", "只能重试当前上下文中最新的失败轮次", 409)
                user = next(m for m in ledger["messages"] if m["id"] == previous["user_message_id"])
                original_ids = [a["id"] for a in user["attachments"]]
                if text == "" and attachment_ids == []:
                    # A retry-only browser request inherits the failed input.
                    text, attachment_ids = user["text"], original_ids
                elif text != user["text"] or attachment_ids != original_ids:
                    raise ChatError("invalid_retry", "重试必须保留原输入及附件", 409)
            attachments = []
            for attachment_id in attachment_ids:
                attachment = self._find_attachment(ledger, attachment_id)
                if attachment["state"] != "ready" or attachment["generation"] != ledger["context_generation"]:
                    raise ChatError("attachment_not_ready", "附件未就绪或属于旧上下文", 409)
                if attachment["bound_turn_ids"] and (retry_of is None or retry_of not in attachment["bound_turn_ids"]):
                    raise ChatError("attachment_bound", "附件已用于其他轮次", 409)
                attachments.append(attachment)
            if sum(a["size"] for a in attachments) > MAX_TURN_UPLOAD_BYTES:
                raise ChatError("attachment_size", "每轮附件总大小最多 25 MiB", 413)
            self._check_files(ledger)
            # Commands can inspect references, but must never drain uploads.
            if text.lstrip().startswith("/"):
                attachments = []
            now = time.time()
            turn_id, user_id, assistant_id = (uuid.uuid4().hex for _ in range(3))
            user = {"id": user_id, "role": "user", "text": text, "status": "completed", "turn_id": turn_id,
                    "created_at": now, "updated_at": now, "attachments": [_public_attachment(a) for a in attachments]}
            assistant = {"id": assistant_id, "role": "assistant", "text": "", "status": "running",
                         "turn_id": turn_id, "created_at": now, "updated_at": now, "attachments": []}
            turn = {"id": turn_id, "client_turn_id": client_turn_id, "user_message_id": user_id,
                    "assistant_message_id": assistant_id, "status": "running", "generation": ledger["context_generation"],
                    "retry_of": retry_of, "created_at": now, "updated_at": now, "request_hash": request_hash}
            for attachment in attachments:
                attachment["bound_turn_ids"].append(turn_id)
                attachment["updated_at"] = now
            ledger["messages"].extend((user, assistant))
            ledger["turns"].append(turn)
            ledger["updated_at"] = now
            self._auto_title(ledger)
            self._prune(ledger)
            self._save(ledger)
            return self._turn_result(ledger, turn, False)

    @staticmethod
    def _turn_result(ledger: dict, turn: dict, duplicate: bool) -> dict:
        messages = {message["id"]: message for message in ledger["messages"]}
        return {"turn": _public_turn(turn), "user": _public_message(messages[turn["user_message_id"]]),
                "assistant": _public_message(messages[turn["assistant_message_id"]]), "duplicate": duplicate,
                "seq": turn.get("seq", 0)}

    @staticmethod
    def _finish_record(ledger: dict, turn: dict, text: str, status: str) -> dict:
        data = _text(text)
        truncated = len(data) > MAX_OUTPUT_BYTES
        if truncated:
            text = data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        assistant = next(m for m in ledger["messages"] if m["id"] == turn["assistant_message_id"])
        now = time.time()
        assistant.update(text=text, status=status, updated_at=now)
        for tool in assistant.get("tool_calls", []):
            if tool["status"] == "running":
                tool["status"] = "cancelled" if status == "cancelled" else "interrupted"
                tool["duration_seconds"] = min(86400.0, max(0.0, now - tool["started_at"]))
        if truncated:
            assistant["truncated"] = True
        turn.update(status=status, updated_at=now)
        ledger["updated_at"] = now
        return assistant

    def finish_turn(self, thread_id: str, turn_id: str, text: str, status: str,
                    tool_calls: list[dict] | None = None, tools_truncated: bool = False, *,
                    notification: dict | None = None, seq: int = 0) -> dict:
        validate_id(turn_id)
        _text(text)
        if type(seq) is not int or seq < 0:
            raise ChatError("invalid_seq", "轮次序号无效")
        notification = _notification(notification, thread_id) if notification is not None else None
        tools = copy.deepcopy(tool_calls) if tool_calls is not None else []
        _validate_tools(tools)
        if type(tools_truncated) is not bool:
            raise ChatError("invalid_tools", "工具活动截断状态无效")
        if not isinstance(status, str) or status not in _TERMINAL:
            raise ChatError("invalid_status", "轮次结束状态无效")
        with self._lock:
            ledger = self._read(thread_id)
            turn = next((t for t in ledger["turns"] if t["id"] == turn_id), None)
            if turn is None:
                raise ChatError("turn_not_found", "轮次不存在或已裁剪", 404)
            if turn["status"] != "running":
                # Late callbacks must not overwrite a terminal answer or reset generation.
                return self._turn_result(ledger, turn, True)["assistant"]
            if notification is not None:
                user = next(m for m in ledger["messages"] if m["id"] == turn["user_message_id"])
                if (notification["kind"] != "reply" or notification["event_id"] != turn_id
                        or notification["message_id"] != turn["assistant_message_id"]
                        or status not in {"complete", "completed"} or user["text"].lstrip().startswith("/")):
                    raise ChatError("invalid_notification", "仅正常聊天回复可以创建回复通知")
                self._enqueue_notification(ledger, notification)
            assistant = self._finish_record(ledger, turn, text, status)
            turn["seq"] = seq
            for tool in tools:
                if tool["status"] == "running":
                    tool["status"] = "cancelled" if status == "cancelled" else "interrupted"
                    tool["duration_seconds"] = min(86400.0, max(0.0, time.time() - tool["started_at"]))
            if tools or tools_truncated:
                assistant["tool_calls"] = tools
                assistant["tools_truncated"] = tools_truncated
            self._prune(ledger)
            self._save(ledger)
            return _public_message(assistant)

    @staticmethod
    def _enqueue_notification(ledger: dict, notification: dict) -> None:
        ledger["pending_notifications"].append(notification)
        # A notification is a hint to fetch history, not a second copy of the answer.
        del ledger["pending_notifications"][:-MAX_PENDING_NOTIFICATIONS]

    def pending_notifications(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy([item for thread_id in self._index
                                  for item in self._read(thread_id)["pending_notifications"]])

    def ack_notification(self, thread_id: str, event_id: str) -> bool:
        validate_id(thread_id)
        _event_id(event_id)
        with self._lock:
            if thread_id not in self._index:
                return False
            ledger = self._read(thread_id)
            pending = ledger["pending_notifications"]
            remaining = [item for item in pending if item["event_id"] != event_id]
            if len(remaining) == len(pending):
                return False
            ledger["pending_notifications"] = remaining
            self._save(ledger)
            return True

    def add_scheduled_message(self, thread_id: str, event_id: str, text: str,
                              kind: str = "reminder", notify: bool = True) -> dict:
        """Append standalone display history, never a user turn or native pi input."""
        _event_id(event_id)
        data = _text(text)
        if not text.strip() or len(data) > MAX_OUTPUT_BYTES:
            raise ChatError("invalid_text", "定时消息须为非空文本且不超过回复上限")
        if not isinstance(kind, str) or kind not in {"reminder", "daily"} or type(notify) is not bool:
            raise ChatError("invalid_notification", "定时消息类型无效")
        with self._lock:
            ledger = self._read(thread_id)
            scheduled = [m for m in ledger["messages"] if "event_id" in m]
            existing = next((m for m in scheduled if m["event_id"] == event_id), None)
            if existing is not None:
                return _public_message(existing)
            if event_id in ledger["retired_scheduled_event_ids"]:
                raise ChatError("event_expired", "定时消息已处理且历史已裁剪；不会重复写入", 409)
            if any(t["id"] == event_id for t in ledger["turns"]):
                raise ChatError("event_conflict", "事件标识已被聊天轮次使用", 409)
            if len(scheduled) + len(ledger["retired_scheduled_event_ids"]) >= MAX_SCHEDULED_IDS:
                raise ChatError("event_limit", "对话生命周期定时消息已达上限", 409)
            now = time.time()
            message = {"id": uuid.uuid4().hex, "role": "assistant", "text": text, "status": "complete",
                       "turn_id": None, "created_at": now, "updated_at": now, "attachments": [],
                       "generation": ledger["context_generation"], "kind": kind, "event_id": event_id}
            ledger["messages"].append(message)
            ledger["updated_at"] = now
            if notify:
                self._enqueue_notification(ledger, {"event_id": event_id, "thread_id": thread_id,
                                                    "message_id": message["id"], "kind": kind})
            self._prune(ledger)
            self._save(ledger)
            return _public_message(message)

    def _prune(self, ledger: dict) -> None:
        def over_limit() -> bool:
            return (len(ledger["messages"]) > MAX_MESSAGES
                    or sum(_message_bytes(m) for m in ledger["messages"]) > MAX_TEXT_BYTES)
        while over_limit():
            removable = {t["id"]: t for t in ledger["turns"][:-1] if t["status"] != "running"}
            first = next((m for m in ledger["messages"][:-1]
                          if m["turn_id"] is None or m["turn_id"] in removable), None)
            if first is None:
                raise ChatError("history_limit", "当前轮次无法放入历史上限", 409)
            if first["turn_id"] is None:
                ledger["messages"].remove(first)
                if "event_id" in first:
                    ledger["retired_scheduled_event_ids"].append(first["event_id"])
            else:
                turn = removable[first["turn_id"]]
                ledger["messages"] = [m for m in ledger["messages"] if m["turn_id"] != turn["id"]]
                ledger["turns"].remove(turn)
                ledger["retired_client_turn_ids"].append(_client_id(turn["client_turn_id"]))
                for key, attachment in list(ledger["attachments"].items()):
                    if turn["id"] in attachment["bound_turn_ids"]:
                        attachment["bound_turn_ids"].remove(turn["id"])
                        if not attachment["bound_turn_ids"]:
                            del ledger["attachments"][key]
            ledger["history_truncated"] = True
