"""Task model overrides stay isolated from retained platform and worker settings."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bbx_blackboard.platform import PlatformStore
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.worker_settings import override_reasoning_effort, task_profile
from bbx_contracts.profile import load_profile
from fastapi import HTTPException


@pytest.mark.parametrize(
    "effort", [None, "none", "low", "high", "max", "minimal", "medium", "xhigh", "off"]
)
@pytest.mark.parametrize("provider", ["deepseek", "openai_responses", "openai_compatible"])
async def test_task_effort_snapshots_preserve_model_and_prior_tasks(effort, provider):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    workers = {"name": "default", "version": 4, **profile.model_dump(mode="json")}
    model = {
        **workers["models"]["explore"],
        "provider": provider,
        "model": "deepseek-flash",
        "reasoning_effort": "xhigh",
        "platform_id": "selected-model",
        "platform_version": 2,
        "context_window": 500000,
    }
    prior_task = deepcopy(workers)
    original_model = deepcopy(model)
    profiles = MagicMock(spec=ProfileStore)
    profiles.get.return_value = workers
    snapshots = []

    async def create(name, snapshot, actor):
        assert (name, actor) == ("task-settings", "task")
        row = {"name": name, "version": len(snapshots) + 1, **snapshot.model_dump(mode="json")}
        snapshots.append(row)
        return row

    profiles.create.side_effect = create
    platform = MagicMock(spec=PlatformStore)
    platform.default_model_name.return_value = "selected-model"
    platform.get.return_value = {"enabled": True}
    platform.public.return_value = {"config": model}
    result = await task_profile(profiles, platform, None, None, effort)
    assert {value["reasoning_effort"] for value in result["models"].values()} == {
        "xhigh" if effort is None else effort
    }
    assert result["params"]["context_threshold"] == 400000
    assert workers == prior_task
    assert model == original_model
    old_snapshot = deepcopy(result)
    await task_profile(profiles, platform, "selected-model", 2, "low")
    assert result == old_snapshot
    platform.get.assert_awaited_with("models", "selected-model", 2)
    assert {value["platform_version"] for value in result["models"].values()} == {2}


@pytest.mark.parametrize(
    ("provider", "model", "effort"),
    [
        ("deepseek", "deepseek-flash", "ultra"),
        ("openai_chat", "gpt-5", "max"),
        ("openai_chat", "gpt-5", "off"),
        ("anthropic", "deepseek-flash", "max"),
        ("anthropic", "claude", "none"),
    ],
)
def test_reject_unsupported_task_reasoning_without_mutation(provider, model, effort):
    models = {"explore": {"provider": provider, "model": model, "reasoning_effort": "high"}}
    original = deepcopy(models)
    with pytest.raises(HTTPException) as error:
        override_reasoning_effort(models, effort)
    assert error.value.status_code == 422
    assert models == original
