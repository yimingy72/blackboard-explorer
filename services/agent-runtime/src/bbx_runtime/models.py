"""Pinned task profiles and provider-specific chat client construction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from inspect import isawaitable
from pathlib import Path
from typing import Any, cast

from agent_framework import (
    BaseChatClient,
    Content,
    FunctionInvocationConfiguration,
    UsageDetails,
)
from agent_framework.amazon import BedrockChatClient
from agent_framework.anthropic import (
    AnthropicBedrockClient,
    AnthropicClient,
    AnthropicFoundryClient,
    AnthropicVertexClient,
)
from agent_framework.foundry import FoundryChatClient
from agent_framework.gemini import GeminiChatClient
from agent_framework.mistral import MistralChatClient
from agent_framework.ollama import OllamaChatClient
from agent_framework.openai import (
    OpenAIChatClient,
    OpenAIChatCompletionClient,
    OpenAIChatCompletionOptions,
)
from azure.identity.aio import ClientSecretCredential
from bbx_contracts.models import AgentProfile, ModelConfig
from bbx_contracts.profile import load_profile
from bbx_contracts.providers import PROVIDERS
from openai import AsyncAzureOpenAI, AsyncOpenAI
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
    store: bool


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


class PlatformFoundryChatClient(FoundryChatClient):
    """Retain the service principal so its transport can be closed."""

    identity: ClientSecretCredential


def load_runtime_profile(source: Path | Mapping[str, Any]) -> AgentProfile:
    """Accept a local profile or the complete versioned document from blackboard."""
    if isinstance(source, Path):
        return load_profile(source)[0]
    return AgentProfile.model_validate(source.get("profile", source))


async def resolve_model_credentials(
    service: BlackboardClient, model: ModelConfig, deepseek_api_key: str
) -> dict[str, str]:
    """Resolve a private credential for the pinned platform version."""
    name = model.platform_id
    version = model.platform_version
    if name is None and version is None:
        if model.provider != "deepseek":
            return {}
        name, version = "deepseek-default", 1
    if not name or version is None:
        raise ValueError("Incomplete platform model reference")
    credential = await service.get_model_credentials(name, version)
    source = credential.get("credential_source")
    secret = credential.get("secret")
    if source == "environment":
        if model.provider != "deepseek":
            raise ValueError("Environment credentials are only available for DeepSeek")
        return {"api_key": deepseek_api_key}
    if source == "stored":
        credentials = credential.get("credentials") or {}
        if not isinstance(credentials, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in credentials.items()
        ):
            raise ValueError("Stored model credential is invalid")
        if not credentials and isinstance(secret, str) and secret:
            credentials = {"api_key": secret}
        if not credentials:
            raise ValueError("Stored model credential is unavailable")
        return credentials
    if source == "none":
        return {}
    raise ValueError("Unknown model credential source")


async def model_api_key(
    service: BlackboardClient, model: ModelConfig, deepseek_api_key: str
) -> str:
    """Backward-compatible key accessor for API-key providers."""
    return (await resolve_model_credentials(service, model, deepseek_api_key)).get(
        "api_key", "not-required"
    )


def make_client(
    model: ModelConfig,
    *,
    api_key: str | None = None,
    credentials: Mapping[str, str] | None = None,
    explore_max_steps: int,
    conclude_grace_calls: int,
    max_duration_seconds: int,
) -> BaseChatClient[Any]:
    provider = model.provider
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported model provider: {model.provider}")
    if min(explore_max_steps, max_duration_seconds) <= 0 or conclude_grace_calls < 0:
        raise ValueError("Model step limit and duration must be valid")
    secrets = dict(credentials or {})
    if api_key and api_key != "not-required":
        secrets.setdefault("api_key", api_key)
    options = model.provider_options
    definition = PROVIDERS[provider]
    for field in definition["options_fields"]:
        if field["required"] and not options.get(field["name"]):
            raise ValueError(f"Missing model option: {field['name']}")
    if not definition["allow_no_auth"] or secrets:
        for field in definition["credential_fields"]:
            if field["required"] and not secrets.get(field["name"]):
                raise ValueError(f"Missing model credential: {field['name']}")
    config: FunctionInvocationConfiguration = {
        "max_iterations": explore_max_steps + conclude_grace_calls + 5,
        "max_duration_seconds": max_duration_seconds,
    }
    key = secrets.get("api_key", "not-required")
    if provider in {"anthropic", "anthropic_foundry", "anthropic_bedrock", "anthropic_vertex"}:
        common = {"model": model.model, "function_invocation_configuration": config}
        if provider == "anthropic":
            return AnthropicClient(api_key=key, base_url=model.base_url, **common)
        if provider == "anthropic_foundry":
            return AnthropicFoundryClient(api_key=key, base_url=model.base_url, **common)
        if provider == "anthropic_bedrock":
            return AnthropicBedrockClient(
                aws_access_key=secrets["access_key_id"],
                aws_secret_key=secrets["secret_access_key"],
                aws_session_token=secrets.get("session_token"),
                aws_region=options["region"],
                **common,
            )
        from google.oauth2 import service_account

        google_credentials = service_account.Credentials.from_service_account_info(
            json.loads(secrets["service_account_json"]),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return AnthropicVertexClient(
            project_id=options["project"],
            region=options["location"],
            credentials=google_credentials,
            **common,
        )
    if provider == "bedrock":
        return BedrockChatClient(
            model=model.model,
            region=options["region"],
            access_key=secrets["access_key_id"],
            secret_key=secrets["secret_access_key"],
            session_token=secrets.get("session_token"),
            function_invocation_configuration=config,
        )
    if provider in {"gemini", "gemini_vertex"}:
        kwargs: dict[str, Any] = {"model": model.model, "function_invocation_configuration": config}
        if provider == "gemini":
            kwargs["api_key"] = key
        else:
            from google.oauth2 import service_account

            kwargs.update(
                vertexai=True,
                project=options["project"],
                location=options["location"],
                credentials=service_account.Credentials.from_service_account_info(
                    json.loads(secrets["service_account_json"]),
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                ),
            )
        return GeminiChatClient(**kwargs)
    if provider == "ollama":
        return OllamaChatClient(
            host=model.base_url, model=model.model, function_invocation_configuration=config
        )
    if provider == "mistral":
        return MistralChatClient(
            model=model.model,
            api_key=key,
            server_url=model.base_url,
            function_invocation_configuration=config,
        )
    if provider == "foundry":
        identity = ClientSecretCredential(
            tenant_id=secrets["tenant_id"],
            client_id=secrets["client_id"],
            client_secret=secrets["client_secret"],
        )
        client = PlatformFoundryChatClient(
            project_endpoint=model.base_url,
            model=model.model,
            credential=identity,
            function_invocation_configuration=config,
        )
        client.identity = identity
        return client
    if provider in {"azure_openai_chat", "azure_openai_responses"}:
        openai_client = AsyncAzureOpenAI(
            api_key=key,
            azure_endpoint=model.base_url,
            api_version=options["api_version"],
            timeout=min(120, max_duration_seconds),
            max_retries=4,
        )
    else:
        openai_client = AsyncOpenAI(
            api_key=key,
            base_url=model.base_url,
            timeout=min(120, max_duration_seconds),
            max_retries=4,
        )
    client_type: Any = (
        DeepSeekChatClient
        if provider == "deepseek"
        else OpenAIChatClient
        if provider in {"openai_responses", "azure_openai_responses"}
        else OpenAIChatCompletionClient
    )
    kwargs: dict[str, Any] = (
        {"response_parser": preserve_reasoning} if provider == "deepseek" else {}
    )
    return client_type(
        model=model.model,
        async_client=openai_client,
        function_invocation_configuration=config,
        **kwargs,
    )


async def close_model_client(client: BaseChatClient[Any]) -> None:
    """Close the transport owned by each MAF connector."""
    if isinstance(client, MistralChatClient):
        await client.close()
    elif isinstance(
        client,
        (AnthropicClient, AnthropicFoundryClient, AnthropicBedrockClient, AnthropicVertexClient),
    ):
        await client.anthropic_client.close()
    elif isinstance(client, BedrockChatClient):
        client._bedrock_client.close()
    elif isinstance(client, GeminiChatClient):
        await client._genai_client.aio.aclose()
        client._genai_client.close()
    elif isinstance(client, OllamaChatClient):
        await client.client._client.aclose()
    elif isinstance(client, PlatformFoundryChatClient):
        try:
            await client.client.close()
        finally:
            try:
                await client.project_client.close()
            finally:
                await client.identity.close()
    else:
        close = getattr(getattr(client, "client", None), "close", None)
        if close is not None:
            result = close()
            if isawaitable(result):
                await result


def model_run_options(model: ModelConfig) -> DeepSeekChatOptions:
    """Use the option shape expected by the pinned provider's API."""
    effort = model.reasoning_effort
    if model.provider in {"openai_responses", "azure_openai_responses"}:
        # Keep history in the platform instead of relying on provider-side storage.
        options: DeepSeekChatOptions = {"store": False}
        if effort not in {"none", "off", ""}:
            options["reasoning"] = {"effort": effort}
        return options
    if effort in {"none", "off", ""}:
        return {}
    if model.provider in {
        "deepseek",
        "openai_chat",
        "openai_compatible",
        "azure_openai_chat",
        "mistral",
    }:
        return {"reasoning_effort": effort}
    return {}
