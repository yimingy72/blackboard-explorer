"""Cancel stale agent coroutines; the runner performs their single durable finish."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from bbx_contracts.models import Params

from bbx_runtime.clients import BlackboardClient
from bbx_runtime.clients.blackboard import RemoteError

from .executor import ActionExecutor

SWEEP_SECONDS = 30.0


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
                await self._finish_stale(aid, "grace_timeout")
            elif heartbeat is not None and instant >= heartbeat + timedelta(
                minutes=self.params.heartbeat_timeout
            ):
                await self._finish_stale(aid, "heartbeat")

    async def _finish_stale(self, aid: str, reason: str) -> None:
        if await self.executor.cancel(aid, reason):
            return
        try:
            await self.service.finish_agent(
                self.executor.task_id,
                aid,
                {"accepted": False, "reason": f"Agent {reason} timeout"},
                reason,
            )
        except RemoteError as error:
            if error.status != 422:
                raise

    async def run(self) -> None:
        while not self._stop.is_set():
            await self.check_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SWEEP_SECONDS)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
