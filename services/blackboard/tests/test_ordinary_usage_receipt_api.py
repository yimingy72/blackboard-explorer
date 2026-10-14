"""New usage receipts remain service-only; legacy heartbeat bodies stay compatible."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_contracts.heartbeat import HeartbeatRequest
from bbx_contracts.models import Usage
from pydantic import BaseModel, Field, ValidationError
from test_api_auth import settings


async def test_receipt_heartbeat_keeps_service_authority_and_requires_original_scope():
    config = settings()
    app = api.create_app(config)
    backend = SimpleNamespace(heartbeat=AsyncMock(return_value={"confirmed": True}))
    app.state.board_service = backend
    tid = uuid4()
    body = HeartbeatRequest(
        request_id=uuid4(),
        expected_run_number=1,
        requested_at=datetime.now(UTC),
        steps=1,
        context_tokens=1000,
        usage=Usage(output_tokens=300),
        last_seen_version=0,
    ).model_dump(mode="json")
    path = f"/api/tasks/{tid}/agents/agent-1"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test", trust_env=False
    ) as client:
        for token in (issue_user_token(config, "alice"), issue_agent_token(config, tid, "agent-1")):
            result = await client.patch(
                path, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            assert result.status_code == 403
        assert not backend.heartbeat.await_count
        result = await client.patch(
            path, json=body, headers={"Authorization": "Bearer service-test"}
        )
        assert result.status_code == 200
        assert backend.heartbeat.await_args.args == (tid, "agent-1")
        assert str(backend.heartbeat.await_args.kwargs["request_id"]) == body["request_id"]
        enveloped = await client.patch(
            path, json={"receipt": body}, headers={"Authorization": "Bearer service-test"}
        )
        assert enveloped.status_code == 200
        for missing in ("expected_run_number", "requested_at"):
            invalid = {key: value for key, value in body.items() if key != missing}
            response = await client.patch(
                path, json=invalid, headers={"Authorization": "Bearer service-test"}
            )
            assert response.status_code == 422


def test_legacy_heartbeat_does_not_require_receipt_fields_and_keeps_ignored_extras():
    value = api.HeartbeatBody.model_validate(
        {
            "steps": 0,
            "context_tokens": 0,
            "usage": {},
            "last_seen_version": 0,
            "legacy_extra": "ignored as before",
        }
    )
    assert value.request_id is None and value.expected_run_number is None


def test_old_additive_heartbeat_cannot_accept_the_receipt_wire_shape():
    class OldHeartbeatBody(BaseModel):
        steps: int = Field(ge=0)
        context_tokens: int = Field(ge=0)
        usage: Usage
        last_seen_version: int = Field(ge=0)

    with pytest.raises(ValidationError):
        OldHeartbeatBody.model_validate(
            {"receipt": {"steps": 1, "context_tokens": 0, "usage": {}, "last_seen_version": 0}}
        )
