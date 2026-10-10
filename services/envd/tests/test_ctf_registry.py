"""CTF boot fencing and command retention without external services."""

import asyncio
import importlib
import importlib.util
import os
import sys
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_envd import core
from bbx_envd.core import CtfCommandRegistry
from bbx_envd.settings import Settings
from pydantic import SecretStr, ValidationError


def setup_registry(tmp_path):
    registry = CtfCommandRegistry(str(uuid4()), tmp_path / "boot")
    identity = {
        "task_id": registry.task_id,
        "boot_id": registry.boot_id,
        "agent_id": "agent-2",
        "member_id": "member-1",
        "generation": 1,
    }
    return registry, identity


def test_fixed_mode_and_restart_fail_closed(tmp_path):
    with pytest.raises(ValidationError):
        Settings(envd_token=SecretStr("test"), envd_mode="ctf")
    registry, identity = setup_registry(tmp_path)
    assert registry.status("agent-2")["drained"] is False
    registry.register(identity)
    restarted = CtfCommandRegistry(registry.task_id, tmp_path / "boot")
    assert restarted.boot_id != registry.boot_id
    assert restarted.status()["state"] == "unknown"
    assert restarted.status()["drained"] is False
    with pytest.raises(ValueError, match="unknown"):
        restarted.register({**identity, "boot_id": restarted.boot_id})


async def test_command_deduplication_survives_client_cancel(tmp_path, monkeypatch):
    registry, identity = setup_registry(tmp_path)
    settings = Settings(envd_token=SecretStr("test"))
    metadata = {**identity, "command_id": str(uuid4())}
    with pytest.raises(ValueError, match="Unregistered"):
        await registry.execute("agent-2", metadata, "echo test", None, 10, False, settings)
    registry.register(identity)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def fake(*args, **kwargs):
        calls.append(kwargs["command_id"])
        entered.set()
        await release.wait()
        return {"exit_code": 0}

    monkeypatch.setattr(core, "execute_command", fake)
    first = asyncio.create_task(
        registry.execute("agent-2", metadata, "echo test", None, 10, False, settings)
    )
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(
        registry.execute("agent-2", metadata, "echo test", None, 10, False, settings)
    )
    release.set()
    assert await second == {"exit_code": 0}
    assert len(calls) == 1
    assert await registry.execute("agent-2", metadata, "echo test", None, 10, False, settings) == {
        "exit_code": 0
    }
    with pytest.raises(ValueError, match="different"):
        await registry.execute("agent-2", metadata, "echo changed", None, 10, False, settings)
    with pytest.raises(ValueError, match="Stale boot"):
        await registry.execute(
            "agent-2", {**metadata, "boot_id": str(uuid4())}, "echo test", None, 10, False, settings
        )
    with pytest.raises(ValueError):
        await registry.execute("agent-2", {}, "echo test", None, 10, False, settings)


async def test_stop_waits_for_spawn_and_real_process_group(tmp_path, monkeypatch):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)
    settings = Settings(envd_token=SecretStr("test"))
    monkeypatch.setattr(core, "agent_home", lambda _: tmp_path)
    monkeypatch.setattr(core, "audit_command", lambda _: None)
    original_spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await original_spawn(
            sys.executable, "-c", "import time; time.sleep(30)", **kwargs
        )
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(core.asyncio, "create_subprocess_exec", spawn)
    command = asyncio.create_task(
        registry.execute(
            "agent-2",
            {**identity, "command_id": str(uuid4())},
            "sleep 30",
            None,
            120,
            False,
            settings,
        )
    )
    await asyncio.wait_for(started.wait(), 3)
    status = await asyncio.wait_for(registry.stop(identity), 5)
    assert status["drained"] is True
    with pytest.raises(ProcessLookupError):
        os.killpg(processes[0].pid, 0)
    with pytest.raises(asyncio.CancelledError):
        await command
    with pytest.raises(ValueError, match="stopping"):
        await registry.execute(
            "agent-2",
            {**identity, "command_id": str(uuid4())},
            "echo late",
            None,
            1,
            False,
            settings,
        )
    registry.register({**identity, "generation": 2})
    with pytest.raises(ValueError, match="Unregistered"):
        await registry.stop(identity)


