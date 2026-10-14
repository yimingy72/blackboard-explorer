"""Internal recovery routes keep ledger payloads and malformed IDs out of tool errors."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_contracts.ctf import load_ctf_profile
from test_ctf_api import settings


async def test_internal_recovery_routes_require_service_and_reject_supplied_ledger(monkeypatch):
    tid = uuid4()
    config = settings()
    app = api.create_app(config)
    backend = SimpleNamespace(
        reconcile_usage_outbox=AsyncMock(return_value={"reconciled": 0}),
        reconcile_budget=AsyncMock(return_value={"reconciled": 0}),
        idle_watchdog=AsyncMock(return_value={"action": "waiting"}),
    )
    app.state.ctf_service = backend
    monkeypatch.setattr(api, "_task", AsyncMock(return_value={"id": tid, "mode": "ctf"}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        user = {"Authorization": f"Bearer {issue_user_token(config, 'alice')}"}
        response = await client.post(
            f"/api/tasks/{tid}/ctf/runtime/reconcile_usage_outbox",
            headers=user,
            json={"agent_id": "lead"},
        )
        assert response.status_code == 403
        service = {"Authorization": "Bearer service-test"}
        for operation, body in (
            ("reconcile_usage_outbox", {"agent_id": "lead", "usage": {"cost": "0"}}),
            ("reconcile_usage_outbox", {"agent_id": ""}),
            ("reconcile_budget", {"usage": {"cost": "0"}}),
            ("idle_watchdog", {"idle_seconds": 0}),
        ):
            response = await client.post(
                f"/api/tasks/{tid}/ctf/runtime/{operation}", headers=service, json=body
            )
            assert response.status_code == 422
        for operation, body in (
            ("reconcile_usage_outbox", {"agent_id": "lead"}),
            ("reconcile_budget", {}),
            ("idle_watchdog", {}),
        ):
            response = await client.post(
                f"/api/tasks/{tid}/ctf/runtime/{operation}", headers=service, json=body
            )
            assert response.status_code == 200
    backend.reconcile_usage_outbox.assert_awaited_once_with(tid, agent_id="lead")
    backend.reconcile_budget.assert_awaited_once_with(tid)
    backend.idle_watchdog.assert_awaited_once_with(tid)


@pytest.mark.parametrize("identifiers", [["not-a-uuid"], [], [str(uuid4())] * 101])
async def test_invalid_confirmation_is_a_safe_422_not_server_failure(monkeypatch, identifiers):
    tid, turn = uuid4(), uuid4()
    config = settings()
    app = api.create_app(config)
    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    backend = SimpleNamespace(
        authorize_member=AsyncMock(return_value={"role": "lead"}),
        authorize_tool=AsyncMock(
            return_value={
                "task": {"agent_profile": "ctf", "agent_profile_version": 1},
                "member": {"id": "lead", "role": "lead"},
                "purpose": "execution",
            }
        ),
        runtime_state=AsyncMock(
            return_value={"task": {"agent_profile": "ctf", "agent_profile_version": 1}}
        ),
        confirm_messages=AsyncMock(),
    )
    app.state.ctf_service = backend
    app.state.profile_store = SimpleNamespace(
        get=AsyncMock(return_value=profile.model_dump(mode="json"))
    )
    monkeypatch.setattr(api, "_task", AsyncMock(return_value={"id": tid, "mode": "ctf"}))
    token = issue_agent_token(config, tid, "lead", generation=1, turn_id=turn)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/tasks/{tid}/ctf/tools/confirm_messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"message_ids": identifiers},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "Message IDs must be UUIDs"
    backend.confirm_messages.assert_not_awaited()
