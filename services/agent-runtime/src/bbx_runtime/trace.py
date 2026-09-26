"""Persist bounded trace metadata and the exact public MAF text content."""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Literal

from agent_framework import ChatResponse

from bbx_runtime.context import RunContext

TraceKind = Literal["initial_context", "board_update", "model_output"]
logger = logging.getLogger(__name__)


def model_content(response: ChatResponse) -> tuple[str, str | None]:
    """Read only public message content; usage counts do not imply reasoning text."""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_names: list[str] = []
    for message in response.messages:
        if message.role != "assistant":
            continue
        for content in message.contents:
            provider_reasoning = content.additional_properties.get("deepseek_reasoning")
            if isinstance(provider_reasoning, str) and provider_reasoning:
                reasoning_parts.append(provider_reasoning)
            if content.type == "text" and content.text:
                text_parts.append(content.text)
            elif content.type == "text_reasoning" and content.text:
                reasoning_parts.append(content.text)
            elif content.type == "function_call" and content.name:
                tool_names.append(content.name)
    text = "\n".join(text_parts)
    if not text and tool_names:
        text = "调用工具：" + "、".join(tool_names)
    return text, "\n".join(reasoning_parts) if reasoning_parts else None


async def record_trace(
    ctx: RunContext,
    kind: TraceKind,
    step: int,
    text: str,
    *,
    reasoning: str | None = None,
) -> None:
    # Minimal fake services used by earlier offline tests do not implement traces.
    recorder = getattr(ctx.service, "record_agent_trace", None)
    if recorder is None:
        return
    uri = f"traces/{ctx.task_id}/{ctx.agent_id}/{step:06d}-{kind}-{secrets.token_hex(6)}.json"
    body: dict[str, Any] = {"text": text}
    if reasoning is not None:
        body["reasoning"] = reasoning
    try:
        await ctx.objects.put(
            uri,
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
        await recorder(
            ctx.task_id,
            ctx.agent_id,
            {"kind": kind, "step": step, "uri": uri, "summary": text[:240]},
        )
    except Exception as exc:
        # Observability must not prevent model usage accounting or an Agent's handoff.
        logger.warning("Agent trace could not be saved (%s)", type(exc).__name__)
