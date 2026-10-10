from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_blackboard.profiles import _content, ctf_profile
from bbx_blackboard.service import ctf_agent_event
from bbx_blackboard.settings import Settings
from bbx_contracts.ctf import load_ctf_profile
from pydantic import SecretStr


def settings():
    return Settings.model_construct(
        postgres_password=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        service_token=SecretStr("service-test"),
        agent_token_secret=SecretStr("test-secret-with-at-least-thirty-two-characters"),
        admin_users=SecretStr("alice:test"),
    )


@pytest.mark.asyncio
async def test_ctf_start_is_idempotent_for_in_progress_tasks(monkeypatch):
    tid = uuid4()
    task = {"id": tid, "mode": "ctf", "status": "provisioning", "deleting": False}
    backend = SimpleNamespace(provision=AsyncMock())
    app = api.create_app(settings())
    app.state.ctf_service = backend
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    headers = {"Authorization": f"Bearer {issue_user_token(settings(), 'alice')}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/tasks/{tid}/start", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "provisioning", "events": []}
        task["status"] = "running"
        response = await client.post(f"/api/tasks/{tid}/start", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"status": "running", "events": []}
    backend.provision.assert_awaited_once_with(tid)


@pytest.mark.asyncio
async def test_ctf_create_auto_provisions_task(monkeypatch):
    tid = uuid4()
    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    profile_row = {"name": "ctf", "version": 1}
    model = profile.model.model_dump(mode="json")
    profiles = SimpleNamespace(
        get=AsyncMock(return_value=profile_row),
        create=AsyncMock(return_value={"name": "ctf-task-settings", "version": 2}),
    )
    platform = SimpleNamespace(
        default_model_name=AsyncMock(return_value="test-model"),
        get=AsyncMock(return_value={"enabled": True, "config": model}),
        public=lambda row: row,
    )
    backend = SimpleNamespace(
        create_task=AsyncMock(return_value=tid),
        provision=AsyncMock(),
    )
    app = api.create_app(settings())
    app.state.profile_store = profiles
    app.state.platform_store = platform
    app.state.ctf_service = backend
    monkeypatch.setattr(api, "ctf_profile", lambda _row: profile)
    monkeypatch.setattr(
        api, "_task", AsyncMock(return_value={"id": tid, "mode": "ctf", "status": "created"})
    )
    headers = {"Authorization": f"Bearer {issue_user_token(settings(), 'alice')}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/tasks",
            headers=headers,
            json={
                "mode": "ctf",
                "goal": "Observe a target",
                "budget": {"max_cost": "1", "max_minutes": 1},
                "agent_profile": "ctf",
                "model_id": "test-model",
            },
        )

    assert response.status_code == 200, response.text
    backend.create_task.assert_awaited_once()
    backend.provision.assert_awaited_once_with(tid)


