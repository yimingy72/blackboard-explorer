"""Database-driven team scheduling; notification loss is covered by polling."""

import asyncio
import logging
import traceback
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.execution import CtfExecutionAdapter, DrainPending, UnknownCommandOutcome
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.settings import Settings

logger = logging.getLogger(__name__)


def failure_diagnostic(stage: str, error: BaseException) -> dict[str, Any]:
    """Return bounded failure metadata without copying exception or request contents."""
    error_type = type(error).__name__
    if not error_type.isidentifier():
        error_type = "RuntimeError"
    correlation_id = str(uuid4())
    summary = "CTF 执行因内部错误停止；请使用失败追踪 ID 查询受控日志。"
    metadata = getattr(error, "bbx_model_error", None)
    if isinstance(metadata, dict):
        if metadata.get("incomplete_reason") == "max_output_tokens":
            summary = "模型响应达到单次输出上限而截断，不完整的工具调用未执行；请查看模型错误记录。"
        elif metadata.get("category") == "content_filter":
            summary = "模型服务的内容审核拦截了本次调用，任务已停止；请查看模型错误记录。"
    diagnostic = {
        "error_type": error_type[:100],
        "phase": stage[:64],
        "occurred_at": datetime.now(UTC).isoformat(),
        "correlation_id": correlation_id,
        "summary": summary,
    }
    # Keep the stack location for operators, but deliberately omit str(error), which may
    # contain credentials, tool arguments, model requests, or remote response bodies.
    stack = "; ".join(
        f"{frame.filename}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)
    )
    logger.error(
        "CTF coordinator failure correlation_id=%s phase=%s error_type=%s stack=%s",
        correlation_id,
        diagnostic["phase"],
        diagnostic["error_type"],
        stack,
    )
    return diagnostic


def budget_exhausted(task: dict[str, Any]) -> bool:
    elapsed = float(task.get("active_seconds") or 0)
    since = task.get("active_since")
    if since:
        started = datetime.fromisoformat(since) if isinstance(since, str) else since
        elapsed += max(0, (datetime.now(UTC) - started).total_seconds())
    control = task.get("ctf_control", {})
    reservations = {
        **control.get("budget_reservations", {}),
        **control.get("budget_unknown_reservations", {}),
    }
    reserved = sum(
        (Decimal(str(item.get("cost", 0))) for item in reservations.values()), Decimal(0)
    )
    return (
        Decimal(str(task.get("usage", {}).get("cost", 0))) + reserved
        >= Decimal(str(task["budget"]["max_cost"]))
        or elapsed >= task["budget"]["max_minutes"] * 60
    )