async def test_ctf_rest_registration_and_missing_mcp_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVD_TOKEN", "replace-me")
    service = importlib.import_module("bbx_envd.app")
    registry, identity = setup_registry(tmp_path)
    monkeypatch.setattr(service, "ctf_registry", registry)
    monkeypatch.setattr(
        service,
        "settings",
        Settings(
            envd_token=SecretStr("replace-me"), envd_mode="ctf", envd_task_id=UUID(registry.task_id)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app),
        base_url="http://test",
        headers={"Authorization": "Bearer replace-me"},
    ) as client:
        assert (await client.get("/ctf/status")).json()["boot_id"] == registry.boot_id
        assert (await client.post("/ctf/register", json=identity)).status_code == 200
        assert (await client.post("/ctf/drain", json=identity)).json()["drained"] is True
        assert (await client.post("/ctf/register", json=identity)).status_code == 409


async def test_mcp_meta_is_required_and_retries_do_not_execute_twice(tmp_path, monkeypatch):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    monkeypatch.setenv("ENVD_TOKEN", "replace-me")
    original = importlib.import_module("bbx_envd.app")
    spec = importlib.util.spec_from_file_location("ctf_test_envd_app", original.__file__)
    assert spec is not None and spec.loader is not None
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    registry, identity = setup_registry(tmp_path)
    monkeypatch.setattr(service, "ctf_registry", registry)
    monkeypatch.setattr(
        service,
        "settings",
        Settings(
            envd_token=SecretStr("replace-me"), envd_mode="ctf", envd_task_id=UUID(registry.task_id)
        ),
    )
    calls = []

    async def fake(*args, **kwargs):
        calls.append(kwargs["command_id"])
        return {"exit_code": 0}

    monkeypatch.setattr(core, "execute_command", fake)
    headers = {"Authorization": "Bearer replace-me", "X-Agent-Id": "agent-2"}
    async with service.mcp.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app), headers=headers
        ) as client:
            async with streamable_http_client("http://test/mcp", http_client=client) as (
                reader,
                writer,
                _,
            ):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    args = {"command": "echo once"}
                    meta = {"ctf": {**identity, "command_id": str(uuid4())}}
                    assert (await session.call_tool("execute_command", args)).isError
                    assert (await session.call_tool("execute_command", args, meta=meta)).isError
                    registry.register(identity)
                    assert not (await session.call_tool("execute_command", args, meta=meta)).isError
                    assert not (await session.call_tool("execute_command", args, meta=meta)).isError
                    assert len(calls) == 1
                    meta["ctf"]["boot_id"] = str(uuid4())
                    assert (await session.call_tool("execute_command", args, meta=meta)).isError


async def test_idle_generation_zero_can_drain_but_not_execute(tmp_path):
    registry, identity = setup_registry(tmp_path)
    identity["generation"] = 0
    registry.register(identity)
    with pytest.raises(ValueError, match="zero"):
        await registry.execute(
            "agent-2",
            {**identity, "command_id": str(uuid4())},
            "id",
            None,
            1,
            False,
            Settings(envd_token=SecretStr("test")),
        )
    assert (await registry.stop(identity))["drained"] is True


async def test_command_ledger_survives_generation_replacement(tmp_path, monkeypatch):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)
    calls = []

    async def fake(*args, **kwargs):
        calls.append(kwargs["command_id"])
        return {"exit_code": 0}

    monkeypatch.setattr(core, "execute_command", fake)
    metadata = {**identity, "command_id": str(uuid4())}
    settings = Settings(envd_token=SecretStr("test"))
    await registry.execute("agent-2", metadata, "echo once", None, 10, False, settings)
    await registry.stop(identity)
    registry.register({**identity, "generation": 2})
    with pytest.raises(ValueError, match="previous generation"):
        await registry.execute(
            "agent-2", {**metadata, "generation": 2}, "echo once", None, 10, False, settings
        )
    with pytest.raises(ValueError, match="different arguments"):
        await registry.execute(
            "agent-2", {**metadata, "generation": 2}, "echo twice", None, 10, False, settings
        )
    assert len(calls) == 1


async def test_drain_waits_for_delayed_process_group_reaping(tmp_path, monkeypatch):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)
    checks = []

    def delayed_reap(_):
        checks.append(True)
        return len(checks) >= 3

    monkeypatch.setattr(registry, "_drained", delayed_reap)
    assert (await registry.stop(identity))["drained"] is True
    assert len(checks) >= 3


async def test_drain_timeout_retains_stopping_without_claiming_success(tmp_path, monkeypatch):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)
    monkeypatch.setattr(registry, "_drained", lambda _: False)
    monkeypatch.setattr(core, "CTF_DRAIN_WAIT_SECONDS", 0.04)
    result = await asyncio.wait_for(registry.stop(identity), 0.5)
    assert result["drained"] is False
    assert result["state"] == "stopping"
    with pytest.raises(ValueError, match="not drained"):
        registry.register({**identity, "generation": 2})
