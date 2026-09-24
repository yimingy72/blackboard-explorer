import importlib

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


@pytest.mark.asyncio
async def test_mcp_transport_and_auth(monkeypatch) -> None:
    monkeypatch.setenv("ENVD_TOKEN", "replace-me")
    service = importlib.import_module("bbx_envd.app")
    async with service.mcp.session_manager.run():
        transport = httpx.ASGITransport(app=service.app)
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await client.post("http://test/mcp")).status_code == 401
        headers = {"Authorization": "Bearer replace-me", "X-Agent-Id": "agent-999999"}
        async with httpx.AsyncClient(transport=transport, headers=headers) as client:
            async with streamable_http_client("http://test/mcp", http_client=client) as (
                reader,
                writer,
                _,
            ):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert [tool.name for tool in tools.tools] == ["execute_command"]
                    result = await session.call_tool("execute_command", {"command": "id"})
                    assert result.isError
