"""Model snapshots govern every caller's input and output capacity."""

import pytest
from bbx_contracts.model_limits import effective_max_input_tokens, effective_max_output_tokens
from bbx_contracts.models import ModelConfig, Price


def model(**limits):
    return ModelConfig(
        provider="openai_compatible",
        model="test-model",
        base_url="https://offline.invalid",
        reasoning_effort="none",
        price=Price(),
        **limits,
    )


@pytest.mark.parametrize(
    "model_id, expected", [("test-model", 8192), ("deepseek-v4-flash", 393216)]
)
def test_legacy_fallback_uses_model_id_and_preserves_explicit_null_context(model_id, expected):
    value = model(context_window=None).model_copy(update={"model": model_id})
    assert effective_max_output_tokens(value) == expected
    assert effective_max_input_tokens(value) is None
    assert value.context_window is None


def test_explicit_platform_limits_override_fallback_and_survive_snapshot_roundtrip():
    value = model(max_input_tokens=600_000, max_output_tokens=500_000)
    saved = value.model_dump(mode="json")
    pinned = ModelConfig.model_validate(saved)
    assert effective_max_output_tokens(pinned) == 500_000
    assert effective_max_input_tokens(pinned) == 500_000
    assert pinned.model_dump(mode="json") == saved


def test_explicit_input_limit_is_independent_of_output_and_restricted_by_context():
    value = model(context_window=100_000, max_input_tokens=20_000, max_output_tokens=30_000)
    assert effective_max_input_tokens(value) == 20_000
    assert effective_max_output_tokens(value) == 30_000
    value = value.model_copy(update={"max_input_tokens": 90_000})
    assert effective_max_input_tokens(value) == 70_000


def test_context_safety_reserves_input_without_overriding_a_large_explicit_output_setting():
    value = model(context_window=100_000, max_output_tokens=80_000)
    assert effective_max_output_tokens(value) == 80_000
    assert effective_max_input_tokens(value) == 20_000


@pytest.mark.parametrize(
    "limits",
    [
        {"max_input_tokens": 100_001},
        {"max_output_tokens": 100_000},
        {"max_output_tokens": 200_000},
    ],
)
def test_explicit_limits_outside_context_are_rejected(limits):
    with pytest.raises(ValueError):
        model(context_window=100_000, **limits)


def test_input_and_output_are_independent_ceilings_not_a_combined_configuration_limit():
    value = model(context_window=100_000, max_input_tokens=100_000, max_output_tokens=99_999)
    assert effective_max_output_tokens(value) == 99_999
    assert effective_max_input_tokens(value) == 1


def test_legacy_null_context_accepts_explicit_limits_without_a_retroactive_context_cap():
    value = model(context_window=None, max_input_tokens=2_000_000, max_output_tokens=2_000_000)
    assert effective_max_input_tokens(value) == effective_max_output_tokens(value) == 2_000_000


def test_smallest_legacy_context_has_zero_input_capacity():
    value = model(context_window=1)
    assert effective_max_output_tokens(value) == 1
    assert effective_max_input_tokens(value) == 0
