"""Role policies preserve the base model and validate the actual selected provider."""

from pathlib import Path

import pytest
from bbx_contracts.ctf import (
    CtfAgentProfile,
    CtfRoleRunOptions,
    ctf_tool_capability_issues,
    load_ctf_profile,
    resolve_ctf_model,
)
from bbx_contracts.model_limits import effective_max_output_tokens


def profile(**updates):
    original = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    return CtfAgentProfile.model_validate({**original.model_dump(), **updates})


def test_inherited_and_overridden_models_are_independent_validated_copies():
    value = profile(
        role_run_options={"lead": {"reasoning_effort": "low", "max_output_tokens": 4096}}
    )
    original = value.model_dump()
    lead, teammate = resolve_ctf_model(value, "lead"), resolve_ctf_model(value, "teammate")
    assert lead.reasoning_effort == "low" and lead.max_output_tokens == 4096
    assert teammate == value.model and teammate is not value.model
    assert {
        key: item
        for key, item in lead.model_dump().items()
        if key not in {"reasoning_effort", "max_output_tokens"}
    } == {
        key: item
        for key, item in value.model.model_dump().items()
        if key not in {"reasoning_effort", "max_output_tokens"}
    }
    lead.price.output_per_m = None
    assert value.model_dump() == original


@pytest.mark.parametrize("output", [0, -1, True, "4096", 1.5])
def test_output_override_requires_a_positive_integer(output):
    with pytest.raises(ValueError):
        CtfRoleRunOptions.model_validate({"max_output_tokens": output})


def test_role_bounds_use_explicit_or_legacy_base_limit():
    original = profile()
    maximum = effective_max_output_tokens(original.model)
    assert (
        resolve_ctf_model(
            profile(role_run_options={"lead": {"max_output_tokens": maximum}}), "lead"
        ).max_output_tokens
        == maximum
    )
    with pytest.raises(ValueError, match="base model output limit"):
        profile(role_run_options={"lead": {"max_output_tokens": maximum + 1}})
    original.model.max_output_tokens = 8192
    with pytest.raises(ValueError, match="base model output limit"):
        profile(model=original.model, role_run_options={"teammate": {"max_output_tokens": 8193}})


@pytest.mark.parametrize(
    "options",
    [
        {"lead": {"reasoning_effort": "unsupported"}},
        {"other": {"reasoning_effort": "low"}},
        {"lead": {"context_threshold": 10000}},
    ],
)
def test_unknown_role_or_policy_is_rejected(options):
    with pytest.raises(ValueError):
        profile(role_run_options=options)


def test_empty_policy_inherits_and_unsupported_provider_cannot_silently_ignore_effort():
    value = profile(role_run_options={"lead": {}, "teammate": {"reasoning_effort": None}})
    assert value.role_run_options == {}
    model = value.model.model_dump()
    model.update(
        provider="ollama", model="synthetic", price={**model["price"], "billing_mode": "fixed"}
    )
    # Existing base effort remains loadable; a new role override must be supported.
    assert profile(model=model).role_run_options == {}
    with pytest.raises(ValueError, match="unsupported"):
        profile(model=model, role_run_options={"lead": {"reasoning_effort": "low"}})


def test_new_tool_names_are_allowed_but_capabilities_do_not_expand_old_allowlists():
    value = profile()
    before = value.model_dump()
    assert "start_command" not in value.worker_tools["lead"].builtin
    value.worker_tools["lead"].builtin.extend(
        name
        for name in ("get_record", "start_command")
        if name not in value.worker_tools["lead"].builtin
    )
    checked = CtfAgentProfile.model_validate(value.model_dump())
    assert ctf_tool_capability_issues(checked)["lead"] == ["cancel_command", "read_command_result"]
    checked.worker_tools["lead"].builtin.remove("read_saved_tool_result")
    assert "read_saved_tool_result" in ctf_tool_capability_issues(checked)["lead"]
    assert "start_command" not in profile().worker_tools["lead"].builtin
    assert before == profile().model_dump()
