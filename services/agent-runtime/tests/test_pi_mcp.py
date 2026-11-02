"""Pinned MCP discovery, dispatch and result conversion through native Pi."""

import json
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from bbx_runtime.pi import Agent, MCPStreamableHTTPTool
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ListToolsResult,
    ResourceLink,
    TextContent,
    TextResourceContents,
    Tool,
)
from pydantic import AnyUrl


class CatalogSession:
    def __init__(self, pages=None, result=None):
        self.pages = pages or {}
        self.result = result or CallToolResult(content=[TextContent(type="text", text="saved")])
        self.requests = []
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def initialize(self):
        pass

    async def list_tools(self, cursor=None):
        self.requests.append(cursor)
        return self.pages[cursor]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def definition(name):
    return Tool(
        name=name,
        inputSchema={"type": "object", "properties": {}, "additionalProperties": True},
    )


def mock_connection(monkeypatch, session, **kwargs):
    @asynccontextmanager
    async def transport(*_args, **_kwargs):
        yield None, None, None

    monkeypatch.setattr("bbx_runtime.pi.tools.streamable_http_client", transport)
    monkeypatch.setattr("bbx_runtime.pi.tools.ClientSession", lambda *_a, **_k: session)
    return MCPStreamableHTTPTool(name="offline", url="http://mcp.invalid/mcp", **kwargs)


async def test_native_pi_mcp_target_cannot_be_overridden_by_model_arguments(monkeypatch):
    session = CatalogSession(
        {None: ListToolsResult(tools=[definition("allowed_read"), definition("allowed_status")])}
    )
    connection = mock_connection(
        monkeypatch, session, allowed_tools=["allowed_read", "allowed_status"]
    )
    arguments = {"_remote_name": "not_allowed_write", "tool_name": "legitimate field"}
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall("allowed_read", arguments),
                    ScriptToolCall("allowed_status", {}),
                )
            ),
            ScriptStep(text="done"),
        ]
    )
    async with connection:
        response = await Agent(client=client, tools=[connection]).run("inspect offline")
    assert response.text == "done"
    assert session.calls == [("allowed_read", arguments), ("allowed_status", {})]


async def test_mcp_dispatch_enforces_an_immutable_allowlist():
    session = CatalogSession()
    allowed = ["read"]
    connection = MCPStreamableHTTPTool(
        name="offline",
        url="http://mcp.invalid/mcp",
        session=cast(Any, session),
        allowed_tools=allowed,
    )
    allowed.append("write")
    with pytest.raises(PermissionError, match="allowlist"):
        await connection.call_tool("write")
    assert session.calls == []


async def test_mcp_loads_selected_tools_from_all_pages_and_reconnects_cleanly(monkeypatch):
    session = CatalogSession(
        {
            None: ListToolsResult(tools=[definition("first_page")], nextCursor="page-2"),
            "page-2": ListToolsResult(tools=[definition("selected_second_page")]),
        }
    )
    connection = mock_connection(monkeypatch, session, allowed_tools=["selected_second_page"])
    for _ in range(2):
        async with connection:
            assert [function.name for function in connection.functions] == ["selected_second_page"]
    assert session.requests == [None, "page-2", None, "page-2"]


async def test_mcp_repeated_cursor_refuses_partial_catalogue_and_closes(monkeypatch):
    session = CatalogSession(
        {
            None: ListToolsResult(tools=[definition("first_page")], nextCursor="repeat"),
            "repeat": ListToolsResult(tools=[definition("second_page")], nextCursor="repeat"),
        }
    )
    connection = mock_connection(monkeypatch, session)
    with pytest.raises(ValueError, match="Repeated MCP cursor"):
        await connection.connect()
    assert session.requests == [None, "repeat"]
    assert connection.functions == [] and connection.session is None
    assert connection._owner is None


async def test_native_pi_receives_embedded_text_evidence():
    session = CatalogSession(
        result=CallToolResult(
            content=[
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri=AnyUrl("file:///synthetic/evidence.txt"),
                        mimeType="text/plain",
                        text="synthetic required evidence",
                    ),
                )
            ]
        )
    )
    connection = MCPStreamableHTTPTool(
        name="offline", url="http://mcp.invalid/mcp", session=cast(Any, session)
    )
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("read_evidence"),)),
            ScriptStep(text="done", expect_contains="synthetic required evidence"),
        ]
    )
    connection.functions = [connection._function(definition("read_evidence"))]
    actor = Agent(client=client, tools=[connection])
    history = actor.create_session()
    assert (await actor.run("read offline", session=history)).text == "done"
    contents = [
        item
        for message in history.state["in_memory"]["messages"]
        for content in message.contents
        for item in content.items or []
    ]
    assert contents[0].additional_properties["mcp_resource"]["uri"].endswith("evidence.txt")


@pytest.mark.parametrize(
    "content",
    [
        ResourceLink(
            type="resource_link", name="evidence", uri=AnyUrl("https://evidence.invalid/1")
        ),
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=AnyUrl("file:///synthetic/data.bin"),
                mimeType="application/octet-stream",
                blob="AA==",
            ),
        ),
    ],
)
async def test_other_mcp_resource_types_keep_a_canonical_representation(content: Any):
    connection = MCPStreamableHTTPTool(
        name="offline",
        url="http://mcp.invalid/mcp",
        session=cast(Any, CatalogSession(result=CallToolResult(content=[content]))),
    )
    result = await connection.call_tool("read_evidence")
    assert json.loads(result[0].text or "") == content.model_dump(mode="json", by_alias=True)


async def test_mcp_preserves_native_images_and_structured_first_policy():
    session = CatalogSession(
        result=CallToolResult(
            content=[ImageContent(type="image", data="AA==", mimeType="image/png")]
        )
    )
    connection = MCPStreamableHTTPTool(
        name="offline", url="http://mcp.invalid/mcp", session=cast(Any, session)
    )
    image = (await connection.call_tool("view"))[0]
    assert image.type == "data" and image.uri == "data:image/png;base64,AA=="
    session.result.structuredContent = {"status": "saved"}
    result = await connection.call_tool("view")
    assert len(result) == 1 and json.loads(result[0].text or "") == {"status": "saved"}
