"""Queue tasks, recover owned environments, and durably archive terminal work."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from bbx_contracts.ctf import CtfAgentProfile
from bbx_contracts.models import Params

from bbx_runtime.clients import BlackboardClient, EnvdClient, RemoteError
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.execenv import ExecEnvHandle, ExecEnvManager
from bbx_runtime.execenv.archive import ArchiveCapacityError
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
        self.archive_blocked: set[str] = set()
        self.stopping = asyncio.Event()
        self.tick_lock = asyncio.Lock()
        self.ctf: CtfCoordinator | None = None
        self.ctf_tasks: dict[str, asyncio.Task[None]] = {}

    def _ctf(self) -> CtfCoordinator:
        if self.ctf is None:
            self.ctf = CtfCoordinator(
                service=self.service,
                settings=self.settings,
                envd_factory=self._ctf_envd,
                envd_replacement=self._replace_ctf_envd,
            )
        return self.ctf

    async def _ctf_envd(self, tid: str) -> EnvdClient:
        state = await self.service.state(tid)
        task = state["task"]
        if task.get("mode") != "ctf":
            raise RuntimeError("CTF execution requires a CTF task")
        handle = await self.manager.find(tid)
        if handle is None:
            if task["status"] != "provisioning":
                raise RuntimeError("CTF execution environment is missing; recovery required")
            document = await self.service.get_profile(
                task["agent_profile"], task["agent_profile_version"]
            )
            profile = CtfAgentProfile.model_validate(document.get("profile", document))
            handle = await self.manager.provision(tid, profile, task.get("resume_workspace_uri"))
        await self.manager.wait_healthy(handle)
        await self.manager.ensure_initial_inputs(handle, task.get("initial_attachments", []))
        return EnvdClient(handle.base_url, handle.token)

    async def _replace_ctf_envd(self, tid: str) -> EnvdClient:
        """Resume a durable replacement without declaring an unknown registry drained."""
        service = self._ctf().service
        state = await service.state(tid)
        task = state["task"]
        replacement = task["ctf_control"].get("replacement")
        handle = await self.manager.find(tid)
        if not replacement or replacement["phase"] == "ready":
            if handle is None:
                raise RuntimeError("Cannot preserve a missing CTF execution environment")
            async with EnvdClient(handle.base_url, handle.token) as client:
                status = await client.ctf_status()
            unconfirmed_boots = {
                member["execution"]["boot_id"]
                for member in state.get("members", [])
                if member.get("execution") and not member["execution"].get("drained")
            }
            if len(unconfirmed_boots) > 1:
                raise RuntimeError("CTF execution bindings disagree about the old boot")
            replacement = {
                "old_container_id": handle.container_id,
                "old_boot_id": next(iter(unconfirmed_boots), status["boot_id"]),
                "phase": "begin",
            }
            await service.runtime(tid, "execution_replacement", **replacement)
        identity = {key: replacement[key] for key in ("old_container_id", "old_boot_id")}
        if replacement["phase"] == "begin":
            if handle is None or handle.container_id != identity["old_container_id"]:
                raise RuntimeError("CTF replacement cannot archive a different container")
            archive = await self.manager.archive_to_store(
                handle, uri=f"workspace/{tid}/replacement-{identity['old_container_id']}.tar.zst"
            )
            await service.runtime(
                tid,
                "execution_replacement",
                **identity,
                phase="archive_saved",
                archive_uri=archive.uri,
            )
            replacement = {**replacement, "phase": "archive_saved", "archive_uri": archive.uri}
        if replacement["phase"] == "archive_saved":
            await self.manager.destroy_confirmed(tid, identity["old_container_id"])
            await service.runtime(tid, "execution_replacement", **identity, phase="destroyed")
            replacement = {**replacement, "phase": "destroyed"}
        document = await self.service.get_profile(
            task["agent_profile"], task["agent_profile_version"]
        )
        profile = CtfAgentProfile.model_validate(document.get("profile", document))
        handle = await self.manager.provision(tid, profile, replacement["archive_uri"])
        client = EnvdClient(handle.base_url, handle.token)
        try:
            status = await client.ctf_status()
            if status.get("state") == "unknown":
                # No registration is admitted before ready. A daemon restart in
                # this restore window can safely retry from the durable archive.
                await client.close()
                await self.manager.destroy_confirmed(tid, handle.container_id)
                handle = await self.manager.provision(tid, profile, replacement["archive_uri"])
                client = EnvdClient(handle.base_url, handle.token)
                status = await client.ctf_status()
                if status.get("state") == "unknown":
                    raise RuntimeError("Replacement CTF environment is not a fresh boot")
            await service.runtime(
                tid,
                "execution_replacement",
                **identity,
                phase="ready",
                new_boot_id=status["boot_id"],
            )
        except BaseException:
            await client.close()
            raise
        return client

    async def _start_ctf(self, tid: str) -> None:
        if tid not in self.ctf_tasks and not self.stopping.is_set():
            self.ctf_tasks[tid] = asyncio.create_task(self._ctf().run(tid), name=f"ctf:{tid}")

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
        attachments = state["task"].get("initial_attachments", [])
        if attachments:
            await self.manager.ensure_initial_inputs(handle, attachments)
        if self.stopping.is_set():
            return
        executor = ActionExecutor(tid, self.service, self.manager, self.runner, handle)
        loop = SchedulerLoop(executor, self.service, Params.model_validate(state["task"]["params"]))
        self.loops[tid] = loop
        self.loop_tasks[tid] = asyncio.create_task(loop.run(), name=f"scheduler:{tid}")

    async def recover(self) -> None:
        for listed in await self.service.list_tasks():
            tid = str(listed["id"])
            if listed.get("mode", "blackboard") == "ctf":
                if listed["status"] == "running":
                    await self._ctf().recover(tid)
                    await self._start_ctf(tid)
                continue
            state = await self.service.state(tid)
            # No previous Python coroutine can survive the exclusive runtime lock handoff.
            for aid, agent in state["agents"].items():
                if agent["status"] in ACTIVE:
                    await self.service.finish_agent(
                        tid,
                        aid,
                        {"accepted": False, "reason": "运行器重启"},
                        "runtime_restart",
                        **(
                            {"expected_derive_round": int(agent.get("derive_round") or 1)}
                            if agent.get("task_type") == "derive"
                            else {}
                        ),
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
                    reason = "failed" if state["task"]["status"] == "failed" else "closing"
                    await self.service.conclude(
                        tid,
                        aid,
                        reason,
                        **(
                            {"expected_derive_round": int(agent.get("derive_round") or 1)}
                            if agent.get("task_type") == "derive"
                            else {}
                        ),
                    )
                except RemoteError as error:
                    if error.code not in {"agent_inactive", "stale_agent_round"}:
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
                        **(
                            {"expected_derive_round": int(agent.get("derive_round") or 1)}
                            if agent.get("task_type") == "derive"
                            else {}
                        ),
                    )
                elif expired:
                    await loop.executor.cancel(  # type: ignore[union-attr]
                        aid,
                        "grace_timeout",
                        int(agent.get("derive_round") or 1)
                        if agent.get("task_type") == "derive"
                        else None,
                    )
            await asyncio.sleep(0.2)
        if loop := self.loops.pop(tid, None):
            await loop.stop(reason="grace_timeout")
            await self.loop_tasks.pop(tid)

    async def _cleanup(self, tid: str) -> None:
        state = await self.service.state(tid)
        ctf = state["task"].get("mode") == "ctf"
        try:
            await self._cleanup_task(tid, state)
        except Exception as error:
            if ctf:
                try:
                    await self._ctf().service.runtime(
                        tid, "cleanup_status", phase="failed", error=type(error).__name__
                    )
                except Exception:
                    LOGGER.error("Could not persist CTF cleanup failure for task %s", tid)
            raise

    async def _cleanup_task(self, tid: str, state: dict[str, Any]) -> None:
        if state["task"].get("mode") == "ctf":
            if future := self.ctf_tasks.pop(tid, None):
                future.cancel()
                await asyncio.gather(future, return_exceptions=True)
            await self._ctf().detach(tid)
            if state["task"].get("ctf_control", {}).get("unresolved_checkpoints"):
                raise RuntimeError("Unconfirmed CTF checkpoint blocks archive cleanup")
        else:
            await self._drain(tid, state)
        state = await self.service.state(tid)
        ctf = state["task"].get("mode") == "ctf"
        if not state["task"].get("workspace_uri"):
            if ctf:
                await self._ctf().service.runtime(tid, "cleanup_status", phase="archiving")
            data = await self.service.archive_data(tid)
            try:
                archive = await self.manager.archive_task(tid, data)
            except ArchiveCapacityError:
                LOGGER.error(
                    "Task %s archive exceeds capacity; execution environment retained.", tid
                )
                self.archive_blocked.add(tid)
                if ctf:
                    await self._ctf().service.runtime(
                        tid, "cleanup_status", phase="failed", error="ArchiveCapacityError"
                    )
                return
            except RemoteError as error:
                if error.status != 413:
                    raise
                LOGGER.error(
                    "Task %s archive exceeds capacity; execution environment retained.", tid
                )
                self.archive_blocked.add(tid)
                if ctf:
                    await self._ctf().service.runtime(
                        tid, "cleanup_status", phase="failed", error="ArchiveCapacityError"
                    )
                return
            await self.service.record_archive(tid, archive.uri, archive.size, archive.fallback)
        if ctf:
            await self._ctf().service.runtime(tid, "cleanup_status", phase="destroying")
        await self.manager.destroy(tid)
        await self.service.record_cleanup(tid)
        self.cleaned.add(tid)

    async def _purge_deleted(self, tid: str) -> None:
        if cleanup := self.cleanups.pop(tid, None):
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
        if future := self.ctf_tasks.pop(tid, None):
            future.cancel()
            await asyncio.gather(future, return_exceptions=True)
        if self.ctf is not None:
            await self.ctf.detach(tid, deleting=True)
        if loop := self.loops.pop(tid, None):
            await loop.stop(reason="runtime_restart")
            await self.loop_tasks.pop(tid)
        await self.manager.destroy(tid)
        await self.service.purge_task(tid)
        self.cleaned.discard(tid)
        self.archive_blocked.discard(tid)

    async def tick(self) -> None:
        async with self.tick_lock:
            if not self.stopping.is_set():
                await self._tick()

    async def _tick(self) -> None:
        deletion_reader = getattr(self.service, "pending_deletions", None)
        for tid in await deletion_reader() if deletion_reader is not None else []:
            try:
                await self._purge_deleted(str(tid))
            except Exception as error:
                LOGGER.error("Deletion cleanup failed for task %s: %s", tid, type(error).__name__)
        tasks = await self.service.list_tasks()
        for tid, future in list(self.ctf_tasks.items()):
            if future.done():
                try:
                    future.result()
                except Exception as error:
                    LOGGER.error(
                        "CTF coordinator exited for task %s: %s", tid, type(error).__name__
                    )
                del self.ctf_tasks[tid]
        for tid, future in list(self.cleanups.items()):
            if future.done():
                try:
                    future.result()
                except Exception as error:
                    LOGGER.error("Cleanup failed for task %s: %s", tid, type(error).__name__)
                del self.cleanups[tid]
        for task in tasks:
            tid = str(task["id"])
            if task.get("mode", "blackboard") == "ctf":
                if task["status"] == "running" and tid not in self.ctf_tasks:
                    # A coordinator can exit while its durable task still owns capacity.
                    # Recovery drains local writers before fencing and resuming it.
                    try:
                        await self._ctf().recover(tid)
                        await self._start_ctf(tid)
                    except Exception as error:
                        LOGGER.error(
                            "CTF recovery deferred for task %s: %s", tid, type(error).__name__
                        )
                if task["status"] in TERMINAL and task.get("cleanup_ready"):
                    self.cleaned.add(tid)
                    if not task.get("deleting"):
                        try:
                            await self._ctf().review_tick(tid)
                        except Exception as error:
                            LOGGER.error(
                                "CTF review deferred for task %s: %s", tid, type(error).__name__
                            )
                if task["status"] not in TERMINAL:
                    if task["status"] == "provisioning":
                        self.archive_blocked.discard(tid)
                    continue
            if task["status"] == "provisioning":
                self.archive_blocked.discard(tid)
            if (
                task["status"] in TERMINAL
                and not task.get("deleting")
                and tid not in self.cleaned
                and tid not in self.archive_blocked
                and tid not in self.cleanups
            ):
                self.cleanups[tid] = asyncio.create_task(self._cleanup(tid), name=f"cleanup:{tid}")
        occupied = {str(t["id"]) for t in tasks if t["status"] in {"running", "closing"}}
        occupied.update(self.cleanups)
        occupied.update(self.loops)
        occupied.update(self.ctf_tasks)
        queue = sorted(
            (t for t in tasks if t["status"] == "provisioning"), key=lambda t: str(t["created_at"])
        )
        for task in queue:
            if self.stopping.is_set() or len(occupied) >= self.settings.max_running_tasks:
                break
            tid = str(task["id"])
            self.cleaned.discard(tid)
            try:
                if task.get("mode", "blackboard") == "ctf":
                    await self._start_ctf(tid)
                    occupied.add(tid)
                    continue
                profile = load_runtime_profile(
                    await self.service.get_profile(
                        task["agent_profile"], task["agent_profile_version"]
                    )
                )
                restore_uri = task.get("resume_workspace_uri")
                handle = (
                    await self.manager.provision(tid, profile, restore_uri)
                    if restore_uri
                    else await self.manager.provision(tid, profile)
                )
                occupied.add(tid)
                fresh = await self.service.state(tid)
                if fresh["task"]["status"] != "provisioning":
                    continue
                attachments = fresh["task"].get("initial_attachments", [])
                if attachments:
                    await self.manager.ensure_initial_inputs(handle, attachments)
                await self.service.transition(tid, "running")
                self.cleaned.discard(tid)
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
                if not self.stopping.is_set() and tid not in {
                    str(item["id"]) for item in tasks if item["status"] == "deleting"
                }:
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
        if self.ctf is not None:
            await self.ctf.close()
        await asyncio.gather(*self.ctf_tasks.values(), return_exceptions=True)
        self.ctf_tasks.clear()
        for task in self.cleanups.values():
            task.cancel()
        await asyncio.gather(*self.cleanups.values(), return_exceptions=True)
        await asyncio.gather(*(loop.stop(reason="runtime_restart") for loop in self.loops.values()))
        await asyncio.gather(*self.loop_tasks.values(), return_exceptions=True)
        self.cleanups.clear()
        self.loops.clear()
        self.loop_tasks.clear()
