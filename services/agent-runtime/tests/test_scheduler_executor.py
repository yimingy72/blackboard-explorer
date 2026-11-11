"""Action execution preserves claim ownership and agent completion order."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from bbx_runtime.clients import BlackboardClient, RemoteError
from bbx_runtime.scheduler.actions import (
    EnterClosing,
    Fail,
    SpawnDerive,
    SpawnExplore,
    SystemClose,
)
from bbx_runtime.scheduler.executor import ActionExecutor, BoardRunner

STARTED_AT = "2026-10-17T12:01:00Z"


class WaitingRunner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_reason: str | None = None
        self.kwargs: dict = {}

    async def stop_agent(self, *_args, **_kwargs) -> None:
        pass

    async def run_agent(self, *_args, **kwargs) -> None:
        self.kwargs = kwargs
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            self.cancel_reason = str(error.args[0]) if error.args else None
            raise
        raise AssertionError("Waiting runner must be cancelled")


def setup_executor():
    history: list[tuple[str, str]] = []

    async def conclude(_tid, aid, reason):
        history.append(("conclude", f"{aid}:{reason}"))

    async def transition(_tid, status, reason):
        history.append(("transition", f"{status}:{reason}"))

    service = SimpleNamespace(
        register_agent=AsyncMock(return_value={"agent_id": "agent-1", "started_at": STARTED_AT}),
        claim_for=AsyncMock(return_value=[]),
        finish_agent=AsyncMock(return_value=[]),
        state=AsyncMock(
            return_value={
                "agents": {
                    "agent-1": {"status": "running", "task_type": "explore"},
                    "agent-2": {"status": "concluding", "task_type": "explore"},
                }
            }
        ),
        conclude=AsyncMock(side_effect=conclude),
        transition=AsyncMock(side_effect=transition),
        system_close=AsyncMock(return_value=[]),
    )
    runner = WaitingRunner()
    executor = ActionExecutor("task", cast(BlackboardClient, service), board_runner=runner)
    return executor, service, runner, history


async def test_stale_round_cancel_cannot_cancel_reactivated_derive():
    executor, _, _, _ = setup_executor()
    running = asyncio.create_task(asyncio.Event().wait())
    executor.tasks["agent-1"] = running  # type: ignore[assignment]
    executor.task_rounds["agent-1"] = 2
    executor.task_executions["agent-1"] = STARTED_AT
    assert await executor.cancel("agent-1", "heartbeat", expected_derive_round=1) is False
    assert not running.done()
    assert await executor.cancel("agent-1", "heartbeat", expected_derive_round=2) is True


async def test_board_runner_cancels_by_original_identity_without_finishing_locally():
    _, service, _, _ = setup_executor()
    started = asyncio.Event()

    async def waiting(*_args, **_kwargs):
        started.set()
        await asyncio.Future()

    board = SimpleNamespace(run_agent=AsyncMock(side_effect=waiting), stop_agent=AsyncMock())
    service.register_agent.return_value = {
        "agent_id": "agent-1",
        "started_at": "2026-10-17T12:01:00Z",
    }
    executor = ActionExecutor(
        "task", cast(BlackboardClient, service), board_runner=cast(BoardRunner, board)
    )
    await executor.execute([SpawnExplore(intent_id="I1")])
    await asyncio.wait_for(started.wait(), 1)
    service.claim_for.assert_awaited_once_with("task", "I1", "agent-1")
    board.run_agent.assert_awaited_once_with(
        "task", "agent-1", started_at="2026-10-17T12:01:00+00:00", derive_round=None
    )
    assert not await executor.cancel(
        "agent-1", "heartbeat", expected_started_at="2026-10-17T12:00:00Z"
    )
    board.stop_agent.assert_not_awaited()
    await executor.shutdown()
    board.stop_agent.assert_not_awaited()
    assert await executor.cancel("agent-1", "heartbeat", expected_started_at="2026-10-17T12:01:00Z")
    board.stop_agent.assert_awaited_once_with(
        "task", "agent-1", "heartbeat", started_at="2026-10-17T12:01:00Z", derive_round=None
    )
    service.finish_agent.assert_not_awaited()


async def test_stale_execution_cancel_cannot_cancel_reactivated_explore():
    executor, service, runner, _ = setup_executor()
    current = "2026-10-17T12:01:00Z"
    service.register_agent.return_value["started_at"] = current
    await executor.execute([SpawnExplore(intent_id="I1")])
    await asyncio.wait_for(runner.started.wait(), 2)
    assert runner.kwargs["started_at"] == "2026-10-17T12:01:00+00:00"
    assert not await executor.cancel(
        "agent-1", "heartbeat", expected_started_at="2026-10-17T12:00:00Z"
    )
    assert runner.cancel_reason is None
    assert await executor.cancel("agent-1", "heartbeat", expected_started_at=current)


async def test_lost_claim_finishes_registration_without_starting_agent():
    executor, service, runner, _ = setup_executor()
    service.claim_for.side_effect = RemoteError(422, "already claimed")
    await executor.execute([SpawnExplore(intent_id="I1")])
    service.finish_agent.assert_awaited_once_with(
        "task",
        "agent-1",
        {"accepted": False, "reason": "claim_lost"},
        "normal",
        expected_started_at=STARTED_AT,
    )
    assert not runner.started.is_set() and executor.tasks == {}


async def test_derive_registration_carries_phase_and_skips_only_stale_phase():
    executor, service, runner, _ = setup_executor()
    service.register_agent.side_effect = RemoteError(422, "Derive phase changed", "stale_derive")
    await executor.execute([SpawnDerive(parallel=True)])
    service.register_agent.assert_awaited_once_with(
        "task",
        "derive",
        is_seed=False,
        intent_id=None,
        close_mode=None,
        derive_parallel=True,
        derive_review=False,
    )
    assert not runner.started.is_set() and executor.tasks == {}

    service.register_agent.reset_mock(side_effect=True)
    service.register_agent.side_effect = RemoteError(422, "Other validation error", "invalid")
    with pytest.raises(RemoteError):
        await executor.execute([SpawnDerive()])
    service.register_agent.assert_awaited_once_with(
        "task",
        "derive",
        is_seed=False,
        intent_id=None,
        close_mode=None,
        derive_parallel=False,
        derive_review=False,
    )


async def test_review_registration_carries_required_review_flag():
    executor, service, runner, _ = setup_executor()
    service.register_agent.side_effect = RemoteError(422, "Review phase changed", "stale_derive")
    await executor.execute([SpawnDerive(review=True)])
    service.register_agent.assert_awaited_once_with(
        "task",
        "derive",
        is_seed=False,
        intent_id=None,
        close_mode=None,
        derive_parallel=False,
        derive_review=True,
    )
    assert not runner.started.is_set()


@pytest.mark.parametrize("action", [SpawnDerive(), SpawnExplore(seed=True)])
async def test_review_busy_registration_waits_for_next_tick(action):
    executor, service, runner, _ = setup_executor()
    service.register_agent.side_effect = RemoteError(
        409, "Review in progress", "agent_review_active"
    )
    await executor.execute([action])
    assert not runner.started.is_set() and executor.tasks == {}
    service.register_agent.side_effect = None
    await executor.execute([action])
    await runner.started.wait()
    assert service.register_agent.await_count == 2
    await executor.cancel("agent-1", "test_complete")


async def test_unrelated_registration_conflict_still_propagates():
    executor, service, _, _ = setup_executor()
    service.register_agent.side_effect = RemoteError(409, "Other conflict", "different_conflict")
    with pytest.raises(RemoteError):
        await executor.execute([SpawnDerive()])


async def test_closing_transitions_then_concludes_and_failure_concludes_then_transitions():
    executor, _service, _runner, history = setup_executor()
    await executor.execute([EnterClosing("accepted")])
    assert history == [
        ("transition", "closing:accepted"),
        ("conclude", "agent-1:closing"),
    ]
    history.clear()
    await executor.execute([Fail("failed reason")])
    assert history == [
        ("conclude", "agent-1:failed"),
        ("transition", "failed:failed reason"),
    ]


async def test_stale_acceptance_skips_conclude_and_retries_on_next_decision():
    executor, service, _runner, history = setup_executor()
    service.transition.side_effect = RemoteError(422, "Board changed", "stale_acceptance")
    await executor.execute([EnterClosing("accepted"), SpawnExplore(seed=True)])
    service.transition.assert_awaited_once_with("task", "closing", "accepted")
    service.conclude.assert_not_awaited()
    service.register_agent.assert_not_awaited()
    assert history == []

    service.transition.side_effect = None
    await executor.execute([EnterClosing("accepted")])
    service.conclude.assert_awaited_once_with("task", "agent-1", "closing")


@pytest.mark.parametrize(
    ("reason", "code"), [("accepted", "invalid"), ("terminated", "stale_acceptance")]
)
async def test_other_transition_errors_still_propagate(reason: str, code: str):
    executor, service, _runner, _history = setup_executor()
    service.transition.side_effect = RemoteError(422, "Other validation error", code)
    with pytest.raises(RemoteError):
        await executor.execute([EnterClosing(reason)])
    service.conclude.assert_not_awaited()


async def test_request_stop_finishes_registered_agent_before_launch():
    executor, service, runner, _ = setup_executor()
    entered, release = asyncio.Event(), asyncio.Event()

    async def register(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"agent_id": "agent-1", "started_at": STARTED_AT}

    service.register_agent.side_effect = register
    executing = asyncio.create_task(executor.execute([SpawnExplore(seed=True)]))
    await asyncio.wait_for(entered.wait(), 2)
    executor.request_stop("runtime_restart")
    release.set()
    await asyncio.wait_for(executing, 2)
    service.finish_agent.assert_awaited_once_with(
        "task",
        "agent-1",
        {"accepted": False, "reason": "runtime_stop_before_launch"},
        "runtime_restart",
        expected_started_at=STARTED_AT,
    )
    assert not runner.started.is_set() and executor.tasks == {}


async def test_request_stop_skips_remaining_actions():
    executor, service, _runner, _ = setup_executor()

    async def close(*_args):
        executor.request_stop("runtime_restart")
        return []

    service.system_close.side_effect = close
    await executor.execute([SystemClose("I1"), SpawnExplore(seed=True)])
    service.system_close.assert_awaited_once_with("task", "I1")
    service.register_agent.assert_not_awaited()
