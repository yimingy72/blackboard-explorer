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
    binding = McpBinding(name="remote-tools", version=1)
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
