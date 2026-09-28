"""Runner completion, failure, and cancellation all reach the durable finish API."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from bbx_contracts.models import McpBinding, Params, WorkerTools
from bbx_contracts.profile import load_profile
from bbx_runtime.execenv import ExecEnvHandle
from bbx_runtime.middleware import GraceGateMiddleware, ToolLogMiddleware
from bbx_runtime.runner import AgentRunner
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient


@pytest.mark.parametrize(
    "outcome", ["normal", "refused", "runtime_error", "cancelled", "setup_error", "timeout"]
)
async def test_runner_finishes_all_terminal_paths(monkeypatch, outcome):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
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
    if outcome == "setup_error":
        service.get_profile.side_effect = ValueError("bad profile")
    if outcome == "timeout":
        real_timeout = asyncio.timeout

        def short_deadline(seconds):
            assert seconds > 60 + Params().grace_timeout * 60
            return real_timeout(0.01)

        monkeypatch.setattr("bbx_runtime.runner.asyncio.timeout", short_deadline)

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def create_session(self):
            return object()

        async def run(self, *args, **kwargs):
            if outcome == "timeout":
                await asyncio.sleep(10)
            if outcome == "cancelled":
                raise asyncio.CancelledError
            if outcome == "runtime_error":
                raise RuntimeError("Never publish provider request details")
            text = (
                '{"accepted":false,"reason":"前提不可用"}'
                if outcome == "refused"
                else '{"accepted":true,"data":{"posted":[],"excluded":[]}}'
            )
            return SimpleNamespace(text=text)

    monkeypatch.setattr("bbx_runtime.runner.Agent", FakeAgent)
    runner = AgentRunner(Settings.model_construct(), service, Mock(), Mock())  # type: ignore[arg-type]
    call = runner.run_agent(
        "task", "agent-1", "derive", agent_token="issued", client=ScriptedChatClient([])
    )
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await call
        expected = "grace_timeout"
    else:
        result = await call
        expected = "runtime_error" if outcome in {"setup_error", "timeout"} else outcome
        assert result.end_reason == expected
        if expected == "runtime_error":
            assert "provider request details" not in str(result.receipt)
    service.finish_agent.assert_awaited_once()
    assert service.finish_agent.call_args.args[-1] == expected


async def test_explore_selection_excludes_exec_and_keeps_external_tool_in_guarded_agent(
    monkeypatch,
):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    profile.worker_tools["explore"] = WorkerTools(
        builtin=["post_fact", "release"],
        mcp_servers=[McpBinding(name="remote-tools", version=2, allowed_tools=["ping"])],
    )
    service = SimpleNamespace(
        state=AsyncMock(
            return_value={
                "task": {
                    "agent_profile": "default",
                    "agent_profile_version": 1,
                    "params": Params().model_dump(),
                    "budget": {"max_minutes": 1},
                },
                "agents": {"agent-1": {"task_type": "explore", "status": "running"}},
            }
        ),
        get_profile=AsyncMock(return_value=profile.model_dump()),
        get_mcp_server=AsyncMock(
            return_value={
                "name": "remote-tools",
                "version": 2,
                "enabled": True,
                "has_secret": False,
                "url": "https://external.test/mcp",
            }
        ),
        finish_agent=AsyncMock(),
        with_token=Mock(),
    )
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def create_session(self):
            return object()

        async def run(self, *_args, **_kwargs):
            return SimpleNamespace(
                text='{"accepted":true,"data":{"intent_result":"none","posted":[],"note":"done"}}'
            )

    monkeypatch.setattr("bbx_runtime.runner.Agent", FakeAgent)
    handle = ExecEnvHandle(
        task_id=uuid4(),
        container_id="envd",
        name="envd",
        base_url="http://envd.test",
        token="token",
    )
    runner = AgentRunner(Settings.model_construct(), service, Mock(), Mock())  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as http:
        result = await runner.run_agent(
            "task",
            "agent-1",
            "explore",
            agent_token="issued",
            client=ScriptedChatClient([]),
            handle=handle,
            envd_http_client=http,
        )
    assert result.end_reason == "normal"
    assert [tool.name for tool in captured["tools"]] == ["post_fact", "release", "platform_0"]
    assert any(isinstance(item, ToolLogMiddleware) for item in captured["middleware"])
    assert any(isinstance(item, GraceGateMiddleware) for item in captured["middleware"])


@pytest.mark.asyncio
async def test_runner_waits_for_finish_after_repeated_cancellation(monkeypatch):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    finish_started = asyncio.Event()
    finish_allowed = asyncio.Event()

    async def finish(*_args, **_kwargs):
        finish_started.set()
        await finish_allowed.wait()

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
        finish_agent=AsyncMock(side_effect=finish),
        with_token=Mock(),
    )

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def create_session(self):
            return object()

        async def run(self, *_args, **_kwargs):
            return SimpleNamespace(text='{"accepted":true,"data":{"posted":[],"excluded":[]}}')

    monkeypatch.setattr("bbx_runtime.runner.Agent", FakeAgent)
    runner = AgentRunner(Settings.model_construct(), service, Mock(), Mock())  # type: ignore[arg-type]
    running = asyncio.create_task(
        runner.run_agent(
            "task", "agent-1", "derive", agent_token="issued", client=ScriptedChatClient([])
        )
    )
    await asyncio.wait_for(finish_started.wait(), 2)
    running.cancel()
    running.cancel()
    await asyncio.sleep(0)
    assert not running.done()
    finish_allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 2)
    service.finish_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_runner_waits_for_envd_cleanup_when_cancelled(monkeypatch):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    cleanup_started = asyncio.Event()
    cleanup_allowed = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def slow_close(_self):
        cleanup_started.set()
        await cleanup_allowed.wait()
        cleanup_done.set()

    monkeypatch.setattr("bbx_runtime.runner.EnvdClient.close", slow_close)
    service = SimpleNamespace(
        state=AsyncMock(
            return_value={
                "task": {
                    "agent_profile": "default",
                    "agent_profile_version": 1,
                    "params": Params().model_dump(),
                    "budget": {"max_minutes": 1},
                },
                "agents": {"agent-1": {"task_type": "explore", "status": "running"}},
            }
        ),
        get_profile=AsyncMock(return_value=profile.model_dump()),
        finish_agent=AsyncMock(),
        with_token=Mock(),
    )

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def create_session(self):
            return object()

        async def run(self, *_args, **_kwargs):
            receipt = '{"accepted":true,"data":{"intent_result":"none","posted":[],"note":"done"}}'
            return SimpleNamespace(text=receipt)

    monkeypatch.setattr("bbx_runtime.runner.Agent", FakeAgent)
    handle = ExecEnvHandle(
        task_id=uuid4(),
        container_id="envd",
        name="envd",
        base_url="http://envd.test",
        token="token",
    )
    runner = AgentRunner(Settings.model_construct(), service, Mock(), Mock())  # type: ignore[arg-type]
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as http:
        running = asyncio.create_task(
            runner.run_agent(
                "task",
                "agent-1",
                "explore",
                agent_token="issued",
                client=ScriptedChatClient([]),
                handle=handle,
                envd_http_client=http,
            )
        )
        await asyncio.wait_for(cleanup_started.wait(), 2)
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        cleanup_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 2)
    assert cleanup_done.is_set()
    service.finish_agent.assert_awaited_once()
