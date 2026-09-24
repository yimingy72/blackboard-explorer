"""Action execution preserves claim ownership and agent completion order."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

from bbx_runtime.clients import BlackboardClient, RemoteError
from bbx_runtime.execenv import ExecEnvHandle, ExecEnvManager
from bbx_runtime.runner import AgentRunner, RunResult
from bbx_runtime.scheduler.actions import EnterClosing, Fail, SpawnExplore, SystemClose
from bbx_runtime.scheduler.executor import ActionExecutor


class WaitingRunner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_reason: str | None = None
        self.kwargs: dict = {}

    async def run_agent(self, *_args, **kwargs) -> RunResult:
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
        register_agent=AsyncMock(return_value={"agent_id": "agent-1", "token": "issued"}),
        claim_for=AsyncMock(return_value=[]),
        finish_agent=AsyncMock(return_value=[]),
        state=AsyncMock(
            return_value={
                "agents": {
                    "agent-1": {"status": "running"},
                    "agent-2": {"status": "concluding"},
                }
            }
        ),
        conclude=AsyncMock(side_effect=conclude),
        transition=AsyncMock(side_effect=transition),
        system_close=AsyncMock(return_value=[]),
    )
    manager = SimpleNamespace(create_user=AsyncMock(return_value={}))
    runner = WaitingRunner()
    handle = ExecEnvHandle(
        task_id=uuid4(), container_id="envd", name="envd", base_url="http://envd", token="test"
    )
    executor = ActionExecutor(
        "task",
        cast(BlackboardClient, service),
        cast(ExecEnvManager, manager),
        cast(AgentRunner, runner),
        handle,
    )
    return executor, service, manager, runner, history


async def test_spawn_claims_before_user_creation_and_cancel_forwards_reason():
    executor, service, manager, runner, _ = setup_executor()
    await executor.execute([SpawnExplore(intent_id="I1")])
    await asyncio.wait_for(runner.started.wait(), 2)
    service.register_agent.assert_awaited_once_with(
        "task", "explore", is_seed=False, close_mode=None
    )
    service.claim_for.assert_awaited_once_with("task", "I1", "agent-1")
    manager.create_user.assert_awaited_once()
    assert runner.kwargs["agent_token"] == "issued"
    assert "agent-1" in executor.tasks
    assert await executor.cancel("agent-1", "heartbeat")
    assert runner.cancel_reason == "heartbeat"
    assert executor.tasks == {}


async def test_lost_claim_finishes_registration_without_starting_agent():
    executor, service, manager, runner, _ = setup_executor()
    service.claim_for.side_effect = RemoteError(422, "already claimed")
    await executor.execute([SpawnExplore(intent_id="I1")])
    service.finish_agent.assert_awaited_once_with(
        "task", "agent-1", {"accepted": False, "reason": "claim_lost"}, "normal"
    )
    manager.create_user.assert_not_awaited()
    assert not runner.started.is_set() and executor.tasks == {}


async def test_closing_transitions_then_concludes_and_failure_concludes_then_transitions():
    executor, _service, _manager, _runner, history = setup_executor()
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


async def test_request_stop_finishes_registered_agent_before_launch():
    executor, service, manager, runner, _ = setup_executor()
    entered, release = asyncio.Event(), asyncio.Event()

    async def register(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"agent_id": "agent-1", "token": "issued"}

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
    )
    manager.create_user.assert_not_awaited()
    assert not runner.started.is_set() and executor.tasks == {}


async def test_request_stop_while_creating_user_releases_claim_without_launch():
    executor, service, manager, runner, _ = setup_executor()
    entered, release = asyncio.Event(), asyncio.Event()

    async def create_user(*_args):
        entered.set()
        await release.wait()
        return {}

    manager.create_user.side_effect = create_user
    executing = asyncio.create_task(executor.execute([SpawnExplore(intent_id="I1")]))
    await asyncio.wait_for(entered.wait(), 2)
    executor.request_stop("runtime_restart")
    release.set()
    await asyncio.wait_for(executing, 2)
    service.claim_for.assert_awaited_once()
    service.finish_agent.assert_awaited_once()
    assert service.finish_agent.call_args.args[-1] == "runtime_restart"
    assert not runner.started.is_set() and executor.tasks == {}


async def test_request_stop_skips_remaining_actions():
    executor, service, _manager, _runner, _ = setup_executor()

    async def close(*_args):
        executor.request_stop("runtime_restart")
        return []

    service.system_close.side_effect = close
    await executor.execute([SystemClose("I1"), SpawnExplore(seed=True)])
    service.system_close.assert_awaited_once_with("task", "I1")
    service.register_agent.assert_not_awaited()
