"""Per-task event wakeups with a periodic decision fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from bbx_contracts.models import Params

from bbx_runtime.clients import BlackboardClient

from .decision import decide
from .executor import ActionExecutor
from .sweeper import Sweeper

logger = logging.getLogger(__name__)
DEBOUNCE_SECONDS = 0.5
FALLBACK_SECONDS = 2.0
RECONNECT_SECONDS = 1.0


def utc_now() -> datetime:
    return datetime.now(UTC)


class SchedulerLoop:
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
        self._tick_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._listener: asyncio.Task[None] | None = None
        self._sweeper = Sweeper(executor, service, params, now)
        self._sweeper_task: asyncio.Task[None] | None = None
        self._last_version = 0
        self._event_serial = 0
        self._last_tick_at = time.monotonic()
        self._shutdown_done = False
        self._stop_reason = "runtime_restart"

    @property
    def task_id(self) -> str:
        return self.executor.task_id

    async def tick(self) -> None:
        async with self._tick_lock:
            if self._stop.is_set():
                return
            state = await self.service.state(self.task_id)
            if state["task"]["status"] in {"finished", "failed", "stopped"}:
                return
            await self.executor.execute(decide(state, self.params, self.now()))
            self._last_tick_at = time.monotonic()

    async def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                async for event in self.service.stream(self.task_id, since=self._last_version):
                    version = int(event["version"])
                    if version > self._last_version:
                        self._last_version = version
                        self._event_serial += 1
                        self._wake.set()
                    if self._stop.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Task %s event stream reconnecting: %s", self.task_id, type(error).__name__
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RECONNECT_SECONDS)
            except TimeoutError:
                pass

    async def run(self) -> None:
        self._listener = asyncio.create_task(self._listen(), name=f"{self.task_id}/events")
        self._sweeper_task = asyncio.create_task(
            self._sweeper.run(), name=f"{self.task_id}/sweeper"
        )
        try:
            await self.tick()
            while not self._stop.is_set():
                if self._sweeper_task is not None and self._sweeper_task.done():
                    await self._sweeper_task
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=FALLBACK_SECONDS)
                except TimeoutError:
                    await self.tick()
                    continue
                self._wake.clear()
                if self._stop.is_set():
                    break
                while not self._stop.is_set():
                    seen = self._event_serial
                    remaining = self._last_tick_at + FALLBACK_SECONDS - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=min(DEBOUNCE_SECONDS, remaining)
                        )
                    except TimeoutError:
                        pass
                    if seen == self._event_serial:
                        break
                await self.tick()
        finally:
            await self.stop(self._stop_reason)

    def request_stop(self, reason: str = "runtime_restart") -> None:
        if not self._stop.is_set() or reason == "runtime_restart":
            self._stop_reason = reason
        self._stop.set()
        self._wake.set()
        self.executor.request_stop(self._stop_reason)
        self._sweeper.stop()

    async def stop(self, reason: str = "runtime_restart") -> None:
        self.request_stop(reason)
        listener = self._listener
        if listener is not None and listener is not asyncio.current_task() and not listener.done():
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
        sweeper_task = self._sweeper_task
        if sweeper_task is not None and sweeper_task is not asyncio.current_task():
            if not sweeper_task.done():
                sweeper_task.cancel()
            try:
                await sweeper_task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                logger.error("Task %s sweeper stopped: %s", self.task_id, type(error).__name__)
        async with self._shutdown_lock:
            if self._shutdown_done:
                return
            async with self._tick_lock:
                await self.executor.shutdown(self._stop_reason)
            self._shutdown_done = True