async def test_ctf_http_boundaries_and_token_authority(monkeypatch):
    tid, turn = uuid4(), uuid4()
    config = settings()
    app = api.create_app(config)
    task = {
        "mode": "ctf",
        "id": tid,
        "ctf_control": {"secret": "control", "phase": "closing"},
        "agent_profile": "ctf",
        "agent_profile_version": 1,
    }
    backend = SimpleNamespace(
        authorize_member=AsyncMock(return_value={"role": "lead"}),
        authorize_reader=AsyncMock(),
        authorize_object=AsyncMock(return_value=False),
        state=AsyncMock(
            return_value={
                "task": task,
                "members": [],
                "messages": [{"body": "private"}],
                "turns": [],
            }
        ),
        post_message=AsyncMock(return_value={"status": "queued"}),
        claim_turn=AsyncMock(return_value={"id": str(turn), "agent_id": "lead", "generation": 2}),
    )
    app.state.ctf_service = backend
    app.state.profile_store = SimpleNamespace(
        get=AsyncMock(return_value={"worker_tools": {"lead": {"builtin": ["send_message"]}}})
    )
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    token = issue_agent_token(config, tid, "lead", generation=2, turn_id=turn)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/tasks/{tid}/ctf/state", headers=headers)
        assert response.status_code == 200
        assert "private" not in response.text and "secret" not in response.text
        assert response.json()["task"]["ctf_phase"] == "closing"
        assert (
            await client.get(f"/api/tasks/{uuid4()}/ctf/state", headers=headers)
        ).status_code == 403
        assert (
            await client.post(f"/api/tasks/{tid}/facts", headers=headers, json={})
        ).status_code == 409
        assert (
            await client.post(f"/api/tasks/{tid}/ctf/runtime/recover", headers=headers, json={})
        ).status_code == 403
        response = await client.post(
            f"/api/tasks/{tid}/ctf/tools/post_message",
            headers=headers,
            json={
                "actor": "system",
                "generation": 999,
                "turn_id": str(uuid4()),
                "recipient_id": "member-1",
                "message_id": str(uuid4()),
                "body": "hello",
            },
        )
        assert response.status_code == 200
        assert backend.post_message.call_args.kwargs["actor"] == "lead"
        assert backend.post_message.call_args.kwargs["generation"] == 2
        app.state.profile_store.get.return_value["worker_tools"]["lead"]["builtin"] = []
        denied = await client.post(
            f"/api/tasks/{tid}/ctf/tools/post_message", headers=headers, json={}
        )
        assert denied.status_code == 403
        assert backend.post_message.await_count == 1
        assert (
            await client.get(f"/api/evidence?uri=traces/{tid}/member-1/x.json", headers=headers)
        ).status_code == 403
        response = await client.post(
            f"/api/tasks/{tid}/ctf/runtime/claim_turn",
            headers={"Authorization": "Bearer service-test"},
            json={"agent_id": "lead", "runtime_instance": "test"},
        )
        assert response.status_code == 200 and response.json()["token"]
        legacy = issue_agent_token(config, tid, "lead")
        assert (
            await client.get(
                f"/api/tasks/{tid}/state", headers={"Authorization": f"Bearer {legacy}"}
            )
        ).status_code == 409


