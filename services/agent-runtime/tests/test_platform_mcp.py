"""Pinned external MCP bindings use private credentials and restricted tools."""

from unittest.mock import AsyncMock

import httpx
import pytest
from bbx_contracts.models import McpBinding
from bbx_runtime.runner import external_mcp_tools
from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette


@pytest.mark.parametrize(
    ("auth_header", "auth_scheme", "expected_header"),
    [
        ("Authorization", "Bearer", "Bearer test-mcp-key"),
        ("X-Key", "", "test-mcp-key"),
    ],
)
async def test_external_mcp_uses_secret_prefix_and_tool_allowlist(
    auth_header: str, auth_scheme: str, expected_header: str
):
    mcp = FastMCP("platform-test", host="0.0.0.0")
    seen_headers: list[str] = []

    @mcp.tool()
    async def ping(ctx: Context) -> str:
        request = ctx.request_context.request
        seen_headers.append(request.headers.get(auth_header, "") if request else "")
        return "pong"

    @mcp.tool()
    async def hidden() -> str:
        return "hidden"

    app = Starlette(routes=[*mcp.streamable_http_app().routes])
    service = AsyncMock()
    service.get_mcp_server.return_value = {
        "name": "remote-tools",
        "version": 4,
        "label": "Remote tools",
        "url": "http://localhost/mcp",
        "auth_header": auth_header,
        "auth_scheme": auth_scheme,
        "has_secret": True,
        "enabled": True,
    }
    service.get_mcp_credentials.return_value = {"secret": "test-mcp-key"}
    binding = McpBinding(name="remote-tools", version=4, allowed_tools=["ping"])
    async with mcp.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http:
            tools = await external_mcp_tools(service, [binding], http)
            assert len(tools) == 1
            assert tools[0].load_prompts_flag is False
            assert tools[0].sampling_max_requests == 0
            async with tools[0]:
                assert [function.name for function in tools[0].functions] == [
                    "mcp_0_remote-tools_ping"
                ]
                response = await tools[0].call_tool("ping")
                result = (
                    response
                    if isinstance(response, str)
                    else "".join(item.text or "" for item in response)
                )
                assert "pong" in result
    assert seen_headers == [expected_header]
    service.get_mcp_server.assert_awaited_once_with("remote-tools", 4)
    service.get_mcp_credentials.assert_awaited_once_with("remote-tools", 4)


async def test_external_mcp_rejects_disabled_or_missing_credentials():
    service = AsyncMock()
    service.get_mcp_server.return_value = {
        "name": "remote-tools",
        "version": 1,
        "enabled": False,
    }
    binding = McpBinding(name="remote-tools", version=1, allowed_tools=["ping"])
    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="unavailable"):
            await external_mcp_tools(service, [binding], http)
        service.get_mcp_server.return_value.update(
            enabled=True,
            url="https://external.test/mcp",
            has_secret=True,
            auth_header="Authorization",
            auth_scheme="Bearer",
        )
        service.get_mcp_credentials.return_value = {"secret": None}
        with pytest.raises(ValueError, match="credential"):
            await external_mcp_tools(service, [binding], http)


