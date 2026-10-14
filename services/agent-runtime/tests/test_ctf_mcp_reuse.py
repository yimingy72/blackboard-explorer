"""Native MCP sessions are run-scoped and never replay an uncertain write."""

import asyncio
import copy
import json
from collections import Counter
from typing import Any, cast

import httpx
import pytest
from agent_framework import MCPStreamableHTTPTool
from bbx_runtime.ctf.platform import PlatformAdapter, PlatformCatalogCache
from bbx_runtime.ctf.runner import CtfRunner
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from test_ctf_platform import Checkpoint, Service, Store, profile, run


class Remote:
    def __init__(self):
        self.methods = Counter()
        self.headers = []
        self.fail = False
        self.invalid = False
        self.block = False
        self.dispatched = asyncio.Event()

    async def request(self, request):
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        method = body["method"]
        self.methods[method] += 1
        self.headers.append(request.headers.get("Authorization"))
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "scripted", "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": binding.tool_name,
                        "inputSchema": {"type": "object"},
                        "outputSchema": {"type": "object", "required": ["status"]},
                    }
                    for binding in profile().platform_tools
                ]
            }
        else:
            assert method == "tools/call"
            if self.block:
                self.dispatched.set()
                await asyncio.Event().wait()
            if self.fail:
                self.fail = False
                return httpx.Response(404)
            result = {
                "content": [],
                "structuredContent": {} if self.invalid else {"status": "ok"},
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
async def native(monkeypatch):
    remote = Remote()
    connections = []
    clients = []

    def connect(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(remote.request))
        clients.append(client)
        connection = MCPStreamableHTTPTool(**kwargs, http_client=client)
        connections.append(connection)
        return connection

    monkeypatch.setattr("bbx_runtime.ctf.platform.MCPStreamableHTTPTool", connect)
    yield remote, connections
    for connection in connections:
        await connection.close()
    for client in clients:
        await client.aclose()


async def build(adapter, checkpoint, *, task="task", role="teammate", configured=None):
    return await adapter.build(
        configured or profile(),
        role,
        task,
        "member-1",
        {"id": "turn", "generation": 1},
        lambda: checkpoint,
    )


async def test_native_two_runs_reuse_each_session_and_keep_durable_receipts(native):
    remote, connections = native
    store, cache = Store(), PlatformCatalogCache()
    service = Service(store)

    class OneShotAdapter(PlatformAdapter):
        async def request(self, server, headers, name, arguments):
            async with httpx.AsyncClient(
                headers=headers, transport=httpx.MockTransport(remote.request)
            ) as http:
                async with streamable_http_client(server["url"], http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        result = (
                            await session.list_tools()
                            if name is None
                            else await session.call_tool(name, arguments)
                        )
                        return result.model_dump(mode="json")

    for index in range(2):
        baseline = OneShotAdapter(service, lambda: store)
        tools = await build(baseline, Checkpoint())
        await run(
            tools,
            [
                ("platform_fake_status", {}),
                ("platform_fake_submit", {"sample": index}),
                ("platform_fake_status", {"sample": index}),
            ],
        )
    assert remote.methods["initialize"] == remote.methods["tools/list"] == 8
    assert remote.methods["tools/call"] == 6
    remote.methods.clear()
    service = Service(store)
    for index in range(2):
        checkpoint = Checkpoint()
        adapter = PlatformAdapter(service, lambda: store, catalog_cache=cache)
        tools = await build(adapter, checkpoint)
        await run(
            tools,
            [
                ("platform_fake_status", {}),
                ("platform_fake_submit", {"sample": index}),
                ("platform_fake_status", {"sample": index}),
            ],
        )
        assert all("record" in item for item in checkpoint.saved["ctf_platform_calls"].values())
        await adapter.close()
    assert remote.methods["initialize"] == len(connections) == 2
    # A fresh native session fetches output schemas on its first successful call.
    assert remote.methods["tools/list"] == 2
    assert remote.methods["tools/call"] == len(service.records) == 6
    assert len(service.registry) == 2
    assert all(connection.session is None for connection in connections)
    assert len(service.begun) == 6


async def test_catalog_single_flight_only_reads_and_is_scoped_to_task_and_role(native):
    remote, _ = native
    store, cache = Store(), PlatformCatalogCache()
    service = Service(store)
    adapters = [PlatformAdapter(service, lambda: store, catalog_cache=cache) for _ in range(4)]
    try:
        await asyncio.gather(*(build(adapter, Checkpoint()) for adapter in adapters[:2]))
        assert remote.methods["tools/list"] == remote.methods["initialize"] == 1
        await build(adapters[2], Checkpoint(), role="lead")
        await build(adapters[3], Checkpoint(), task="another-task")
        assert remote.methods["tools/list"] == remote.methods["initialize"] == 3
        cache.clear("task")
        await build(adapters[1], Checkpoint())
        assert remote.methods["tools/list"] == 4
        assert remote.methods["tools/call"] == 0
    finally:
        for adapter in adapters:
            await adapter.close()


async def test_failed_or_cleared_catalog_load_cannot_poison_cache():
    cache = PlatformCatalogCache()
    started, release = asyncio.Event(), asyncio.Event()

    async def failed():
        raise ValueError("discovery failed")

    with pytest.raises(ValueError, match="discovery failed"):
        await cache.get("task", "identity", failed)

    async def loaded():
        started.set()
        await release.wait()
        return {"tools": {"status": {}}}

    pending = asyncio.create_task(cache.get("task", "identity", loaded))
    await started.wait()
    cache.clear("task")
    release.set()
    assert await pending == {"tools": {"status": {}}}
    assert cache.tasks == {}


async def test_build_rechecks_enabled_identity_credentials_and_grants(native, monkeypatch):
    remote, _ = native
    store, cache = Store(), PlatformCatalogCache()
    service = Service(store)
    server = dict(
        name="fake",
        version=3,
        enabled=True,
        url="http://fake/mcp",
        has_secret=True,
        auth_header="Authorization",
        auth_scheme="Bearer",
    )
    secret = "synthetic-first"
    credentials_read = 0

    async def registry(*_):
        return copy.deepcopy(server)

    async def credentials(*_):
        nonlocal credentials_read
        credentials_read += 1
        return {"secret": secret}

    monkeypatch.setattr(service, "get_mcp_server", registry)
    monkeypatch.setattr(service, "get_mcp_credentials", credentials, raising=False)
    adapters = []

    async def checked_build(**kwargs):
        adapter = PlatformAdapter(service, lambda: store, catalog_cache=cache)
        adapters.append(adapter)
        return await build(adapter, Checkpoint(), **kwargs)

    try:
        await checked_build()
        await checked_build()
        assert remote.methods["tools/list"] == 1
        secret = "synthetic-rotated"
        await checked_build()
        server["url"] = "http://changed/mcp"
        await checked_build()
        configured = profile()
        configured.worker_tools["teammate"].mcp_servers[0].allowed_tools = ["status"]
        assert len(await checked_build(configured=configured)) == 1
        assert remote.methods["tools/list"] == 4
        assert credentials_read == 5
        assert "Bearer synthetic-first" in remote.headers
        assert "Bearer synthetic-rotated" in remote.headers
        assert "synthetic" not in repr(cache.tasks)
        server["enabled"] = False
        with pytest.raises(ValueError, match="unavailable"):
            await checked_build()
        server["enabled"] = True
        server["version"] = 9
        with pytest.raises(ValueError, match="unavailable"):
            await checked_build()
        server["version"] = 3
        secret = ""
        with pytest.raises(ValueError, match="credential"):
            await checked_build()
        assert remote.methods["tools/list"] == 4
    finally:
        for adapter in adapters:
            await adapter.close()


async def test_404_keeps_unknown_evidence_no_replay_and_fresh_authorized_call_reconnects(native):
    remote, connections = native
    checkpoint, store = Checkpoint(), Store()
    service = Service(store)
    adapter = PlatformAdapter(service, lambda: store)
    tools = await build(adapter, checkpoint)
    remote.fail = True
    await run(
        tools,
        [
            ("platform_fake_submit", {"sample": "first"}),
            ("platform_fake_submit", {"sample": "first"}),
            ("platform_fake_status", {}),
        ],
    )
    assert remote.methods["tools/call"] == 2
    assert remote.methods["initialize"] == 2
    assert service.records[0][1]["status"] == "unknown"
    assert service.records[1][1]["structuredContent"] == {"status": "ok"}
    assert len(service.begun) == 2
    assert any(
        item["state"] == "unknown" for item in checkpoint.saved["ctf_platform_calls"].values()
    )
    await adapter.close()
    assert all(connection.session is None for connection in connections)


async def test_cancelled_native_call_saves_unknown_receipt_and_closes_connection(native):
    remote, connections = native
    checkpoint, store = Checkpoint(), Store()
    service = Service(store)
    adapter = PlatformAdapter(service, lambda: store)
    tools = await build(adapter, checkpoint)
    remote.block = True
    pending = asyncio.create_task(run(tools, [("platform_fake_submit", {"sample": "cancel"})]))
    await asyncio.wait_for(remote.dispatched.wait(), timeout=2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert remote.methods["tools/call"] == 1
    assert service.records[0][1]["status"] == "unknown"
    assert len(service.begun) == 1
    assert all("record" in item for item in checkpoint.saved["ctf_platform_calls"].values())
    await adapter.close()
    assert all(connection.session is None for connection in connections)


async def test_native_schema_validation_and_list_changed_do_not_expand_fixed_wrappers(native):
    remote, connections = native
    store = Store()
    service = Service(store)
    cache = PlatformCatalogCache()
    first = PlatformAdapter(service, lambda: store, catalog_cache=cache)
    await build(first, Checkpoint())
    await first.close()
    second = PlatformAdapter(service, lambda: store, catalog_cache=cache)
    tools = await build(second, Checkpoint())
    remote.invalid = True
    with pytest.raises(RuntimeError, match="Invalid structured content"):
        await second.request(await service.get_mcp_server("fake", 3), {}, "status", {})
    assert remote.methods["tools/list"] == 2
    assert remote.methods["tools/call"] == 1
    assert connections[-1].session is None
    remote.invalid = False
    server = await service.get_mcp_server("fake", 3)
    await second.request(server, {}, "status", {})
    connection = connections[-1]
    await connection.message_handler(types.ServerNotification(types.ToolListChangedNotification()))
    for _ in range(30):
        await asyncio.sleep(0.005)
        if remote.methods["tools/list"] >= 4:
            break
    assert remote.methods["tools/list"] == 4
    assert {item.name for item in tools} == {"platform_fake_submit", "platform_fake_status"}
    assert connection.functions == []
    assert remote.methods["tools/call"] == 2
    await second.close()


async def test_runner_closes_owned_adapter_when_build_or_run_fails(monkeypatch):
    closed = []

    class Adapter:
        def __init__(self, *_):
            pass

        async def close(self):
            closed.append(True)

    class Runner(CtfRunner):
        async def _run(self, task_id, member, turn, platform):
            raise ValueError("original failure")

    monkeypatch.setattr("bbx_runtime.ctf.runner.PlatformAdapter", Adapter)
    runner = Runner(cast(Any, None), cast(Any, None))
    with pytest.raises(ValueError, match="original failure"):
        await runner.run("task", {}, {})
    assert closed == [True]
    runner.platform_catalog.tasks["task"] = {}
    runner.clear_platform_catalog("task")
    assert runner.platform_catalog.tasks == {}


async def test_runner_closes_native_session_after_partial_build_failure(native):
    _, connections = native
    store = Store()
    configured = profile()
    configured.platform_tools[1].tool_name = "missing"
    configured.worker_tools["teammate"].mcp_servers[0].allowed_tools.append("missing")

    class Runner(CtfRunner):
        async def _run(self, task_id, member, turn, platform):
            await build(platform, Checkpoint(), configured=configured)

    runner = Runner(cast(Any, Service(store)), cast(Any, None), objects=store)
    with pytest.raises(ValueError, match="Configured MCP tool is unavailable"):
        await runner.run("task", {}, {})
    assert len(connections) == 1
    assert connections[0].session is None


async def test_cleanup_failure_cannot_replace_request_error(native, monkeypatch):
    remote, connections = native
    store = Store()
    adapter = PlatformAdapter(Service(store), lambda: store)
    await build(adapter, Checkpoint())
    connection = connections[0]
    original_close = connection.close

    async def failed_close():
        await original_close()
        raise ValueError("cleanup failed")

    monkeypatch.setattr(connection, "close", failed_close)
    remote.fail = True
    with pytest.raises(McpError, match="Session terminated"):
        await adapter.request(await adapter.service.get_mcp_server("fake", 3), {}, "submit", {})
    assert adapter.connections == {}
    monkeypatch.setattr(connection, "close", original_close)


async def test_request_error_survives_cleanup_cancel(native, monkeypatch):
    _, connections = native
    store = Store()
    adapter = PlatformAdapter(Service(store), lambda: store)
    await build(adapter, Checkpoint())
    connection = connections[0]
    original_close = connection.close

    async def failed_call(*_):
        raise ValueError("first request error")

    async def cancelled_close():
        await original_close()
        raise asyncio.CancelledError("cleanup cancel")

    monkeypatch.setattr(connection.session, "call_tool", failed_call)
    monkeypatch.setattr(connection, "close", cancelled_close)
    try:
        with pytest.raises(ValueError, match="first request error"):
            await adapter.request(await adapter.service.get_mcp_server("fake", 3), {}, "submit", {})
    finally:
        monkeypatch.setattr(connection, "close", original_close)


async def test_runner_error_survives_adapter_cleanup_cancel(monkeypatch):
    class Adapter:
        def __init__(self, *_):
            pass

        async def close(self):
            raise asyncio.CancelledError("cleanup cancel")

    class Runner(CtfRunner):
        async def _run(self, task_id, member, turn, platform):
            raise ValueError("first run error")

    monkeypatch.setattr("bbx_runtime.ctf.runner.PlatformAdapter", Adapter)
    with pytest.raises(ValueError, match="first run error"):
        await Runner(cast(Any, None), cast(Any, None)).run("task", {}, {})


async def test_normal_cleanup_propagates_external_cancel(native, monkeypatch):
    _, connections = native
    store = Store()
    adapter = PlatformAdapter(Service(store), lambda: store)
    await build(adapter, Checkpoint())
    connection = connections[0]
    original_close = connection.close
    entered = asyncio.Event()

    async def blocked_close():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(connection, "close", blocked_close)
    closing = asyncio.create_task(adapter.close())
    await entered.wait()
    closing.cancel("external cleanup cancellation")
    try:
        with pytest.raises(asyncio.CancelledError, match="external cleanup cancellation"):
            await closing
    finally:
        monkeypatch.setattr(connection, "close", original_close)
        await original_close()


async def test_second_cancel_joins_cleanup_and_preserves_first_cancel(native, monkeypatch):
    _, connections = native
    store = Store()
    adapter = PlatformAdapter(Service(store), lambda: store)
    await build(adapter, Checkpoint())
    connection = connections[0]
    original_close = connection.close
    dispatched, entered, release, closed = (asyncio.Event() for _ in range(4))

    async def blocked_call(*_):
        dispatched.set()
        await asyncio.Event().wait()

    async def slow_close():
        entered.set()
        await release.wait()
        await original_close()
        closed.set()

    monkeypatch.setattr(connection.session, "call_tool", blocked_call)
    monkeypatch.setattr(connection, "close", slow_close)
    server = await adapter.service.get_mcp_server("fake", 3)
    work = asyncio.create_task(adapter.request(server, {}, "submit", {}))
    await dispatched.wait()
    work.cancel("first cancellation")
    await entered.wait()
    work.cancel("second cancellation")
    await asyncio.sleep(0)
    release.set()
    try:
        with pytest.raises(asyncio.CancelledError, match="first cancellation"):
            await work
        assert closed.is_set()
        assert connection.session is None
    finally:
        monkeypatch.setattr(connection, "close", original_close)
