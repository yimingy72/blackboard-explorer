"""Native image delivery keeps bytes out of tool logs and persisted sessions."""

from __future__ import annotations

import asyncio
import hashlib
import struct
import zlib
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agent_framework import (
    Agent,
    AgentSession,
    ChatContext,
    ChatMiddleware,
    ChatResponseUpdate,
    Content,
    Message,
    tool,
)
from agent_framework.amazon import BedrockChatClient
from agent_framework.anthropic import AnthropicClient
from agent_framework.foundry import FoundryChatClient
from agent_framework.gemini import GeminiChatClient
from agent_framework.mistral import MistralChatClient
from agent_framework.ollama import OllamaChatClient
from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient
from bbx_runtime.clients import BlackboardClient, RemoteError
from bbx_runtime.context import RunContext
from bbx_runtime.image_view import (
    IMAGE_STATE_KEY,
    ImageViewMiddleware,
    append_pending_images,
    finish_pending_images,
    make_view_image_tool,
)
from bbx_runtime.session import CheckpointHistoryProvider, SessionCheckpoint
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall

TASK = "11111111-1111-4111-8111-111111111111"


def png() -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        body = name + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        header
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


class MemoryObjects:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def exists(self, uri: str) -> bool:
        return uri in self.data

    async def put(self, uri: str, data: bytes, **_: Any) -> None:
        self.data[uri] = data

    async def get(self, uri: str) -> bytes:
        return self.data[uri]


class Envd:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def stat(self, path: str) -> dict[str, Any]:
        return {"is_file": path == "/workspace/image.png", "size": len(self.data)}

    async def read_file(self, path: str) -> bytes:
        assert path == "/workspace/image.png"
        return self.data


class Board:
    def __init__(self, objects: MemoryObjects) -> None:
        self.objects = objects

    async def read_evidence(self, uri: str) -> bytes:
        return self.objects.data[uri]


def context(data: bytes | None = None) -> tuple[RunContext, MemoryObjects]:
    objects = MemoryObjects()
    board = Board(objects)
    ctx = SimpleNamespace(
        task_id=TASK,
        agent_id="agent-1",
        task_type="explore",
        profile=SimpleNamespace(
            models=SimpleNamespace(explore=SimpleNamespace(provider="gemini", model="fake"))
        ),
        checkpoint=SimpleNamespace(session=AgentSession()),
        envd=Envd(data or png()),
        board=board,
        objects=objects,
    )
    return cast(RunContext, ctx), objects


async def invoke(ctx: RunContext, path_or_uri: str) -> str:
    result = await make_view_image_tool(ctx).invoke(arguments={"path_or_uri": path_or_uri})
    assert result and result[0].text is not None
    return result[0].text


async def test_view_image_stages_native_content_once_and_persists_only_reference() -> None:
    ctx, objects = context()
    result = await invoke(ctx, "/workspace/image.png")
    digest = hashlib.sha256(png()).hexdigest()
    uri = f"evidence/{TASK}/agent-1/view-{digest}.png"
    assert uri in result
    assert objects.data[uri] == png()
    checkpoint = ctx.checkpoint
    assert checkpoint is not None
    pending = checkpoint.session.state[IMAGE_STATE_KEY]
    assert pending[0]["sha256"] == digest
    assert "data:image" not in str(checkpoint.session.to_dict())

    messages: list[Message] = []
    injection = await append_pending_images(ctx, messages)
    assert len(injection.new) == 1
    assert injection.new[0].contents[1].type == "data"
    assert (injection.new[0].contents[1].uri or "").startswith("data:image/png;base64,")
    original_image_uri = injection.new[0].contents[1].uri
    finish_pending_images(messages, injection, sent=True)
    assert [part.type for part in injection.new[0].contents] == ["text"]
    assert "data:image" not in str(injection.new[0].to_dict())

    # A failed checkpoint retains pending. A saved native message proves delivery.
    checkpoint.session.state["in_memory"] = {"messages": injection.new}
    next_messages = [*injection.new]
    second = await append_pending_images(ctx, next_messages)
    assert second.new == []
    assert second.hydrated[0].contents[1].uri == original_image_uri
    finish_pending_images(next_messages, second, sent=True)
    assert checkpoint.session.state[IMAGE_STATE_KEY] == []


async def test_view_image_rejects_foreign_uri_invalid_format_and_unsupported_client() -> None:
    ctx, objects = context(b"not a picture")
    assert "无效" in await invoke(ctx, "/workspace/image.png")
    assert not objects.data
    assert "无效" in await invoke(ctx, f"evidence/{'2' * 36}/agent-1/file.png")
    assert "无效" in await invoke(ctx, "https://example.com/image.png")
    ctx.profile.models.explore.provider = "bedrock"
    assert "丢弃图片" in await invoke(ctx, "/workspace/image.png")
    ctx.profile.models.explore.provider = "gemini"
    ctx.profile.models.explore.supports_vision = False
    assert "明确不支持" in await invoke(ctx, "/workspace/image.png")
    ctx.profile.models.explore.supports_vision = None
    ctx.profile.models.explore.provider = "deepseek"
    ctx.profile.models.explore.model = "deepseek-v4-pro"
    assert "不支持图片" in await invoke(ctx, "/workspace/image.png")
    checkpoint = ctx.checkpoint
    assert checkpoint is not None
    assert not checkpoint.session.state.get(IMAGE_STATE_KEY)


