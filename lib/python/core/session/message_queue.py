from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from observability.telemetry import emit

logger = logging.getLogger(__name__)


def _channel(session_key: str) -> str:
    return "wechat" if session_key.startswith("wechat:") else "feishu"


@dataclass(slots=True)
class QueueStatus:
    accepted: bool
    position: int = 0  # 0 = 立即执行；>0 = 排队序号（前面还有 position 条）


@dataclass(slots=True)
class _SessionQueue:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None
    running: bool = False


class SessionMessageQueue:
    """Per-session FIFO：同会话消息排队串行处理，跨会话并行。"""

    def __init__(self, max_pending: int = 10) -> None:
        self._max_pending = max_pending
        self._sessions: dict[str, _SessionQueue] = {}
        self._lock = asyncio.Lock()

    async def submit(
        self,
        session_key: str,
        job: Callable[[], Awaitable[None]],
        on_drop: Callable[[], None] | None = None,
    ) -> QueueStatus:
        """入队一条消息处理任务。返回是否接受与排队位置。"""
        async with self._lock:
            state = self._sessions.get(session_key)
            if state is None:
                state = _SessionQueue()
                self._sessions[session_key] = state

            pending = state.queue.qsize()
            if pending >= self._max_pending:
                emit("queue", channel=_channel(session_key), status="rejected", count=1)
                return QueueStatus(accepted=False)

            position = pending + (1 if state.running else 0)
            state.queue.put_nowait((job, on_drop, time.monotonic()))
            emit("queue", channel=_channel(session_key), status="accepted", count=1)
            if state.worker is None or state.worker.done():
                state.worker = asyncio.create_task(self._drain(session_key, state))
        return QueueStatus(accepted=True, position=position)

    async def clear(self, session_key: str) -> int:
        """清空指定会话的等待队列，返回丢弃条数（不影响正在执行的任务）。"""
        async with self._lock:
            state = self._sessions.get(session_key)
            if state is None:
                return 0
            dropped = 0
            while not state.queue.empty():
                try:
                    # Accept legacy two-item entries used by queue integrations.
                    _job, on_drop, *_timing = state.queue.get_nowait()
                    state.queue.task_done()
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
                if on_drop is not None:
                    try:
                        on_drop()
                    except Exception:
                        logger.exception("queue on_drop callback failed", extra={"event": "queue.drop_error"})
            if dropped:
                emit("queue", channel=_channel(session_key), status="dropped", count=dropped)
            return dropped

    def pending_count(self, session_key: str) -> int:
        state = self._sessions.get(session_key)
        if state is None:
            return 0
        return state.queue.qsize() + (1 if state.running else 0)

    async def close(self) -> None:
        async with self._lock:
            workers = [state.worker for state in self._sessions.values() if state.worker is not None]
            self._sessions.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def _drain(self, session_key: str, state: _SessionQueue) -> None:
        while True:
            try:
                job, _on_drop, *timing = state.queue.get_nowait()
            except asyncio.QueueEmpty:
                async with self._lock:
                    if state.queue.empty():
                        state.worker = None
                        return
                continue

            if timing:
                emit("stage", channel=_channel(session_key), stage="queue_wait",
                     status="success", duration_seconds=max(0.0, time.monotonic() - timing[0]))
            state.running = True
            try:
                await job()
            except asyncio.CancelledError:
                state.running = False
                state.queue.task_done()
                raise
            except Exception:
                logger.exception(
                    "queued message job failed",
                    extra={"event": "queue.job_error"},
                )
            state.running = False
            state.queue.task_done()
