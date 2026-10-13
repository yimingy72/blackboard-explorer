"""Closing converges with bounded writes and preserves unconfirmed work."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import bbx_runtime.ctf.coordinator as control
import httpx
import pytest
from agent_framework import AgentSession
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.ctf.execution import DrainPending, UnknownCommandOutcome
from bbx_runtime.ctf.runner import CtfRunner


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(control, "monotonic", value)
    return value


class Store:
    def __init__(self, *, unresolved=False, finished=False, stopping=False):
        self.task: dict[str, Any] = {
            "status": "running",
            "run_number": 2,
            "ctf_control": {
                "phase": "closing",
                "unresolved_checkpoints": ["turn"] if unresolved else [],
            },
            "ctf_conclusion": {"end_reason": "goal_claimed"},
        }
        self.turn = {
            "id": "turn",
            "agent_id": "member",
            "generation": 3,
            "purpose": "execution",
            "status": "finished" if finished or not (unresolved or stopping) else "running",
        }
        self.members = (
            [
                {
                    "id": "member",
                    "generation": 3,
                    "current_turn_id": None if finished else "turn",
                    "pending_operation": {"kind": "stop", "request_id": "stop"}
                    if stopping
                    else None,
                }
            ]
            if unresolved or stopping
            else []
        )
        self.operations: list[tuple[str, dict[str, Any]]] = []
        self.unavailable: str | None = None

    async def runtime_state(self, _):
        if self.unavailable == "state":
            raise httpx.ReadError("synthetic unavailable database")
        return deepcopy(
            {
                "task": self.task,
                "members": self.members,
                # The production scheduling endpoint exposes running turns only.
                "turns": [self.turn] if self.turn["status"] == "running" else [],
            }
        )

    def retire(self):
        barrier = self.task["ctf_control"]["unresolved_checkpoints"]
        if barrier:
            self.task["ctf_control"]["checkpoint_failures"] = {
                "turn": {"purpose": "execution", "run_number": 2}
            }
            barrier.clear()

    async def runtime(self, _, operation, **body):
        self.operations.append((operation, body))
        if self.unavailable == operation:
            raise httpx.ReadError("synthetic unavailable database")
        if operation == "complete_member_operation":
            assert body["proof"]["drained"]
            if self.task["ctf_control"]["unresolved_checkpoints"]:
                assert body.get("checkpoint_failure")
                self.retire()
            self.members[0]["pending_operation"] = None
            self.members[0]["current_turn_id"] = None
            self.turn["status"] = "finished"
        elif operation == "recover":
            for member in self.members:
                if member["pending_operation"]:
                    continue
                if member["current_turn_id"]:
                    self.turn["status"] = "finished"
                    member["current_turn_id"] = None
                    member["generation"] += 1
                    self.retire()
        elif operation == "finalize_close":
            if any(member["pending_operation"] for member in self.members):
                raise RemoteError(409, "synthetic pending member operation")
            if self.members and self.turn["status"] == "running":
                raise RemoteError(409, "synthetic running writer")
            self.retire()
            failed = bool(body.get("checkpoint_failure")) or bool(
                self.task["ctf_control"].get("checkpoint_failures")
            )
            self.task["ctf_control"]["phase"] = "closed"
            self.task["status"] = "failed" if failed else "finished"
            if failed:
                self.task["ctf_conclusion"]["end_reason"] = "system_failure"
        return {}


class Envd:
    def __init__(self, outcome=True):
        self.outcome = outcome
        self.drains = 0
        self.stops = 0

    async def drain(self, _):
        self.drains += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def stop_member(self, _, member):
        self.stops += 1
        return {"drained": True, "generation": member["generation"], "boot_id": "fake"}


class Pending:
    failed = True

    def __init__(self, error):
        self.session = AgentSession()
        self.error = error
        self.saves = 0

    async def save(self):
        self.saves += 1
        raise self.error


def coordinator(store, envd=None, checkpoint=None):
    runner = CtfRunner(cast(CtfClient, store), cast(Any, SimpleNamespace()))
    if checkpoint is not None:
        runner.pending_checkpoints[("task", "member")] = (
            checkpoint,
            {"id": "member"},
            store.turn.copy(),
            "synthetic answer",
        )
    instance = CtfCoordinator(
        cast(Any, None), cast(Any, SimpleNamespace()), runner=runner, envd=envd
    )
    instance.service = cast(CtfClient, store)
    return instance, runner


async def advance_to_close(instance, clock, *, ticks=12):
    for _ in range(ticks):
        if not await instance.tick("task"):
            return
        clock.value = instance.close_retries["task"].next_retry
    pytest.fail("Closing did not converge within its bounded retry budget")


async def test_healthy_close_finishes_immediately_without_checkpoint_failure(clock):
    store, envd = Store(), Envd()
    instance, _ = coordinator(store, envd)
    assert await instance.tick("task") is False
    assert [operation for operation, _ in store.operations] == ["recover", "finalize_close"]
    assert store.task["status"] == "finished"
    assert "checkpoint_failure" not in store.operations[-1][1]
    assert envd.drains == 1


async def test_legacy_finished_barrier_retires_once_and_closes_as_failure(clock, caplog):
    store = Store(unresolved=True, finished=True)
    pending = Pending(RemoteError(409, "synthetic finished writer"))
    instance, runner = coordinator(store, Envd(), pending)
    await advance_to_close(instance, clock)
    assert store.task["status"] == "failed"
    assert store.task["ctf_conclusion"]["end_reason"] == "system_failure"
    assert not runner.pending_checkpoints
    assert pending.saves == 1
    assert caplog.text.count("CTF checkpoint retry discarded") == 1
    assert len(store.operations) == 2


async def test_running_barrier_remains_on_first_tick_without_drain_or_finalization(clock):
    store, envd = Store(unresolved=True), Envd()
    instance, _ = coordinator(store, envd)
    assert await instance.tick("task") is True
    assert not store.operations
    assert envd.drains == 0
    assert store.turn["status"] == "running"


@pytest.mark.parametrize("expiration", ["attempts", "seconds"])
async def test_close_retry_limit_fails_without_discarding_local_snapshot(clock, expiration):
    store = Store(unresolved=True)
    pending = Pending(httpx.ReadError("synthetic failed snapshot"))
    instance, runner = coordinator(store, Envd(), pending)
    assert await instance.tick("task") is True
    if expiration == "seconds":
        clock.value = control.CLOSE_TIMEOUT_SECONDS
    else:
        clock.value = instance.close_retries["task"].next_retry
    await advance_to_close(instance, clock)
    assert store.task["status"] == "failed"
    assert store.operations[-1][1]["checkpoint_failure"]
    assert runner.pending_checkpoints[("task", "member")][0] is pending
    assert pending.saves <= control.CLOSE_RETRY_LIMIT
    assert store.task["ctf_control"]["checkpoint_failures"]


async def test_backoff_prevents_repeated_checkpoint_writes_between_retry_times(clock):
    store = Store(unresolved=True)
    pending = Pending(httpx.ReadError("synthetic failed snapshot"))
    instance, _ = coordinator(store, Envd(), pending)
    assert await instance.tick("task") is True
    for _ in range(5):
        clock.value += 0.1
        assert await instance.tick("task") is True
    assert pending.saves == 1
    assert not store.operations


@pytest.mark.parametrize("outcome", [False, DrainPending(), UnknownCommandOutcome("unknown")])
async def test_unproven_drain_never_recovers_or_finalizes_even_after_timeout(clock, outcome):
    store, envd = Store(), Envd(outcome)
    instance, _ = coordinator(store, envd)
    for _ in range(control.CLOSE_RETRY_LIMIT + 3):
        assert await instance.tick("task") is True
        clock.value = instance.close_retries["task"].next_retry
    assert not store.operations
    assert store.task["ctf_control"]["phase"] == "closing"
    assert store.task["status"] == "running"


@pytest.mark.parametrize("failure", ["state", "recover", "finalize_close"])
async def test_database_failure_never_manufactures_terminal_state_or_drops_snapshot(clock, failure):
    store = Store(unresolved=True)
    pending = Pending(httpx.ReadError("synthetic failed snapshot"))
    instance, runner = coordinator(store, Envd(), pending)
    await instance.tick("task")
    clock.value = control.CLOSE_TIMEOUT_SECONDS
    store.unavailable = failure
    if failure == "state":
        with pytest.raises(httpx.ReadError):
            await instance.tick("task")
    else:
        assert await instance.tick("task") is True
    assert store.task["ctf_control"]["phase"] == "closing"
    assert store.task["status"] == "running"
    assert runner.pending_checkpoints[("task", "member")][0] is pending


async def test_restart_uses_persistent_close_timestamp_for_immediate_escalation(clock):
    store = Store(unresolved=True)
    store.task["ctf_conclusion"]["requested_at"] = (
        datetime.now(UTC) - timedelta(seconds=control.CLOSE_TIMEOUT_SECONDS + 5)
    ).isoformat()
    pending = Pending(httpx.ReadError("synthetic failed snapshot"))
    instance, runner = coordinator(store, Envd(), pending)
    assert await instance.tick("task") is False
    assert pending.saves == 0
    assert runner.pending_checkpoints
    assert store.task["status"] == "failed"


async def test_pending_stop_and_failed_checkpoint_complete_after_proven_drain(clock):
    store, envd = Store(unresolved=True, stopping=True), Envd()
    pending = Pending(httpx.ReadError("synthetic failed snapshot"))
    instance, runner = coordinator(store, envd, pending)
    await advance_to_close(instance, clock)
    assert store.task["status"] == "failed"
    assert store.members[0]["pending_operation"] is None
    assert envd.drains > 0 and envd.stops == 1
    completion = next(
        body for operation, body in store.operations if operation == "complete_member_operation"
    )
    assert completion["proof"]["drained"] and completion["checkpoint_failure"]
    assert runner.pending_checkpoints[("task", "member")][0] is pending


async def test_closing_does_not_cancel_a_writer_again_during_its_shielded_save(clock, monkeypatch):
    store = Store()
    instance, _ = coordinator(store, Envd())
    release = asyncio.Event()
    saving = asyncio.Event()
    cancelled_again = asyncio.Event()
    save_tasks = []

    async def writer():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            save_task = asyncio.create_task(release.wait())
            save_tasks.append(save_task)
            saving.set()
            try:
                await asyncio.shield(save_task)
            except asyncio.CancelledError:
                cancelled_again.set()
                raise

    async def elapsed_join(futures, *, timeout):
        assert timeout == 5
        await asyncio.sleep(0)
        return {future for future in futures if future.done()}, {
            future for future in futures if not future.done()
        }

    monkeypatch.setattr(control.asyncio, "wait", elapsed_join)
    future = asyncio.create_task(writer())
    await asyncio.sleep(0)
    instance.active[("task", "member")] = (future, store.turn)
    try:
        assert await instance.tick("task") is True
        assert saving.is_set()
        clock.value = instance.close_retries["task"].next_retry
        assert await instance.tick("task") is True
        assert not cancelled_again.is_set()
        assert not future.done()
        assert not store.operations
    finally:
        release.set()
        await asyncio.gather(future, return_exceptions=True)
        await asyncio.gather(*save_tasks)
