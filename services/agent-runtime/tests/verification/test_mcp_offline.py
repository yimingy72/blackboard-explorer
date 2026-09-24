"""MAF's MCP wrapper against FakeEnvd's real streamable HTTP transport."""

import asyncio
from pathlib import Path

import httpx
from agent_framework import Agent, FunctionMiddleware, MCPStreamableHTTPTool
from bbx_runtime.testing.fake_envd import CommandOutput, FakeEnvd
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall


async def _connected_tool(fake: FakeEnvd, http: httpx.AsyncClient) -> MCPStreamableHTTPTool:
    response = await http.post(
        "/users",
        headers={"Authorization": f"Bearer {fake.token}"},
        json={"agent_id": "agent-1"},
    )
    assert response.status_code == 200
    return MCPStreamableHTTPTool(
        name="exec",
        url="http://fake/mcp",
        static_headers={"Authorization": f"Bearer {fake.token}", "X-Agent-Id": "agent-1"},
        http_client=http,
    )


async def test_08_mcp_static_headers_and_two_concurrent_commands(tmp_path: Path):
    fake = FakeEnvd(
        tmp_path,
        command_outputs={
            "first": CommandOutput(stdout="alpha", delay=0.05),
            "second": CommandOutput(stdout="beta", delay=0.05),
        },
    )
    transport = httpx.ASGITransport(app=fake.app)
    async with fake, httpx.AsyncClient(transport=transport, base_url="http://fake") as http:
        tool = await _connected_tool(fake, http)
        async with tool:
            first, second = await asyncio.gather(
                tool.call_tool("execute_command", command="first"),
                tool.call_tool("execute_command", command="second"),
            )
    first_text = first if isinstance(first, str) else "\n".join(item.text or "" for item in first)
    second_text = (
        second if isinstance(second, str) else "\n".join(item.text or "" for item in second)
    )
    assert "alpha" in first_text and "beta" in second_text
    assert fake.commands == ["first", "second"]
    assert fake.max_concurrent_commands == 2
    assert {call["agent_id"] for call in fake.calls} == {"agent-1"}


async def test_06_mcp_tool_result_can_be_replaced_without_calling_envd(tmp_path: Path):
    fake = FakeEnvd(tmp_path, command_outputs={"blocked": CommandOutput(stdout="real")})
    transport = httpx.ASGITransport(app=fake.app)

    class Replace(FunctionMiddleware):
        async def process(self, context, call_next) -> None:
            if context.function.name == "execute_command":
                context.result = "MCP call blocked by grace gate"
                return
            await call_next()

    async with fake, httpx.AsyncClient(transport=transport, base_url="http://fake") as http:
        tool = await _connected_tool(fake, http)
        async with tool:
            client = ScriptedChatClient(
                [
                    ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "blocked"}),)),
                    ScriptStep(text="continued", expect_contains="MCP call blocked by grace gate"),
                ]
            )
            response = await Agent(client=client, tools=[tool], middleware=[Replace()]).run("start")
    assert response.text == "continued"
    assert fake.commands == []
