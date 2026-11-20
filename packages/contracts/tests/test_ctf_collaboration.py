from uuid import uuid4

import pytest
from bbx_contracts.challenge_board import business_manifest, native_dispatch_enabled
from bbx_contracts.ctf_collaboration import (
    ChallengeCollaboration,
    CollaborativeChallengeRequest,
    ReviewChallenge,
    WorkerContext,
    collaboration_enabled,
)
from bbx_contracts.models import Intent
from pydantic import ValidationError


def test_peer_manifest_has_worker_claims_and_lead_review_without_helper_or_assignment():
    manifest = business_manifest(automatic=True, collaboration=True)
    task = {"ctf_control": {"business_tools": manifest}}
    assert collaboration_enabled(task) and native_dispatch_enabled(task)
    assert "board_claim_challenge" in manifest["roles"]["teammate"]
    assert "board_review_challenge" in manifest["roles"]["lead"]
    assert "board_dispatch_challenge" not in manifest["roles"]["lead"]
    assert not any("helper" in name for names in manifest["roles"].values() for name in names)
    assert not collaboration_enabled(
        {"ctf_control": {"business_tools": business_manifest(automatic=True)}}
    )


def test_multistage_default_and_total_team_limit():
    request = CollaborativeChallengeRequest(request_id=uuid4(), title="Synthetic", multi_stage=True)
    assert request.difficulty == "hard"
    assert ChallengeCollaboration(assistance_target=9).assistance_target == 9
    with pytest.raises(ValidationError):
        ChallengeCollaboration(assistance_target=10)
    with pytest.raises(ValidationError):
        ReviewChallenge(
            request_id=uuid4(),
            challenge_id=uuid4(),
            expected_revision=1,
            review_revision=1,
            decision="rejected",
            reason="   ",
        )


def test_peer_holder_has_a_distinct_claim_assignment():
    origin = {
        "agent_id": "w1",
        "generation": 1,
        "turn_id": uuid4(),
        "epoch": 1,
        "owner_id": "w1",
        "scope_revision": 1,
        "assignment_id": uuid4(),
    }
    data = {
        "id": "I1",
        "challenge_id": uuid4(),
        "assignment_ref": origin,
        "statement": "Inspect",
        "based_on": ["F1"],
        "expected": "Evidence",
        "method": "Read the provided sample",
        "author": "w1",
        "holder": "w2",
        "status": "claimed",
    }
    with pytest.raises(ValidationError):
        Intent.model_validate(data)
    claim = {**origin, "agent_id": "w2", "assignment_id": uuid4()}
    value = Intent.model_validate({**data, "claim_assignment_ref": claim})
    assert value.author == "w1" and value.holder == "w2"
    assert value.claim_assignment_ref and value.assignment_ref
    assert value.claim_assignment_ref.assignment_id != value.assignment_ref.assignment_id
    with pytest.raises(ValidationError):
        WorkerContext(revision=1, assignment_ref="old", challenge_id=None)
