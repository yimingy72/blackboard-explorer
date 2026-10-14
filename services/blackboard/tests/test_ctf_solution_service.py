"""Service calls enforce delivery validation before any persisted mutation."""

from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.ctf_api import invalid_request
from bbx_contracts.ctf import CtfChallengeUpdateRequest, CtfVerificationCandidateRequest
from ctf_solution_fixtures import solution
from fastapi import HTTPException
from pydantic import ValidationError


@pytest.mark.parametrize("candidate", [True, False])
@pytest.mark.parametrize(
    "status,report",
    [
        ("completed", None),
        ("blocked", solution()),
        ("completed", {"conclusion": " "}),
    ],
)
async def test_direct_service_cannot_bypass_solution_contract(candidate, status, report):
    service = CtfService(AsyncMock())
    service._board_write = AsyncMock()
    payload = dict(
        tid=uuid4(),
        actor="user",
        challenge_id=str(uuid4()),
        request_id=str(uuid4()),
        expected_revision=1,
        work_status=status,
        solution=report,
    )
    with pytest.raises(HTTPException) as exc:
        if candidate:
            await service.record_candidate(**payload, status="unknown", summary="Delivered")
        else:
            await service.update_challenge(**payload, action="set_status")
    assert exc.value.status_code == 422
    service._board_write.assert_not_awaited()


async def test_progress_is_validated_on_direct_service_calls():
    service = CtfService(AsyncMock())
    service._board_write = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await service.append_record(
            uuid4(),
            "user",
            str(uuid4()),
            str(uuid4()),
            "Note",
            progress={"stage": "attempt", "title": " ", "summary": "Unknown"},
        )
    assert exc.value.status_code == 422
    service._board_write.assert_not_awaited()


@pytest.mark.parametrize("candidate", [True, False])
def test_schema_delivery_errors_remain_actionable_for_tools(candidate):
    model = CtfVerificationCandidateRequest if candidate else CtfChallengeUpdateRequest
    payload = dict(request_id=str(uuid4()), expected_revision=1, work_status="completed")
    payload.update(
        dict(status="unknown", summary="Delivered") if candidate else dict(action="set_status")
    )
    for report in (None, {"conclusion": " "}):
        with pytest.raises(ValidationError) as exc:
            model.model_validate({**payload, "solution": report})
        response = invalid_request(exc.value, "Invalid request")
        assert response.status_code == 422
        assert isinstance(response.detail, dict)
        detail = cast(dict[str, Any], response.detail)
        assert {key: value for key, value in detail.items() if key != "fields"} == {
            "code": "invalid_solution_delivery",
            "message": "Owner or Lead must supply a valid solution when completing work; "
            "solution is forbidden for other statuses",
        }
        assert detail["fields"]