async def test_derive_view_stays_read_only_even_if_an_envd_client_is_present() -> None:
    ctx, objects = context()
    source = f"evidence/{TASK}/other-agent/proof.png"
    objects.data[source] = png()
    ctx.task_type = "derive"
    ctx.profile.models.derive = ctx.profile.models.explore
    assert "下一次模型调用" in await invoke(ctx, source)
    assert "只能查看" in await invoke(ctx, "/workspace/image.png")
    assert list(objects.data) == [source]


async def test_read_only_view_never_reads_workspace_or_writes_objects() -> None:
    ctx, objects = context()
    source = f"evidence/{TASK}/other-agent/proof.png"
    objects.data[source] = png()
    tool = make_view_image_tool(ctx, read_only=True)
    denied = await tool.invoke(arguments={"path_or_uri": "/workspace/image.png"})
    assert "只能查看" in (denied[0].text or "")
    assert list(objects.data) == [source]
    allowed = await tool.invoke(arguments={"path_or_uri": source})
    assert source in (allowed[0].text or "")
    assert list(objects.data) == [source]


async def test_view_image_rechecks_persisted_bytes_before_delivery() -> None:
    ctx, objects = context()
    await invoke(ctx, "/workspace/image.png")
    checkpoint = ctx.checkpoint
    assert checkpoint is not None
    uri = checkpoint.session.state[IMAGE_STATE_KEY][0]["uri"]
    objects.data[uri] = b"changed"
    try:
        await append_pending_images(ctx, [])
    except ValueError as exc:
        assert "integrity" in str(exc)
    else:
        raise AssertionError("corrupt image was injected")


class SessionService:
    def __init__(self, objects: MemoryObjects) -> None:
        self.saved: dict[str, Any] | None = None
        self.objects = objects
        self.snapshots: list[dict[str, Any]] = []

    async def read_evidence(self, uri: str) -> bytes:
        return self.objects.data[uri]

    async def get_agent_session(self, _task_id: str, _agent_id: str) -> dict[str, Any]:
        if self.saved is None:
            raise RemoteError(404, "missing")
        return self.saved

    async def put_agent_session(
        self,
        _task_id: str,
        _agent_id: str,
        *,
        session: dict[str, Any],
        opening_instructions: str,
        origin: str,
        expected_revision: int,
        deliveries: list[Any],
        review_claim: Any,
    ) -> dict[str, Any]:
        self.saved = {
            "session": session,
            "opening_instructions": opening_instructions,
            "origin": origin,
            "revision": expected_revision + 1,
        }
        self.snapshots.append(self.saved)
        return self.saved


class ImageMiddleware(ChatMiddleware):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def process(self, context: ChatContext, call_next: Any) -> None:
        messages = cast(list[Message], context.messages)
        injection = await append_pending_images(self.ctx, messages)
        sent = False
        try:
            await call_next()
            sent = True
        finally:
            finish_pending_images(messages, injection, sent=sent)


