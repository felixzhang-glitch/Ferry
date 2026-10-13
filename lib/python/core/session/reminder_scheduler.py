from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from observability.telemetry import emit

logger = logging.getLogger(__name__)

ReminderCallback = Callable[[str, str, str], Awaitable[None]]

# A transient Feishu outage at fire time used to orphan the reminder for the
# whole process lifetime: the task was popped while the reminder stayed in the
# store, and only a restart (possibly weeks later) replayed it. Retry in-process.
MAX_DELIVERY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 60.0


@dataclass(slots=True)
class Reminder:
    reminder_id: str
    chat_id: str
    text: str
    due_at: float
    created_at: float = field(default_factory=time.time)


class ReminderScheduler:
    def __init__(
        self,
        callback: ReminderCallback,
        store_path: str = "",
        *,
        max_delivery_attempts: int = MAX_DELIVERY_ATTEMPTS,
        retry_delay_seconds: float = RETRY_DELAY_SECONDS,
    ) -> None:
        self._callback = callback
        self._max_delivery_attempts = max(1, int(max_delivery_attempts))
        self._retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._reminders: dict[str, Reminder] = {}
        self._lock = asyncio.Lock()
        self._store_path = Path(store_path) if store_path else None

    async def start(self) -> None:
        for reminder in self._load_reminders():
            async with self._lock:
                task = asyncio.create_task(self._run(reminder))
                self._reminders[reminder.reminder_id] = reminder
                self._tasks[reminder.reminder_id] = task

    async def schedule(self, chat_id: str, text: str, delay_seconds: float) -> Reminder:
        delay = max(0.0, delay_seconds)
        reminder = Reminder(
            reminder_id=uuid.uuid4().hex,
            chat_id=chat_id,
            text=text,
            due_at=time.time() + delay,
        )

        async with self._lock:
            task = asyncio.create_task(self._run(reminder))
            self._reminders[reminder.reminder_id] = reminder
            self._tasks[reminder.reminder_id] = task
            await self._save_reminders_unlocked()

        return reminder

    async def cancel(self, reminder_id: str) -> bool:
        async with self._lock:
            reminder = self._reminders.pop(reminder_id, None)
            task = self._tasks.pop(reminder_id, None)
            await self._save_reminders_unlocked()

        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        return reminder is not None

    async def close(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, reminder: Reminder) -> None:
        remove_on_exit = False
        trace_id = f"reminder-{reminder.reminder_id}"
        try:
            await asyncio.sleep(max(0.0, reminder.due_at - time.time()))
            for attempt in range(1, self._max_delivery_attempts + 1):
                delivery_started = time.monotonic()
                delivery_status = "error"
                try:
                    await self._callback(reminder.chat_id, reminder.text, trace_id)
                    delivery_status = "success"
                except asyncio.CancelledError:
                    delivery_status = "cancelled"
                    raise
                except Exception:
                    logger.exception(
                        "failed to deliver reminder",
                        extra={
                            "trace_id": trace_id,
                            "event": "reminder.error",
                            "attempt": attempt,
                            "max_attempts": self._max_delivery_attempts,
                        },
                    )
                finally:
                    emit("task", channel="feishu", source="scheduled", operation="reminder",
                         stage="task_delivery", status=delivery_status, count=1,
                         duration_seconds=time.monotonic() - delivery_started)

                if delivery_status == "success":
                    remove_on_exit = True
                    logger.info(
                        "reminder delivered",
                        extra={"trace_id": trace_id, "event": "reminder.delivered"},
                    )
                    return
                if attempt < self._max_delivery_attempts:
                    await asyncio.sleep(self._retry_delay_seconds)
            # All attempts failed: keep the reminder in the store so the next
            # startup replays it, and drop the dead task to avoid a leak.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "reminder task failed",
                extra={"trace_id": trace_id, "event": "reminder.error"},
            )
        finally:
            async with self._lock:
                self._tasks.pop(reminder.reminder_id, None)
                if remove_on_exit:
                    self._reminders.pop(reminder.reminder_id, None)
                await self._save_reminders_unlocked()

    def _load_reminders(self) -> list[Reminder]:
        if self._store_path is None or not self._store_path.exists():
            return []

        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("failed to load reminders", extra={"event": "reminder.load"})
            return []

        if not isinstance(payload, list):
            return []

        reminders: list[Reminder] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            reminder = self._parse_reminder(item)
            if reminder is not None:
                reminders.append(reminder)
        return reminders

    def _build_reminders_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "reminder_id": reminder.reminder_id,
                "chat_id": reminder.chat_id,
                "text": reminder.text,
                "due_at": reminder.due_at,
                "created_at": reminder.created_at,
            }
            for reminder in self._reminders.values()
        ]

    async def _save_reminders_unlocked(self) -> None:
        if self._store_path is None:
            return

        payload = self._build_reminders_payload()
        await asyncio.to_thread(self._write_reminders_payload, payload)

    def _write_reminders_payload(self, payload: list[dict[str, Any]]) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._store_path.with_suffix(f"{self._store_path.suffix}.tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, self._store_path)
        except OSError:
            logger.exception("failed to save reminders", extra={"event": "reminder.save"})

    @staticmethod
    def _parse_reminder(item: dict[str, Any]) -> Reminder | None:
        reminder_id = str(item.get("reminder_id", "")).strip()
        chat_id = str(item.get("chat_id", "")).strip()
        text = str(item.get("text", "")).strip()
        if not reminder_id or not chat_id or not text:
            return None

        try:
            due_at = float(item.get("due_at"))
            created_at = float(item.get("created_at", time.time()))
        except (TypeError, ValueError):
            return None

        return Reminder(
            reminder_id=reminder_id,
            chat_id=chat_id,
            text=text,
            due_at=due_at,
            created_at=created_at,
        )
