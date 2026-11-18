"""Pinned model policy and native Pi provider construction, without network."""

from pathlib import Path

import pytest
from bbx_contracts.model_limits import effective_max_output_tokens
from bbx_contracts.providers import PROVIDERS
from bbx_runtime.models import close_model_client, load_runtime_profile, model_run_options
from bbx_runtime.pi import PiChatClient
from bbx_runtime.pi.client import API_BY_PROVIDER
from bbx_runtime.testing import pi_client as runtime_models
from bbx_runtime.testing.pi_client import make_test_client as make_client

PROFILE_DIR = Path(__file__).resolve().parents[3] / "profiles" / "default"


def test_make_client_is_explicit_and_bounded():
    model = load_runtime_profile(PROFILE_DIR).models.explore
    client = make_client(
        model,
        api_key="test-only",
        explore_max_steps=60,
        conclude_grace_calls=3,
        max_duration_seconds=3600,
    )
    assert isinstance(client, PiChatClient)
    assert client.bridge.process is None
    assert client.function_invocation_configuration == {
        "max_iterations": 68,
        "max_duration_seconds": 3600,
    }
    assert client.model_definition()["maxTokens"] == 393216
    assert client.max_retries == 4
    assert model_run_options(model) == {"reasoning_effort": "high", "max_tokens": 393216}


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
async def test_every_provider_uses_native_pi_api_and_keeps_snapshot(provider):
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": provider,
            "model": "test-model",
            "base_url": "https://model.test/v1",
            "provider_options": {
                "region": "us-east-1",
                "project": "test-project",
                "location": "us-central1",
                "api_version": "2025-01-01",
            },
        }
    )
    before = model.model_dump(mode="json")
    client = make_client(
        model,
        credentials={
            "api_key": "test-only",
            "access_key_id": "test",
            "secret_access_key": "test",
            "service_account_json": "{}",
            "tenant_id": "test",
            "client_id": "test",
            "client_secret": "test",
        },
        explore_max_steps=2,
        conclude_grace_calls=0,
        max_duration_seconds=30,
    )
    try:
        definition = client.model_definition()
        assert definition["api"] == API_BY_PROVIDER[provider]
        assert definition["id"] == "test-model"
        assert definition["contextWindow"] == model.context_window
        assert definition["cost"] == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        assert model.model_dump(mode="json") == before
    finally:
        await close_model_client(client)


@pytest.mark.parametrize("containerized", [False, True])
@pytest.mark.parametrize("provider", ["deepseek", "openai_chat", "openai_responses", "ollama"])
async def test_local_sdk_url_resolves_without_changing_fixed_model(
    monkeypatch, containerized, provider
):
    monkeypatch.setattr(runtime_models, "in_container", lambda: containerized)
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": provider,
            "base_url": "http://127.0.0.1:3425/v1",
        }
    )
    client = make_client(
        model,
        api_key="test-only",
        explore_max_steps=2,
        conclude_grace_calls=0,
        max_duration_seconds=30,
    )
    assert client.model.base_url == (
        "http://host.docker.internal:3425/v1" if containerized else model.base_url
    )
    assert model.base_url == "http://127.0.0.1:3425/v1"
    await close_model_client(client)


def test_profile_snapshot_keeps_template_bodies():
    local = load_runtime_profile(PROFILE_DIR)
    pinned = load_runtime_profile(local.model_dump(mode="json"))
    wrapped = load_runtime_profile(
        {"name": "default", "version": 7, "profile": local.model_dump(mode="json")}
    )
    assert pinned.prompt_templates.explore == local.prompt_templates.explore
    assert pinned.prompt_templates.derive == local.prompt_templates.derive
    assert pinned.prompt_templates.close == local.prompt_templates.close
    assert pinned.prompt_templates.explore
    assert wrapped.prompt_templates == pinned.prompt_templates


def test_make_client_rejects_invalid_configuration():
    model = load_runtime_profile(PROFILE_DIR).models.explore
    with pytest.raises(ValueError, match="key"):
        make_client(
            model,
            api_key="",
            explore_max_steps=60,
            conclude_grace_calls=3,
            max_duration_seconds=3600,
        )
    with pytest.raises(ValueError, match="Unsupported"):
        make_client(
            model.model_copy(update={"provider": "other"}),
            api_key="test-only-key",
            explore_max_steps=60,
            conclude_grace_calls=3,
            max_duration_seconds=3600,
        )


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
@pytest.mark.parametrize("effort", ["high", "none", "off", ""])
def test_only_responses_providers_disable_service_side_history(provider, effort):
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={"provider": provider, "reasoning_effort": effort}
    )
    options = model_run_options(model)
    assert options.pop("max_tokens") == effective_max_output_tokens(model)
    if provider in {"openai_responses", "azure_openai_responses"}:
        assert options == (
            {"store": False, "reasoning": {"effort": "high"}}
            if effort == "high"
            else {"store": False}
        )
    else:
        assert "store" not in options
        if effort != "high":
            assert options == {}


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
async def test_provider_auth_requirement_matches_catalog(provider):
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": provider,
            "base_url": "https://model.test/v1",
            "provider_options": {
                "api_version": "2025-01-01-preview",
                "region": "us-east-1",
                "project": "test-project",
                "location": "us-east5",
            },
        }
    )
    if PROVIDERS[provider]["allow_no_auth"]:
        client = make_client(
            model,
            credentials={},
            explore_max_steps=1,
            conclude_grace_calls=0,
            max_duration_seconds=10,
        )
        await close_model_client(client)
    else:
        with pytest.raises(ValueError, match="Missing model credential"):
            make_client(
                model,
                credentials={},
                explore_max_steps=1,
                conclude_grace_calls=0,
                max_duration_seconds=10,
            )
