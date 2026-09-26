"""Pinned task profiles and DeepSeek chat client construction."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from agent_framework import Content, UsageDetails
from agent_framework.openai import OpenAIChatCompletionClient, OpenAIChatCompletionOptions
from bbx_contracts.models import AgentProfile, ModelConfig
from bbx_contracts.profile import load_profile
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessage
from openai.types.chat.chat_completion_chunk import ChoiceDelta
from openai.types.completion_usage import CompletionUsage


def preserve_reasoning(
    message: ChatCompletionMessage | ChoiceDelta, contents: list[Content]
) -> list[Content]:
    """Keep provider-returned reasoning as trace metadata, never as assistant text."""
    reasoning = getattr(message, "reasoning_content", None)
    if isinstance(reasoning, str) and reasoning:
        if not contents:
            contents = [Content.from_text("")]
        contents[0].additional_properties["deepseek_reasoning"] = reasoning
    return contents


class DeepSeekChatOptions(OpenAIChatCompletionOptions[None], total=False):
    reasoning_effort: str
    parallel_tool_calls: bool


class DeepSeekChatClient(OpenAIChatCompletionClient):
    """Preserve the two DeepSeek cache counters omitted by MAF's default parser."""

    def _parse_usage_from_openai(self, usage: CompletionUsage) -> UsageDetails:
        details = dict(super()._parse_usage_from_openai(usage))
        extra = usage.model_extra or {}
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            value = extra.get(key, getattr(usage, key, None))
            if isinstance(value, int) and value >= 0:
                details[key] = value
        if "cache_read_input_token_count" not in details and "prompt_cache_hit_tokens" in details:
            details["cache_read_input_token_count"] = details["prompt_cache_hit_tokens"]
        return cast(UsageDetails, details)


def load_runtime_profile(source: Path | Mapping[str, Any]) -> AgentProfile:
    """Accept a local profile or the complete versioned document from blackboard."""
    if isinstance(source, Path):
        return load_profile(source)[0]
    return AgentProfile.model_validate(source.get("profile", source))


def make_client(
    model: ModelConfig,
    *,
    api_key: str,
    explore_max_steps: int,
    conclude_grace_calls: int,
    max_duration_seconds: int,
) -> DeepSeekChatClient:
    if model.provider != "deepseek":
        raise ValueError(f"Unsupported model provider: {model.provider}")
    if not api_key or min(explore_max_steps, max_duration_seconds) <= 0 or conclude_grace_calls < 0:
        raise ValueError("Model key, step limit, and duration must be valid")
    return DeepSeekChatClient(
        model=model.model,
        response_parser=preserve_reasoning,
        async_client=AsyncOpenAI(
            api_key=api_key,
            base_url=model.base_url,
            timeout=min(120, max_duration_seconds),
        ),
        function_invocation_configuration={
            "max_iterations": explore_max_steps + conclude_grace_calls + 5,
            "max_duration_seconds": max_duration_seconds,
        },
    )


def model_run_options(model: ModelConfig) -> DeepSeekChatOptions:
    """Pass DeepSeek's reasoning setting through MAF Chat Completions options."""
    return {"reasoning_effort": model.reasoning_effort}