@pytest.mark.parametrize(
    "control",
    [
        {"completion_requirements": "完成全部目标\n保留原文 {{ task_id }}"},
        {"completion_requirements": None},
        {},
        {"completion_requirements": {"secret": "private-marker"}},
    ],
)
async def test_human_ctf_state_exposes_only_completion_requirement_text(monkeypatch, control):
    tid, turn = uuid4(), uuid4()
    config = settings()
    app = api.create_app(config)
    task = {
        "id": tid,
        "mode": "ctf",
        "domain_context": "Existing background",
        "ctf_control": {"phase": "running", "secret": "private-marker", **control},
    }
    data = {
        "task": task,
        "members": [],
        "turns": [{"private": "private-marker"}],
        "messages": [{"body": "private-marker"}],
        "sessions": [{"state": "private-marker"}],
    }
    backend = SimpleNamespace(state=AsyncMock(return_value=data), authorize_reader=AsyncMock())
    app.state.ctf_service = backend
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    user = {"Authorization": f"Bearer {issue_user_token(config, 'alice')}"}
    agent_token = issue_agent_token(config, tid, "lead", generation=1, turn_id=turn)
    agent = {"Authorization": f"Bearer {agent_token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        path = f"/api/tasks/{tid}/ctf/state"
        assert (await client.get(path)).status_code == 401
        backend.state.assert_not_awaited()
        response = await client.get(path, headers=user)
        assert response.status_code == 200
        public = response.json()
        expected = control.get("completion_requirements")
        assert public["task"]["completion_requirements"] == (
            expected if isinstance(expected, str) else None
        )
        assert public["task"]["domain_context"] == task["domain_context"]
        assert public["task"]["ctf_phase"] == "running"
        assert not {"turns", "messages", "sessions"} & public.keys()
        assert "ctf_control" not in public["task"]
        assert "private-marker" not in response.text
        agent_view = await client.get(path, headers=agent)
        assert agent_view.status_code == 200
        assert "completion_requirements" not in agent_view.json()["task"]
        assert (
            await client.get(f"/api/tasks/{uuid4()}/ctf/state", headers=agent)
        ).status_code == 403
        service_view = await client.get(path, headers={"Authorization": "Bearer service-test"})
        assert service_view.status_code == 200
        assert service_view.json()["task"]["ctf_control"] == task["ctf_control"]
    assert backend.state.await_count == 3


def test_ctf_profile_roundtrip_and_public_document():
    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    row = {
        **_content(profile),
        "name": "ctf",
        "version": 1,
        "created_by": "test",
        "created_at": datetime.now(UTC),
    }
    assert ctf_profile(row) == profile
    assert api._profile_document(row).profile == profile


def test_ctf_event_filter_preserves_cursor_and_own_messages():
    event = {
        "type": "ctf.message.posted",
        "version": 9,
        "actor": "a",
        "object_id": "message",
        "payload": {
            "sender_id": "a",
            "recipient_id": "b",
            "body": "secret",
            "claim_token": "lease",
        },
        "addressed_to": ["a", "b"],
    }
    hidden = ctf_agent_event(event, "c")
    assert hidden["version"] == 9 and hidden["type"] == "ctf.cursor"
    assert hidden["payload"] == {} and hidden["object_id"] is None
    visible = ctf_agent_event(event, "b")
    assert visible["payload"]["body"] == "secret"
    assert "claim_token" not in visible["payload"]


async def test_ctf_sse_uses_filtered_events_and_advances_hidden_cursor():
    import asyncio
    import json
    from contextlib import asynccontextmanager

    from bbx_blackboard.sse import SSEDispatcher, stream_events

    private = {
        "type": "ctf.turn.finished",
        "version": 11,
        "actor": "other",
        "object_id": "private-turn",
        "payload": {"final_answer": "private answer"},
    }
    own = {
        "type": "ctf.message.posted",
        "version": 12,
        "actor": "lead",
        "object_id": "msg",
        "payload": {"sender_id": "lead", "recipient_id": "me", "body": "work"},
    }

    class Dispatcher:
        @asynccontextmanager
        async def subscribe(self, _tid):
            yield asyncio.Queue()

    async def events(_tid, since, for_agent):
        assert since == 10 and for_agent == "me"
        return [ctf_agent_event(item, for_agent) for item in (private, own)]

    stream = stream_events(
        SimpleNamespace(events=events), cast(SSEDispatcher, Dispatcher()), uuid4(), 10, "me"
    )
    first = await anext(stream)
    assert first["id"] == "11" and first["event"] == "ctf.cursor"
    assert "private" not in first["data"]
    second = await anext(stream)
    assert json.loads(second["data"])["payload"]["body"] == "work"
    await stream.aclose()


async def test_ctf_stream_reader_rechecks_generation_before_next_delivery():
    from bbx_blackboard.ctf_api import ReaderEvents
    from fastapi import HTTPException

    identity = SimpleNamespace(agent_id="lead", generation=1)
    ctf = SimpleNamespace(authorize_reader=AsyncMock())
    board = SimpleNamespace(events=AsyncMock(return_value=[{"version": 1}]))
    reader = ReaderEvents(board, ctf, identity)
    tid = uuid4()
    assert await reader.events(tid, 0) == [{"version": 1}]
    ctf.authorize_reader.side_effect = HTTPException(409, "Stale generation")
    with pytest.raises(HTTPException):
        await reader.events(tid, 1)
    assert board.events.await_count == 1


async def test_events_http_serializes_both_ctf_events_and_legacy_events(monkeypatch):
    tid = uuid4()
    app = api.create_app(settings())
    task = {"id": tid, "mode": "ctf"}
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    app.state.ctf_service = SimpleNamespace()
    event_types = ["task.created", "ctf.member.created", "ctf.message.posted", "ctf.cursor"]
    app.state.board_service = SimpleNamespace(
        events=AsyncMock(
            return_value=[
                {
                    "task_id": tid,
                    "version": version,
                    "type": kind,
                    "actor": "system",
                    "payload": {},
                    "created_at": datetime.now(UTC),
                }
                for version, kind in enumerate(event_types, 1)
            ]
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/tasks/{tid}/events", headers={"Authorization": "Bearer service-test"}
        )
    assert response.status_code == 200
    assert [item["type"] for item in response.json()] == event_types


async def test_member_lifecycle_routes_bind_actor_and_operation(monkeypatch):
    from bbx_blackboard.auth import issue_user_token

    tid, turn, request_id = uuid4(), uuid4(), uuid4()
    config = settings()
    app = api.create_app(config)
    task = {"id": tid, "mode": "ctf", "agent_profile": "ctf", "agent_profile_version": 1}
    backend = SimpleNamespace(
        authorize_member=AsyncMock(return_value={"role": "lead"}),
        state=AsyncMock(return_value={"task": task, "members": []}),
        request_member_operation=AsyncMock(return_value={"status": "stopping"}),
        complete_member_operation=AsyncMock(return_value={"lifecycle": "stopped"}),
    )
    app.state.ctf_service = backend
    app.state.profile_store = SimpleNamespace(
        get=AsyncMock(
            return_value={
                "worker_tools": {
                    "lead": {"builtin": ["stop_teammate", "resume_teammate", "remove_teammate"]}
                }
            }
        )
    )
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    lead_token = issue_agent_token(config, tid, "lead", generation=2, turn_id=turn)
    lead = {"Authorization": f"Bearer {lead_token}"}
    user = {"Authorization": f"Bearer {issue_user_token(config, 'alice')}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/tasks/{tid}/ctf/tools/stop_teammate",
            headers=lead,
            json={
                "member_id": "member-1",
                "request_id": str(request_id),
                "actor": "system",
                "operation": "remove",
                "generation": 999,
            },
        )
        assert response.status_code == 200
        args = backend.request_member_operation.call_args.kwargs
        assert args["actor"] == "lead" and args["operation"] == "stop" and args["generation"] == 2
        response = await client.post(
            f"/api/tasks/{tid}/ctf/members/member-1/resume",
            headers=user,
            json={"request_id": str(request_id)},
        )
        assert response.status_code == 202
        assert backend.request_member_operation.call_args.kwargs["actor"] == "user"
        response = await client.post(
            f"/api/tasks/{tid}/ctf/members/member-1/remove",
            headers=lead,
            json={"request_id": str(request_id)},
        )
        assert response.status_code == 403
        response = await client.post(
            f"/api/tasks/{tid}/ctf/tools/remove_teammate",
            headers=lead,
            json={"member_id": "member-1"},
        )
        assert response.status_code == 422
        runtime_path = f"/api/tasks/{tid}/ctf/runtime/complete_member_operation"
        payload = {
            "member_id": "member-1",
            "request_id": str(request_id),
            "proof": {"boot_id": "boot", "generation": 0, "drained": False},
        }
        response = await client.post(
            runtime_path, headers={"Authorization": "Bearer service-test"}, json=payload
        )
        assert response.status_code == 422
        backend.complete_member_operation.assert_not_awaited()
        payload["proof"]["drained"] = True
        response = await client.post(
            runtime_path, headers={"Authorization": "Bearer service-test"}, json=payload
        )
        assert response.status_code == 200
        assert backend.complete_member_operation.call_args.kwargs["proof"]["generation"] == 0


async def test_board_routes_use_contracts_and_execution_authority(monkeypatch):
    tid, turn, cid, rid = uuid4(), uuid4(), uuid4(), uuid4()
    config = settings()
    app = api.create_app(config)
    task = {"id": tid, "mode": "ctf", "agent_profile": "ctf", "agent_profile_version": 1}
    backend = SimpleNamespace(
        authorize_member=AsyncMock(return_value={"role": "teammate"}),
        authorize_reader=AsyncMock(),
        state=AsyncMock(return_value={"task": task, "members": []}),
        create_challenge=AsyncMock(return_value={"id": str(cid), "owner_id": None}),
        update_challenge=AsyncMock(return_value={"id": str(cid), "owner_id": "member-1"}),
        append_record=AsyncMock(return_value={"id": str(rid)}),
        request_help=AsyncMock(return_value={"id": str(rid)}),
        list_challenges=AsyncMock(return_value={"challenges": [{"id": str(cid)}]}),
    )
    app.state.ctf_service = backend
    tools = [
        "create_challenge",
        "update_challenge",
        "append_record",
        "request_help",
        "list_challenges",
    ]
    app.state.profile_store = SimpleNamespace(
        get=AsyncMock(return_value={"worker_tools": {"teammate": {"builtin": tools}}})
    )
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    token = issue_agent_token(config, tid, "member-1", generation=2, turn_id=turn)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/tasks/{tid}/ctf/challenges",
            headers=headers,
            json={"request_id": str(rid), "title": "Problem"},
        )
        assert response.status_code == 200
        assert backend.create_challenge.call_args.kwargs["actor"] == "member-1"
        assert backend.create_challenge.call_args.kwargs["generation"] == 2
        response = await client.post(
            f"/api/tasks/{tid}/ctf/challenges/{cid}/claim",
            headers=headers,
            json={"request_id": str(rid), "expected_revision": 1},
        )
        assert response.status_code == 200
        assert backend.update_challenge.call_args.kwargs["action"] == "claim"
        response = await client.patch(
            f"/api/tasks/{tid}/ctf/challenges/{cid}",
            headers=headers,
            json={
                "request_id": str(rid),
                "expected_revision": 1,
                "action": "set_status",
                "work_status": "completed",
                "source": "platform",
                "accepted": True,
            },
        )
        assert response.status_code == 422
        response = await client.post(
            f"/api/tasks/{tid}/ctf/challenges/{cid}/records",
            headers=headers,
            json={
                "request_id": str(rid),
                "body": "notes",
                "artifact_refs": [{"uri": "evidence/forged"}],
            },
        )
        assert response.status_code == 422
        backend.append_record.assert_not_awaited()
        help_body = {
            "request_id": str(rid),
            "expected_revision": 2,
            "body": "help",
            "attempted_routes": "tried route",
            "observations_and_basis": "observed failure",
            "failure_conditions": "input shape",
            "current_blocker": "unknown parser",
            "help_needed": "review parser",
            "no_artifacts_reason": "No script was needed",
        }
        response = await client.post(
            f"/api/tasks/{tid}/ctf/challenges/{cid}/help", headers=headers, json=help_body
        )
        assert response.status_code == 200
        assert backend.request_help.call_args.kwargs["challenge_id"] == str(cid)
        tools.remove("create_challenge")
        response = await client.post(
            f"/api/tasks/{tid}/ctf/challenges",
            headers=headers,
            json={"request_id": str(uuid4()), "title": "Blocked"},
        )
        assert response.status_code == 403
        assert backend.create_challenge.await_count == 1
        response = await client.get(f"/api/tasks/{tid}/ctf/challenges?limit=0", headers=headers)
        assert response.status_code == 422


async def test_registered_shared_artifacts_are_readable_but_unknown_objects_are_not(monkeypatch):
    tid, turn, aid = uuid4(), uuid4(), uuid4()
    config = settings()
    app = api.create_app(config)
    uri = f"evidence/{tid}/member-2/{aid}-script.py"

    async def content(_uri):
        yield b"script"

    app.state.objects = SimpleNamespace(exists=AsyncMock(return_value=True), stream=content)
    task = {"id": tid, "mode": "ctf"}
    backend = SimpleNamespace(
        authorize_reader=AsyncMock(),
        authorize_member=AsyncMock(),
        authorize_object=AsyncMock(side_effect=lambda _tid, _agent, key: key == uri),
        list_artifacts=AsyncMock(return_value={"artifacts": [{"id": str(aid), "uri": uri}]}),
    )
    app.state.ctf_service = backend
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    token = issue_agent_token(config, tid, "member-1", generation=2, turn_id=turn)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/api/evidence", params={"uri": uri}, headers=headers)
        assert response.status_code == 200 and response.content == b"script"
        response = await client.get(
            "/api/evidence",
            params={"uri": f"evidence/{tid}/member-2/unregistered.py"},
            headers=headers,
        )
        assert response.status_code == 403
        response = await client.get(
            "/api/evidence", params={"uri": f"traces/{tid}/member-2/private.json"}, headers=headers
        )
        assert response.status_code == 403
        response = await client.get(
            "/api/evidence", params={"uri": f"evidence/{uuid4()}/member-2/x.py"}, headers=headers
        )
        assert response.status_code == 403
        response = await client.get(f"/api/tasks/{tid}/ctf/artifacts/{aid}", headers=headers)
        assert response.status_code == 200
        response = await client.get(f"/api/tasks/{tid}/ctf/artifacts/{uuid4()}", headers=headers)
        assert response.status_code == 404
        response = await client.post(
            f"/api/tasks/{tid}/ctf/runtime/register_artifact", headers=headers, json={}
        )
        assert response.status_code == 403


async def test_verification_endpoints_derive_source_from_authenticated_identity(monkeypatch):
    tid, turn, cid = uuid4(), uuid4(), uuid4()
    config = settings()
    app = api.create_app(config)
    task = {"mode": "ctf", "id": tid, "agent_profile": "ctf", "agent_profile_version": 1}
    backend = SimpleNamespace(
        authorize_member=AsyncMock(return_value={"role": "teammate"}),
        authorize_reader=AsyncMock(),
        state=AsyncMock(return_value={"task": task, "members": []}),
        record_candidate=AsyncMock(return_value={"verification": {"source": "agent"}}),
        manual_verification=AsyncMock(return_value={"verification": {"source": "user"}}),
    )
    app.state.ctf_service = backend
    app.state.profile_store = SimpleNamespace(
        get=AsyncMock(
            return_value={"worker_tools": {"teammate": {"builtin": ["record_candidate"]}}}
        )
    )
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    token = issue_agent_token(config, tid, "member-1", generation=1, turn_id=turn)
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "request_id": str(uuid4()),
        "expected_revision": 1,
        "status": "candidate",
        "summary": "A claim",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                f"/api/tasks/{tid}/ctf/tools/record_candidate",
                headers=headers,
                json={**body, "challenge_id": str(cid), "source": "platform"},
            )
        ).status_code == 422
        assert (
            await client.post(
                f"/api/tasks/{tid}/ctf/tools/record_candidate",
                headers=headers,
                json={**body, "challenge_id": str(cid), "status": "accepted"},
            )
        ).status_code == 422
        assert (
            await client.post(
                f"/api/tasks/{tid}/ctf/tools/record_candidate",
                headers=headers,
                json={**body, "challenge_id": str(cid)},
            )
        ).status_code == 200
        assert backend.record_candidate.call_args.kwargs["actor"] == "member-1"
        url = f"/api/tasks/{tid}/ctf/challenges/{cid}/verification/manual"
        assert (
            await client.post(url, headers=headers, json={**body, "status": "accepted"})
        ).status_code == 403
        assert (
            await client.post(
                url,
                headers={"Authorization": "Bearer service-test"},
                json={**body, "status": "accepted"},
            )
        ).status_code == 403
        assert (
            await client.post(
                url,
                headers={"Authorization": f"Bearer {issue_user_token(config, 'alice')}"},
                json={**body, "status": "accepted"},
            )
        ).status_code == 200
        assert backend.manual_verification.call_args.kwargs["user_id"] == "alice"


def test_platform_purpose_metadata_roundtrips_in_existing_profile_columns():
    from bbx_contracts.ctf import CtfAgentProfile

    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    data = profile.model_dump(mode="json")
    data["worker_tools"]["lead"]["mcp_servers"] = [
        {"name": "fake", "version": 1, "allowed_tools": ["start"]}
    ]
    data["platform_tools"] = [
        {
            "server_name": "fake",
            "server_version": 1,
            "tool_name": "start",
            "purpose": "management",
            "result_adapter": "fake_ctf_v1",
        }
    ]
    profile = CtfAgentProfile.model_validate(data)
    encoded = _content(profile)
    assert "platform_tools" not in encoded
    assert encoded["prompts"]["platform_tools"][0]["tool_name"] == "start"
    assert ctf_profile(encoded).platform_tools == profile.platform_tools
