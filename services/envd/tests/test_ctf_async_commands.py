"""Asynchronous commands share the boot registry and never replay on client cancellation."""

import asyncio
import importlib
import importlib.util
import os
import sys
from uuid import uuid4

import httpx
import pytest
from bbx_envd import core
from bbx_envd.settings import Settings
from pydantic import SecretStr
from test_ctf_registry import setup_registry


async def test_capability_discovery_does_not_change_member_or_drain_proof_shape(tmp_path):
    registry, identity = setup_registry(tmp_path)
    assert registry.status()["capabilities"] == ["ctf_async_commands_v1"]
    assert "capabilities" not in registry.status(identity["agent_id"])
    registry.register(identity)
    expected = {"task_id", "boot_id", "agent_id", "member_id", "generation", "state", "drained"}
    assert set(registry.status(identity["agent_id"])) == expected
    drained = await registry.stop(identity)
    assert set(drained) == expected and drained["drained"] is True


async def test_async_excludes_new_sync_and_async_but_keeps_other_members_independent(
    tmp_path, monkeypatch
):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)
    settings = Settings(envd_token=SecretStr("test"))
    release = asyncio.Event()
    calls = []

    async def execute(*args, **kwargs):
        calls.append(kwargs["command_id"])
        await release.wait()
        return {"exit_code": 0, "stdout": "saved output"}

    monkeypatch.setattr(core, "execute_command", execute)
    metadata = {**identity, "command_id": str(uuid4())}
    result = await registry.start("agent-2", metadata, "inspect", None, 120, False, settings)
    assert result["state"] == "running"
    other = {**identity, "command_id": str(uuid4())}
    assert (await registry.execute("agent-2", other, "other", None, 120, False, settings))[
        "state"
    ] == "busy"
    assert (await registry.start("agent-2", other, "other", None, 120, False, settings))[
        "state"
    ] == "busy"
    peer = {**identity, "agent_id": "agent-3", "member_id": "member-2", "command_id": str(uuid4())}
    registry.register(peer)
    parallel = asyncio.create_task(
        registry.execute("agent-3", peer, "peer", None, 120, False, settings)
    )
    await asyncio.sleep(0)
    release.set()
    await parallel
    assert registry.command_status(metadata)["state"] == "completed"
    assert calls == [metadata["command_id"], peer["command_id"]]
    assert registry.status()["capabilities"] == ["ctf_async_commands_v1"]


async def test_existing_sync_batch_is_unchanged_and_async_does_not_cancel_it(tmp_path, monkeypatch):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)
    settings = Settings(envd_token=SecretStr("test"))
    release = asyncio.Event()
    entered = asyncio.Event()
    calls = []

    async def execute(*args, **kwargs):
        calls.append(kwargs["command_id"])
        entered.set()
        await release.wait()
        return {"exit_code": 0}

    monkeypatch.setattr(core, "execute_command", execute)
    first = asyncio.create_task(
        registry.execute(
            "agent-2", {**identity, "command_id": str(uuid4())}, "a", None, 120, False, settings
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        registry.execute(
            "agent-2", {**identity, "command_id": str(uuid4())}, "b", None, 120, False, settings
        )
    )
    await asyncio.sleep(0)
    busy = await registry.start(
        "agent-2", {**identity, "command_id": str(uuid4())}, "c", None, 120, False, settings
    )
    assert busy["state"] == "busy" and not first.done()
    release.set()
    await asyncio.gather(first, second)
    assert len(calls) == 2


@pytest.mark.parametrize("async_first", [True, False])
async def test_parallel_tool_batch_admits_only_one_execution_mode(
    tmp_path, monkeypatch, async_first
):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)
    settings = Settings(envd_token=SecretStr("test"))
    release = asyncio.Event()
    calls = []

    async def execute(*args, **kwargs):
        calls.append(kwargs["command_id"])
        await release.wait()
        return {"exit_code": 0}

    monkeypatch.setattr(core, "execute_command", execute)
    operations = (
        [registry.start, registry.execute] if async_first else [registry.execute, registry.start]
    )
    tasks = [
        asyncio.create_task(
            operation(
                "agent-2",
                {**identity, "command_id": str(uuid4())},
                "inspect",
                None,
                120,
                False,
                settings,
            )
        )
        for operation in operations
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)
    assert len(calls) == 1
    assert sum(result.get("state") == "busy" for result in results) == 1


