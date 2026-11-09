"""Task model overrides stay isolated from retained platform and worker settings."""

from copy import deepcopy
from pathlib import Path

import pytest
from bbx_blackboard.worker_settings import model_context_threshold
from bbx_contracts.profile import load_profile


@pytest.mark.parametrize(
    "limits, expected",
    [
        (
            {"context_window": 100_000, "max_input_tokens": 40_000, "max_output_tokens": 20_000},
            32_000,
        ),
        (
            {"context_window": 100_000, "max_input_tokens": 100_000, "max_output_tokens": 80_000},
            16_000,
        ),
        ({"context_window": None, "max_input_tokens": 20_000}, 16_000),
        ({"context_window": None}, None),
    ],
)
def test_new_task_threshold_uses_effective_input_capacity_without_mutating_models(limits, expected):
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    configured = {**profile.models.explore.model_dump(mode="json"), **limits}
    original = deepcopy(configured)
    assert model_context_threshold({"explore": configured}) == expected
    assert configured == original
