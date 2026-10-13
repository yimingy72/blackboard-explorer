"""Database-driven team scheduling; notification loss is covered by polling."""

import asyncio
import logging
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx

from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.execution import CtfExecutionAdapter, DrainPending, UnknownCommandOutcome
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.settings import Settings

logger = logging.getLogger(__name__)

CLOSE_RETRY_LIMIT = 8
CLOSE_TIMEOUT_SECONDS = 60


@dataclass
class CloseRetry:
    started: float
    attempts: int = 0
    next_retry: float = 0
    escalated: bool = False


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
    reservations = control.get("budget_unknown_reservations", {})
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
        self.renewals: dict[tuple[str, str], tuple[str, int, float]] = {}
        self.review_recovered: set[str] = set()
        self.stopping = asyncio.Event()
        self.recovery_pending: set[str] = set()
        self.watchdog_checks: dict[str, float] = {}
        self.tick_locks: dict[str, asyncio.Lock] = {}
        self.control_waits: dict[str, tuple[str, float]] = {}
        self.close_retries: dict[str, CloseRetry] = {}

    def waiting(self, task_id: str, stage: str) -> None:
        self.control_waits[task_id] = (stage, monotonic())

    async def adapter(self, task_id: str) -> Any:
        if task_id not in self.envds and self.envd_factory is not None:
            task = (await self.service.runtime_state(task_id))["task"]
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
        current = (await self.service.runtime_state(task_id))["task"]
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

    async def member_operations(
        self, task_id: str, state: dict[str, Any], *, checkpoint_failure: str | None = None
    ) -> None:
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
            blocked = (task_id, member["id"]) in getattr(
                self.runner, "pending_checkpoints", {}
            ) or member.get("current_turn_id") in state["task"]["ctf_control"].get(
                "unresolved_checkpoints", []
            )
            if blocked and checkpoint_failure is None:
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
                **({"checkpoint_failure": checkpoint_failure} if checkpoint_failure else {}),
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
        await self.member_operations(task_id, await self.service.runtime_state(task_id))
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
        self.watchdog_checks.pop(task_id, None)
        for key in [key for key in self.renewals if key[0] == task_id]:
            del self.renewals[key]
        entries = getattr(self.runner, "pending_settlements", {})
        for key in [key for key in entries if key[0] == task_id]:
            del entries[key]

    async def renew(self, task_id: str, agent_id: str, turn: dict[str, Any]) -> None:
        key = (task_id, agent_id)
        previous = self.renewals.get(key)
        identity = (turn["id"], turn["generation"])
        if previous is not None and previous[:2] == identity and monotonic() < previous[2]:
            return
        await self.service.runtime(
            task_id,
            "renew_turn",
            agent_id=agent_id,
            turn_id=turn["id"],
            generation=turn["generation"],
        )
        # Mailbox leases last 300 seconds; renew independently of model/tool completion.
        self.renewals[key] = (*identity, monotonic() + 60)

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
            task = (await self.service.runtime_state(task_id))["task"]
            if task["ctf_control"]["phase"] not in {"closing", "closed"}:
                raise

    async def tick(self, task_id: str) -> bool:
        async with self.tick_locks.setdefault(task_id, asyncio.Lock()):
            self.waiting(task_id, "runtime_state")
            try:
                return await self._tick(task_id)
            finally:
                self.control_waits.pop(task_id, None)

    async def _tick(self, task_id: str) -> bool:
        state = await self.service.runtime_state(task_id)
        phase = state["task"]["ctf_control"]["phase"]
        if phase == "closed":
            self.close_retries.pop(task_id, None)
            return False
        if phase == "closing":
            return await self._close_tick(task_id, state)
        if task_id in self.recovery_pending:
            self.waiting(task_id, "recover")
            await self.recover(task_id)
            return True
        self.waiting(task_id, "execution_status")
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
            self.waiting(task_id, "retry_settlements")
            await retry_settlements(task_id)
        self.waiting(task_id, "member_operations")
        await self.member_operations(task_id, state)
        task = state["task"]
        phase = task["ctf_control"]["phase"]
        if phase == "closed":
            return False
        if phase == "running" and budget_exhausted(task):
            self.waiting(task_id, "reconcile_budget")
            await self.service.runtime(task_id, "reconcile_budget")
            state = await self.service.runtime_state(task_id)
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
                self.renewals.pop(key, None)
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
                    self.waiting(task_id, "renew_turn")
                    await self.renew(task_id, key[1], turn)
                except RemoteError as error:
                    if error.status != 409:
                        raise
                    latest = await self.service.runtime_state(task_id)
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
            return await self._close_tick(task_id, state)
        if phase == "running":
            ready = set(state["ready_member_ids"])
            for member in state["members"]:
                key = (task_id, member["id"])
                if (
                    key in self.active
                    or member["lifecycle"] != "active"
                    or member["id"] not in ready
                ):
                    continue
                self.waiting(task_id, "claim_turn")
                turn = await self.service.runtime(
                    task_id, "claim_turn", agent_id=member["id"], runtime_instance=self.instance
                )
                if turn:
                    self.active[key] = (
                        asyncio.create_task(self.runner.run(task_id, member, turn)),
                        turn,
                    )
            if (
                not ready
                and not any(tid == task_id for tid, _ in self.active)
                and not any(
                    tid == task_id
                    for name in ("pending_checkpoints", "pending_settlements")
                    for tid, _ in getattr(self.runner, name, {})
                )
            ):
                if monotonic() >= self.watchdog_checks.get(task_id, 0):
                    self.waiting(task_id, "idle_watchdog")
                    await self.service.runtime(task_id, "idle_watchdog")
                    self.watchdog_checks[task_id] = monotonic() + 5
            else:
                self.watchdog_checks.pop(task_id, None)
        return True

    async def _close_tick(self, task_id: str, state: dict[str, Any]) -> bool:
        task = state["task"]
        clock = monotonic()
        if task_id not in self.close_retries:
            requested = (task.get("ctf_conclusion") or {}).get("requested_at")
            elapsed = 0
            if requested:
                stamp = datetime.fromisoformat(str(requested).replace("Z", "+00:00"))
                elapsed = max(0, (datetime.now(UTC) - stamp).total_seconds())
            self.close_retries[task_id] = CloseRetry(started=clock - elapsed)
        retry = self.close_retries[task_id]
        if clock < retry.next_retry:
            return True
        expired = (
            retry.attempts >= CLOSE_RETRY_LIMIT or clock - retry.started >= CLOSE_TIMEOUT_SECONDS
        )
        retry.attempts += 1
        retry.next_retry = clock + (30 if expired else min(2 ** (retry.attempts - 1), 8))
        if expired and not retry.escalated:
            retry.escalated = True
            logger.warning(
                "CTF closing retries exhausted task=%s attempts=%s elapsed_seconds=%.1f",
                task_id,
                retry.attempts - 1,
                clock - retry.started,
            )
        try:
            self.waiting(task_id, "join_turns")
            pending = [future for (tid, _), (future, _) in self.active.items() if tid == task_id]
            for future in pending:
                if not future.cancelling():
                    future.cancel()
            if pending:
                _, unfinished = await asyncio.wait(pending, timeout=5)
                if unfinished:
                    return True
            for key in [key for key in self.active if key[0] == task_id]:
                del self.active[key]
                self.renewals.pop(key, None)
            if not expired:
                settle = getattr(self.runner, "retry_settlements", None)
                if settle is not None:
                    self.waiting(task_id, "retry_settlements")
                    await settle(task_id)
                await self.member_operations(task_id, state)
                save = getattr(self.runner, "retry_checkpoints", None)
                if save is not None:
                    self.waiting(task_id, "retry_checkpoints")
                    await save(task_id)
                state = await self.service.runtime_state(task_id)
                if state["task"]["ctf_control"].get("unresolved_checkpoints") or any(
                    tid == task_id
                    for name in ("pending_checkpoints", "pending_settlements")
                    for tid, _ in getattr(self.runner, name, {})
                ):
                    return True
            envd = await self.adapter(task_id)
            self.waiting(task_id, "drain_execution")
            if envd is not None and not await envd.drain(task_id):
                return True
            arguments: dict[str, Any] = {"drained": True}
            if expired:
                arguments["checkpoint_failure"] = (
                    "CTF closing exceeded its checkpoint/settlement retry limit. "
                    "The execution environment and any unconfirmed Session or usage are retained."
                )
                await self.member_operations(
                    task_id,
                    await self.service.runtime_state(task_id),
                    checkpoint_failure=arguments["checkpoint_failure"],
                )
            self.waiting(task_id, "finalize_close")
            await self.service.runtime(task_id, "recover")
            await self.service.runtime(task_id, "finalize_close", **arguments)
            self.close_retries.pop(task_id, None)
            return False
        except DrainPending:
            return True
        except Exception as error:
            logger.warning(
                "CTF closing deferred task=%s attempt=%s error_type=%s status=%s escalated=%s",
                task_id,
                retry.attempts,
                type(error).__name__,
                getattr(error, "status", None),
                expired,
            )
            return True

    async def run(self, task_id: str) -> None:
        self.waiting(task_id, "startup")
        try:
            await self.adapter(task_id)
            task = (await self.service.runtime_state(task_id))["task"]
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
        finally:
            self.control_waits.pop(task_id, None)
        failures = 0
        while not self.stopping.is_set():
            delay = self.poll_seconds
            try:
                if not await self.tick(task_id):
                    return
                failures = 0
                if retry := self.close_retries.get(task_id):
                    delay = max(delay, retry.next_retry - monotonic())
            except httpx.TransportError as error:
                failures += 1
                logger.warning(
                    "CTF control transport retry task=%s attempt=%s error_type=%s",
                    task_id,
                    failures,
                    type(error).__name__,
                )
                if failures >= 3:
                    failure = failure_diagnostic("control_transport", error)
                    await self.request_close(task_id, "system_failure", failure=failure)
                    raise
                delay = max(self.poll_seconds, min(2 ** (failures - 1), 4))
            except Exception as error:
                failure = failure_diagnostic("tick", error)
                # If persistence is unavailable, retain authoritative running/closing
                # state for recovery rather than manufacture a terminal result.
                await self.request_close(task_id, "system_failure", failure=failure)
            try:
                await asyncio.wait_for(self.stopping.wait(), delay)
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
                self.renewals.pop(key, None)
            else:
                await self.renew(task_id, key[1], turn)
        state = await self.service.runtime_state(task_id)
        if state["task"]["status"] not in {"finished", "stopped", "failed"}:
            self.review_recovered.discard(task_id)
            return
        ready = set(state["review_ready_member_ids"])
        for member in state["members"]:
            key = (task_id, member["id"])
            if key in self.review_active or member["id"] not in ready:
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
        self.control_waits.pop(task_id, None)
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
        self.renewals.clear()
        self.watchdog_checks.clear()
        self.control_waits.clear()
        for adapter in self.envds.values():
            await adapter.close()
        self.envds.clear()
