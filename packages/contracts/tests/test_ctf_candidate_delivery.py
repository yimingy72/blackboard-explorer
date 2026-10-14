"""Candidate delivery declares work separately from trusted acceptance."""

from uuid import uuid4

import pytest
from bbx_contracts.ctf import CtfSolution, CtfVerificationCandidateRequest
from pydantic import ValidationError


@pytest.mark.parametrize("work_status", [None, "in_progress", "blocked", "completed"])
def test_candidate_delivery_work_status(work_status):
    request = CtfVerificationCandidateRequest(
        request_id=uuid4(),
        expected_revision=1,
        status="unknown",
        summary="Work delivered",
        work_status=work_status,
        solution=CtfSolution.model_validate(
            {
                "conclusion": "Work remains",
                "approach": "Inspect sample",
                "steps": [
                    {
                        "kind": "attempt",
                        "title": "Inspect",
                        "result": "inconclusive",
                        "summary": "No result",
                    }
                ],
                "remaining": [],
                "reproduction": "Inspect again",
            }
        )
        if work_status == "completed"
        else None,
    )
    assert request.work_status == work_status
    assert request.status == "unknown"


@pytest.mark.parametrize(
    "patch", [{"work_status": "accepted"}, {"work_status": "cancelled"}, {"status": "accepted"}]
)
def test_candidate_delivery_rejects_acceptance_and_unrelated_status(patch):
    with pytest.raises(ValidationError):
        CtfVerificationCandidateRequest.model_validate(
            {
                "request_id": uuid4(),
                "expected_revision": 1,
                "status": "candidate",
                "summary": "Candidate",
                **patch,
            }
        )
