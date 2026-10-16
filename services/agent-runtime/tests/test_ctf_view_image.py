"""CTF members receive images through the same native MAF content path."""

from __future__ import annotations

import hashlib
import struct
import zlib
from types import SimpleNamespace
from typing import Any, cast

from agent_framework import AgentSession, ChatContext, Content, FunctionTool, Message
from bbx_runtime.ctf.images import ImageInjectionMiddleware, make_view_image_tool
from bbx_runtime.image_view import IMAGE_STATE_KEY, inject_pending_images

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


class MemberEnvd:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.scopes: list[str | None] = []

    async def stat(self, path: str, *, scope_agent_id: str | None = None) -> dict[str, Any]:
        self.scopes.append(scope_agent_id)
        return {"is_file": path.endswith("shot.png"), "size": len(self.data)}

    async def read_file(self, path: str) -> bytes:
        return self.data


class Service:
    def __init__(self, objects: MemoryObjects) -> None:
        self.objects = objects

    async def read_evidence(self, uri: str) -> bytes:
        return self.objects.data[uri]


class Checkpoint:
    def __init__(self) -> None:
        self.session = AgentSession()


MEMBER = "member-1"
SCOPE = "agent-2"
OWN_IMAGE = f"/workspace/agents/{SCOPE}/private/shot.png"


def build(
    model: Any | None = None, data: bytes | None = None
) -> tuple[FunctionTool, MemoryObjects, Checkpoint, MemberEnvd]:
    objects = MemoryObjects()
    checkpoint = Checkpoint()
    envd = MemberEnvd(data if data is not None else png())
    tool = make_view_image_tool(
        service=Service(objects),
        task_id=TASK,
        member_id=MEMBER,
        envd=envd,
        objects=lambda: objects,
        checkpoint_provider=lambda: checkpoint,
        model=model or SimpleNamespace(provider="deepseek", model="deepseek-flash", base_url=""),
    )
    return tool, objects, checkpoint, envd


async def invoke(tool: FunctionTool, reference: str) -> str:
    result = await tool.invoke(arguments={"path_or_uri": reference})
    assert result and result[0].text is not None
    return result[0].text


async def test_member_image_is_stored_once_and_queued_for_the_next_call() -> None:
    tool, objects, checkpoint, envd = build()
    text = await invoke(tool, OWN_IMAGE)
    digest = hashlib.sha256(png()).hexdigest()
    uri = f"evidence/{TASK}/{SCOPE}/view-{digest}.png"
    assert uri in text and "下一次模型调用" in text
    assert objects.data[uri] == png()
    assert envd.scopes == [SCOPE]
    pending = checkpoint.session.state[IMAGE_STATE_KEY]
    assert [record["sha256"] for record in pending] == [digest]
    assert "data:image" not in str(checkpoint.session.to_dict())

    # A second request for the same bytes reuses the stored object.
    await invoke(tool, OWN_IMAGE)
    assert list(objects.data) == [uri]


async def test_view_image_rejects_out_of_scope_paths_foreign_evidence_and_bad_bytes() -> None:
    tool, objects, _checkpoint, _envd = build()
    assert "无效" in await invoke(tool, "/workspace/agents/agent-9/private/shot.png")
    assert "无效" in await invoke(tool, "/etc/shadow")
    assert "无效" in await invoke(tool, f"evidence/{'2' * 36}/{SCOPE}/shot.png")
    assert "无效" in await invoke(tool, "https://example.com/shot.png")
    assert not objects.data

    bad, objects_bad, _c, _e = build(data=b"not an image")
    assert "无效" in await invoke(bad, OWN_IMAGE)
    assert not objects_bad.data


async def test_view_image_refuses_when_the_task_model_cannot_receive_images() -> None:
    cases = [
        (
            SimpleNamespace(provider="deepseek", model="deepseek-flash", supports_vision=False),
            "明确不支持",
        ),
        (SimpleNamespace(provider="bedrock", model="claude", base_url=""), "丢弃图片"),
        (
            SimpleNamespace(
                provider="deepseek", model="deepseek-v4-pro", base_url="https://api.deepseek.com"
            ),
            "不支持图片",
        ),
    ]
    for model, expected in cases:
        tool, objects, checkpoint, _envd = build(model=model)
        assert expected in await invoke(tool, OWN_IMAGE)
        assert not objects.data
        assert IMAGE_STATE_KEY not in checkpoint.session.state


async def test_view_image_tool_refuses_without_a_runner_container() -> None:
    objects = MemoryObjects()
    tool = make_view_image_tool(
        service=Service(objects),
        task_id=TASK,
        member_id=MEMBER,
        envd=None,
        objects=lambda: objects,
        checkpoint_provider=lambda: Checkpoint(),
        model=SimpleNamespace(provider="deepseek", model="deepseek-flash", base_url=""),
    )
    assert "执行容器尚未连接" in await invoke(tool, OWN_IMAGE)


async def test_middleware_delivers_native_content_then_keeps_only_the_reference() -> None:
    tool, objects, checkpoint, _envd = build()
    await invoke(tool, OWN_IMAGE)
    middleware = ImageInjectionMiddleware(Service(objects), TASK, lambda: checkpoint)
    messages = [Message(role="user", contents=[Content.from_text("看一下这张图")])]
    context = ChatContext(client=cast(Any, None), messages=messages, options={})
    during: list[list[str]] = []
    uris: list[str] = []

    async def call_next() -> None:
        injected = context.messages[-1]
        during.append([part.type for part in injected.contents])
        uris.extend(part.uri or "" for part in injected.contents if part.type == "data")

    await middleware.process(context, call_next)
    # The provider call sees the native image, the finished turn keeps a reference.
    assert during == [["text", "data"]]
    assert uris and uris[0].startswith("data:image/png;base64,")
    assert [part.type for part in context.messages[-1].contents] == ["text"]
    assert "data:image" not in str(checkpoint.session.to_dict())
    # Once the turn history is persisted the reference stays and pending drains;
    # the next request rehydrates the same bytes from the object store.
    checkpoint.session.state["in_memory"] = {"messages": list(context.messages)}
    following = list(context.messages)
    second = await inject_pending_images(
        task_id=TASK,
        state=checkpoint.session.state,
        read_evidence=Service(objects).read_evidence,
        messages=following,
    )
    assert second.new == []
    assert [part.type for part in following[-1].contents] == ["text", "data"]
    assert checkpoint.session.state[IMAGE_STATE_KEY] == []
