"""Pinned task profiles and provider-specific chat client construction."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from agent_framework import BaseChatClient, Content, UsageDetails
from agent_framework.openai import (
    OpenAIChatClient,
    OpenAIChatCompletionClient,
    OpenAIChatCompletionOptions,
)
from bbx_contracts.models import AgentProfile, ModelConfig
from bbx_contracts.profile import load_profile
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessage
from openai.types.chat.chat_completion_chunk import ChoiceDelta
from openai.types.completion_usage import CompletionUsage

from bbx_runtime.clients import BlackboardClient


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
    reasoning: dict[str, str]


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


async def model_api_key(
    service: BlackboardClient, model: ModelConfig, deepseek_api_key: str
) -> str:
    """Resolve a private credential for the pinned platform version."""
    name = model.platform_id
    version = model.platform_version
    if name is None and version is None:
        return deepseek_api_key if model.provider == "deepseek" else "not-required"
    if not name or version is None:
        raise ValueError("Incomplete platform model reference")
    credential = await service.get_model_credentials(name, version)
    source = credential.get("credential_source")
    secret = credential.get("secret")
    if source == "environment":
        if model.provider != "deepseek":
            raise ValueError("Environment credentials are only available for DeepSeek")
        return deepseek_api_key
    if source == "stored":
        if not isinstance(secret, str) or not secret:
            raise ValueError("Stored model credential is unavailable")
        return secret
    if source == "none":
        return "not-required"
    raise ValueError("Unknown model credential source")


def make_client(
    model: ModelConfig,
    *,
    api_key: str,
    explore_max_steps: int,
    conclude_grace_calls: int,
    max_duration_seconds: int,
) -> BaseChatClient:
    provider = model.provider
    if provider not in {"deepseek", "openai_chat", "openai_responses", "openai_compatible"}:
        raise ValueError(f"Unsupported model provider: {model.provider}")
    if not api_key or min(explore_max_steps, max_duration_seconds) <= 0 or conclude_grace_calls < 0:
        raise ValueError("Model key, step limit, and duration must be valid")
    client_type = (
        DeepSeekChatClient
        if provider == "deepseek"
        else OpenAIChatClient
        if provider == "openai_responses"
        else OpenAIChatCompletionClient
    )
    kwargs: dict[str, Any] = (
        {"response_parser": preserve_reasoning} if provider == "deepseek" else {}
    )
    return client_type(
        model=model.model,
        async_client=AsyncOpenAI(
            api_key=api_key,
            base_url=model.base_url,
            timeout=min(120, max_duration_seconds),
        ),
        function_invocation_configuration={
            "max_iterations": explore_max_steps + conclude_grace_calls + 5,
            "max_duration_seconds": max_duration_seconds,
        },
        **kwargs,
    )


def model_run_options(model: ModelConfig) -> DeepSeekChatOptions:
    """Use the option shape expected by the pinned provider's API."""
    effort = model.reasoning_effort
    if effort in {"none", "off", ""}:
        return {}
    if model.provider == "openai_responses":
        return {"reasoning": {"effort": effort}}
    return {"reasoning_effort": effort}