class CtfCoordinator:
    def __init__(
        self,
        service: BlackboardClient,
        settings: Settings,
        *,
        runner: Any = None,
        envd: Any = None,
        poll_seconds: float = 0.2,
        envd_factory: Any = None,
        envd_replacement: Any = None,
    ) -> None:
        self.service = CtfClient(service)
        self.runner = runner or CtfRunner(self.service, settings, envd=envd)
        self.envd, self.poll_seconds = envd, poll_seconds
        self.envd_factory = envd_factory
        self.envd_replacement = envd_replacement
        self.envds: dict[str, Any] = {}
        self.instance = str(uuid4())
        self.active: dict[tuple[str, str], tuple[asyncio.Task, dict[str, Any]]] = {}
        self.review_active: dict[tuple[str, str], tuple[asyncio.Task, dict[str, Any]]] = {}
        self.review_recovered: set[str] = set()
        self.stopping = asyncio.Event()
        self.recovery_pending: set[str] = set()

    async def adapter(self, task_id: str) -> Any:
        if task_id not in self.envds and self.envd_factory is not None:
            task = (await self.service.state(task_id))["task"]
            replacement = task["ctf_control"].get("replacement")
            if replacement and replacement["phase"] != "ready":
                await self.replace_execution(task_id)
                return self.envds[task_id]
            self.envds[task_id] = CtfExecutionAdapter(
                self.service, await self.envd_factory(task_id)
            )
            self.runner.envds[task_id] = self.envds[task_id]
        return self.envds.get(task_id, self.envd)

    async def replace_execution(self, task_id: str) -> None:
        if self.envd_replacement is None:
            raise UnknownCommandOutcome("Execution replacement is required")
        futures = [future for (tid, _), (future, _) in self.active.items() if tid == task_id]
        for future in futures:
            future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        for key in [key for key in self.active if key[0] == task_id]:
            del self.active[key]
        await self.runner.retry_checkpoints(task_id, settle=False)
        current = (await self.service.state(task_id))["task"]
        if current["ctf_control"].get("unresolved_checkpoints"):
            raise UnknownCommandOutcome("Unconfirmed checkpoint blocks execution replacement")
        replacement = await self.envd_replacement(task_id)
        old = self.envds.get(task_id)
        self.envds[task_id] = CtfExecutionAdapter(self.service, replacement)
        self.runner.envds[task_id] = self.envds[task_id]
        if old is not None:
            await old.close()
        await self.service.runtime(task_id, "recover")
        self._discard_settlements(task_id)

    async def member_operations(self, task_id: str, state: dict[str, Any]) -> None:
        envd = await self.adapter(task_id)
        for member in state["members"]:
            operation = member.get("pending_operation")
            if not operation:
                continue
            running = self.active.pop((task_id, member["id"]), None)
            if running:
                running[0].cancel()
                await asyncio.gather(running[0], return_exceptions=True)
            if not hasattr(envd, "stop_member"):
                continue
            if (task_id, member["id"]) in getattr(
                self.runner, "pending_checkpoints", {}
            ) or member.get("current_turn_id") in state["task"]["ctf_control"].get(
                "unresolved_checkpoints", []
            ):
                await self.runner.retry_checkpoints(task_id)
                continue
            try:
                proof = await envd.stop_member(task_id, member)
            except UnknownCommandOutcome:
                if self.envd_replacement is not None:
                    await self.replace_execution(task_id)
                return
            except RemoteError:
                continue
            await self.service.runtime(
                task_id,
                "complete_member_operation",
                member_id=member["id"],
                request_id=operation["request_id"],
                proof=proof,
            )

    async def recover(self, task_id: str) -> None:
        pending = [future for (tid, _), (future, _) in self.active.items() if tid == task_id]
        for future in pending:
            future.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for key in [key for key in self.active if key[0] == task_id]:
            del self.active[key]
        # The execution adapter must prove remote drain before abandoned writers
        # are fenced or any new generation is dispatched.
        await self.adapter(task_id)
        await self.member_operations(task_id, await self.service.state(task_id))
        envd = await self.adapter(task_id)
        if envd is not None:
            try:
                if not await envd.drain(task_id):
                    raise UnknownCommandOutcome("CTF execution drain is unconfirmed")
            except DrainPending:
                self.recovery_pending.add(task_id)
                return
            except UnknownCommandOutcome:
                await self.replace_execution(task_id)
        await self.service.runtime(task_id, "recover")
        self.recovery_pending.discard(task_id)
        self._discard_settlements(task_id)

    def _discard_settlements(self, task_id: str) -> None:
        entries = getattr(self.runner, "pending_settlements", {})
        for key in [key for key in entries if key[0] == task_id]:
            del entries[key]

    async def request_close(
        self, task_id: str, reason: str, *, failure: dict[str, Any] | None = None
    ) -> None:
        summary = failure["summary"] if failure is not None else reason
        try:
            await self.service.runtime(
                task_id,
                "request_finish",
                actor="system",
                request_id=str(uuid4()),
                conclusion={
                    "end_reason": reason,
                    "summary": summary,
                    "unresolved_items": [],
                    "evidence_refs": [],
                    "lead_claim": False,
                    **({"failure": failure} if failure is not None else {}),
                },
            )
        except RemoteError as error:
            if error.status != 409:
                raise
            task = (await self.service.state(task_id))["task"]
            if task["ctf_control"]["phase"] not in {"closing", "closed"}:
                raise

    async def tick(self, task_id: str) -> bool:
        if task_id in self.recovery_pending:
            await self.recover(task_id)
            return True
        state = await self.service.state(task_id)
        envd = await self.adapter(task_id)
        if isinstance(envd, CtfExecutionAdapter):
            root = await envd.envd.ctf_status()
            bindings = [m.get("execution") for m in state["members"] if m.get("execution")]
            if root.get("state") == "unknown" or any(
                (
                    binding.get("replacement_boot_id")
                    if binding.get("container_destroyed")
                    else binding["boot_id"]
                )
                != root.get("boot_id")
                for binding in bindings
            ):
                await self.replace_execution(task_id)
                return True
        retry_settlements = getattr(self.runner, "retry_settlements", None)
        if retry_settlements is not None:
            await retry_settlements(task_id)
        await self.member_operations(task_id, state)
        task = state["task"]
        phase = task["ctf_control"]["phase"]
        if phase == "closed":
            return False
        if phase == "running" and budget_exhausted(task):
            await self.request_close(task_id, "budget_exhausted")
            return True
        for key, (future, turn) in list(self.active.items()):
            if key[0] != task_id:
                continue
            if future.done():
                del self.active[key]
                error = None if future.cancelled() else future.exception()
                if error is not None:
                    # An unconfirmed checkpoint must remain recoverable. Close admission
                    # before recovery settles it as interrupted, never as success.
                    await self.request_close(
                        task_id,
                        "system_failure",
                        failure=failure_diagnostic("turn", error),
                    )
                    return True
            elif phase == "running":
                try:
                    await self.service.runtime(
                        task_id,
                        "renew_turn",
                        agent_id=key[1],
                        turn_id=turn["id"],
                        generation=turn["generation"],
                    )
                except RemoteError as error:
                    if error.status != 409:
                        raise
                    latest = await self.service.state(task_id)
                    latest_task = latest["task"]
                    latest_member = next(
                        member for member in latest["members"] if member["id"] == key[1]
                    )
                    latest_turn = next(
                        (item for item in latest["turns"] if item["id"] == turn["id"]), None
                    )
                    if (
                        latest_task["ctf_control"]["phase"] == "running"
                        and latest_member.get("current_turn_id") == turn["id"]
                        and latest_turn is not None
                        and latest_turn["status"] == "running"
                    ):
                        raise
                    # The runner can settle a turn between the active check and
                    # this lease renewal. A fenced 409 is then an expected race.
        if phase == "closing":
            pending = [future for (tid, _), (future, _) in self.active.items() if tid == task_id]
            for future in pending:
                future.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for key in [key for key in self.active if key[0] == task_id]:
                del self.active[key]
            if task["ctf_control"].get("unresolved_checkpoints") or any(
                tid == task_id for tid, _ in getattr(self.runner, "pending_checkpoints", {})
            ):
                retry = getattr(self.runner, "retry_checkpoints", None)
                if retry is not None:
                    await retry(task_id)
                # Re-read on the next tick. After restart, never invent the lost
                # final snapshot: the durable close barrier remains visible.
                return True
            if any(tid == task_id for tid, _ in getattr(self.runner, "pending_settlements", {})):
                return True
            envd = await self.adapter(task_id)
            try:
                drained = envd is None or await envd.drain(task_id)
            except DrainPending:
                return True
            if drained:
                await self.service.runtime(task_id, "recover")
                await self.service.runtime(task_id, "finalize_close", drained=True)
            return not drained
        if phase == "running":
            for member in state["members"]:
                key = (task_id, member["id"])
                if key in self.active or member["lifecycle"] != "active":
                    continue
                turn = await self.service.runtime(
                    task_id, "claim_turn", agent_id=member["id"], runtime_instance=self.instance
                )
                if turn:
                    self.active[key] = (
                        asyncio.create_task(self.runner.run(task_id, member, turn)),
                        turn,
                    )
        return True

    async def run(self, task_id: str) -> None:
        try:
            await self.adapter(task_id)
            task = (await self.service.state(task_id))["task"]
            if task["ctf_control"]["phase"] not in {"closing", "closed"}:
                await self.service.runtime(task_id, "start")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = failure_diagnostic("startup", error)
            try:
                await self.service.runtime(
                    task_id,
                    "fail_start",
                    reason=failure["summary"],
                    failure=failure,
                )
            except Exception:
                pass
            raise
        failures = 0
        while not self.stopping.is_set():
            try:
                if not await self.tick(task_id):
                    return
                failures = 0
            except Exception as error:
                failures += 1
                failure = failure_diagnostic("tick", error)
                # If persistence is unavailable, retain authoritative running/closing
                # state for recovery rather than manufacture a terminal result.
                if failures >= 3:
                    await self.request_close(task_id, "system_failure", failure=failure)
                    raise
                await self.request_close(task_id, "system_failure", failure=failure)
            try:
                await asyncio.wait_for(self.stopping.wait(), self.poll_seconds)
            except TimeoutError:
                pass

    async def stop(self, task_id: str) -> None:
        await self.request_close(task_id, "user_stop")

    async def review_tick(self, task_id: str) -> None:
        """Review owns separate turns and never provisions an execution environment."""
        if task_id not in self.review_recovered:
            await self.service.runtime(task_id, "recover_reviews", runtime_instance=self.instance)
            self.review_recovered.add(task_id)
        for key, (future, turn) in list(self.review_active.items()):
            if key[0] != task_id:
                continue
            if future.done():
                error = None if future.cancelled() else future.exception()
                if key in self.runner.pending_checkpoints:
                    await self.runner.retry_checkpoints(task_id)
                elif error is not None:
                    await self.runner.settle(
                        task_id, {"id": key[1]}, turn, "failed", "复盘中断，可重试。"
                    )
                del self.review_active[key]
            else:
                await self.service.runtime(
                    task_id,
                    "renew_turn",
                    agent_id=key[1],
                    turn_id=turn["id"],
                    generation=turn["generation"],
                )
        state = await self.service.state(task_id)
        if state["task"]["status"] not in {"finished", "stopped", "failed"}:
            self.review_recovered.discard(task_id)
            return
        for member in state["members"]:
            key = (task_id, member["id"])
            if key in self.review_active:
                continue
            turn = await self.service.runtime(
                task_id, "claim_review_turn", agent_id=member["id"], runtime_instance=self.instance
            )
            if turn:
                self.review_active[key] = (
                    asyncio.create_task(self.runner.run(task_id, member, turn)),
                    turn,
                )

    async def detach(self, task_id: str, *, deleting: bool = False) -> None:
        """Join local writers and discard per-run transport state without destroying files."""
        futures = [
            future
            for group in (self.active, self.review_active)
            for (tid, _), (future, _) in group.items()
            if tid == task_id
        ]
        for future in futures:
            future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        for group in (self.active, self.review_active):
            for key in [key for key in group if key[0] == task_id]:
                del group[key]
        self.review_recovered.discard(task_id)
        if not deleting:
            await self.runner.retry_checkpoints(task_id, settle=False)
            if any(key[0] == task_id for key in self.runner.pending_checkpoints):
                raise RuntimeError("Unconfirmed CTF checkpoint blocks archive cleanup")
        else:
            for key in [key for key in self.runner.pending_checkpoints if key[0] == task_id]:
                del self.runner.pending_checkpoints[key]
        self._discard_settlements(task_id)
        self.recovery_pending.discard(task_id)
        self.runner.envds.pop(task_id, None)
        adapter = self.envds.pop(task_id, None)
        if adapter is not None:
            await adapter.close()

    async def close(self) -> None:
        self.stopping.set()
        futures = [
            future for group in (self.active, self.review_active) for future, _ in group.values()
        ]
        for future in futures:
            future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        self.active.clear()
        self.review_active.clear()
        for adapter in self.envds.values():
            await adapter.close()
        self.envds.clear()
