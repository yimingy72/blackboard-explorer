"""Profile snapshot and DeepSeek client construction, without network calls."""

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agent_framework import FunctionInvocationLayer
from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient
from bbx_runtime.models import load_runtime_profile, make_client, model_api_key, model_run_options

PROFILE_DIR = Path(__file__).resolve().parents[3] / "profiles" / "default"


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


def test_make_client_is_explicit_and_bounded(monkeypatch):
    import agent_framework._settings as maf_settings

    monkeypatch.setattr(
        maf_settings,
        "dotenv_values",
        lambda **_kwargs: pytest.fail("make_client must not read a dotenv file"),
    )
    model = load_runtime_profile(PROFILE_DIR).models.explore
    client = cast(
        Any,
        make_client(
            model,
            api_key="test-only-key",
            explore_max_steps=60,
            conclude_grace_calls=3,
            max_duration_seconds=3600,
        ),
    )
    assert isinstance(client, OpenAIChatCompletionClient)
    assert isinstance(client, FunctionInvocationLayer)
    assert client.function_invocation_configuration.get("max_iterations") == 68
    assert client.function_invocation_configuration.get("max_duration_seconds") == 3600
    assert client.client.timeout == 120
    assert model_run_options(model) == {"reasoning_effort": "high"}


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


@pytest.mark.parametrize(
    ("provider", "client_type"),
    [
        ("deepseek", OpenAIChatCompletionClient),
        ("openai_chat", OpenAIChatCompletionClient),
        ("openai_compatible", OpenAIChatCompletionClient),
        ("openai_responses", OpenAIChatClient),
    ],
)
def test_platform_provider_uses_pinned_endpoint(provider, client_type):
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={
            "provider": provider,
            "model": "snapshot-model",
            "base_url": "https://model.test/v1",
        }
    )
    client = cast(
        Any,
        make_client(
            model,
            api_key="stored-test-key",
            explore_max_steps=4,
            conclude_grace_calls=1,
            max_duration_seconds=180,
        ),
    )
    assert isinstance(client, client_type)
    assert client.model == "snapshot-model"
    assert str(client.client.base_url) == "https://model.test/v1/"
    assert model_run_options(model) == (
        {"reasoning": {"effort": "high"}}
        if provider == "openai_responses"
        else {"reasoning_effort": "high"}
    )
    assert model_run_options(model.model_copy(update={"reasoning_effort": "off"})) == {}


async def test_model_credentials_use_pinned_version_and_environment_only_for_deepseek():
    model = load_runtime_profile(PROFILE_DIR).models.explore.model_copy(
        update={"platform_id": "model-a", "platform_version": 3}
    )
    service = AsyncMock()
    service.get_model_credentials.return_value = {
        "secret": "stored-test-key",
        "credential_source": "stored",
    }
    assert await model_api_key(service, model, "legacy-test-key") == "stored-test-key"
    service.get_model_credentials.assert_awaited_once_with("model-a", 3)
    service.get_model_credentials.return_value = {
        "secret": None,
        "credential_source": "environment",
    }
    assert await model_api_key(service, model, "legacy-test-key") == "legacy-test-key"
    service.get_model_credentials.return_value = {"secret": None, "credential_source": "none"}
    assert await model_api_key(service, model, "legacy-test-key") == "not-required"
    service.get_model_credentials.return_value = {
        "secret": None,
        "credential_source": "environment",
    }
    with pytest.raises(ValueError, match="DeepSeek"):
        await model_api_key(service, model.model_copy(update={"provider": "openai_chat"}), "legacy")
    service.get_model_credentials.return_value = {"secret": None, "credential_source": "stored"}
    with pytest.raises(ValueError, match="unavailable"):
        await model_api_key(service, model, "legacy")