async def test_published_mcp_uses_exact_sdk_wire_grant_and_refuses_schema_or_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    import asyncio
    import json
    import logging
    import re
    from copy import deepcopy
    from types import SimpleNamespace
    from typing import cast

    from bbx_contracts.mcp_catalog import tool_schema_digest
    from bbx_runtime.context import RunContext
    from bbx_runtime.mcp_egress import PinnedMcpTransport
    from bbx_runtime.middleware import ToolLogMiddleware
    from bbx_runtime.pi import AgentSession, FunctionInvocationContext
    from bbx_runtime.session import EXTERNAL_MCP_PENDING, ExternalMcpUnconfirmed, SessionCheckpoint

    secret = "fixture-protected-mcp-credential"
    seen = []
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "published-fixture",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=["fixture.test"]),
    )

    @mcp.tool(
        name="docs.find", description="Unapproved remote description", structured_output=False
    )
    async def find(phrase: str, ctx: Context) -> str:
        agent = "echo" if phrase == "echo" else "first"
        assert (
            saved[agent]["session"]["state"][EXTERNAL_MCP_PENDING]["call_id"]
            == {
                "safe": "good-original",
                "drop": "lost-original",
                "echo": "echo-original",
            }[phrase]
        )
        request = ctx.request_context.request
        assert request is not None
        seen.append((phrase, request.headers.get("authorization")))
        return secret if phrase == "echo" else "synthetic result"

    @mcp.tool(name="docs.hidden")
    async def hidden() -> str:
        pytest.fail("An unapproved tool was dispatched")

    schema = mcp._tool_manager.get_tool("docs.find").parameters  # type: ignore[union-attr]
    frozen = {
        "name": "docs.find",
        "description": "Approved description",
        "input_schema": schema,
        "output_schema": None,
        "schema_digest": tool_schema_digest(schema, None),
    }
    binding = McpBinding(
        name="remote-tools", version=4, allowed_tools=["docs.find"], catalog_version="p6b"
    )
    service = AsyncMock()
    saved = {}
    persisted = []

    async def put(_task, agent, *, expected_revision, session, opening_instructions, origin, **_):
        assert expected_revision == saved.get(agent, {}).get("revision", 0)
        value = {
            "revision": expected_revision + 1,
            "session": deepcopy(session),
            "opening_instructions": opening_instructions,
            "origin": origin,
        }
        saved[agent] = value
        persisted.append((agent, deepcopy(session["state"])))
        return value

    service.put_agent_session.side_effect = put
    service.get_agent_session.side_effect = lambda _task, agent: deepcopy(saved[agent])
    service.get_mcp_material.return_value = {
        "name": "remote-tools",
        "version": 4,
        "url": "https://fixture.test/mcp",
        "auth_header": "Authorization",
        "auth_scheme": "Bearer",
        "enabled": True,
        "secret": secret,
        "published": {"roles": ["lead", "explore"], "tools": [frozen]},
    }
    monkeypatch.setattr(
        "bbx_runtime.runner.provider_addresses", AsyncMock(return_value=["93.184.215.14"])
    )
    app = mcp.streamable_http_app()
    malformed = False
    malformed_seen = False

    def isolated_transport(url, address):
        pinned = PinnedMcpTransport(url, address)
        asgi = httpx.ASGITransport(app=app)

        async def verify_wire(request):
            nonlocal malformed_seen
            assert request.url.host == "93.184.215.14"
            assert request.headers["host"] == "fixture.test"
            assert request.headers["accept-encoding"] == "identity"
            assert request.extensions["sni_hostname"] == "fixture.test"
            assert request.url.path == "/mcp"
            if (
                malformed
                and request.method == "POST"
                and json.loads(await request.aread()).get("method") == "initialize"
            ):
                malformed_seen = True
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 0, "result": {"unexpected": secret}}
                )
            before = len(seen)
            response = await asgi.handle_async_request(request)
            if len(seen) != before and seen[-1][0] == "drop":
                await response.aclose()
                raise httpx.ReadError("synthetic reply loss")
            return response

        monkeypatch.setattr(pinned.transport, "handle_async_request", verify_wire)
        return pinned

    monkeypatch.setattr("bbx_runtime.runner.PinnedMcpTransport", isolated_transport)
    async with mcp.session_manager.run(), httpx.AsyncClient() as unused:

        async def connector_for(checkpoint):
            (connector,) = await external_mcp_tools(
                service, [binding], unused, [], "fixture-runtime", checkpoint
            )
            connector.request_timeout = 0.5
            return connector

        first = SessionCheckpoint(service, "task", "first", AgentSession())
        first.expected_started_at = "2026-11-10T00:00:00Z"
        connector = await connector_for(first)
        with pytest.raises(RuntimeError, match="Published MCP connection terminated"):
            async with connector:
                (function,) = connector.functions
                assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", function.name)
                assert function.description == "Approved description"
                invalid = FunctionInvocationContext(function, {"phrase": 5}, first.session)
                with pytest.raises(ValueError, match="approved MCP tool schema"):
                    await function.invoke(arguments=invalid.arguments, context=invalid)
                assert not seen and not persisted

                context = FunctionInvocationContext(
                    function, {"phrase": "safe"}, first.session, {"call_id": "good-original"}
                )
                log = ToolLogMiddleware(
                    cast(
                        RunContext,
                        SimpleNamespace(
                            checkpoint=first,
                            task_id="task",
                            agent_id="first",
                            expected_derive_round=None,
                            service=service,
                            objects=AsyncMock(),
                        ),
                    )
                )

                async def execute():
                    context.result = await function.invoke(
                        arguments=context.arguments, context=context
                    )

                await log.process(context, execute)
                assert "synthetic result" in (context.result[0].text or "")
                first_write, settled_write = [
                    state for agent, state in persisted if agent == "first"
                ]
                marker = first_write[EXTERNAL_MCP_PENDING]
                assert marker["call_id"] == "good-original"
                assert EXTERNAL_MCP_PENDING not in settled_write
                assert settled_write["bbx_external_mcp_completed"] == marker
                assert (
                    settled_write["bbx_tool_results"]["good-original"]["type"] == "function_result"
                )
                assert all(
                    item.kwargs["expected_started_at"] == first.expected_started_at
                    for item in service.put_agent_session.await_args_list
                )

                dropped = FunctionInvocationContext(
                    function, {"phrase": "drop"}, first.session, {"call_id": "lost-original"}
                )
                with pytest.raises((RuntimeError, TimeoutError)):
                    async with asyncio.timeout(2):
                        await function.invoke(arguments=dropped.arguments, context=dropped)
        restored = await SessionCheckpoint.load(service, "task", "first")
        assert restored is not None and restored.external_mcp_pending
        assert restored.session.state[EXTERNAL_MCP_PENDING]["call_id"] == "lost-original"
        async with await connector_for(restored) as reopened:
            (function,) = reopened.functions
            retry = FunctionInvocationContext(
                function, {"phrase": "drop"}, restored.session, {"call_id": "new-tool-id"}
            )
            with pytest.raises(ExternalMcpUnconfirmed, match="original external MCP call"):
                await function.invoke(arguments=retry.arguments, context=retry)
        assert [phrase for phrase, _ in seen] == ["safe", "drop"]

        other = SessionCheckpoint(service, "task", "echo", AgentSession())
        other.expected_started_at = first.expected_started_at
        async with await connector_for(other) as separate:
            (function,) = separate.functions
            echo = FunctionInvocationContext(
                function, {"phrase": "echo"}, other.session, {"call_id": "echo-original"}
            )
            with pytest.raises(RuntimeError, match="protected credential data"):
                await function.invoke(arguments=echo.arguments, context=echo)
        assert other.external_mcp_pending
        assert seen == [(phrase, "Bearer " + secret) for phrase in ["safe", "drop", "echo"]]

        changed = {"type": "object", "properties": {"phrase": {"type": "integer"}}}
        frozen.update(input_schema=changed, schema_digest=tool_schema_digest(changed, None))
        with pytest.raises(RuntimeError, match="schema verification failed"):
            await (await connector_for(first)).connect()
        assert len(seen) == 3
        malformed = True
        with pytest.raises(RuntimeError, match="schema verification failed"):
            await (await connector_for(first)).connect()
        assert malformed_seen
        logging.getLogger("client").warning("fixture normal logging")
        assert "fixture normal logging" in caplog.text and secret not in caplog.text
    service.get_mcp_server.assert_not_awaited()
    service.get_mcp_credentials.assert_not_awaited()


