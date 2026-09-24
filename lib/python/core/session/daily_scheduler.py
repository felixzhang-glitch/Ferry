from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from observability.telemetry import emit, observe_run, set_status

logger = logging.getLogger(__name__)

# (channel, target_id, text, trace_id) -> None
DailyPushCallback = Callable[[str, str, str, str], Awaitable[None]]
# (prompt, session_key, trace_id) -> answer
DailyRunCallback = Callable[[str, str, str], Awaitable[str]]

CATCHUP_WINDOW_SECONDS = 6 * 3600


@dataclass(slots=True)
class DailyTask:
    task_id: str
    channel: str
    target_id: str
    prompt: str
    hour: int
    minute: int
    last_run_date: str = ""
    created_at: float = field(default_factory=time.time)


class DailyTaskScheduler:
    """Recurring daily briefing tasks: run the agent backend at HH:MM and push the answer."""

    def __init__(
        self,
        run_callback: DailyRunCallback,
        push_callback: DailyPushCallback,
        store_path: str = "",
    ) -> None:
        self._run_callback = run_callback
        self._push_callback = push_callback
        self._store_path = Path(store_path) if store_path else None
        self._tasks: dict[str, DailyTask] = {}
        self._loops: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        for task in self._load_tasks():
            async with self._lock:
                self._tasks[task.task_id] = task
                self._loops[task.task_id] = asyncio.create_task(self._run_loop(task))

    async def schedule(self, channel: str, target_id: str, prompt: str, hour: int, minute: int) -> DailyTask:
        task = DailyTask(
            task_id=uuid.uuid4().hex,
            channel=channel,
            target_id=target_id,
            prompt=prompt,
            hour=hour,
            minute=minute,
        )
        async with self._lock:
            self._tasks[task.task_id] = task
            self._loops[task.task_id] = asyncio.create_task(self._run_loop(task))
            await self._save_unlocked()
        return task

    def list_tasks(self) -> list[DailyTask]:
        return sorted(self._tasks.values(), key=lambda task: (task.hour, task.minute, task.created_at))

    async def cancel(self, task_id_prefix: str) -> DailyTask | None:
        prefix = task_id_prefix.strip()
        if not prefix:
            return None
        async with self._lock:
            matches = [task for task in self._tasks.values() if task.task_id.startswith(prefix)]
            if len(matches) != 1:
                return None
            task = matches[0]
            self._tasks.pop(task.task_id, None)
            loop_task = self._loops.pop(task.task_id, None)
            await self._save_unlocked()

        if loop_task is not None:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
        return task

    async def close(self) -> None:
        async with self._lock:
            loops = list(self._loops.values())
            self._loops.clear()
        for loop_task in loops:
            loop_task.cancel()
        if loops:
            await asyncio.gather(*loops, return_exceptions=True)

    async def _run_loop(self, task: DailyTask) -> None:
        try:
            if self._should_catch_up(task):
                await self._execute(task)
            while True:
                await asyncio.sleep(self._seconds_until_next_run(task))
                if task.last_run_date == self._today():
                    continue
                await self._execute(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "daily task loop crashed",
                extra={"trace_id": task.task_id, "event": "daily.loop_error"},
            )

    @observe_run(channel="scheduled")
    async def _execute(self, task: DailyTask) -> None:
        today = self._today()
        trace_id = f"daily-{task.task_id[:8]}-{today}"
        session_key = f"daily:{task.task_id}:{today}"

        answer = ""
        generation_started = time.monotonic()
        generation_status = "error"
        try:
            for attempt in range(2):
                try:
                    answer = (await self._run_callback(task.prompt, session_key, trace_id)).strip()
                    if answer:
                        generation_status = "success"
                        break
                except Exception:
                    logger.exception(
                        "daily task run failed",
                        extra={"trace_id": trace_id, "event": "daily.run_error"},
                    )
                if attempt == 0:
                    await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            generation_status = "cancelled"
            raise
        finally:
            emit("task", channel=task.channel, source="scheduled", operation="daily",
                 stage="task_generation", status=generation_status, count=1,
                 duration_seconds=time.monotonic() - generation_started)

        if not answer:
            set_status("error")
            answer = f"简报生成失败（任务 {task.task_id[:8]}），请检查服务日志。"

        delivery_started = time.monotonic()
        delivery_status = "error"
        try:
            await self._push_callback(task.channel, task.target_id, answer, trace_id)
            delivery_status = "success"
            # Delivering an error notice must not turn failed generation green.
            if generation_status == "success":
                set_status("success")
            logger.info(
                "daily task delivered",
                extra={"trace_id": trace_id, "event": "daily.delivered"},
            )
        except asyncio.CancelledError:
            delivery_status = "cancelled"
            raise
        except Exception:
            set_status("error")
            logger.exception(
                "daily task push failed",
                extra={"trace_id": trace_id, "event": "daily.push_error"},
            )
        finally:
            emit("task", channel=task.channel, source="scheduled", operation="daily",
                 stage="task_delivery", status=delivery_status, count=1,
                 duration_seconds=time.monotonic() - delivery_started)

        task.last_run_date = today
        async with self._lock:
            await self._save_unlocked()

    def _should_catch_up(self, task: DailyTask) -> bool:
        today = self._today()
        if task.last_run_date >= today:
            return False
        now = self._now()
        due = now.replace(hour=task.hour, minute=task.minute, second=0, microsecond=0)
        elapsed = (now - due).total_seconds()
        return 0 <= elapsed <= CATCHUP_WINDOW_SECONDS

    def _seconds_until_next_run(self, task: DailyTask) -> float:
        now = self._now()
        due = now.replace(hour=task.hour, minute=task.minute, second=0, microsecond=0)
        if due <= now or task.last_run_date == self._today():
            due += datetime.timedelta(days=1)
        return max(1.0, (due - now).total_seconds())

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now()

    def _today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _load_tasks(self) -> list[DailyTask]:
        if self._store_path is None or not self._store_path.exists():
            return []
        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("failed to load daily tasks", extra={"event": "daily.load"})
            return []
        if not isinstance(payload, list):
            return []

        tasks: list[DailyTask] = []
        for item in payload:
            if isinstance(item, dict):
                task = self._parse_task(item)
                if task is not None:
                    tasks.append(task)
        return tasks

    async def _save_unlocked(self) -> None:
        if self._store_path is None:
            return
        payload = [
            {
                "task_id": task.task_id,
                "channel": task.channel,
                "target_id": task.target_id,
                "prompt": task.prompt,
                "hour": task.hour,
                "minute": task.minute,
                "last_run_date": task.last_run_date,
                "created_at": task.created_at,
            }
            for task in self._tasks.values()
        ]
        await asyncio.to_thread(self._write_payload, payload)

    def _write_payload(self, payload: list[dict[str, Any]]) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._store_path.with_suffix(f"{self._store_path.suffix}.tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, self._store_path)
        except OSError:
            logger.exception("failed to save daily tasks", extra={"event": "daily.save"})

    @staticmethod
    def _parse_task(item: dict[str, Any]) -> DailyTask | None:
        task_id = str(item.get("task_id", "")).strip()
        channel = str(item.get("channel", "")).strip()
        target_id = str(item.get("target_id", "")).strip()
        prompt = str(item.get("prompt", "")).strip()
        if not task_id or channel not in {"feishu", "wechat"} or not target_id or not prompt:
            return None
        try:
            hour = int(item.get("hour"))
            minute = int(item.get("minute"))
            created_at = float(item.get("created_at", time.time()))
        except (TypeError, ValueError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return DailyTask(
            task_id=task_id,
            channel=channel,
            target_id=target_id,
            prompt=prompt,
            hour=hour,
            minute=minute,
            last_run_date=str(item.get("last_run_date", "")).strip(),
            created_at=created_at,
        )
