"""Inject an offline HTTP transport through pi-ai's supported fetch extension."""

from collections.abc import Mapping
from pathlib import Path

from bbx_contracts.gateway_models import GatewayModelSnapshot, GatewayUsdRates
from bbx_contracts.model_limits import effective_max_output_tokens
from bbx_contracts.model_urls import in_container, resolve_model_url
from bbx_contracts.models import ModelConfig
from bbx_contracts.providers import PROVIDERS

from bbx_runtime.models import load_runtime_profile
from bbx_runtime.pi import PiChatClient


def make_test_client(
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


def gateway_test_snapshot(model: ModelConfig) -> dict:
    """A fully specified, non-network selection for scripted worker tests."""
    context = max(model.context_window or 100_000, 1024)
    return GatewayModelSnapshot(
        alias="scripted-gateway-model",
        catalog_revision="a" * 64,
        accounting_source_id="scripted-gateway",
        route_revision=1,
        route_digest="b" * 64,
        model_catalog_digest="c" * 64,
        label="Scripted model",
        context_window=context,
        max_output_tokens=min(effective_max_output_tokens(model), context - 1),
        input_modalities=["text", "image"],
        tool_call=True,
        usd_rates=GatewayUsdRates.model_validate(
            {"input": "1", "output": "2", "cache_read": "3", "cache_write": "4"}
        ),
    ).model_dump(mode="json")


def responses_client(
    *,
    model="offline",
    async_client=None,
    api_key="test-only",
    base_url="https://model.test/v1",
    responses_replay_mode="native",
    **_kwargs,
):
    definition = load_runtime_profile(
        Path(__file__).resolve().parents[5] / "profiles/default"
    ).models.explore
    if async_client is not None:
        base_url = str(async_client.base_url)
    definition = definition.model_copy(
        update={
            "provider": "openai_responses",
            "model": model,
            "base_url": base_url,
            "provider_options": {"responses_replay_mode": responses_replay_mode},
        }
    )
    client = make_test_client(
        definition,
        api_key=api_key,
        explore_max_steps=40,
        conclude_grace_calls=0,
        max_duration_seconds=120,
        max_retries=0,
    )
    if async_client is not None:
        client.request_transport = async_client._client._transport
    return client


def patch_transport(monkeypatch, transport) -> None:
    constructor = PiChatClient.__init__

    def configure(self, *args, **kwargs):
        constructor(self, *args, **kwargs)
        self.request_transport = transport

    monkeypatch.setattr(PiChatClient, "__init__", configure)
