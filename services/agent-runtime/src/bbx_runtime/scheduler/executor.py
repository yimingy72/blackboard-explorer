"""Turn pure scheduling decisions into blackboard writes and running agents."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from bbx_runtime.clients import BlackboardClient, RemoteError
from bbx_runtime.context import CloseMode, TaskType, execution_timestamp

from .actions import (
    Action,
    Conclude,
    EnterClosing,
    Fail,
    SpawnClose,
    SpawnDerive,
    SpawnExplore,
    SystemClose,
)

logger = logging.getLogger(__name__)


class _StopBeforeLaunch(Exception):
    pass


class BoardRunner(Protocol):
    async def run_agent(
        self, task_id: str, agent_id: str, *, started_at: str, derive_round: int | None
    ) -> None: ...

    async def stop_agent(
        self, task_id: str, agent_id: str, reason: str, *, started_at: str, derive_round: int | None
    ) -> None: ...


class ActionExecutor:
    def __init__(
        self,
        task_id: str,
        service: BlackboardClient,
        *,
        board_runner: BoardRunner,
    ) -> None:
        self.task_id = task_id
        self.service = service
        self.board_runner = board_runner
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.task_rounds: dict[str, int | None] = {}
        self.task_starts: dict[str, asyncio.Event] = {}
        self.task_executions: dict[str, str | None] = {}
        self._stopping = False
        self._stop_reason = "runtime_restart"

    def request_stop(self, reason: str = "runtime_restart") -> None:
        if not self._stopping or reason == "runtime_restart":
            self._stop_reason = reason
        self._stopping = True

    def _check_stopped(self) -> None:
        if self._stopping:
            raise _StopBeforeLaunch

    async def execute(self, actions: Sequence[Action]) -> None:
        for action in actions:
            if self._stopping:
                return
            match action:
                case SpawnExplore():
                    await self._spawn("explore", intent_id=action.intent_id, seed=action.seed)
                case SpawnDerive():
                    await self._spawn(
                        "derive", derive_parallel=action.parallel, derive_review=action.review
                    )
                case SpawnClose():
                    await self._spawn("close", mode=action.mode)
                case Conclude():
                    await self._conclude(action.agent_id, action.reason)
                case SystemClose():
                    await self.service.system_close(self.task_id, action.intent_id)
                case EnterClosing():
                    try:
                        await self.service.transition(self.task_id, "closing", action.reason)
                    except RemoteError as error:
                        if not (
                            action.reason == "accepted"
                            and error.status == 422
                            and error.code == "stale_acceptance"
                        ):
                            raise
                        return
                    if not self._stopping:
                        await self._conclude_running("closing")
                case Fail():
                    await self._conclude_running("failed")
                    if not self._stopping:
                        await self.service.transition(self.task_id, "failed", action.reason)

    async def _spawn(
        self,
        task_type: TaskType,
        *,
        intent_id: str | None = None,
        seed: bool = False,
        mode: CloseMode | None = None,
        derive_parallel: bool | None = None,
        derive_review: bool | None = None,
    ) -> None:
        try:
            registration = await self.service.register_agent(
                self.task_id,
                task_type,
                is_seed=seed,
                intent_id=intent_id if task_type == "explore" else None,
                close_mode=mode,
                **({"derive_parallel": derive_parallel} if derive_parallel is not None else {}),
                **({"derive_review": derive_review} if derive_review is not None else {}),
            )
        except RemoteError as error:
            if error.status == 409 and error.code == "agent_review_active":
                return
            if task_type == "derive" and error.status == 422 and error.code == "stale_derive":
                return
            raise
        aid = registration["agent_id"]
        raw_round = registration.get("derive_round") if task_type == "derive" else None
        derive_round = int(raw_round) if raw_round is not None else None
        round_kwargs: dict[str, Any] = (
            {"expected_derive_round": derive_round} if derive_round else {}
        )
        if registration.get("started_at") is not None:
            round_kwargs["expected_started_at"] = registration["started_at"]
        started_at = execution_timestamp(registration.get("started_at"))
        if started_at is None:
            raise RuntimeError("The original blackboard AgentRun identity is unavailable")
        try:
            self._check_stopped()
            if intent_id is not None:
                try:
                    await self.service.claim_for(self.task_id, intent_id, aid)
                except RemoteError as error:
                    if error.status != 422:
                        raise
                    await self.service.finish_agent(
                        self.task_id,
                        aid,
                        {"accepted": False, "reason": "claim_lost"},
                        "normal",
                        **round_kwargs,
                    )
                    return
                self._check_stopped()
        except _StopBeforeLaunch:
            await self.service.finish_agent(
                self.task_id,
                aid,
                {"accepted": False, "reason": "runtime_stop_before_launch"},
                self._stop_reason,
                **round_kwargs,
            )
            return
        except Exception:
            await self.service.finish_agent(
                self.task_id,
                aid,
                {"accepted": False, "reason": "Agent 启动准备失败"},
                "runtime_error",
                **round_kwargs,
            )
            raise

        async def run() -> None:
            await self.board_runner.run_agent(
                self.task_id, aid, started_at=started_at, derive_round=derive_round
            )

        self._track(aid, started_at, derive_round, run)

    def recover_board(self, state: dict[str, Any]) -> None:
        """Reconnect existing ordinary AgentRuns without registering or claiming again."""
        driver = self.board_runner
        for aid, agent in state["agents"].items():
            started_at = execution_timestamp(agent.get("started_at"))
            if (
                aid in self.tasks
                or not started_at
                or agent["status"] not in {"running", "concluding"}
            ):
                continue
            derive_round = int(agent["derive_round"]) if agent["task_type"] == "derive" else None

            async def run(
                agent_id=aid, expected_start=started_at, round_number=derive_round
            ) -> None:
                await driver.run_agent(
                    self.task_id, agent_id, started_at=expected_start, derive_round=round_number
                )

            self._track(aid, started_at, derive_round, run)

    def _track(
        self,
        aid: str,
        started_at: str | None,
        derive_round: int | None,
        run: Callable[[], Awaitable[None]],
    ) -> None:
        started = asyncio.Event()

        async def run_tracked() -> None:
            started.set()
            return await run()

        running = asyncio.create_task(run_tracked(), name=f"{self.task_id}/{aid}")
        self.tasks[aid] = running
        self.task_rounds[aid] = derive_round
        self.task_starts[aid] = started
        self.task_executions[aid] = started_at
        running.add_done_callback(lambda done, agent_id=aid: self._forget(agent_id, done))

    def _forget(self, aid: str, task: asyncio.Task[None]) -> None:
        if self.tasks.get(aid) is task:
            self.tasks.pop(aid, None)
            self.task_rounds.pop(aid, None)
            self.task_starts.pop(aid, None)
            self.task_executions.pop(aid, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logger.error("Agent %s task failed: %s", aid, type(error).__name__)

    async def _conclude(
        self, aid: str, reason: str, expected_derive_round: int | None = None
    ) -> None:
        try:
            await self.service.conclude(
                self.task_id,
                aid,
                reason,
                **(
                    {"expected_derive_round": expected_derive_round}
                    if expected_derive_round is not None
                    else {}
                ),
            )
        except RemoteError as error:
            if error.status != 422:
                raise

    async def _conclude_running(self, reason: str) -> None:
        state = await self.service.state(self.task_id)
        for aid, agent in state["agents"].items():
            if self._stopping:
                return
            if agent["status"] == "running":
                await self._conclude(
                    aid,
                    reason,
                    int(agent.get("derive_round") or 1) if agent["task_type"] == "derive" else None,
                )

    async def cancel(
        self,
        aid: str,
        reason: str,
        expected_derive_round: int | None = None,
        *,
        expected_started_at: str | None = None,
    ) -> bool:
        task = self.tasks.get(aid)
        if task is None:
            if reason != "runtime_restart" and expected_started_at is not None:
                await self.board_runner.stop_agent(
                    self.task_id,
                    aid,
                    reason,
                    started_at=expected_started_at,
                    derive_round=expected_derive_round,
                )
            # A vanished local waiter does not prove that the durable Runner has exited.
            return True
        if expected_derive_round is not None and self.task_rounds.get(aid) != expected_derive_round:
            return False
        if expected_started_at is not None and self.task_executions.get(aid) != execution_timestamp(
            expected_started_at
        ):
            return False
        started = self.task_starts.get(aid)
        if started is not None and not task.done():
            # Cancellation before the first coroutine step skips the runner's durable finish.
            await started.wait()
        if reason != "runtime_restart":
            started_at = self.task_executions.get(aid)
            assert started_at is not None
            await self.board_runner.stop_agent(
                self.task_id,
                aid,
                reason,
                started_at=started_at,
                derive_round=self.task_rounds.get(aid),
            )
        if not task.done():
            task.cancel(reason)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logger.error("Agent %s ended during cancellation: %s", aid, type(error).__name__)
        return True

    async def shutdown(self, reason: str = "runtime_restart") -> None:
        self.request_stop(reason)
        await asyncio.gather(*(self.cancel(aid, self._stop_reason) for aid in tuple(self.tasks)))
