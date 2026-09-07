from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Round:
    user: str
    assistant: str
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class SessionState:
    session_id: str
    rounds: list[Round] = field(default_factory=list)
    pending_files: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


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
    """Fold queued file notices into the next user message.

    The pi backend only forwards the last user message (its session holds the
    rest), so a notice recorded in `rounds` would never reach it. Riding the
    next user turn is what makes the file visible to every backend.
    """
    active = [notice for notice in notices if notice.strip()]
    if not active:
        return text
    return "\n".join([FILE_NOTICE_HEADER, *active, "", text])


class SessionManager:
    def __init__(self, max_history_rounds: int = 10) -> None:
        self._max_history_rounds = max(1, max_history_rounds)
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.RLock()

    @staticmethod
    def build_key(user_id: str, chat_id: str) -> str:
        return f"{user_id}:{chat_id}"

    def get_or_create(self, key: str) -> SessionState:
        with self._lock:
            if key not in self._sessions:
                self._sessions[key] = SessionState(session_id=self._new_session_id())
            return self._sessions[key]

    def new_session(self, key: str) -> str:
        with self._lock:
            self._sessions[key] = SessionState(session_id=self._new_session_id())
            return self._sessions[key].session_id

    def reset_session(self, key: str) -> str:
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = SessionState(session_id=self._new_session_id())
                self._sessions[key] = session
            session.rounds.clear()
            session.pending_files.clear()
            session.updated_at = time.time()
            return session.session_id

    def compact_session(
        self,
        key: str,
        keep_last_rounds: int = 2,
        max_summary_chars: int = 2400,
    ) -> tuple[int, int]:
        with self._lock:
            session = self.get_or_create(key)
            before = len(session.rounds)
            keep = max(0, keep_last_rounds)
            if before <= keep + 1:
                return before, before

            old_rounds = session.rounds[: before - keep]
            recent_rounds = session.rounds[before - keep :] if keep else []
            summary = self._build_compact_summary(old_rounds, max_summary_chars=max_summary_chars)
            session.rounds = [
                Round(
                    user="[历史上下文压缩]",
                    assistant=summary,
                )
            ] + recent_rounds
            session.updated_at = time.time()
            return before, len(session.rounds)

    # Bounds the queued notices so a burst of files with no follow-up message
    # cannot inflate every later prompt.
    MAX_PENDING_FILES = 20

    def note_incoming_file(self, key: str, notice: str) -> None:
        if not notice.strip():
            return
        with self._lock:
            session = self.get_or_create(key)
            session.pending_files.append(notice)
            if len(session.pending_files) > self.MAX_PENDING_FILES:
                del session.pending_files[: len(session.pending_files) - self.MAX_PENDING_FILES]
            session.updated_at = time.time()

    def take_pending_files(self, key: str) -> list[str]:
        """Drain queued file notices so each one is injected exactly once."""
        with self._lock:
            session = self._sessions.get(key)
            if session is None or not session.pending_files:
                return []
            notices = list(session.pending_files)
            session.pending_files.clear()
            session.updated_at = time.time()
            return notices

    def append_round(self, key: str, user: str, assistant: str) -> None:
        with self._lock:
            session = self.get_or_create(key)
            session.rounds.append(Round(user=user, assistant=assistant))
            session.updated_at = time.time()
            if len(session.rounds) > self._max_history_rounds:
                over = len(session.rounds) - self._max_history_rounds
                del session.rounds[:over]

    def build_messages(self, key: str) -> list[dict[str, str]]:
        with self._lock:
            session = self.get_or_create(key)
            messages: list[dict[str, str]] = []
            for turn in session.rounds:
                messages.append({"role": "user", "content": turn.user})
                messages.append({"role": "assistant", "content": turn.assistant})
            return messages

    def round_count(self, key: str) -> int:
        with self._lock:
            session = self.get_or_create(key)
            return len(session.rounds)

    @staticmethod
    def _new_session_id() -> str:
        return uuid.uuid4().hex

    @classmethod
    def _build_compact_summary(cls, rounds: list[Round], max_summary_chars: int) -> str:
        lines = ["以下为已压缩的历史上下文摘要，用于延续当前会话。"]
        for idx, turn in enumerate(rounds, start=1):
            lines.append(
                f"{idx}. 用户: {cls._truncate(turn.user, 180)}\n"
                f"   助手: {cls._truncate(turn.assistant, 360)}"
            )

        summary = "\n".join(lines).strip()
        return cls._truncate(summary, max(200, max_summary_chars))

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        compact = " ".join(text.strip().split())
        if len(compact) <= limit:
            return compact
        return f"{compact[: limit - 3]}..."
