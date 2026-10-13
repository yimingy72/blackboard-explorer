"""The runtime snapshot is restricted to the trusted service identity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_blackboard.settings import Settings
from fastapi import HTTPException
from pydantic import SecretStr


async def test_runtime_state_is_service_only_and_public_state_remains_unchanged(monkeypatch):
    tid, turn = uuid4(), uuid4()
    settings = Settings.model_construct(
        postgres_password=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        service_token=SecretStr("service-test"),
        agent_token_secret=SecretStr("test-secret-with-at-least-thirty-two-characters"),
        admin_users=SecretStr("alice:test"),
    )
    app = api.create_app(settings)
    lean = {"task": {}, "members": [], "turns": [], "ready_member_ids": ["lead"]}
    full = {"task": {"ctf_control": {}}, "members": [], "messages": [{"body": "history"}]}
    backend = SimpleNamespace(
        runtime_state=AsyncMock(return_value=lean),
        state=AsyncMock(return_value=full),
        authorize_reader=AsyncMock(),
    )
    app.state.ctf_service = backend
    monkeypatch.setattr(api, "_task", AsyncMock(return_value={"id": tid, "mode": "ctf"}))
    tokens = (
        issue_user_token(settings, "alice"),
        issue_agent_token(settings, tid, "lead", generation=1, turn_id=turn),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for token in tokens:
            response = await client.get(
                f"/api/tasks/{tid}/ctf/runtime-state",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 403
        backend.runtime_state.assert_not_awaited()
        headers = {"Authorization": "Bearer service-test"}
        response = await client.get(f"/api/tasks/{tid}/ctf/runtime-state", headers=headers)
        assert response.status_code == 200
        assert response.json() == lean
        response = await client.get(f"/api/tasks/{tid}/ctf/state", headers=headers)
        assert response.json() == full
    backend.runtime_state.assert_awaited_once_with(tid)
    backend.state.assert_awaited_once_with(tid)


async def test_list_members_reads_only_roster_and_matches_full_state(monkeypatch):
    from unittest.mock import Mock

    from bbx_blackboard import ctf as module

    tid = uuid4()
    tables = []
    rows = {
        "ctf_members": [{"id": "lead"}, {"id": "member-1"}],
        "ctf_turns": [{"id": "old-turn", "status": "finished"}],
        "ctf_messages": [{"id": "old-message", "body": "history"}],
        "ctf_challenges": [{"data": {"id": "challenge"}}],
        "ctf_records": [{"data": {"id": "record", "body": "record history"}}],
    }

    async def execute(query):
        table = query.get_final_froms()[0].name
        tables.append(table)
        return SimpleNamespace(mappings=lambda: rows[table])

    connection = AsyncMock()
    connection.execute = execute
    context = AsyncMock()
    context.__aenter__.return_value = connection
    engine = Mock()
    engine.connect.return_value = context
    monkeypatch.setattr(module, "row", AsyncMock(return_value={"mode": "ctf"}))
    backend = module.CtfService(engine)
    roster = await backend.list_members(tid)
    assert tables == ["ctf_members"]
    assert roster["members"][0]["agent_id"] == "lead"
    full = await backend.state(tid)
    assert roster["members"] == full["members"]
    assert full["messages"] == rows["ctf_messages"]
    assert full["turns"] == rows["ctf_turns"]
    assert full["records"] == [{"id": "record", "body": "record history"}]


@pytest.mark.parametrize("role", ["lead", "teammate"])
async def test_roster_tool_uses_lightweight_state_and_preserves_filtering_and_fences(
    monkeypatch, role
):
    tid, turn = uuid4(), uuid4()
    settings = Settings.model_construct(
        postgres_password=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        service_token=SecretStr("service-test"),
        agent_token_secret=SecretStr("test-secret-with-at-least-thirty-two-characters"),
        admin_users=SecretStr("alice:test"),
    )
    app = api.create_app(settings)
    task = {"id": tid, "mode": "ctf", "agent_profile": "ctf", "agent_profile_version": 1}
    member = {
        "id": "member-1",
        "role": "teammate",
        "execution": {"boot_id": "private-marker"},
        "pending_operation": {"request_id": "private-marker"},
        "operation_history": ["private-marker"],
        "create_request_id": "private-marker",
        "normalized_name": "private-marker",
    }
    backend = SimpleNamespace(
        authorize_member=AsyncMock(return_value={"role": role}),
        authorize_reader=AsyncMock(),
        runtime_state=AsyncMock(return_value={"task": task, "members": [member]}),
        state=AsyncMock(side_effect=AssertionError("Tools must not read complete history")),
    )
    app.state.ctf_service = backend
    profile = {"worker_tools": {role: {"builtin": ["list_members"]}}}
    app.state.profile_store = SimpleNamespace(get=AsyncMock(return_value=profile))
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    token = issue_agent_token(settings, tid, "lead", generation=2, turn_id=turn)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        path = f"/api/tasks/{tid}/ctf/tools/list_team"
        response = await client.post(path, headers=headers, json={"generation": 999})
        assert response.status_code == 200
        assert response.json() == [{"id": "member-1", "agent_id": "member-1", "role": "teammate"}]
        assert "private-marker" not in response.text
        assert backend.authorize_member.await_count == 2
        assert all(
            call.args == (tid, "lead", 2, turn) for call in backend.authorize_member.await_args_list
        )
        backend.runtime_state.assert_awaited_once_with(tid)
        profile["worker_tools"][role]["builtin"] = []
        assert (await client.post(path, headers=headers, json={})).status_code == 403
        before = backend.runtime_state.await_count
        backend.authorize_member.side_effect = HTTPException(409, "Stale CTF generation")
        assert (await client.post(path, headers=headers, json={})).status_code == 409
        assert backend.runtime_state.await_count == before
    backend.state.assert_not_awaited()
