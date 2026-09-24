"""Bounded persistent pi SDK workers, presenting one turn as a CLI process.

Each lease exclusively owns one worker until ferry_done. Native pi events keep
flowing through PiCliClient's existing parser; only Node/module initialization
is shared, never an in-memory conversation. A cancelled lease kills its entire
worker process group, so tools cannot outlive a cancelled request.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path

from core.agent.types import AgentClientError
from observability.telemetry import emit

logger = logging.getLogger(__name__)
_WORKER_SCRIPT = Path(__file__).resolve().parents[3] / "js" / "pi-worker.mjs"


def kill_group(process: asyncio.subprocess.Process) -> None:
    # A crashed worker may have living tool children holding its pipes open.
    # Kill the group even when the group's original leader has already exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()


def _linux_process_identity(pid: int) -> tuple[str, int] | None:
    """Start time prevents PID reuse from turning cleanup into an unrelated kill."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19], int(fields[2])  # starttime, process group
    except (OSError, ValueError, IndexError):
        return None


class _DescendantGroups:
    """Linux safety net for detached tools when Node cannot handle SIGTERM.

    Track only this worker's descendants, not a global process-name scan. Live
    previously-seen children remain roots after reparenting on a worker crash.
    On other systems the SDK's cooperative SIGTERM cleanup is still used.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.leader = _linux_process_identity(pid)
        self.children: dict[int, tuple[str, int]] = {}

    def capture(self) -> None:
        self.children = {
            pid: now for pid, old in self.children.items()
            if (now := _linux_process_identity(pid)) is not None and now[0] == old[0]
        }
        roots = list(self.children)
        leader = _linux_process_identity(self.pid)
        if leader is not None and self.leader is not None and leader[0] == self.leader[0]:
            roots.append(self.pid)
        seen = set()
        while roots:
            pid = roots.pop()
            if pid in seen:
                continue
            seen.add(pid)
            # A native child can be spawned by any libuv thread, not just TID=PID.
            for task_children in Path(f"/proc/{pid}/task").glob("*/children"):
                try:
                    children = [int(value) for value in task_children.read_text().split()]
                except (OSError, ValueError):
                    continue
                for child in children:
                    identity = _linux_process_identity(child)
                    if identity is not None:
                        self.children[child] = identity
                        roots.append(child)

    def kill(self) -> None:
        self.capture()
        own_group = os.getpgrp()
        groups = set()
        for pid, old in list(self.children.items()):
            now = _linux_process_identity(pid)
            if now is None or now[0] != old[0]:
                continue
            group = now[1]
            if group not in (own_group, self.pid) and group not in groups:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(group, signal.SIGKILL)
                groups.add(group)
            # Also catch children that didn't start a separate process group.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


class _Worker:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.busy = False
        self.stopping = False
        self.turns = 0
        self.stderr: deque[bytes] = deque(maxlen=4)
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        self._close_task: asyncio.Task | None = None
        self._kill_timer: asyncio.TimerHandle | None = None
        self.descendants = _DescendantGroups(process.pid)
        self.tracker_task = asyncio.create_task(self._track_children())

    async def _track_children(self) -> None:
        while True:
            self.descendants.capture()
            await asyncio.sleep(0.2)

    def _force_kill(self) -> None:
        self.descendants.kill()
        kill_group(self.process)

    async def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        while data := await self.process.stderr.read(4096):
            self.stderr.append(data)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    def request_stop(self) -> None:
        self.stopping = True
        if self._kill_timer is not None:
            return
        self.descendants.capture()
        if self.process.returncode is None:
            # pi tracks detached bash groups. SIGTERM lets the worker kill them
            # before it exits; a wedged worker still has a bounded kill fallback.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
        self._kill_timer = asyncio.get_running_loop().call_later(1.0, self._force_kill)

    async def _close(self) -> None:
        self.request_stop()
        await self.process.wait()
        if self._kill_timer is not None:
            self._kill_timer.cancel()
        self._force_kill()
        self.tracker_task.cancel()
        await asyncio.gather(self.tracker_task, return_exceptions=True)
        try:
            await asyncio.wait_for(self.stderr_task, timeout=2.0)
        except asyncio.TimeoutError:
            # A detached extension child must not block Ferry shutdown forever.
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)


class _LeaseStderr:
    def __init__(self, lease: PiWorkerLease) -> None:
        self.lease = lease

    async def read(self) -> bytes:
        await self.lease.finished.wait()
        return b"".join(self.lease.worker.stderr)


class PiWorkerLease:
    """Process-compatible view; EOF denotes settled turn, not worker exit."""

    def __init__(self, pool: PiWorkerPool, worker: _Worker, request_id: str) -> None:
        self.pool = pool
        self.worker = worker
        self.request_id = request_id
        self.pid = worker.process.pid
        self.returncode: int | None = None
        self.trace_id = ""
        self.stdout = self
        self.stderr = _LeaseStderr(self)
        self.finished = asyncio.Event()
        self.released = False
        self.started = time.monotonic()
        self.first_text = False
        self.loop = asyncio.get_running_loop()
        self.session_key: str | None = None

    async def readline(self) -> bytes:
        if self.returncode is not None:
            return b""
        assert self.worker.process.stdout is not None
        while True:
            line = await self.worker.process.stdout.readline()
            if not line:
                # Even exit(0) is a failure without the per-turn completion marker.
                self.returncode = 1
                self.finished.set()
                raise AgentClientError("pi worker exited before ferry_done")
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise AgentClientError("pi worker emitted invalid JSON") from exc
            if not isinstance(event, dict) or event.get("requestId") != self.request_id:
                raise AgentClientError("pi worker response request ID mismatch")
            kind = event.get("type")
            if kind == "ferry_done":
                code = event.get("returncode")
                if type(code) is not int or code not in (0, 1):
                    raise AgentClientError("pi worker invalid completion marker")
                self.returncode = code
                self.finished.set()
                return b""
            if kind == "ferry_turn_ready":
                setup_ms = event.get("setup_ms")
                if type(setup_ms) in (int, float) and setup_ms >= 0:
                    emit("stage", stage="worker_setup", status="success", duration_seconds=setup_ms / 1000)
                logger.info(
                    "pi worker turn ready",
                    extra={"event": "pi.worker_turn_ready", "trace_id": self.trace_id,
                           "worker_pid": self.pid, "worker_reused": self.worker.turns > 0,
                           "duration_ms": round((time.monotonic() - self.started) * 1000),
                           "setup_ms": event.get("setup_ms")},
                )
                continue
            if (not self.first_text and kind == "message_update"
                    and event.get("assistantMessageEvent", {}).get("type") == "text_delta"):
                self.first_text = True
                logger.info(
                    "pi worker first text",
                    extra={"event": "pi.worker_first_text", "trace_id": self.trace_id,
                           "worker_pid": self.pid,
                           "duration_ms": round((time.monotonic() - self.started) * 1000)},
                )
            return line

    async def wait(self) -> int:
        await self.finished.wait()
        return self.returncode if self.returncode is not None else 1

    def kill(self) -> None:
        self.worker.stopping = True
        self.loop.call_soon_threadsafe(self.worker.request_stop)

    async def close(self) -> None:
        if self.released:
            return
        self.released = True
        self.finished.set()
        try:
            await self.pool.release(self.worker, reusable=self.returncode == 0, completed=True)
        finally:
            if self.session_key is not None:
                self.pool._release_session(self.session_key, acquired=True)


class PiWorkerPool:
    def __init__(self, *, node_bin: str, sdk_module: str, size: int, startup_timeout: float,
                 cwd: str, env: dict[str, str], read_limit: int) -> None:
        self.command = [node_bin, str(_WORKER_SCRIPT), sdk_module]
        self.size = size
        self.startup_timeout = startup_timeout
        self.cwd = cwd
        self.env = env
        self.read_limit = read_limit
        self.workers: list[_Worker] = []
        self.starting = 0
        self.closed = False
        self.condition = asyncio.Condition()
        self._session_users: dict[str, tuple[asyncio.Lock, int]] = {}

    async def _spawn(self) -> _Worker:
        start = time.monotonic()
        worker = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command, cwd=self.cwd, env=self.env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=self.read_limit, start_new_session=True,
            )
            worker = _Worker(process)
            assert process.stdout is not None
            line = await asyncio.wait_for(process.stdout.readline(), self.startup_timeout)
            ready = json.loads(line)
            if (not isinstance(ready, dict) or ready.get("type") != "ferry_ready"
                    or ready.get("protocol") != 1 or ready.get("pid") != process.pid):
                raise ValueError("invalid readiness handshake")
            logger.info("pi worker ready", extra={
                "event": "pi.worker_ready", "worker_pid": process.pid,
                "duration_ms": round((time.monotonic() - start) * 1000),
                "pi_version": ready.get("version"),
            })
            return worker
        except BaseException as exc:
            if worker is not None:
                await worker.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            # Do not echo stderr: third-party extensions may print credentials.
            raise AgentClientError(
                "pi worker startup failed; check PI_NODE_BIN / PI_SDK_MODULE and pi version"
            ) from exc

    async def _acquire(self) -> _Worker:
        while True:
            dead = []
            async with self.condition:
                if self.closed:
                    raise AgentClientError("pi worker pool is closed")
                for worker in list(self.workers):
                    if not worker.busy and worker.process.returncode is not None:
                        self.workers.remove(worker)
                        dead.append(worker)
                idle = next((w for w in self.workers if not w.busy), None)
                if idle is not None:
                    idle.busy = True
                elif len(self.workers) + self.starting < self.size:
                    self.starting += 1
                else:
                    await self.condition.wait()
                    continue
            worker = None
            try:
                for stale in dead:
                    await stale.close()
                if idle is not None:
                    return idle
                worker = await self._spawn()
                async with self.condition:
                    if self.closed:
                        raise AgentClientError("pi worker pool is closed")
                    worker.busy = True
                    self.workers.append(worker)
                return worker
            except BaseException:
                if worker is not None:
                    await worker.close()
                if idle is not None:
                    await self.release(idle, reusable=True)
                raise
            finally:
                async with self.condition:
                    if idle is None:
                        self.starting -= 1
                    self.condition.notify_all()

    async def warmup(self) -> None:
        # Hold the leases until all workers are ready; otherwise each acquisition
        # could select the same idle worker and never fill the pool.
        held = []
        try:
            for _ in range(self.size):
                held.append(await self._acquire())
        except BaseException:
            await self.close()
            raise
        finally:
            for worker in held:
                await self.release(worker, reusable=True)

    def _release_session(self, key: str, *, acquired: bool) -> None:
        lock, count = self._session_users[key]
        if acquired:
            lock.release()
        if count == 1:
            del self._session_users[key]
        else:
            self._session_users[key] = (lock, count - 1)

    async def run(self, args: list[str], *, prepare: Callable[[], list[str]] | None = None) -> PiWorkerLease:
        # Channel queues normally serialize turns already. Enforce native file
        # exclusivity here too, for direct callers and scheduled jobs.
        key = args[args.index("--session-id") + 1] if "--session-id" in args else None
        acquired = False
        if key is not None:
            lock, count = self._session_users.get(key, (asyncio.Lock(), 0))
            self._session_users[key] = (lock, count + 1)
        try:
            if key is not None:
                await lock.acquire()
                acquired = True
            lease = await self._run(args, prepare)
            lease.session_key = key
            return lease
        except BaseException:
            if key is not None:
                self._release_session(key, acquired=acquired)
            raise

    async def _run(self, args: list[str], prepare: Callable[[], list[str]] | None = None) -> PiWorkerLease:
        wait_started = time.monotonic()
        worker = await self._acquire()
        emit("stage", stage="worker_wait", status="success", duration_seconds=time.monotonic() - wait_started)
        worker.stderr.clear()
        lease = PiWorkerLease(self, worker, uuid.uuid4().hex)
        try:
            if prepare is not None:
                args = prepare()
            assert worker.process.stdin is not None
            worker.process.stdin.write((json.dumps({
                "type": "run", "id": lease.request_id, "args": args,
            }, ensure_ascii=False) + "\n").encode())
            await asyncio.wait_for(worker.process.stdin.drain(), self.startup_timeout)
            return lease
        except BaseException as exc:
            await lease.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AgentClientError("pi worker request write failed") from exc

    async def release(self, worker: _Worker, *, reusable: bool, completed: bool = False) -> None:
        async with self.condition:
            keep = reusable and not self.closed and not worker.stopping and worker.process.returncode is None
            if not keep:
                if worker in self.workers:
                    self.workers.remove(worker)
            elif completed:
                worker.turns += 1
            worker.busy = False
            self.condition.notify_all()
        if not keep:
            await worker.close()

    async def close(self) -> None:
        async with self.condition:
            self.closed = True
            workers, self.workers = self.workers, []
            self.condition.notify_all()
        await asyncio.gather(*(w.close() for w in workers))
