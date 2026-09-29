"""External sweeper and shutdown reasons reach one durable finish."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agent_framework import ResponseStream
from bbx_contracts.models import Params
from bbx_contracts.profile import load_profile
from bbx_runtime.runner import AgentRunner
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient


@pytest.mark.parametrize(
    "cancel_reason,expected",
    [
        ("heartbeat", "heartbeat"),
        ("grace_timeout", "grace_timeout"),
        ("runtime_restart", "runtime_restart"),
        ("unknown", "grace_timeout"),
    ],
)
async def test_runner_finishes_once_with_allowed_cancel_reason(
    monkeypatch, cancel_reason, expected
):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    started = asyncio.Event()
    service = SimpleNamespace(
        state=AsyncMock(
            return_value={
                "task": {
                    "agent_profile": "default",
                    "agent_profile_version": 1,
                    "params": Params().model_dump(),
                    "budget": {"max_minutes": 1},
                },
                "agents": {"agent-1": {"task_type": "derive", "status": "running"}},
            }
        ),
        get_profile=AsyncMock(return_value=profile.model_dump()),
        finish_agent=AsyncMock(),
        with_token=Mock(),
    )

    class WaitingAgent:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def create_session(self):
            return object()

        def run(self, *_args, **_kwargs):
            async def updates():
                started.set()
                await asyncio.Event().wait()
                yield None

            return ResponseStream(updates())

    monkeypatch.setattr("bbx_runtime.runner.Agent", WaitingAgent)
    runner = AgentRunner(Settings.model_construct(), service, Mock(), Mock())  # type: ignore[arg-type]
    task = asyncio.create_task(
        runner.run_agent(
            "task", "agent-1", "derive", agent_token="issued", client=ScriptedChatClient([])
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel(cancel_reason)
    with pytest.raises(asyncio.CancelledError):
        await task
    service.finish_agent.assert_awaited_once()
    assert service.finish_agent.call_args.args[-1] == expected
