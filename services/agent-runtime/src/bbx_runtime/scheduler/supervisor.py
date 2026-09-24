"""Queue tasks, recover owned environments, and durably archive terminal work."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from bbx_contracts.models import Params

from bbx_runtime.clients import BlackboardClient, RemoteError
from bbx_runtime.execenv import ExecEnvHandle, ExecEnvManager
from bbx_runtime.models import load_runtime_profile
from bbx_runtime.runner import AgentRunner
from bbx_runtime.scheduler.executor import ActionExecutor
from bbx_runtime.scheduler.loop import SchedulerLoop
from bbx_runtime.settings import Settings

LOGGER = logging.getLogger(__name__)
TERMINAL = {"finished", "failed", "stopped"}
ACTIVE = {"running", "concluding"}


class TaskSupervisor:
    def __init__(
        self,
        settings: Settings,
        service: BlackboardClient,
        manager: ExecEnvManager,
        runner: AgentRunner,
    ) -> None:
        self.settings = settings
        self.service = service
        self.manager = manager
        self.runner = runner
        self.loops: dict[str, SchedulerLoop] = {}
        self.loop_tasks: dict[str, asyncio.Task[None]] = {}
        self.cleanups: dict[str, asyncio.Task[None]] = {}
        self.cleaned: set[str] = set()
        self.stopping = asyncio.Event()
        self.tick_lock = asyncio.Lock()

    async def _fail_if_active(self, tid: str, reason: str) -> None:
        if (await self.service.state(tid))["task"]["status"] in TERMINAL:
            return
        try:
            await self.service.transition(tid, "failed", reason)
        except RemoteError:
            if (await self.service.state(tid))["task"]["status"] not in TERMINAL:
                raise

    async def _start(self, state: dict[str, Any], handle: ExecEnvHandle) -> None:
        tid = str(state["task"]["id"])
        if tid in self.loops or self.stopping.is_set():
            return
        executor = ActionExecutor(tid, self.service, self.manager, self.runner, handle)
        loop = SchedulerLoop(executor, self.service, Params.model_validate(state["task"]["params"]))
        self.loops[tid] = loop
        self.loop_tasks[tid] = asyncio.create_task(loop.run(), name=f"scheduler:{tid}")

    async def recover(self) -> None:
        for listed in await self.service.list_tasks():
            tid = str(listed["id"])
            state = await self.service.state(tid)
            # No previous Python coroutine can survive the exclusive runtime lock handoff.
            for aid, agent in state["agents"].items():
                if agent["status"] in ACTIVE:
                    await self.service.finish_agent(
                        tid, aid, {"accepted": False, "reason": "运行器重启"}, "runtime_restart"
                    )
            state = await self.service.state(tid)
            status = state["task"]["status"]
            if status not in {"provisioning", "running", "closing"}:
                continue
            try:
                handle = await self.manager.find(tid)
                if handle is None:
                    if status == "provisioning":
                        continue
                    raise RuntimeError("Execution environment is missing")
                await self.manager.wait_healthy(handle)
                if status == "provisioning":
                    # Rejoin the queue: tick applies the capacity limit before promoting it.
                    continue
                await self._start(state, handle)
            except Exception as error:
                LOGGER.error("Recovery failed for task %s: %s", tid, type(error).__name__)
                await self._fail_if_active(tid, "执行环境丢失或无法恢复")

    async def _drain(self, tid: str, state: dict[str, Any]) -> None:
        params = Params.model_validate(state["task"]["params"])
        for aid, agent in state["agents"].items():
            if agent["status"] == "running":
                try:
                    await self.service.conclude(tid, aid, "failed")
                except RemoteError as error:
                    if error.code != "agent_inactive":
                        raise
        while True:
            state = await self.service.state(tid)
            active = {aid: a for aid, a in state["agents"].items() if a["status"] in ACTIVE}
            if not active:
                break
            loop = self.loops.get(tid)
            now = datetime.now(UTC)
            for aid, agent in active.items():
                stamp = agent.get("conclude_requested_at")
                requested = (
                    datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) if stamp else now
                )
                expired = (now - requested).total_seconds() >= params.grace_timeout * 60
                task = loop.executor.tasks.get(aid) if loop is not None else None
                if task is None or task.done():
                    await self.service.finish_agent(
                        tid,
                        aid,
                        {"accepted": False, "reason": "运行协程已不存在"},
                        "runtime_restart",
                    )
                elif expired:
                    await loop.executor.cancel(aid, "grace_timeout")  # type: ignore[union-attr]
            await asyncio.sleep(0.2)
        if loop := self.loops.pop(tid, None):
            await loop.stop(reason="grace_timeout")
            await self.loop_tasks.pop(tid)

    async def _cleanup(self, tid: str) -> None:
        state = await self.service.state(tid)
        await self._drain(tid, state)
        state = await self.service.state(tid)
        handle = await self.manager.find(tid)
        if handle is not None:
            if not state["task"].get("workspace_uri"):
                archive = await self.manager.archive_to_store(handle)
                await self.service.record_archive(tid, archive.uri, archive.size, archive.fallback)
            await self.manager.destroy(tid)
        elif state["task"].get("started_at") and not state["task"].get("workspace_uri"):
            LOGGER.error(
                "Task %s execution environment is missing; workspace cannot be archived. "
                "Previously persisted evidence is retained.",
                tid,
            )
        self.cleaned.add(tid)

    async def tick(self) -> None:
        async with self.tick_lock:
            if not self.stopping.is_set():
                await self._tick()

    async def _tick(self) -> None:
        tasks = await self.service.list_tasks()
        for tid, future in list(self.cleanups.items()):
            if future.done():
                try:
                    future.result()
                except Exception as error:
                    LOGGER.error("Cleanup failed for task %s: %s", tid, type(error).__name__)
                del self.cleanups[tid]
        for task in tasks:
            tid = str(task["id"])
            if task["status"] in TERMINAL and tid not in self.cleaned and tid not in self.cleanups:
                self.cleanups[tid] = asyncio.create_task(self._cleanup(tid), name=f"cleanup:{tid}")
        occupied = {str(t["id"]) for t in tasks if t["status"] in {"running", "closing"}}
        occupied.update(self.cleanups)
        occupied.update(self.loops)
        queue = sorted(
            (t for t in tasks if t["status"] == "provisioning"), key=lambda t: str(t["created_at"])
        )
        for task in queue:
            if self.stopping.is_set() or len(occupied) >= self.settings.max_running_tasks:
                break
            tid = str(task["id"])
            try:
                profile = load_runtime_profile(
                    await self.service.get_profile(
                        task["agent_profile"], task["agent_profile_version"]
                    )
                )
                handle = await self.manager.provision(tid, profile)
                occupied.add(tid)
                fresh = await self.service.state(tid)
                if fresh["task"]["status"] != "provisioning":
                    continue
                await self.service.transition(tid, "running")
                await self._start(await self.service.state(tid), handle)
            except Exception as error:
                LOGGER.error("Provisioning failed for task %s: %s", tid, type(error).__name__)
                await self._fail_if_active(tid, "执行环境创建失败")
        for tid, future in list(self.loop_tasks.items()):
            if future.done() and tid not in self.cleanups:
                try:
                    future.result()
                except Exception as error:
                    LOGGER.error("Scheduler exited for task %s: %s", tid, type(error).__name__)
                await self._fail_if_active(tid, "调度循环异常退出")

    async def run(self) -> None:
        async with self.tick_lock:
            await self.recover()
        while not self.stopping.is_set():
            try:
                await self.tick()
            except Exception as error:
                LOGGER.error("Task supervision tick failed: %s", type(error).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=2)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self.stopping.set()
        for loop in list(self.loops.values()):
            loop.request_stop("runtime_restart")
        async with self.tick_lock:
            await self._stop()

    async def _stop(self) -> None:
        for task in self.cleanups.values():
            task.cancel()
        await asyncio.gather(*self.cleanups.values(), return_exceptions=True)
        await asyncio.gather(*(loop.stop(reason="runtime_restart") for loop in self.loops.values()))
        await asyncio.gather(*self.loop_tasks.values(), return_exceptions=True)
        self.cleanups.clear()
        self.loops.clear()
        self.loop_tasks.clear()
