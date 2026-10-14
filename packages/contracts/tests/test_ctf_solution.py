"""Bounded structured progress and honest unsuccessful deliveries."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from bbx_contracts.ctf import (
    CtfChallenge,
    CtfChallengeUpdateRequest,
    CtfProgress,
    CtfRecord,
    CtfSolution,
    CtfVerificationCandidateRequest,
)
from pydantic import ValidationError


def payload() -> dict[str, Any]:
    return dict(
        conclusion="No solution yet",
        approach="Inspect sample",
        steps=[dict(kind="attempt", title="Read", result="ruled_out", summary="Invalid format")],
        reproduction="Read the sample header",
        remaining=["Need another sample"],
    )


def test_unsuccessful_delivery_and_old_board_shapes():
    assert CtfSolution.model_validate(payload()).steps[0].result == "ruled_out"
    challenge = CtfChallenge(id=uuid4(), task_id=uuid4(), title="Legacy", revision=1)
    assert challenge.solution_record_id is None
    record = CtfRecord(
        id=uuid4(),
        task_id=challenge.task_id,
        challenge_id=challenge.id,
        author_id="owner",
        kind="note",
        body="Legacy",
        created_version=1,
        created_at=datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert record.solution is None and record.progress is None


@pytest.mark.parametrize("field", ["conclusion", "approach", "reproduction"])
@pytest.mark.parametrize("value", ["", " \n ", "x" * 301])
def test_solution_requires_nonblank_bounded_text(field, value):
    with pytest.raises(ValidationError):
        CtfSolution.model_validate({**payload(), field: value})


@pytest.mark.parametrize(
    "patch",
    [
        {"steps": []},
        {"steps": payload()["steps"] * 7},
        {"remaining": ["a"] * 4},
        {"remaining": [" "]},
        {"steps": [{**payload()["steps"][0], "record_ids": ["path"]}]},
    ],
)
def test_solution_rejects_unbounded_lists_and_paths(patch):
    with pytest.raises(ValidationError):
        CtfSolution.model_validate({**payload(), **patch})


def test_total_text_bound_and_progress_metadata():
    step = dict(kind="attempt", title="t" * 40, result="inconclusive", summary="s" * 160)
    with pytest.raises(ValidationError):
        CtfSolution.model_validate(
            {**payload(), "steps": [step] * 6, "reproduction": "r" * 300, "conclusion": "c" * 200}
        )
    for patch in ({"title": " "}, {"summary": "s" * 161}, {"stage": "solved"}):
        with pytest.raises(ValidationError):
            CtfProgress.model_validate(
                {"stage": "attempt", "title": "Read", "summary": "Unclear", **patch}
            )


@pytest.mark.parametrize("candidate", [True, False])
def test_completed_requires_solution_and_other_status_rejects_it(candidate):
    model = CtfVerificationCandidateRequest if candidate else CtfChallengeUpdateRequest
    base = dict(request_id=uuid4(), expected_revision=1, work_status="completed")
    base.update(
        dict(status="unknown", summary="Delivered") if candidate else dict(action="set_status")
    )
    with pytest.raises(ValidationError):
        model.model_validate(base)
    assert model.model_validate({**base, "solution": payload()})
    with pytest.raises(ValidationError):
        model.model_validate({**base, "work_status": "blocked", "solution": payload()})


@pytest.mark.parametrize("field", ["record_ids", "artifact_ids"])
def test_solution_step_reference_limits(field):
    report = payload()
    step = {**report["steps"][0], field: [str(uuid4()) for _ in range(8)]}
    assert CtfSolution.model_validate({**report, "steps": [step]})
    with pytest.raises(ValidationError):
        CtfSolution.model_validate(
            {**report, "steps": [{**step, field: step[field] + [str(uuid4())]}]}
        )
