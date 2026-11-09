"""Frozen model context and scoped AgentGateway sends through Pi."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypedDict

from bbx_contracts.gateway_models import GatewayModelRef, GatewayModelSnapshot
from bbx_contracts.model_limits import effective_max_output_tokens
from bbx_contracts.models import AgentProfile, ModelConfig
from bbx_contracts.profile import load_profile

from bbx_runtime.clients import BlackboardClient
from bbx_runtime.gateway_model import GatewayDispatch, Proof, Purpose, gateway_origin
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


def make_gateway_client(
    model: ModelConfig,
    *,
    snapshot: GatewayModelSnapshot,
    service: BlackboardClient,
    task_id: str,
    purpose: Purpose,
    gateway_url: str | None,
    proof: Proof,
    explore_max_steps: int,
    conclude_grace_calls: int,
    max_duration_seconds: int,
) -> PiChatClient:
    origin = gateway_origin(gateway_url)
    dispatch = GatewayDispatch(
        service,
        task_id,
        purpose,
        GatewayModelRef(alias=snapshot.alias, catalog_revision=snapshot.catalog_revision),
        origin,
        proof,
    )
    selected = model.model_copy(
        update={
            "model": snapshot.alias,
            "provider": "openai_compatible",
            "provider_options": {},
            "base_url": origin + "/v1",
            "context_window": snapshot.context_window,
            "max_input_tokens": snapshot.max_input_tokens,
            "max_output_tokens": snapshot.max_output_tokens,
            "supports_vision": "image" in snapshot.input_modalities,
            "reasoning_effort": model.reasoning_effort if snapshot.reasoning is True else "off",
        }
    )
    return PiChatClient(
        selected,
        credentials={},
        function_invocation_configuration={
            "max_iterations": explore_max_steps + conclude_grace_calls + 5,
            "max_duration_seconds": max_duration_seconds,
        },
        max_retries=0,
        gateway=dispatch,
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
