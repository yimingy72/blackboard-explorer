"""Member image intake for CTF turns.

Uses the same native-multimodal path as the ordinary blackboard mode: the tool
returns a reference immediately, the bytes are stored in the object store once
and are delivered as native image content on the next model request, while the
durable session keeps only references. Browser automation is not involved.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_framework import ChatContext, ChatMiddleware, FunctionTool, Message, tool
from bbx_contracts.ctf import execution_agent_id

from bbx_runtime.clients import RemoteError
from bbx_runtime.ctf.artifacts import artifact_path
from bbx_runtime.image_view import (
    IMAGE_MAX_BYTES,
    IMAGE_MAX_PENDING,
    inject_pending_images,
    pending_images,
    run_with_image_injection,
    stage_image,
    vision_refusal,
)


def make_view_image_tool(
    *,
    service: Any,
    task_id: str,
    member_id: str,
    envd: Any,
    objects: Callable[[], Any],
    checkpoint_provider: Callable[[], Any],
    model: Any,
) -> FunctionTool:
    """Build the member-facing view_image tool for the current CTF turn."""

    @tool(approval_mode="never_require")
    async def view_image(path_or_uri: str) -> str:
        """查看本任务容器内的图片或已登记的证据图片（PNG/JPEG/WebP，≤10 MiB）。

        path_or_uri 为本成员工作目录/共享目录下的文件路径，
        或本任务已登记的证据 URI（evidence/<task_id>/...）。
        图片会在下一次模型调用中作为原生图片输入。
        """
        refusal = vision_refusal(model)
        if refusal is not None:
            return refusal
        checkpoint = checkpoint_provider() if checkpoint_provider is not None else None
        if checkpoint is None or getattr(checkpoint, "session", None) is None:
            return "图片会话尚未就绪，无法保证把图片送入模型。"
        if envd is None:
            return "执行容器尚未连接，无法读取容器内图片。"
        state = checkpoint.session.state
        if len(pending_images(state)) >= IMAGE_MAX_PENDING:
            return "已有过多待看的图片，请先处理当前图片。"
        scope = execution_agent_id(member_id)
        if path_or_uri.startswith("/workspace/"):
            try:
                path = artifact_path(path_or_uri, member_id)
            except ValueError:
                return "图片路径无效，请使用本任务工作目录或共享目录下的文件。"
            try:
                stat = await envd.stat(path, scope_agent_id=scope)
                if not stat.get("is_file"):
                    return "图片文件不存在或不是普通文件。"
                if int(stat.get("size") or 0) > IMAGE_MAX_BYTES:
                    return "图片超过 10 MiB 上限，请先缩小图片。"
                data = await envd.read_file(path)
            except RemoteError as exc:
                return f"无法读取图片：{exc.message}"
        elif _task_evidence_uri(path_or_uri, task_id):
            try:
                data = await service.read_evidence(path_or_uri)
            except RemoteError as exc:
                return f"无法读取证据图片：{exc.message}"
        else:
            return "图片引用无效，请使用本任务容器内路径或已登记的证据 URI。"
        return await stage_image(
            task_id=task_id,
            agent_id=scope,
            state=state,
            objects=objects(),
            data=data,
        )

    return view_image


def _task_evidence_uri(uri: str, task_id: str) -> bool:
    """Accept only evidence URIs registered for this task."""
    parts = [part for part in uri.split("/")]
    return (
        len(parts) >= 3
        and parts[0] == "evidence"
        and parts[1] == task_id
        and all(part not in {"", ".", ".."} for part in parts)
        and "\\" not in uri
        and ":" not in uri
    )


class ImageInjectionMiddleware(ChatMiddleware):
    """Deliver newly requested images during a CTF turn."""

    def __init__(
        self,
        service: Any,
        task_id: str,
        checkpoint_provider: Callable[[], Any],
    ) -> None:
        self.service = service
        self.task_id = task_id
        self.checkpoint_provider = checkpoint_provider

    async def process(self, context: ChatContext, call_next) -> None:
        checkpoint = self.checkpoint_provider() if self.checkpoint_provider else None
        if checkpoint is None or getattr(checkpoint, "session", None) is None:
            await call_next()
            return
        context.messages = list(context.messages)
        messages: list[Message] = context.messages
        service = self.service

        async def read_evidence(uri: str) -> bytes:
            return await service.read_evidence(uri)

        injection = await inject_pending_images(
            task_id=self.task_id,
            state=checkpoint.session.state,
            read_evidence=read_evidence,
            messages=messages,
        )
        await run_with_image_injection(context, messages, injection, call_next)


__all__ = ["ImageInjectionMiddleware", "make_view_image_tool"]
