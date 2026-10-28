"""New CTF capabilities preserve frozen contracts and bound batch inputs."""

from pathlib import Path
from uuid import uuid4

import pytest
from bbx_contracts.ctf import (
    CtfAgentProfile,
    CtfChallengeBatchAssignRequest,
    CtfChallengeBatchCreateRequest,
    ctf_tool_capability_issues,
    load_ctf_profile,
)


def profile():
    return load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")


def test_legacy_profile_can_load_but_new_selection_reports_missing_readback():
    original = profile().model_dump(mode="json")
    original["worker_tools"]["lead"]["builtin"].remove("read_saved_tool_result")
    original["worker_tools"]["teammate"]["builtin"].remove("get_challenge")
    legacy = CtfAgentProfile.model_validate(original)
    assert ctf_tool_capability_issues(legacy) == {
        "lead": ["read_saved_tool_result"],
        "teammate": ["get_challenge"],
    }
    assert ctf_tool_capability_issues(legacy, ("lead",)) == {"lead": ["read_saved_tool_result"]}
    assert legacy.model_dump(mode="json") == original


@pytest.mark.parametrize("count", [0, 21])
def test_batch_creation_size_is_bounded(count):
    with pytest.raises(ValueError):
        CtfChallengeBatchCreateRequest.model_validate(
            {"items": [{"request_id": uuid4(), "title": "Independent objective"}] * count}
        )


def test_batch_assignment_requires_explicit_work_and_unique_recipients():
    item = {
        "request_id": uuid4(),
        "challenge_id": uuid4(),
        "expected_revision": 1,
        "owner_id": "member-1",
    }
    with pytest.raises(ValueError):
        CtfChallengeBatchAssignRequest.model_validate({"items": [item]})
    instruction = {"recipient_id": "member-1", "body": "Inspect this objective"}
    with pytest.raises(ValueError):
        CtfChallengeBatchAssignRequest.model_validate(
            {"items": [{**item, "instructions": [instruction] * 2}]}
        )
    request = CtfChallengeBatchAssignRequest.model_validate(
        {"items": [{**item, "instructions": [instruction]}]}
    )
    assert request.items[0].request_id == item["request_id"]


def test_default_profile_has_complete_readback_capabilities():
    assert ctf_tool_capability_issues(profile()) == {}
