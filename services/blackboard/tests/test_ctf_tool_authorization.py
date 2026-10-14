"""Tool authority reuses execution fences without loading scheduling state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard import ctf as module
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from fastapi import HTTPException
from test_ctf_api import settings


@pytest.fixture
def authority(monkeypatch):
    tid, turn_id = uuid4(), str(uuid4())
    task = {
        "mode": "ctf",
        "deleting": False,
        "status": "running",
        "ctf_control": {"phase": "running"},
        "agent_profile": "saved-profile",
        "agent_profile_version": 4,
        "active_seconds": 0,
        "active_since": None,
        "usage": {},
        "budget": {"max_cost": "1", "max_minutes": 60},
    }
    member = {
        "id": "lead",
        "role": "lead",
        "generation": 2,
        "current_turn_id": turn_id,
        "lifecycle": "active",
        "run_state": "running",
        "pending_operation": None,
        "execution": {"private": "executor-registration"},
    }
    turn = {"id": turn_id, "purpose": "execution", "status": "running"}
    reads = []

    async def read(_conn, table, _tid, oid=None):
        assert _tid == tid
        reads.append((table.name, oid))
        return {"tasks": task, "ctf_members": member, "ctf_turns": turn}[table.name]

    context = AsyncMock()
    context.__aenter__.return_value = AsyncMock()
    engine = Mock()
    engine.begin.return_value = context
    service = module.CtfService(engine)
    service.repo.lock = AsyncMock()
    monkeypatch.setattr(module, "row", read)
    return SimpleNamespace(
        service=service, tid=tid, turn=turn, task=task, member=member, reads=reads
    )


async def test_tool_authority_is_narrow_and_preserves_member_authorization(authority):
    setup = authority
    result = await setup.service.authorize_tool(setup.tid, "lead", 2, setup.turn["id"])
    assert result == {
        "task": {"agent_profile": "saved-profile", "agent_profile_version": 4},
        "member": {"id": "lead", "role": "lead"},
        "purpose": "execution",
    }
    assert setup.reads == [
        ("tasks", None),
        ("ctf_members", "lead"),
        ("ctf_turns", setup.turn["id"]),
    ]
    setup.service.repo.lock.assert_awaited_once()
    assert (
        await setup.service.authorize_member(setup.tid, "lead", 2, setup.turn["id"]) == setup.member
    )


@pytest.mark.parametrize(
    "target,change",
    [
        ("task", {"deleting": True}),
        ("task", {"mode": "blackboard"}),
        ("task", {"ctf_control": {"phase": "closed"}, "status": "stopped"}),
        ("task", {"ctf_control": {"phase": "running", "replacement": {"phase": "draining"}}}),
        ("task", {"usage": {"cost": "1"}}),
        ("task", {"active_seconds": 3600}),
        ("member", {"generation": 3}),
        ("member", {"current_turn_id": str(uuid4())}),
        ("member", {"lifecycle": "stopped"}),
        ("member", {"run_state": "stopping"}),
        ("member", {"pending_operation": {"operation": "stop"}}),
        ("turn", {"status": "finished"}),
        ("turn", {"purpose": "review"}),
    ],
)
async def test_tool_authority_keeps_existing_execution_rejections(authority, target, change):
    setup = authority
    getattr(setup, target).update(change)
    with pytest.raises(HTTPException):
        await setup.service.authorize_tool(setup.tid, "lead", 2, setup.turn["id"])


async def test_tool_authority_keeps_pending_model_reservation_and_budget_recovery(authority):
    setup = authority
    setup.task["ctf_control"]["budget_reservations"] = {
        "call": {"turn_id": setup.turn["id"], "cost": "0.1"}
    }
    with pytest.raises(HTTPException, match="already reserved"):
        await setup.service.authorize_tool(setup.tid, "lead", 2, setup.turn["id"])
    setup.task["ctf_control"] = {
        "phase": "running",
        "budget_unknown_reservations": {"old": {"cost": "1"}},
    }

    async def reconcile(_conn, _tid):
        setup.task["ctf_control"]["budget_unknown_reservations"] = {}

    setup.service._reconcile_budget = AsyncMock(side_effect=reconcile)
    result = await setup.service.authorize_tool(setup.tid, "lead", 2, setup.turn["id"])
    assert result["purpose"] == "execution"
    setup.service._reconcile_budget.assert_awaited_once()


async def test_review_authority_does_not_grant_execution_or_spend_execution_budget(authority):
    setup = authority
    setup.task.update(status="stopped", usage={"cost": "1"}, ctf_control={"phase": "closed"})
    setup.member.update(lifecycle="removed", run_state="stopped")
    setup.turn["purpose"] = "review"
    result = await setup.service.authorize_tool(setup.tid, "lead", 2, setup.turn["id"])
    assert result["purpose"] == "review"
    with pytest.raises(HTTPException, match="Execution turn required"):
        await setup.service.authorize_member(setup.tid, "lead", 2, setup.turn["id"])
    # Authority must be read again after a terminal task resumes.
    setup.task["status"] = "running"
    with pytest.raises(HTTPException, match="Review task resumed"):
        await setup.service.authorize_tool(setup.tid, "lead", 2, setup.turn["id"])


def tool_app(monkeypatch, *, purpose="execution", enabled=True):
    tid, turn_id = uuid4(), uuid4()
    config = settings()
    app = api.create_app(config)
    task = {"mode": "ctf", "agent_profile": "saved-profile", "agent_profile_version": 4}
    service = SimpleNamespace(
        authorize_tool=AsyncMock(
            return_value={
                "task": task,
                "member": {"id": "lead", "role": "lead"},
                "purpose": purpose,
            }
        ),
        authorize_member=AsyncMock(side_effect=AssertionError("Duplicate execution authorization")),
        authorize_review=AsyncMock(side_effect=AssertionError("Speculative review authorization")),
        runtime_state=AsyncMock(side_effect=AssertionError("Unrelated scheduling query")),
        state=AsyncMock(side_effect=AssertionError("Unrelated history query")),
        list_challenges=AsyncMock(return_value={"challenges": []}),
        list_members=AsyncMock(return_value={"members": []}),
        post_message=AsyncMock(return_value={"status": "queued"}),
    )
    app.state.ctf_service = service
    profile = {
        "worker_tools": {
            "lead": {
                "builtin": ["list_challenges", "list_members", "send_message"] if enabled else []
            }
        }
    }
    app.state.profile_store = SimpleNamespace(get=AsyncMock(return_value=profile))
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    token = issue_agent_token(config, tid, "lead", generation=2, turn_id=turn_id)
    return SimpleNamespace(
        tid=tid,
        turn_id=turn_id,
        app=app,
        service=service,
        headers={"Authorization": f"Bearer {token}"},
        config=config,
    )


@pytest.mark.parametrize("operation", ["list_challenges", "list_team", "post_message"])
async def test_asgi_tool_authorizes_once_and_reads_only_its_own_data(monkeypatch, operation):
    setup = tool_app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/tasks/{setup.tid}/ctf/tools/{operation}", headers=setup.headers, json={}
        )
    assert response.status_code == 200, response.text
    setup.service.authorize_tool.assert_awaited_once_with(setup.tid, "lead", 2, setup.turn_id)
    setup.app.state.profile_store.get.assert_awaited_once_with("saved-profile", 4)
    assert setup.service.list_members.await_count == (1 if operation == "list_team" else 0)
    setup.service.runtime_state.assert_not_awaited()
    setup.service.state.assert_not_awaited()


@pytest.mark.parametrize("operation", ["list_challenges", "send_message", "list_team"])
async def test_asgi_review_retains_its_original_tool_whitelist(monkeypatch, operation):
    setup = tool_app(monkeypatch, purpose="review", enabled=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/tasks/{setup.tid}/ctf/tools/{operation}", headers=setup.headers, json={}
        )
    assert response.status_code == (200 if operation == "list_challenges" else 403), response.text
    setup.service.authorize_tool.assert_awaited_once()
    setup.app.state.profile_store.get.assert_not_awaited()
    setup.service.list_members.assert_not_awaited()
    setup.service.post_message.assert_not_awaited()


async def test_asgi_disabled_tool_and_stale_authority_are_rechecked_every_request(monkeypatch):
    setup = tool_app(monkeypatch, enabled=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        path = f"/api/tasks/{setup.tid}/ctf/tools/list_challenges"
        assert (await client.post(path, headers=setup.headers, json={})).status_code == 403
        setup.service.list_challenges.assert_not_awaited()
        setup.service.authorize_tool.side_effect = HTTPException(409, "Member is stopping")
        assert (await client.post(path, headers=setup.headers, json={})).status_code == 409
        assert setup.service.authorize_tool.await_count == 2
        assert setup.app.state.profile_store.get.await_count == 1
        for token, expected in (
            (issue_user_token(setup.config, "alice"), 403),
            (issue_agent_token(setup.config, setup.tid, "lead", generation=2), 409),
            (
                issue_agent_token(
                    setup.config, uuid4(), "lead", generation=2, turn_id=setup.turn_id
                ),
                403,
            ),
        ):
            assert (
                await client.post(path, headers={"Authorization": f"Bearer {token}"}, json={})
            ).status_code == expected
        assert setup.service.authorize_tool.await_count == 2
