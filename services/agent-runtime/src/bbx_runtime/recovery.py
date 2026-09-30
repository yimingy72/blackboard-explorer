"""Coordinate bounded recovery using workers' real model requests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

RECOVERY_SECONDS = 120.0
RECOVERY_BACKOFF = (2.0, 4.0)
MAX_AGENT_RECOVERIES = 2
CONTROL_POLL_SECONDS = 1.0


class RecoveryExhausted(RuntimeError):
    def __init__(self, metadata: dict[str, Any]) -> None:
        super().__init__("Model connection recovery exhausted")
        self.bbx_model_error = metadata.copy()


@dataclass
class RecoveryEpisode:
    deadline: float
    next_probe_at: float
    metadata: dict[str, Any]
    probes: int = 0
    probe_ticket: int | None = None
    final_dispatched: bool = False


@dataclass
class ModelCallPermit:
    ticket: int
    episode: RecoveryEpisode | None = None
    started_serial: int = 0


def recoverable_transport(metadata: dict[str, Any]) -> bool:
    return (
        metadata.get("category") in {"connection", "timeout"}
        and metadata.get("transport_type") != "LocalProtocolError"
    )


class ModelRecoveryGate:
    def __init__(self) -> None:
        self.episode: RecoveryEpisode | None = None
        self._serial = 0
        self._started_serial = 0
        self._healthy_through = 0
        self._changed = asyncio.Event()

    @property
    def paused(self) -> bool:
        return self.episode is not None and monotonic() < self.episode.deadline

    def _notify(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def _permit(self, episode: RecoveryEpisode | None = None) -> ModelCallPermit:
        self._serial += 1
        return ModelCallPermit(self._serial, episode)

    def started(self, permit: ModelCallPermit) -> None:
        # Admission reserves the probe; model start follows control preflight.
        self._started_serial += 1
        permit.started_serial = self._started_serial

    async def acquire(
        self, check_control: Callable[[], Awaitable[None]], *, deadline: float | None = None
    ) -> ModelCallPermit:
        if not self.paused:
            return self._permit()
        # A waiter must not roll its expired episode into another one. Only a new
        # request admitted after cooldown can begin the next failure window.
        observed = self.episode if self.paused else None
        while True:
            await check_control()
            episode = self.episode
            if episode is None:
                return self._permit()
            now = monotonic()
            if deadline is not None and now >= deadline:
                raise RecoveryExhausted(episode.metadata)
            if now >= episode.deadline:
                if observed is episode:
                    raise RecoveryExhausted(episode.metadata)
                return self._permit()
            observed = episode
            if episode.probe_ticket is None:
                if episode.probes >= len(RECOVERY_BACKOFF):
                    raise RecoveryExhausted(episode.metadata)
                if now >= episode.next_probe_at:
                    permit = self._permit(episode)
                    episode.probes += 1
                    episode.probe_ticket = permit.ticket
                    return permit
            delay = min(CONTROL_POLL_SECONDS, episode.deadline - now)
            if deadline is not None:
                delay = min(delay, max(0.001, deadline - now))
            if episode.probe_ticket is None:
                delay = min(delay, max(0.001, episode.next_probe_at - now))
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), timeout=delay)
            except TimeoutError:
                pass

    def succeeded(self, permit: ModelCallPermit) -> None:
        # All requests already started when success arrives are older evidence.
        # Their late errors cannot reopen an outage that this response disproved.
        self._healthy_through = self._started_serial
        self.episode = None
        self._notify()

    def failed(self, permit: ModelCallPermit, metadata: dict[str, Any]) -> RecoveryEpisode | None:
        self.release(permit)
        if permit.started_serial <= self._healthy_through:
            return None
        now = monotonic()
        episode = self.episode
        if episode is None or (now >= episode.deadline and permit.episode is not episode):
            episode = RecoveryEpisode(
                deadline=now + RECOVERY_SECONDS,
                next_probe_at=now + RECOVERY_BACKOFF[0],
                metadata=metadata.copy(),
            )
            self.episode = episode
        else:
            episode.metadata = metadata.copy()
            if permit.episode is episode:
                episode.next_probe_at = now + RECOVERY_BACKOFF[-1]
        self._notify()
        return episode

    def release(self, permit: ModelCallPermit) -> None:
        episode = permit.episode
        if episode is not None and episode.probe_ticket == permit.ticket:
            episode.probe_ticket = None
            self._notify()

    def abort(self, permit: ModelCallPermit, metadata: dict[str, Any]) -> None:
        self.release(permit)
        if permit.episode is self.episode and self.episode is not None:
            self.episode.metadata = metadata.copy()
            self.episode.probes = len(RECOVERY_BACKOFF)
            self._notify()
