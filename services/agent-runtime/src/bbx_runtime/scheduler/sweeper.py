"""Cancel stale agent coroutines; the runner performs their single durable finish."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from bbx_contracts.models import Params

from bbx_runtime.clients import BlackboardClient
from bbx_runtime.clients.blackboard import RemoteError, is_control_transport_error

from .executor import ActionExecutor

SWEEP_SECONDS = 30.0
logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    stamp = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


class Sweeper:
    def __init__(
        self,
        executor: ActionExecutor,
        service: BlackboardClient,
        params: Params,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.executor = executor
        self.service = service
        self.params = params
        self.now = now
        self._stop = asyncio.Event()

    async def check_once(self) -> None:
        state = await self.service.state(self.executor.task_id)
        instant = _time(self.now())
        assert instant is not None
        for aid, agent in state["agents"].items():
            if agent["status"] not in {"running", "concluding"}:
                continue
            concluded = _time(agent.get("conclude_requested_at"))
            heartbeat = _time(agent.get("last_heartbeat_at"))
            if (
                agent["status"] == "concluding"
                and concluded is not None
                and instant >= concluded + timedelta(minutes=self.params.grace_timeout)
            ):
                await self._finish_stale(aid, "grace_timeout", agent, state.get("task", {}))
            elif heartbeat is not None and instant >= heartbeat + timedelta(
                minutes=self.params.heartbeat_timeout
            ):
                await self._finish_stale(aid, "heartbeat", agent, state.get("task", {}))

    async def _finish_stale(self, aid: str, reason: str, agent: dict, task: dict) -> None:
        expected_round = (
            int(agent.get("derive_round") or 1) if agent.get("task_type") == "derive" else None
        )
        if await self.executor.cancel(
            aid, reason, *([expected_round] if expected_round is not None else [])
        ):
            return
        reconcile = getattr(getattr(self.executor, "runner", None), "reconcile_final_receipt", None)
        if asyncio.iscoroutinefunction(reconcile) and await reconcile(
            self.executor.task_id, aid, agent, task
        ):
            return
        finish_options: dict[str, Any] = (
            {"expected_derive_round": expected_round} if expected_round is not None else {}
        )
        try:
            await self.service.finish_agent(
                self.executor.task_id,
                aid,
                {"accepted": False, "reason": f"Agent {reason} timeout"},
                reason,
                **finish_options,
            )
        except RemoteError as error:
            if error.status != 422:
                raise

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_once()
            except (httpx.TransportError, ConnectionError, RemoteError) as error:
                if not is_control_transport_error(error):
                    raise
                logger.warning(
                    "Task %s sweeper reconnecting phase=sweep error=%s",
                    self.executor.task_id,
                    type(error).__name__,
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SWEEP_SECONDS)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
