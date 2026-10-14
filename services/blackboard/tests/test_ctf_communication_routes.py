"""New readback and proof routes keep the existing identity boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.auth import issue_agent_token
from test_ctf_api import settings


def setup(operation, monkeypatch, *, enabled=True):
    tid, turn = uuid4(), uuid4()
    service = SimpleNamespace(
        authorize_member=AsyncMock(return_value={"id": "lead", "role": "lead"}),
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
    )
    setattr(service, operation, AsyncMock(return_value={"ok": True}))
    app = api.create_app(settings())
    app.state.ctf_service = service
    monkeypatch.setattr(
        api,
        "_task",
        AsyncMock(return_value={"id": tid, "mode": "ctf", "status": "running", "deleting": False}),
    )
    app.state.profile_store = SimpleNamespace(
        get=AsyncMock(
            return_value={"worker_tools": {"lead": {"builtin": [operation] if enabled else []}}}
        )
    )
    token = issue_agent_token(settings(), tid, "lead", generation=1, turn_id=turn)
    return tid, turn, app, service, {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "operation,body",
    [
        ("read_turn_result", {"turn_id": str(uuid4())}),
        ("read_saved_tool_result", {"call_id": "saved-call"}),
        ("list_deferred_messages", {}),
    ],
)
async def test_readback_routes_use_token_actor_and_validate_paging(operation, body, monkeypatch):
    tid, _, app, service, headers = setup(operation, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/api/tasks/{tid}/ctf/tools/{operation}"
        response = await client.post(path, json=body, headers=headers)
        assert response.status_code == 200
        call = getattr(service, operation).await_args
        assert call.kwargs["actor"] == "lead"
        assert call.kwargs["offset"] == 0
        assert call.kwargs["limit"] == (5 if operation == "list_deferred_messages" else 2000)
        for invalid in ({"actor": "other-member"}, {"limit": 0}, {"offset": -1}):
            response = await client.post(path, json={**body, **invalid}, headers=headers)
            assert response.status_code == 422
        response = await client.post(path, json=body)
        assert response.status_code in {401, 403}
    assert getattr(service, operation).await_count == 1


async def test_readback_tool_cannot_bypass_saved_worker_allowlist(monkeypatch):
    tid, _, app, service, headers = setup("read_saved_tool_result", monkeypatch, enabled=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/tasks/{tid}/ctf/tools/read_saved_tool_result",
            json={"call_id": "saved-call"},
            headers=headers,
        )
    assert response.status_code == 403
    service.read_saved_tool_result.assert_not_awaited()


async def test_recovery_input_proofs_are_service_only_and_body_is_bounded(monkeypatch):
    tid, turn, app, service, agent_headers = setup("recovery_input_proofs", monkeypatch)
    body = {
        "agent_id": "lead",
        "generation": 1,
        "turn_id": str(turn),
        "message_ids": [str(uuid4())],
    }
    service_headers = {"Authorization": "Bearer service-test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/api/tasks/{tid}/ctf/runtime/recovery_input_proofs"
        assert (await client.post(path, json=body, headers=agent_headers)).status_code == 403
        assert (await client.post(path, json=body, headers=service_headers)).status_code == 200
        for extra in ({"body": "unexpected"}, {"generation": -1}, {"message_ids": ["not-a-uuid"]}):
            assert (
                await client.post(path, json={**body, **extra}, headers=service_headers)
            ).status_code == 422
    assert service.recovery_input_proofs.await_count == 1
