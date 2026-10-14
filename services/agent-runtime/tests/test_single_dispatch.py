"""The built-in command keeps native JSON results without SDK or model replay."""

import asyncio
import json
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from agent_framework import Agent, MCPStreamableHTTPTool, tool
from anyio import ClosedResourceError
from bbx_runtime.clients.envd import EnvdClient
from bbx_runtime.middleware import ToolLogMiddleware
from bbx_runtime.single_dispatch import SingleDispatchExecutionTool
from bbx_runtime.testing.fake_envd import CommandOutput, FakeEnvd
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from bbx_runtime.tool_failures import BlackboardToolFailure
from mcp.types import CallToolResult, ImageContent, TextContent
from test_middleware import FakeService, run_context


class Session:
    def __init__(self, response=None, failure=None):
        self.calls = []
        self.response = response or CallToolResult(
            content=[TextContent(type="text", text='{"stdout":"synthetic"}')],
            structuredContent={"stdout": "synthetic", "exit_code": 0},
        )
        self.failure = failure

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None, meta=None):
        self.calls.append((name, arguments))
        if self.failure:
            raise self.failure
        return self.response


def execution_tool(session):
    item = SingleDispatchExecutionTool(
        name="exec",
        url="http://synthetic.invalid/mcp",
        session=cast(Any, session),
        load_tools=True,
        load_prompts=False,
        sampling_max_requests=0,
    )
    item.connect = AsyncMock()
    return item


async def test_single_public_call_does_not_reconnect_or_repeat_uncertain_command():
    session = Session(failure=ClosedResourceError("synthetic lost response after effect"))
    item = execution_tool(session)
    with pytest.raises(ClosedResourceError):
        await item.call_tool("execute_command", command="synthetic effect")
    assert len(session.calls) == 1
    cast(AsyncMock, item.connect).assert_not_awaited()


async def test_structured_json_matches_native_maf_and_drops_runtime_context_arguments():
    session = Session()
    item = execution_tool(session)
    result = await item.call_tool(
        "execute_command",
        command="synthetic",
        cwd=None,
        timeout_sec=120,
        privileged=False,
        session=object(),
        conversation_id="private runtime context",
    )
    assert session.calls == [
        (
            "execute_command",
            {
                "command": "synthetic",
                "cwd": None,
                "timeout_sec": 120,
                "privileged": False,
            },
        )
    ]
    # Compare through the public original MCP call; the original filters runtime fields too.
    original = MCPStreamableHTTPTool(
        name="exec",
        url="http://synthetic.invalid/mcp",
        session=cast(Any, Session()),
        load_tools=True,
        load_prompts=False,
        sampling_max_requests=0,
    )
    native = await original.call_tool("execute_command")
    assert isinstance(native, list)
    assert [content.to_dict() for content in result] == [content.to_dict() for content in native]


@pytest.mark.parametrize(
    "response",
    [
        CallToolResult(content=[TextContent(type="text", text="unexpected text")]),
        CallToolResult(content=[ImageContent(type="image", data="AA==", mimeType="image/png")]),
        CallToolResult(
            content=[TextContent(type="text", text="private server error")], isError=True
        ),
    ],
)
async def test_unsupported_or_error_reply_fails_closed_instead_of_silently_changing_content(
    response,
):
    item = execution_tool(Session(response=response))
    with pytest.raises(RuntimeError):
        await item.call_tool("execute_command", command="synthetic")


async def test_native_tool_pipeline_never_repeats_single_dispatch_unknown_effect():
    session = Session(failure=ClosedResourceError("synthetic lost response after effect"))
    item = execution_tool(session)
    context = run_context(FakeService())

    @tool
    async def execute_command(command: str):
        return await item.call_tool("execute_command", command=command)

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "synthetic"}),)),
            ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "synthetic"}),)),
            ScriptStep(text="must not repeat"),
        ]
    )
    async with Agent(
        client=client, tools=[execute_command], middleware=[ToolLogMiddleware(context)]
    ) as agent:
        with pytest.raises(BlackboardToolFailure):
            await agent.run("Synthetic command")
    assert len(session.calls) == len(client.received_messages) == 1
    cast(AsyncMock, item.connect).assert_not_awaited()


@pytest.mark.parametrize("drop_response", [False, True])
async def test_real_maf_mcp_lifecycle_sends_one_command_and_keeps_json_contract(
    tmp_path, drop_response
):
    fake = FakeEnvd(
        tmp_path, command_outputs={"inspect": CommandOutput(stdout="synthetic observed")}
    )

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.delegate = httpx.ASGITransport(app=fake.app)
            self.command_requests = 0

        async def handle_async_request(self, request):
            body = await request.aread()
            payload = json.loads(body) if body else {}
            response = await self.delegate.handle_async_request(request)
            if payload.get("method") == "tools/call":
                self.command_requests += 1
                if drop_response:
                    await response.aclose()
                    raise httpx.ReadError(
                        "synthetic committed command response lost", request=request
                    )
            return response

    transport = Transport()
    async with fake, httpx.AsyncClient(transport=transport, trust_env=False) as http:
        envd = EnvdClient("http://synthetic.invalid", fake.token, http)
        await envd.create_user("agent-1")
        item = SingleDispatchExecutionTool(
            name="exec",
            url="http://synthetic.invalid/mcp",
            static_headers={**envd.headers, "X-Agent-Id": "agent-1"},
            http_client=http,
            load_prompts=False,
            sampling_max_requests=0,
            request_timeout=1,
        )
        context = run_context(FakeService())
        client = ScriptedChatClient(
            [
                ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "inspect"}),)),
                ScriptStep(text="done", expect_contains="synthetic observed"),
            ]
        )
        async with Agent(
            client=client, tools=[item], middleware=[ToolLogMiddleware(context)]
        ) as agent:
            if drop_response:
                with pytest.raises(BlackboardToolFailure):
                    async with asyncio.timeout(5):
                        await agent.run("Synthetic command")
            else:
                async with asyncio.timeout(5):
                    assert (await agent.run("Synthetic command")).text == "done"
        assert fake.commands == ["inspect"]
        assert transport.command_requests == 1
