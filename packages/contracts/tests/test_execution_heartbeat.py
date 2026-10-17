"""Optional execution fences preserve pre-upgrade usage receipt digests."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from bbx_contracts.heartbeat import HeartbeatRequest, heartbeat_digest
from bbx_contracts.models import Usage
from pydantic import ValidationError


def test_missing_execution_fence_keeps_legacy_receipt_digest():
    legacy = {
        "requested_at": None,
        "expected_derive_round": None,
        "steps": 0,
        "context_tokens": 0,
        "usage": {},
        "usage_known": True,
        "last_seen_version": 0,
        "request_id": None,
        "expected_run_number": None,
    }
    normalized = HeartbeatRequest.model_validate(legacy).model_dump(mode="json")
    normalized.pop("expected_started_at")
    expected = hashlib.sha256(
        json.dumps(
            {"task_id": "task", "agent_id": "agent", "request": normalized},
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert heartbeat_digest("task", "agent", legacy) == expected
    assert heartbeat_digest("task", "agent", {**legacy, "expected_started_at": None}) == expected
    fenced = {**legacy, "expected_started_at": datetime(2026, 10, 17, tzinfo=UTC)}
    assert heartbeat_digest("task", "agent", fenced) != expected


def test_execution_fence_requires_timezone():
    with pytest.raises(ValidationError):
        HeartbeatRequest(
            steps=0,
            context_tokens=0,
            usage=Usage(),
            last_seen_version=0,
            expected_started_at=datetime(2026, 10, 17),
        )