async def test_sandbox_direct_uses_sdk_only_after_scoped_authorization(
    monkeypatch: pytest.MonkeyPatch,
):
    from bbx_runtime.sandbox_mcp import SandboxMCP
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    host = SandboxMCP({"mcp_transport": "direct-v2", "inbound_key": "old-fixture"})
    host.ready = True
    checked = []

    async def control(method, route, body=None, *, authorization=None, sdk=None):
        if authorization != "Bearer fixture-sandbox" or sdk != "sdk-frozen":
            return {"authorized": False}
        checked.append((method, route, body))
        return ["read"] if route == "catalog" else {"authorized": True}

    monkeypatch.setattr(host, "control", control)
    async with (
        host.manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host.app, client=("127.0.0.1", 41234)),
            base_url="http://127.0.0.1:8766",
            headers={
                "authorization": "Bearer fixture-sandbox",
                "x-bbx-sdk-session-id": "sdk-frozen",
            },
        ) as client,
    ):
        async with streamable_http_client("http://127.0.0.1:8766/mcp/", http_client=client) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                assert (await session.initialize()).serverInfo.name == "bbx-sandbox-v1"
                await session.list_tools()
        assert any(
            body == {"method": "tools/list", "sdk_session_id": "sdk-frozen"}
            for method, route, body in checked
            if route == "authorize"
        )
        assert ("GET", "catalog", None) in checked
        packet = {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}
        legacy = await client.post(
            "/mcp/", headers={"x-bbx-sandbox-inbound": "old-fixture"}, json=packet
        )
        wrong_scope = await client.post(
            "/mcp/", headers={"authorization": "Bearer wrong"}, json=packet
        )
        assert legacy.status_code == 401
        assert wrong_scope.status_code == 503
        assert sum(route == "catalog" for _, route, _ in checked) == 1
