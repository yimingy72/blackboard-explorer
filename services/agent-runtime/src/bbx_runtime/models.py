"""Pinned task profiles and provider-specific chat client construction."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Mapping, Sequence
from inspect import isawaitable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationConfiguration,
    Message,
    ResponseStream,
    UsageDetails,
)
from agent_framework.amazon import BedrockChatClient
from agent_framework.anthropic import (
    AnthropicBedrockClient,
    AnthropicClient,
    AnthropicFoundryClient,
    AnthropicVertexClient,
)
from agent_framework.exceptions import (
    ChatClientContentFilterException,
    ChatClientInvalidRequestException,
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
from bbx_contracts.model_limits import effective_max_output_tokens
from bbx_contracts.model_urls import in_container, resolve_model_url
from bbx_contracts.models import AgentProfile, ModelConfig
from bbx_contracts.profile import load_profile
from bbx_contracts.providers import PROVIDERS
from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai.types.chat import ChatCompletionMessage
from openai.types.chat.chat_completion_chunk import ChoiceDelta
from openai.types.completion_usage import CompletionUsage

from bbx_runtime.clients import BlackboardClient
from bbx_runtime.model_errors import (
    IncompleteModelStreamError,
    ModelRequestValidationError,
    ModelStreamError,
)
from bbx_runtime.responses_history import project_tool_observations


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


def _require_complete_stream(
    stream: ResponseStream[ChatResponseUpdate, ChatResponse], *, responses: bool
) -> ResponseStream[ChatResponseUpdate, ChatResponse]:
    """Reject an incomplete provider turn before MAF executes its tool calls."""
    completed = False
    rejected: ModelStreamError | None = None

    def terminal_usage(raw: Any, *, chat: bool = False) -> dict[str, Any] | None:
        if raw is None:
            return None
        input_details = getattr(
            raw, "prompt_tokens_details" if chat else "input_tokens_details", None
        )
        output_details = getattr(
            raw, "completion_tokens_details" if chat else "output_tokens_details", None
        )
        cached = getattr(raw, "prompt_cache_hit_tokens", None) if chat else None
        if cached is None:
            cached = getattr(input_details, "cached_tokens", None)
        reasoning = getattr(output_details, "reasoning_tokens", None)
        return {
            "input_token_count": getattr(raw, "prompt_tokens" if chat else "input_tokens", None),
            "output_token_count": getattr(
                raw, "completion_tokens" if chat else "output_tokens", None
            ),
            "total_token_count": getattr(raw, "total_tokens", None),
            "cache_read_input_token_count": 0 if cached is None else cached,
            "reasoning_output_token_count": 0 if reasoning is None else reasoning,
        }

    def observe(update: ChatResponseUpdate) -> ChatResponseUpdate:
        nonlocal completed, rejected
        if responses:
            event = update.raw_representation
            kind = getattr(event, "type", None)
            if (
                kind == "response.content_part.added"
                and getattr(getattr(event, "part", None), "type", None) == "output_text"
            ):
                # Some compatible gateways send a null placeholder before text deltas.
                for content in update.contents:
                    if content.type == "text" and content.text is None:
                        content.text = ""
            if kind in {"response.failed", "response.incomplete", "error"}:
                response = getattr(event, "response", None)
                code = (
                    getattr(event, "code", None)
                    if kind == "error"
                    else getattr(getattr(response, "error", None), "code", None)
                )
                reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
                category = (
                    "rate_limit"
                    if code in {"rate_limit", "rate_limit_exceeded"}
                    else "server_error"
                    if code == "server_error"
                    else "invalid_response"
                    if kind == "response.incomplete"
                    else "unknown"
                )
                terminal_error = ModelStreamError(
                    category,
                    event_type=kind,
                    provider_code=code,
                    incomplete_reason=reason,
                    provider_response_id=getattr(response, "id", None),
                    terminal_usage=terminal_usage(getattr(response, "usage", None))
                    if kind in {"response.failed", "response.incomplete"}
                    and getattr(response, "status", None) == kind.removeprefix("response.")
                    else None,
                )
                if reason == "content_filter" or code == "content_filter":
                    raise ChatClientContentFilterException(
                        "Model stream was filtered"
                    ) from terminal_error
                raise terminal_error
            if kind == "response.completed":
                completed = getattr(getattr(event, "response", None), "status", None) == "completed"
        else:
            if update.finish_reason is not None:
                if update.finish_reason not in {"stop", "tool_calls"}:
                    if update.finish_reason == "content_filter":
                        raise ChatClientContentFilterException("Model stream was filtered")
                    rejected = ModelStreamError(
                        "invalid_response",
                        finish_reason=str(update.finish_reason),
                        provider_response_id=update.response_id,
                    )
                else:
                    completed = True
            if rejected is not None:
                # Chat usage commonly follows the finish_reason in a separate final chunk.
                usage = terminal_usage(getattr(update.raw_representation, "usage", None), chat=True)
                if usage is not None:
                    rejected = ModelStreamError(
                        "invalid_response",
                        finish_reason=rejected.finish_reason,
                        provider_response_id=rejected.provider_response_id,
                        terminal_usage=usage,
                    )
        return update

    def verify(response: ChatResponse) -> ChatResponse:
        if rejected is not None:
            raise rejected
        if not completed:
            raise IncompleteModelStreamError("Model stream ended without a completed response")
        return response

    stream.with_transform_hook(observe)
    stream.with_result_hook(verify)
    return stream


class CompleteChatCompletionClient(OpenAIChatCompletionClient):
    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        stream: bool = False,
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        result = super()._inner_get_response(
            messages=messages, options=options, stream=stream, **kwargs
        )
        if isinstance(result, ResponseStream):
            return _require_complete_stream(result, responses=False)
        return result


class CompleteResponsesClient(OpenAIChatClient):
    def __init__(self, *args: Any, responses_replay_mode: str = "native", **kwargs: Any) -> None:
        if responses_replay_mode not in {"native", "tool_observations"}:
            raise ValueError("Invalid resolved Responses replay mode")
        super().__init__(*args, **kwargs)
        self.responses_replay_mode = responses_replay_mode

    def _prepare_messages_for_openai(
        self,
        chat_messages: Sequence[Message],
        *,
        request_uses_service_side_storage: bool = True,
    ) -> list[dict[str, Any]]:
        if (
            self.responses_replay_mode == "tool_observations"
            and not request_uses_service_side_storage
        ):
            chat_messages = project_tool_observations(chat_messages)
        try:
            return super()._prepare_messages_for_openai(
                chat_messages, request_uses_service_side_storage=request_uses_service_side_storage
            )
        except ChatClientInvalidRequestException as error:
            raise ModelRequestValidationError(
                "Local Responses history validation failed", log_level=None
            ) from error

    def _prepare_message_for_openai(
        self,
        message: Message,
        *,
        request_uses_service_side_storage: bool = True,
        reasoning_items: Mapping[str, dict[str, Any]] | None = None,
        serialized_reasoning_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        items = super()._prepare_message_for_openai(
            message,
            request_uses_service_side_storage=request_uses_service_side_storage,
            reasoning_items=reasoning_items,
            serialized_reasoning_ids=serialized_reasoning_ids,
        )
        if message.role != "assistant" or request_uses_service_side_storage:
            return items
        first_call = next(
            (index for index, item in enumerate(items) if item.get("type") == "function_call"),
            None,
        )
        if first_call is None:
            return items
        for index, item in enumerate(items):
            contents = item.get("content")
            if (
                index > first_call
                and item.get("type") == "message"
                and item.get("role") == "assistant"
                and isinstance(contents, list)
                and any(
                    isinstance(part, Mapping)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                    and bool(part["text"])
                    for part in contents
                )
            ):
                return [*items[:first_call], item, *items[first_call:index], *items[index + 1 :]]
        return items

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        stream: bool = False,
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        result = super()._inner_get_response(
            messages=messages, options=options, stream=stream, **kwargs
        )
        if isinstance(result, ResponseStream):
            return _require_complete_stream(result, responses=True)
        return result


class DeepSeekChatClient(CompleteChatCompletionClient):
    """Preserve the two DeepSeek cache counters omitted by MAF's default parser."""

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        stream: bool = False,
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        result = super()._inner_get_response(
            messages=messages, options=options, stream=stream, **kwargs
        )
        if isinstance(result, ResponseStream):

            def keep_reasoning(response: ChatResponse) -> ChatResponse:
                reasoning = "".join(
                    value
                    for update in result.updates
                    for content in update.contents
                    if isinstance(
                        value := content.additional_properties.get("deepseek_reasoning"), str
                    )
                )
                if reasoning:
                    for message in response.messages:
                        if message.role == "assistant" and message.contents:
                            for content in message.contents:
                                content.additional_properties.pop("deepseek_reasoning", None)
                            message.contents[0].additional_properties["deepseek_reasoning"] = (
                                reasoning
                            )
                            break
                return response

            result.with_result_hook(keep_reasoning)
        return result

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
    max_retries: int | None = None,
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
    if max_retries is not None and max_retries < 0:
        raise ValueError("Model retry limit must be nonnegative")
    model = model.model_copy(
        update={"base_url": resolve_model_url(model.base_url, containerized=in_container())}
    )

    def configured(client: Any) -> Any:
        if max_retries is not None:
            if provider.startswith("anthropic"):
                client.anthropic_client.max_retries = max_retries
            elif provider == "mistral":
                if max_retries != 0:
                    raise ValueError("Explicit Mistral retries only support zero")
                client.client.sdk_configuration.retry_config = None
            elif provider == "foundry":
                client.client.max_retries = max_retries
        return client

    key = secrets.get("api_key", "not-required")
    if provider in {"anthropic", "anthropic_foundry", "anthropic_bedrock", "anthropic_vertex"}:
        common = {"model": model.model, "function_invocation_configuration": config}
        if provider == "anthropic":
            return configured(AnthropicClient(api_key=key, base_url=model.base_url, **common))
        if provider == "anthropic_foundry":
            return configured(
                AnthropicFoundryClient(api_key=key, base_url=model.base_url, **common)
            )
        if provider == "anthropic_bedrock":
            return configured(
                AnthropicBedrockClient(
                    aws_access_key=secrets["access_key_id"],
                    aws_secret_key=secrets["secret_access_key"],
                    aws_session_token=secrets.get("session_token"),
                    aws_region=options["region"],
                    **common,
                )
            )
        from google.oauth2 import service_account

        google_credentials = service_account.Credentials.from_service_account_info(
            json.loads(secrets["service_account_json"]),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return configured(
            AnthropicVertexClient(
                project_id=options["project"],
                region=options["location"],
                credentials=google_credentials,
                **common,
            )
        )
    if provider == "bedrock":
        extra: dict[str, Any] = {}
        if max_retries is not None:
            from boto3.session import Session as BotoSession
            from botocore.config import Config as BotoConfig

            extra["client"] = BotoSession(
                aws_access_key_id=secrets["access_key_id"],
                aws_secret_access_key=secrets["secret_access_key"],
                aws_session_token=secrets.get("session_token"),
                region_name=options["region"],
            ).client(
                "bedrock-runtime",
                config=BotoConfig(retries={"total_max_attempts": max_retries + 1}),
            )
        return BedrockChatClient(
            model=model.model,
            region=options["region"],
            access_key=secrets["access_key_id"],
            secret_key=secrets["secret_access_key"],
            session_token=secrets.get("session_token"),
            function_invocation_configuration=config,
            **extra,
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
        if max_retries is not None:
            from google import genai

            credentials = {
                key: value
                for key, value in kwargs.items()
                if key not in {"model", "function_invocation_configuration"}
            }
            kwargs = {
                "model": model.model,
                "function_invocation_configuration": config,
                "client": genai.Client(
                    **credentials, http_options={"retry_options": {"attempts": max_retries + 1}}
                ),
            }
        return GeminiChatClient(**kwargs)
    if provider == "ollama":
        return OllamaChatClient(
            host=model.base_url, model=model.model, function_invocation_configuration=config
        )
    if provider == "mistral":
        return configured(
            MistralChatClient(
                model=model.model,
                api_key=key,
                server_url=model.base_url,
                function_invocation_configuration=config,
            )
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
        return configured(client)
    if provider in {"azure_openai_chat", "azure_openai_responses"}:
        openai_client = AsyncAzureOpenAI(
            api_key=key,
            azure_endpoint=model.base_url,
            api_version=options["api_version"],
            timeout=min(120, max_duration_seconds),
            max_retries=4 if max_retries is None else max_retries,
        )
    else:
        openai_client = AsyncOpenAI(
            api_key=key,
            base_url=model.base_url,
            timeout=min(120, max_duration_seconds),
            max_retries=4 if max_retries is None else max_retries,
        )
    client_type: Any
    if provider == "deepseek":
        client_type = DeepSeekChatClient
    elif provider in {"openai_responses", "azure_openai_responses"}:
        client_type = CompleteResponsesClient
    else:
        client_type = CompleteChatCompletionClient
    kwargs: dict[str, Any] = (
        {"response_parser": preserve_reasoning} if provider == "deepseek" else {}
    )
    if provider in {"openai_responses", "azure_openai_responses"}:
        mode = options.get("responses_replay_mode", "auto")
        if mode not in {"auto", "native", "tool_observations"}:
            raise ValueError("Invalid Responses replay mode")
        if mode == "auto":
            mode = (
                "native"
                if provider == "azure_openai_responses"
                or not model.base_url
                or urlsplit(model.base_url).hostname == "api.openai.com"
                else "tool_observations"
            )
        kwargs["responses_replay_mode"] = mode
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
    options: DeepSeekChatOptions = {"max_tokens": effective_max_output_tokens(model)}
    effort = model.reasoning_effort
    if model.provider in {"openai_responses", "azure_openai_responses"}:
        # Keep history in the platform instead of relying on provider-side storage.
        options["store"] = False
        if effort not in {"none", "off", ""}:
            options["reasoning"] = {"effort": effort}
        return options
    if effort in {"none", "off", ""}:
        return options
    if model.provider in {
        "deepseek",
        "openai_chat",
        "openai_compatible",
        "azure_openai_chat",
        "mistral",
    }:
        options["reasoning_effort"] = effort
    return options


def ctf_model_run_options(model: ModelConfig) -> DeepSeekChatOptions:
    """Keep the CTF compatibility entry point on the shared model policy."""
    return model_run_options(model)
