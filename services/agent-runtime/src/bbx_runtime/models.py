"""Frozen model context and scoped AgentGateway sends through Pi."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypedDict

from bbx_contracts.gateway_models import GatewayModelRef, GatewayModelSnapshot
from bbx_contracts.model_limits import effective_max_output_tokens
from bbx_contracts.models import AgentProfile, ModelConfig
from bbx_contracts.pi_models import PiModelRef
from bbx_contracts.profile import load_profile

from bbx_runtime.clients import BlackboardClient
from bbx_runtime.gateway_model import GatewayDispatch, Proof, Purpose, gateway_origin
from bbx_runtime.pi import BaseChatClient, PiChatClient
from bbx_runtime.pi.bridge import PiBridge
from bbx_runtime.pi_sender import OrdinaryPiModel, PiSender
from bbx_runtime.settings import NativeRunnerSettings, Settings


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


def make_task_model_client(
    model: ModelConfig,
    *,
    source: Mapping[str, Any],
    settings: Settings | NativeRunnerSettings,
    service: BlackboardClient,
    task_id: str,
    purpose: Purpose,
    proof: Proof,
    explore_max_steps: int,
    conclude_grace_calls: int,
    max_duration_seconds: int,
) -> PiChatClient:
    direct = source.get("pi_model")
    if direct is None:
        return make_gateway_client(
            model,
            snapshot=GatewayModelSnapshot.model_validate(source.get("gateway_model")),
            service=service,
            task_id=task_id,
            purpose=purpose,
            gateway_url=settings.runner_gateway_url,
            proof=proof,
            explore_max_steps=explore_max_steps,
            conclude_grace_calls=conclude_grace_calls,
            max_duration_seconds=max_duration_seconds,
        )
    reference = PiModelRef.model_validate(direct)
    token = settings.pi_runtime_sender_token
    if token is None:
        raise ValueError("The trusted Pi model sender is not configured")
    logical = model.model_copy(
        update={
            "model": reference.model_id,
            "provider": "openai_compatible",
            "provider_options": {},
            "base_url": "https://pi-runtime.invalid/v1",
        }
    )
    client = PiChatClient(
        logical,
        credentials={},
        max_retries=0,
        function_invocation_configuration={
            "max_iterations": explore_max_steps + conclude_grace_calls + 5,
            "max_duration_seconds": max_duration_seconds,
        },
    )
    client.bridge = PiBridge(use_env_proxy=False)
    client.trusted_model = OrdinaryPiModel(
        PiSender(
            service, token.get_secret_value(), settings.pi_provider_allowed_origins, client.bridge
        ),
        task_id,
        reference,
        purpose,
        proof,
    )
    return client


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
