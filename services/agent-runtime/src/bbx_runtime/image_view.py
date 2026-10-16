"""Task-scoped image viewing through native multimodal model content."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from agent_framework import (
    AgentSession,
    ChatContext,
    ChatMiddleware,
    Content,
    FunctionTool,
    Message,
    ResponseStream,
    tool,
)

from bbx_runtime.clients import RemoteError
from bbx_runtime.context import RunContext

IMAGE_MAX_BYTES = 10 * 1024 * 1024
IMAGE_MAX_PENDING = 4
IMAGE_STATE_KEY = "bbx_view_images"
IMAGE_RECORDS_KEY = "bbx_image_records"


@dataclass
class ImageInjection:
    new: list[Message]
    hydrated: list[Message]


def _media_type(data: bytes) -> tuple[str, str] | None:
    if (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        and len(data) >= 45
        and data[8:16] == b"\x00\x00\x00\rIHDR"
        and data[-12:] == b"\x00\x00\x00\x00IEND\xaeB`\x82"
    ):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return "image/jpeg", "jpg"
    if (
        len(data) >= 20
        and data[:4] == b"RIFF"
        and int.from_bytes(data[4:8], "little") + 8 == len(data)
        and data[8:12] == b"WEBP"
        and data[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}
    ):
        return "image/webp", "webp"
    return None


def _task_evidence_uri(uri: str, task_id: str) -> bool:
    parts = PurePosixPath(uri).parts
    input_uri = False
    if len(parts) == 4 and parts[:2] == ("inputs", task_id):
        try:
            input_uri = str(UUID(parts[2])) == parts[2]
        except ValueError:
            pass
    return (
        ((len(parts) >= 4 and parts[:2] == ("evidence", task_id)) or input_uri)
        and all(part not in {"", ".", ".."} for part in uri.split("/"))
        and "\\" not in uri
        and (input_uri or ":" not in uri)
    )


async def _image_content(
    task_id: str, record: dict[str, str], read_evidence: Callable[[str], Awaitable[bytes]]
) -> Content:
    uri = record["uri"]
    if not _task_evidence_uri(uri, task_id):
        raise ValueError("Image history URI belongs to another task")
    data = await read_evidence(uri)
    if len(data) > IMAGE_MAX_BYTES or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ValueError("Image history failed integrity verification")
    detected = _media_type(data)
    if detected is None or detected[0] != record["media_type"]:
        raise ValueError("Image history has an invalid media type")
    return Content.from_data(data, media_type=record["media_type"])


async def hydrate_image_messages(
    task_id: str,
    session: AgentSession,
    read_evidence: Callable[[str], Awaitable[bytes]],
    messages: Sequence[Message],
) -> None:
    """Restore stored image references before MAF assembles a model request."""
    records = session.state.get(IMAGE_RECORDS_KEY, {})
    hydrated: list[Message] = []
    try:
        for message in messages:
            record = records.get(message.message_id)
            if not isinstance(record, dict) or any(
                content.type == "data" for content in message.contents
            ):
                continue
            message.contents.append(await _image_content(task_id, record, read_evidence))
            hydrated.append(message)
    except Exception:
        for message in hydrated:
            message.contents = [content for content in message.contents if content.type != "data"]
        raise


def vision_refusal(model: Any) -> str | None:
    """Return the refusal message when the model cannot receive native images."""
    if getattr(model, "supports_vision", None) is False:
        return "当前模型配置明确不支持看图，未读取图片。请改用支持视觉的模型。"
    if getattr(model, "provider", None) == "bedrock":
        return "当前 Bedrock 连接器会丢弃图片输入，未读取图片。请改用支持图片传递的连接方式。"
    if (
        getattr(model, "provider", None) == "deepseek"
        or urlsplit(getattr(model, "base_url", "") or "").hostname == "api.deepseek.com"
    ) and str(getattr(model, "model", "")).lower() == "deepseek-v4-pro":
        return "当前 DeepSeek V4 Pro 模型不支持图片输入，未读取图片。"
    return None


def pending_images(state: dict[str, Any]) -> list[dict[str, str]]:
    """Return the mutable pending-image list stored in a session state."""
    pending = state.get(IMAGE_STATE_KEY)
    if not isinstance(pending, list):
        pending = []
        state[IMAGE_STATE_KEY] = pending
    return pending


async def stage_image(
    *,
    task_id: str,
    agent_id: str,
    state: dict[str, Any],
    objects: Any,
    data: bytes,
    stored_uri: str | None = None,
) -> str:
    """Bound, verify and queue image bytes; the next model call receives them."""
    if len(data) > IMAGE_MAX_BYTES:
        return "图片超过 10 MiB 上限，请先缩小图片。"
    detected = _media_type(data)
    if detected is None:
        return "图片格式无效；仅支持 PNG、JPEG 或 WebP。"
    media_type, extension = detected
    digest = hashlib.sha256(data).hexdigest()
    if stored_uri is None:
        uri = f"evidence/{task_id}/{agent_id}/view-{digest}.{extension}"
        try:
            if not await objects.exists(uri):
                await objects.put(uri, data, content_type=media_type)
        except Exception:
            return "图片持久化失败，未送入模型。"
    else:
        uri = stored_uri
    record = {
        "uri": uri,
        "sha256": digest,
        "media_type": media_type,
        "message_id": f"bbx-view-{uuid4()}",
    }
    pending_images(state).append(record)
    records = state.setdefault(IMAGE_RECORDS_KEY, {})
    records[record["message_id"]] = record
    return f"图片 {uri} 已读取，将在下一次模型调用中作为原生图片输入。"


async def inject_pending_images(
    *,
    task_id: str,
    state: dict[str, Any],
    read_evidence: Callable[[str], Awaitable[bytes]],
    messages: list[Message],
) -> ImageInjection:
    """Rehydrate image history and append pending images for the model request."""
    pending = pending_images(state)
    history = state.get("in_memory", {})
    history_messages = history.get("messages", []) if isinstance(history, dict) else []
    saved_ids = {item.message_id for item in history_messages if item.role == "user"}
    pending[:] = [record for record in pending if record["message_id"] not in saved_ids]
    records = state.get(IMAGE_RECORDS_KEY, {})
    added: list[Message] = []
    for record in pending:
        message = Message(
            role="user",
            message_id=record["message_id"],
            contents=[Content.from_text(f"[图片查看] {record['uri']}")],
        )
        messages.append(message)
        added.append(message)
    hydrated: list[Message] = []
    try:
        for message in messages:
            record = records.get(message.message_id)
            if not isinstance(record, dict):
                continue
            if any(content.type == "data" for content in message.contents):
                continue
            content = await _image_content(task_id, record, read_evidence)
            message.contents.append(content)
            hydrated.append(message)
    except Exception:
        finish_pending_images(messages, ImageInjection(added, hydrated), sent=False)
        raise
    return ImageInjection(added, hydrated)


async def run_with_image_injection(
    context: ChatContext, messages: list[Message], injection: ImageInjection, call_next: Callable
) -> None:
    """Call the model with injected images, restoring history-only references."""
    try:
        await call_next()
    except BaseException:
        finish_pending_images(messages, injection, sent=False)
        raise
    if isinstance(context.result, ResponseStream):
        inner = context.result

        async def updates():
            sent = False
            try:
                async for update in inner:
                    yield update
                await inner.get_final_response()
                sent = True
            finally:
                finish_pending_images(messages, injection, sent=sent)

        context.result = ResponseStream(updates(), finalizer=lambda _: inner.get_final_response())
    else:
        finish_pending_images(messages, injection, sent=True)


def make_view_image_tool(ctx: RunContext, *, read_only: bool = False) -> FunctionTool:
    """Return a reference now; deliver image bytes to the next model request."""
    read_only = read_only or ctx.task_type != "explore"

    @tool(approval_mode="never_require")
    async def view_image(path_or_uri: str) -> str:
        """查看本任务已保存的资料/证据图片；仅 Explore 可读 /workspace。"""
        model = getattr(ctx.profile.models, ctx.task_type)
        refusal = vision_refusal(model)
        if refusal is not None:
            return refusal
        if ctx.checkpoint is None:
            return "图片会话尚未就绪，无法保证把图片送入模型。"
        state = ctx.checkpoint.session.state
        if len(pending_images(state)) >= IMAGE_MAX_PENDING:
            return "已有过多待看的图片，请先处理当前图片。"
        if path_or_uri.startswith("/workspace/"):
            if read_only:
                return "推导、裁定和复盘只能查看本任务已保存的图片。"
            path = PurePosixPath(path_or_uri)
            if ".." in path.parts or ctx.envd is None:
                return "图片路径无效，请使用本任务 /workspace 下的文件。"
            try:
                stat = await ctx.envd.stat(path_or_uri)
                if not stat.get("is_file"):
                    return "图片文件不存在或不是普通文件。"
                if int(stat.get("size") or 0) > IMAGE_MAX_BYTES:
                    return "图片超过 10 MiB 上限，请先缩小图片。"
                data = await ctx.envd.read_file(path_or_uri)
            except RemoteError as exc:
                return f"无法读取图片：{exc.message}"
        elif _task_evidence_uri(path_or_uri, ctx.task_id):
            try:
                data = await ctx.board.read_evidence(path_or_uri)
            except RemoteError as exc:
                return f"无法读取证据图片：{exc.message}"
        else:
            return "图片引用无效，请使用本任务 /workspace 路径或已登记的资料/证据 URI。"
        return await stage_image(
            task_id=ctx.task_id,
            agent_id=ctx.agent_id,
            state=state,
            objects=ctx.objects,
            data=data,
            stored_uri=path_or_uri if read_only else None,
        )

    return view_image


async def append_pending_images(ctx: RunContext, messages: list[Message]) -> ImageInjection:
    """Rehydrate image history and append pending images for the model request."""
    checkpoint = ctx.checkpoint
    if checkpoint is None:
        return ImageInjection([], [])

    async def read_evidence(uri: str) -> bytes:
        return await ctx.board.read_evidence(uri)

    return await inject_pending_images(
        task_id=ctx.task_id,
        state=checkpoint.session.state,
        read_evidence=read_evidence,
        messages=messages,
    )


def finish_pending_images(
    messages: list[Message], injection: ImageInjection, *, sent: bool
) -> None:
    """Keep stable image markers in history without serializing image bytes."""
    for message in injection.hydrated:
        message.contents = [content for content in message.contents if content.type != "data"]
    if not sent:
        for message in injection.new:
            messages.remove(message)


class ImageViewMiddleware(ChatMiddleware):
    """Deliver newly requested images during a read-only review tool loop."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def process(self, context: ChatContext, call_next) -> None:
        context.messages = list(context.messages)
        messages = context.messages
        injection = await append_pending_images(self.ctx, messages)
        await run_with_image_injection(context, messages, injection, call_next)


def strip_image_history(session: AgentSession) -> None:
    """Keep only image references in the durable MAF history snapshot."""
    history = session.state.get("in_memory", {})
    records = session.state.get(IMAGE_RECORDS_KEY, {})
    for message in history.get("messages", []) if isinstance(history, dict) else []:
        if message.message_id in records:
            message.contents = [content for content in message.contents if content.type != "data"]