async def test_start_client_disconnect_and_cancel_have_distinct_process_outcomes(
    tmp_path, monkeypatch
):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)
    settings = Settings(envd_token=SecretStr("test"))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def execute(*args, **kwargs):
        calls.append(kwargs["command_id"])
        entered.set()
        await release.wait()
        return {"exit_code": 0}

    monkeypatch.setattr(core, "execute_command", execute)
    metadata = {**identity, "command_id": str(uuid4())}
    client = asyncio.create_task(
        registry.start("agent-2", metadata, "inspect", None, 120, False, settings)
    )
    await entered.wait()
    client.cancel()
    with pytest.raises(asyncio.CancelledError):
        await client
    assert registry.command_status(metadata)["state"] == "running"
    assert (await registry.cancel_command(metadata))["state"] == "cancelled"
    assert (await registry.cancel_command(metadata))["state"] == "cancelled"
    assert calls == [metadata["command_id"]]
    assert registry.status("agent-2")["state"] == "registered"
    with pytest.raises(ValueError, match="Unregistered"):
        registry.command_status({**metadata, "member_id": "member-9"})
    with pytest.raises(ValueError, match="Stale boot"):
        registry.command_status({**metadata, "boot_id": str(uuid4())})


async def test_completion_wins_cancel_and_failed_task_has_bounded_public_result(
    tmp_path, monkeypatch
):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)
    settings = Settings(envd_token=SecretStr("test"))

    async def execute(*args, **kwargs):
        return {"exit_code": 124, "stdout": "partial"}

    monkeypatch.setattr(core, "execute_command", execute)
    metadata = {**identity, "command_id": str(uuid4())}
    assert (await registry.start("agent-2", metadata, "inspect", None, 1, False, settings))[
        "state"
    ] == "timed_out"
    assert (await registry.cancel_command(metadata))["result"] == {
        "exit_code": 124,
        "stdout": "partial",
    }

    async def fail(*args, **kwargs):
        raise ValueError("secret-sentinel")

    monkeypatch.setattr(core, "execute_command", fail)
    failed = await registry.start(
        "agent-2", {**identity, "command_id": str(uuid4())}, "fail", None, 1, False, settings
    )
    assert failed["state"] == "failed" and "secret-sentinel" not in str(failed)


async def test_async_cancel_reaps_real_process_group_without_stopping_member(tmp_path, monkeypatch):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)
    monkeypatch.setattr(core, "agent_home", lambda _: tmp_path)
    monkeypatch.setattr(core, "audit_command", lambda _: None)
    original_spawn = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original_spawn(
            sys.executable, "-c", "import time; time.sleep(30)", **kwargs
        )
        processes.append(process)
        return process

    monkeypatch.setattr(core.asyncio, "create_subprocess_exec", spawn)
    metadata = {**identity, "command_id": str(uuid4())}
    try:
        pending = await registry.start(
            "agent-2",
            metadata,
            "long task",
            None,
            120,
            False,
            Settings(envd_token=SecretStr("test")),
        )
        assert pending["state"] == "running"
        assert (await registry.cancel_command(metadata))["state"] == "cancelled"
        with pytest.raises(ProcessLookupError):
            os.killpg(processes[0].pid, 0)
        assert registry.status("agent-2")["state"] == "registered"
    finally:
        await registry.stop(identity)


async def test_async_mcp_and_http_status_share_the_fenced_registry(tmp_path, monkeypatch):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    registry, identity = setup_registry(tmp_path)
    monkeypatch.setenv("ENVD_TOKEN", "replace-me")
    original = importlib.import_module("bbx_envd.app")
    monkeypatch.setenv("ENVD_MODE", "ctf")
    monkeypatch.setenv("ENVD_TASK_ID", registry.task_id)
    spec = importlib.util.spec_from_file_location("async_test_envd_app", original.__file__)
    assert spec is not None and spec.loader is not None
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    monkeypatch.setattr(service, "ctf_registry", registry)
    calls = []

    async def execute(*args, **kwargs):
        calls.append(kwargs["command_id"])
        return {"exit_code": 0, "stdout": "evidence"}

    monkeypatch.setattr(core, "execute_command", execute)
    metadata = {**identity, "command_id": str(uuid4())}
    async with service.mcp.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app),
            base_url="http://test",
            headers={"Authorization": "Bearer replace-me", "X-Agent-Id": "agent-2"},
        ) as client:
            await client.post("/ctf/register", json=identity)
            async with streamable_http_client("http://test/mcp", http_client=client) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert (
                        await session.call_tool("start_command", {"command": "inspect"})
                    ).isError
                    for _ in range(2):
                        result = await session.call_tool(
                            "start_command", {"command": "inspect"}, meta={"ctf": metadata}
                        )
                        assert not result.isError and result.structuredContent is not None
                        assert result.structuredContent["state"] == "completed"
            assert len(calls) == 1
            result = await client.get("/ctf/status", params=metadata)
            assert result.json()["result"]["stdout"] == "evidence"
            assert (
                await client.get("/ctf/status", params={**metadata, "member_id": "member-9"})
            ).status_code == 409
            assert (await client.post("/ctf/cancel-command", json=metadata)).json()[
                "state"
            ] == "completed"
