"""Profile snapshot and DeepSeek client construction, without network calls."""

from pathlib import Path

import pytest
from agent_framework import FunctionInvocationLayer
from agent_framework.openai import OpenAIChatCompletionClient
from bbx_runtime.models import load_runtime_profile, make_client, model_run_options

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
    client = make_client(
        model,
        api_key="test-only-key",
        explore_max_steps=60,
        conclude_grace_calls=3,
        max_duration_seconds=3600,
    )
    assert isinstance(client, OpenAIChatCompletionClient)
    assert isinstance(client, FunctionInvocationLayer)
    assert client.function_invocation_configuration.get("max_iterations") == 68
    assert client.function_invocation_configuration.get("max_duration_seconds") == 3600
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
