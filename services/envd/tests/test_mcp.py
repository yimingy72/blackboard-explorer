import asyncio
import importlib
import os

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


@pytest.mark.asyncio
async def test_audit_export_reports_missing_and_oversized_logs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ENVD_TOKEN", "replace-me")
    service = importlib.import_module("bbx_envd.app")
    commands = tmp_path / "commands.jsonl"
    sudo = tmp_path / "sudo.log"
    monkeypatch.setattr(service, "COMMAND_LOG", commands)
    monkeypatch.setattr(service, "SUDO_LOG", sudo)
    monkeypatch.setattr(service, "AUDIT_MAX_BYTES", 10)
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(
        transport=transport, headers={"Authorization": "Bearer replace-me"}
    ) as client:
        assert (await client.get("http://test/audit")).status_code == 503
        commands.write_text("abc")
        sudo.write_text("def")
        assert (await client.get("http://test/audit")).json() == {
            "format": "bbx.runtime-audit.v1",
            "commands": "abc",
            "sudo": "def",
        }
        sudo.write_text("x" * 8)
        assert (await client.get("http://test/audit")).status_code == 413
        sudo.write_text("x" * 11)
        assert (await client.get("http://test/audit")).status_code == 413
        sudo.unlink()
        secret = tmp_path / "secret"
        secret.write_text("do-not-export")
        sudo.symlink_to(secret)
        response = await client.get("http://test/audit")
        assert response.status_code == 503
        assert "do-not-export" not in response.text
        sudo.unlink()
        os.mkfifo(sudo)
        response = await asyncio.wait_for(client.get("http://test/audit"), 1)
        assert response.status_code == 503
