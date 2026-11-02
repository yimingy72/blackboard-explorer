"""Pinned platform policy with native Pi provider calls."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypedDict

from bbx_contracts.model_limits import effective_max_output_tokens
from bbx_contracts.model_urls import in_container, resolve_model_url
from bbx_contracts.models import AgentProfile, ModelConfig
from bbx_contracts.profile import load_profile
from bbx_contracts.providers import PROVIDERS

from bbx_runtime.clients import BlackboardClient
from bbx_runtime.pi import BaseChatClient, PiChatClient


class DeepSeekChatOptions(TypedDict, total=False):
    max_tokens: int
    reasoning_effort: str
    parallel_tool_calls: bool
    reasoning: dict[str, str]
    store: bool


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
) -> PiChatClient:
    if model.provider not in PROVIDERS:
        raise ValueError(f"Unsupported model provider: {model.provider}")
    if min(explore_max_steps, max_duration_seconds) <= 0 or conclude_grace_calls < 0:
        raise ValueError("Model step limit and duration must be valid")
    if max_retries is not None and max_retries < 0:
        raise ValueError("Model retry limit must be nonnegative")
    secrets = dict(credentials or {})
    if api_key and api_key != "not-required":
        secrets.setdefault("api_key", api_key)
    definition = PROVIDERS[model.provider]
    for field in definition["options_fields"]:
        if field["required"] and not model.provider_options.get(field["name"]):
            raise ValueError(f"Missing model option: {field['name']}")
    if not definition["allow_no_auth"] or secrets:
        for field in definition["credential_fields"]:
            if field["required"] and not secrets.get(field["name"]):
                raise ValueError(f"Missing model credential: {field['name']}")
    options = model.provider_options
    if options.get("responses_replay_mode", "auto") not in {"auto", "native", "tool_observations"}:
        raise ValueError("Invalid Responses replay mode")
    model = model.model_copy(
        update={
            "base_url": resolve_model_url(model.base_url, containerized=in_container()),
        }
    )
    return PiChatClient(
        model,
        credentials=secrets,
        function_invocation_configuration={
            "max_iterations": explore_max_steps + conclude_grace_calls + 5,
            "max_duration_seconds": max_duration_seconds,
        },
        max_retries=max_retries
        if max_retries is not None
        else (
            4
            if model.provider
            in {
                "deepseek",
                "openai_chat",
                "openai_responses",
                "openai_compatible",
                "azure_openai_chat",
                "azure_openai_responses",
            }
            else 0
        ),
    )


async def close_model_client(client: BaseChatClient[Any]) -> None:
    await client.close()


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
