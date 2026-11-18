"""Persist bounded trace metadata and the provider-returned Pi text content."""

from __future__ import annotations

import logging
from typing import Literal

from bbx_runtime.pi import ChatResponse

TraceKind = Literal["initial_context", "board_update", "model_output", "model_error"]
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
