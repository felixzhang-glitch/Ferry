from __future__ import annotations

import threading
from pathlib import Path


FILE_NOTICE_HEADER = (
    "[文件通知] 用户此前发来过以下文件，已归档到本地。"
    "仅当本轮确实需要时才读取，不要主动展开内容："
)


def format_file_notice(file_name: str, file_path: str, size: int | None = None) -> str:
    name = (file_name or "").strip() or Path(file_path).name
    if size is None:
        return f"- {name} → {file_path}"
    return f"- {name} → {file_path} ({size} bytes)"


def apply_pending_notices(text: str, notices: list[str]) -> str:
    """Fold queued file notices into the next user turn; pi owns the history."""
    active = [notice for notice in notices if notice.strip()]
    if not active:
        return text
    return "\n".join([FILE_NOTICE_HEADER, *active, "", text])


class SessionManager:
    """Queue attachments by native session key, without duplicating pi history."""

    MAX_PENDING_FILES = 20

    def __init__(self) -> None:
        self._pending_files: dict[str, list[str]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def build_key(user_id: str, chat_id: str) -> str:
        return f"{user_id}:{chat_id}"

    def reset_session(self, key: str) -> None:
        with self._lock:
            self._pending_files.pop(key, None)

    def note_incoming_file(self, key: str, notice: str) -> None:
        if not notice.strip():
            return
        with self._lock:
            pending = self._pending_files.setdefault(key, [])
            pending.append(notice)
            if len(pending) > self.MAX_PENDING_FILES:
                del pending[: len(pending) - self.MAX_PENDING_FILES]

    def take_pending_files(self, key: str) -> list[str]:
        """Drain queued file notices so each one is injected exactly once."""
        with self._lock:
            return self._pending_files.pop(key, [])