async def test_native_maf_two_calls_keep_same_image_and_small_session() -> None:
    ctx, objects = context()
    service = SessionService(objects)
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), TASK, "agent-1", AgentSession())
    ctx.checkpoint = checkpoint
    await invoke(ctx, "/workspace/image.png")

    first = ScriptedChatClient([ScriptStep(text="saw it")])
    async with Agent(
        client=first,
        context_providers=[CheckpointHistoryProvider(checkpoint)],
        middleware=[ImageMiddleware(ctx)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        await agent.run("look", session=checkpoint.session)
    first_images = [
        part.uri
        for message in first.received_messages[0]
        for part in message.contents
        if part.type == "data"
    ]
    assert len(first_images) == 1
    assert service.saved is not None
    assert "data:image" not in str(service.saved)

    restored = await SessionCheckpoint.load(cast(BlackboardClient, service), TASK, "agent-1")
    assert restored is not None
    ctx.checkpoint = restored
    second = ScriptedChatClient([ScriptStep(text="still visible")])
    async with Agent(
        client=second,
        context_providers=[CheckpointHistoryProvider(restored)],
        middleware=[ImageMiddleware(ctx)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        await agent.run("continue", session=restored.session)
    second_images = [
        part.uri
        for message in second.received_messages[0]
        for part in message.contents
        if part.type == "data"
    ]
    assert second_images == first_images
    first_prefix = [
        (message.role, [part.to_dict() for part in message.contents])
        for message in first.received_messages[0]
    ]
    second_prefix = [
        (message.role, [part.to_dict() for part in message.contents])
        for message in second.received_messages[0][: len(first_prefix)]
    ]
    assert second_prefix == first_prefix
    assert objects.data
    assert service.saved is not None and "data:image" not in str(service.saved)


async def test_stream_cancel_removes_transient_image_bytes_and_keeps_pending_reference() -> None:
    ctx, objects = context()
    service = SessionService(objects)
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), TASK, "agent-1", AgentSession())
    ctx.checkpoint = checkpoint
    await invoke(ctx, "/workspace/image.png")
    started = asyncio.Event()

    class WaitingStream(ScriptedChatClient):
        def _inner_get_response(self, *, stream, **kwargs):
            assert stream

            async def updates():
                yield ChatResponseUpdate(role="assistant", contents=[Content.from_text("partial")])
                started.set()
                await asyncio.Event().wait()

            return self._build_response_stream(updates())

    client = WaitingStream([])
    async with Agent(
        client=client,
        context_providers=[CheckpointHistoryProvider(checkpoint)],
        middleware=[ImageViewMiddleware(ctx)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        task = asyncio.create_task(
            agent.run("look", stream=True, session=checkpoint.session).get_final_response()
        )
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert checkpoint.session.state[IMAGE_STATE_KEY]
    assert "data:image" not in str(checkpoint.session.to_dict())


async def test_maf_tool_loop_keeps_image_in_all_following_calls() -> None:
    ctx, objects = context()
    service = SessionService(objects)
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), TASK, "agent-1", AgentSession())
    ctx.checkpoint = checkpoint

    @tool
    async def noop() -> str:
        return "continue"

    script = ScriptedChatClient(
        [
            ScriptStep(
                calls=(ScriptToolCall("view_image", {"path_or_uri": "/workspace/image.png"}),)
            ),
            ScriptStep(calls=(ScriptToolCall("noop"),)),
            ScriptStep(text="done"),
        ]
    )
    async with Agent(
        client=script,
        tools=[make_view_image_tool(ctx), noop],
        context_providers=[CheckpointHistoryProvider(checkpoint)],
        middleware=[ImageMiddleware(ctx)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        await agent.run("start", session=checkpoint.session)
    assert len(script.received_messages) == 3
    image_uris = [
        [part.uri for message in turn for part in message.contents if part.type == "data"]
        for turn in script.received_messages
    ]
    assert image_uris[0] == []
    assert len(image_uris[1]) == 1
    assert image_uris[2] == image_uris[1]
    assert all("data:image" not in str(snapshot) for snapshot in service.snapshots)

    restored = await SessionCheckpoint.load(cast(BlackboardClient, service), TASK, "agent-1")
    assert restored is not None
    ctx.checkpoint = restored
    followup = ScriptedChatClient([ScriptStep(text="again")])
    async with Agent(
        client=followup,
        context_providers=[CheckpointHistoryProvider(restored)],
        middleware=[ImageMiddleware(ctx)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        await agent.run("review", session=restored.session)
    restored_uris = [
        part.uri
        for message in followup.received_messages[0]
        for part in message.contents
        if part.type == "data"
    ]
    assert restored_uris == image_uris[1]


def test_installed_maf_request_converters_keep_image_bytes() -> None:
    image = png()
    message = Message(
        role="user", contents=[Content.from_text("look"), Content.from_data(image, "image/png")]
    )
    chat = OpenAIChatCompletionClient(model="test", api_key="fake")
    converted_chat = chat._prepare_message_for_openai(message)
    assert converted_chat[-1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    responses = OpenAIChatClient(model="test", api_key="fake")
    converted_responses = responses._prepare_message_for_openai(message)
    assert converted_responses[0]["content"][1]["type"] == "input_image"
    foundry = FoundryChatClient.__new__(FoundryChatClient)
    converted_foundry = foundry._prepare_message_for_openai(message)
    assert converted_foundry[0]["content"][1]["type"] == "input_image"
    gemini = GeminiChatClient(model="test", api_key="fake")
    _, converted_gemini = gemini._prepare_gemini_messages([message])
    parts = converted_gemini[0].parts
    assert parts is not None
    inline = parts[-1].inline_data
    assert inline is not None and inline.data == image
    anthropic = AnthropicClient.__new__(AnthropicClient)
    converted_anthropic = anthropic._prepare_message_for_anthropic(message)
    assert converted_anthropic["content"][1]["source"]["type"] == "base64"
    mistral = MistralChatClient.__new__(MistralChatClient)
    converted_mistral = mistral._format_user_message(message)
    assert converted_mistral["content"][1]["type"] == "image_url"
    ollama = OllamaChatClient.__new__(OllamaChatClient)
    converted_ollama = ollama._format_user_message(message)
    assert converted_ollama[0]["images"]
    bedrock = BedrockChatClient.__new__(BedrockChatClient)
    assert bedrock._convert_content_to_bedrock_block(message.contents[1]) is None
