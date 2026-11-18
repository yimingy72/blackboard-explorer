"""Versioned board permissions, protected values and real PostgreSQL transactions."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import Principal, issue_agent_token, issue_user_token
from bbx_blackboard.board_events import emit
from bbx_blackboard.flag_board import FlagBoardService, verification_round
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.store import schema as s
from bbx_blackboard.task_board import TaskBoardService
from bbx_contracts.challenge_board import BOARD_CONTRACT
from bbx_contracts.ctf import CtfAgentProfile, CtfTaskCreate, load_ctf_profile
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import insert, select, update
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url

pytestmark = pytest.mark.integration
SOURCE = {"server_name": "flag-platform", "server_version": 1, "tool_name": "submit"}


class Objects:
    def __init__(self):
        self.values = {}

    async def exists(self, uri):
        return uri in self.values

    async def get(self, uri):
        return self.values[uri]

    async def put(self, uri, data, **kwargs):
        self.values[uri] = data

    async def stream(self, uri):
        yield self.values[uri]

    async def list(self, prefix):
        return [key for key in self.values if key.startswith(prefix)]

    async def remove(self, key):
        self.values.pop(key, None)


def identity(tid, aid, generation, turn_id):
    return Principal(
        "agent",
        aid,
        task_id=tid,
        agent_id=aid,
        mode="ctf",
        generation=generation,
        turn_id=UUID(str(turn_id)),
    )


def headers(who):
    return {
        "Authorization": "Bearer "
        + issue_agent_token(
            settings(), who.task_id, who.agent_id, generation=who.generation, turn_id=who.turn_id
        )
    }


def client(ctf):
    app = create_app(settings(), engine=ctf.repo.engine, objects=ctf.objects)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    )


async def task(ctf):
    ctf.objects = Objects()
    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    content = profile.model_dump(mode="json")
    content["worker_tools"]["lead"]["mcp_servers"] = [
        {"name": SOURCE["server_name"], "version": 1, "allowed_tools": ["submit"]}
    ]
    content["platform_tools"] = [
        {**SOURCE, "purpose": "submit", "result_adapter": "flag_receipt_v1", "read_only": False}
    ]
    name = "board-" + uuid4().hex
    stored = await ProfileStore(ctf.repo.engine).create(
        name, CtfAgentProfile.model_validate(content), "test"
    )
    async with ctf.repo.engine.begin() as conn:
        known = (
            await conn.execute(
                select(s.platform_configs.c.name).where(
                    s.platform_configs.c.kind == "mcp-servers",
                    s.platform_configs.c.name == SOURCE["server_name"],
                    s.platform_configs.c.version == 1,
                )
            )
        ).first()
        if known is None:
            await conn.execute(
                insert(s.platform_configs).values(
                    kind="mcp-servers",
                    name=SOURCE["server_name"],
                    version=1,
                    label="Synthetic source",
                    config={"url": "http://fake.invalid/mcp"},
                    credential_source="none",
                    enabled=True,
                    deleted=False,
                    created_by="test",
                )
            )
    return await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Synthetic two-Flag task",
            "budget": {"max_cost": "10", "max_minutes": 60},
            "ctf_options": {"max_teammates": 2},
            "agent_profile": name,
        },
        profile_version=stored["version"],
        business_contract=BOARD_CONTRACT,
    )


async def install_fake_runner(ctf, tid):
    """Emulate P3's trusted binding, never the forbidden legacy claim entry."""
    instant = datetime.now(UTC)
    async with ctf.repo.engine.begin() as conn:
        original = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                status="running",
                started_at=instant,
                active_since=instant,
                run_started=True,
                ctf_control={
                    **original["ctf_control"],
                    "phase": "running",
                    "network_status": "offline",
                    "environment_revision": "observed-env-1",
                },
            )
        )
    people = ["lead"]
    for label in ("Owner", "Peer"):
        people.append((await ctf.create_member(tid, "user", label, str(uuid4())))["id"])
    actors = []
    async with ctf.repo.engine.begin() as conn:
        for aid in people:
            turn_id = str(uuid4())
            await conn.execute(
                insert(s.ctf_turns).values(
                    task_id=tid,
                    id=turn_id,
                    agent_id=aid,
                    generation=1,
                    runtime_instance="fake-v1-binding",
                    status="running",
                    purpose="execution",
                    assignment_message_ids=[],
                    explicit_reply_ids=[],
                    checkpoint_revision=0,
                    usage={},
                    usage_requests=[],
                    started_at=instant,
                )
            )
            await conn.execute(
                update(s.ctf_members)
                .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == aid)
                .values(generation=1, current_turn_id=turn_id, run_state="running")
            )
            actors.append(identity(tid, aid, 1, turn_id))
    return actors


async def bind_owner(ctf, tid, item, owner):
    async with ctf.repo.engine.begin() as conn:
        item = {
            **item,
            "owner_id": owner.agent_id,
            "work_status": "in_progress",
            "revision": item["revision"] + 1,
        }
        await ctf._board_event(
            conn, tid, "service", "ctf.challenge.updated", str(uuid4()), "fake-binding", item
        )
    return item


async def setup(ctf):
    tid = await task(ctf)
    lead, owner, peer = await install_fake_runner(ctf, tid)
    board = TaskBoardService(ctf)
    item = await board.create_challenge(
        tid, lead, {"request_id": str(uuid4()), "title": "Synthetic challenge"}
    )
    item = await bind_owner(ctf, tid, item, owner)
    payload = b"synthetic evidence"
    uri = f"evidence/{tid}/{owner.agent_id}/proof.txt"
    ctf.objects.values[uri] = payload
    artifact = await ctf.register_artifact(
        tid,
        owner.agent_id,
        owner.turn_id,
        owner.generation,
        str(uuid4()),
        "/workspace/shared/proof.txt",
        uri,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "proof.txt",
    )
    flags = FlagBoardService(ctf, settings().agent_token_secret.get_secret_value())
    return tid, lead, owner, peer, item, artifact, board, flags


def candidate(item, artifact, value="flag{synthetic}"):
    return {
        "request_id": str(uuid4()),
        "expected_revision": item["revision"],
        "value": value,
        "evidence_refs": [artifact["id"]],
    }


def submission(item, value):
    return {
        "request_id": str(uuid4()),
        "expected_revision": item["revision"],
        "candidate_id": value["id"],
        "source": SOURCE,
    }


async def save_receipt(ctf, flags, tid, submitted, result):
    async with flags.repo.engine.connect() as conn:
        current = await flags._submission(conn, tid, submitted["id"])
    await flags.capture(
        tid,
        Principal("service", "scheduler"),
        {
            "request_id": str(uuid4()),
            "platform_call_id": current["platform_call_id"],
            "source": current["source"],
            "request_digest": current["request_digest"],
            "response_text": json.dumps(result),
        },
    )
    raw = await flags._seal_verified_receipt(tid, submitted["id"])
    uri = f"evidence/{tid}/platform/{uuid4().hex}.json"
    ctf.objects.values[uri] = raw
    return {
        "request_id": str(uuid4()),
        "response_uri": uri,
        "response_sha256": hashlib.sha256(raw).hexdigest(),
    }


async def test_atomic_contract_legacy_guard_lead_creation_and_announcements(ctf):
    tid = await task(ctf)
    before = await ctf.state(tid)
    assert before["task"]["ctf_control"]["business_contract"] == BOARD_CONTRACT
    await ctf.provision(tid)
    for method in (ctf.start, ctf.recover, ctf.fail_start):
        with pytest.raises(HTTPException):
            await method(tid)
    assert await ctf.claim_turn(tid, "lead", "legacy-asserts-v1") is None
    assert await ctf.claim_review_turn(tid, "lead", "legacy-review") is None
    assert not (await ctf.state(tid))["turns"]
    with pytest.raises(ValidationError):
        CtfTaskCreate.model_validate(
            {
                "mode": "ctf",
                "goal": "No body contract",
                "budget": {"max_cost": "1", "max_minutes": 1},
                "business_contract": BOARD_CONTRACT,
            }
        )
    async with client(ctf) as http:
        denied = await http.post(
            f"/api/tasks/{tid}/ctf/runtime/start",
            headers={"Authorization": "Bearer service-test"},
            json={"runner_contract": BOARD_CONTRACT},
        )
        assert denied.status_code == 422
    lead, owner, _ = await install_fake_runner(ctf, tid)
    base = f"/api/tasks/{tid}/ctf/knowledge"
    async with client(ctf) as http:
        assert (
            await http.post(
                base + "/challenges",
                headers=headers(owner),
                json={"request_id": str(uuid4()), "title": "Forbidden"},
            )
        ).status_code == 403
        assert (
            await http.post(
                f"/api/tasks/{tid}/ctf/tools/create_challenge",
                headers=headers(owner),
                json={"request_id": str(uuid4()), "title": "No legacy bypass"},
            )
        ).status_code == 403
        assert (
            await http.post(
                base + "/tools/board_create_challenge",
                headers=headers(owner),
                json={"request_id": str(uuid4()), "title": "Forbidden"},
            )
        ).status_code == 403
        created = await http.post(
            base + "/challenges",
            headers=headers(lead),
            json={
                "request_id": str(uuid4()),
                "title": "Lead only",
                "description": "Other challenge private description",
                "connection": "https://other-challenge.invalid/private",
            },
        )
        assert created.status_code == 200
        body = {
            "request_id": str(uuid4()),
            "expected_revision": 0,
            "body": "Network is connected (an announcement is only text).",
        }
        published = await http.post(base + "/announcements", headers=headers(lead), json=body)
        assert published.status_code == 200, published.text
        assert published.json()["environment_revision"] == "observed-env-1"
        assert (
            await http.post(base + "/announcements", headers=headers(lead), json=body)
        ).json() == published.json()
        assert (
            await http.post(
                base + "/announcements",
                headers=headers(owner),
                json={**body, "request_id": str(uuid4())},
            )
        ).status_code == 403
        assert (
            await http.post(
                base + "/announcements",
                headers=headers(lead),
                json={**body, "request_id": str(uuid4())},
            )
        ).status_code == 409
        assert (
            await http.post(
                base + "/announcements", headers=headers(lead), json={**body, "network_ready": True}
            )
        ).status_code == 422
        assert len((await http.get(base + "/announcements", headers=headers(owner))).json()) == 1
        board = TaskBoardService(ctf)
        owned = await board.create_challenge(
            tid, lead, {"request_id": str(uuid4()), "title": "Worker's assigned challenge"}
        )
        await bind_owner(ctf, tid, owned, owner)
        unscoped_id = str(uuid4())
        async with ctf.repo.engine.begin() as conn:
            await emit(
                ctf.repo,
                conn,
                tid,
                "lead",
                "board.request.linked",
                uuid4(),
                "legacy-unscoped-link",
                {"id": unscoped_id, "description": "Unscoped private result"},
            )
            original_events = list(
                (await conn.execute(select(s.events).where(s.events.c.task_id == tid))).mappings()
            )
        response = await http.get(f"/api/tasks/{tid}/events", headers=headers(owner))
        assert response.status_code == 200, response.text
        visible = {event["version"]: event for event in response.json()}
        assert set(visible) == {event["version"] for event in original_events}
        other_events = [
            event
            for event in original_events
            if event["object_id"] in {created.json()["id"], unscoped_id}
        ]
        assert {event["type"] for event in other_events} == {
            "ctf.challenge.created",
            "board.request.linked",
        }
        for event in other_events:
            assert visible[event["version"]]["type"] == "ctf.cursor"
            assert visible[event["version"]]["payload"] == {}
        assert any(
            event["type"] == "ctf.challenge.created" and event["object_id"] == owned["id"]
            for event in visible.values()
        )
        assert any(event["type"] == "board.announcement.published" for event in visible.values())
        assert "Other challenge private description" not in response.text
        assert "other-challenge.invalid" not in response.text
        assert "Unscoped private result" not in response.text
    assert (await ctf.state(tid))["task"]["ctf_control"]["network_status"] == "offline"


async def test_protected_candidates_exact_dedupe_and_atomic_lead_notice(ctf, monkeypatch):
    tid, lead, owner, peer, item, artifact, _, flags = await setup(ctf)
    cid = UUID(item["id"])
    secret = "  flag{private-\u00e9}\n"
    payload = candidate(item, artifact, secret)
    values = await asyncio.gather(
        *(
            flags.candidate(tid, cid, owner, {**payload, "request_id": str(uuid4())})
            for _ in range(3)
        )
    )
    assert len({value["id"] for value in values}) == 1
    first = values[0]
    assert (await flags.candidate(tid, cid, owner, candidate(item, artifact, secret.strip())))[
        "id"
    ] != first["id"]
    async with ctf.repo.engine.connect() as conn:
        protected = (
            (
                await conn.execute(
                    select(s.flag_candidates).where(
                        s.flag_candidates.c.task_id == tid, s.flag_candidates.c.id == first["id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        assert secret not in protected["value_ciphertext"] and "value" not in protected["data"]
        notices = list(
            (
                await conn.execute(
                    select(s.ctf_messages.c.body).where(
                        s.ctf_messages.c.task_id == tid,
                        s.ctf_messages.c.notification_purpose == "flag_candidate",
                    )
                )
            ).scalars()
        )
        assert len(notices) == 2 and all(secret not in message for message in notices)
    base = f"/api/tasks/{tid}/ctf/knowledge/{cid}/flags"
    async with client(ctf) as http:
        assert (
            await http.get(base + f"/candidates/{first['id']}/value", headers=headers(owner))
        ).json()["value"] == secret
        assert (
            await http.get(base + f"/candidates/{first['id']}/value", headers=headers(lead))
        ).json()["value"] == secret
        user = {"Authorization": "Bearer " + issue_user_token(settings(), "alice")}
        assert (await http.get(base + f"/candidates/{first['id']}/value", headers=user)).json()[
            "value"
        ] == secret
        assert (
            await http.get(base + f"/candidates/{first['id']}/value", headers=headers(peer))
        ).status_code == 403
        assert (
            await http.get(
                base + f"/candidates/{first['id']}/value",
                headers={"Authorization": "Bearer service-test"},
            )
        ).status_code == 403
        assert secret not in json.dumps((await http.get(base, headers=headers(owner))).json())
        events = (await http.get(f"/api/tasks/{tid}/events", headers=headers(lead))).json()
        assert secret not in json.dumps(events) and protected["value_ciphertext"] not in json.dumps(
            events
        )
        assert (
            await http.post(
                base + "/candidates",
                headers=headers(owner),
                json={**payload, "request_id": str(uuid4()), "verification_round": "caller-chosen"},
            )
        ).status_code == 422
    original = ctf._message
    monkeypatch.setattr(
        ctf, "_message", AsyncMock(side_effect=RuntimeError("Injected notification failure"))
    )
    with pytest.raises(RuntimeError, match="Injected"):
        await flags.candidate(tid, cid, owner, candidate(item, artifact, "flag{rollback}"))
    monkeypatch.setattr(ctf, "_message", original)
    assert len((await flags.board(tid, cid, lead))["candidates"]) == 2
    old_round = verification_round((await ctf.state(tid))["task"], item)
    assert (
        verification_round(
            (await ctf.state(tid))["task"], {**item, "description": "Caller text changed"}
        )
        == old_round
    )


async def test_single_dispatch_trusted_multiflag_unknown_and_duplicate_scores(ctf):
    tid, lead, owner, _, item, artifact, board, flags = await setup(ctf)
    cid, system = UUID(item["id"]), Principal("service", "scheduler")
    first = await flags.candidate(tid, cid, owner, candidate(item, artifact, "flag{one}"))
    requests = [submission(item, first), submission(item, first)]
    prepared = await asyncio.gather(*(flags.prepare(tid, cid, lead, body) for body in requests))
    assert (
        prepared[0]["id"] == prepared[1]["id"]
        and prepared[0]["operation_id"] == prepared[1]["operation_id"]
    )
    sent = await asyncio.gather(
        *(
            flags.dispatch(
                tid,
                prepared[0]["id"],
                system,
                {"request_id": str(uuid4()), "platform_call_id": call_id},
            )
            for call_id in ("native:call/one", "native:call/two")
        )
    )
    assert sum(value["send_allowed"] for value in sent) == 1
    active = next(value for value in sent if value["send_allowed"])["submission"]
    assert active["platform_call_id"] in {"native:call/one", "native:call/two"}
    await flags.unknown(
        tid,
        active["id"],
        system,
        {"request_id": str(uuid4()), "platform_call_id": active["platform_call_id"]},
    )
    assert (await flags.prepare(tid, cid, lead, submission(item, first)))["id"] == active["id"]
    assert not (
        await flags.dispatch(
            tid,
            active["id"],
            system,
            {"request_id": str(uuid4()), "platform_call_id": "must-not-retry"},
        )
    )["send_allowed"]
    second = await flags.candidate(tid, cid, owner, candidate(item, artifact, "flag{two}"))
    another = await flags.prepare(tid, cid, lead, submission(item, second))
    queued = await flags.dispatch(
        tid,
        another["id"],
        system,
        {"request_id": str(uuid4()), "platform_call_id": "wait-for-first"},
    )
    assert not queued["send_allowed"] and queued["submission"]["status"] == "prepared"
    forged = b'{"status":"accepted","correct_count":2,"total_count":2}'
    uri = f"evidence/{tid}/platform/forged.json"
    ctf.objects.values[uri] = forged
    assert (
        await flags.receipt(
            tid,
            active["id"],
            system,
            {
                "request_id": str(uuid4()),
                "response_uri": uri,
                "response_sha256": hashlib.sha256(forged).hexdigest(),
            },
        )
    )["status"] == "unknown"
    with pytest.raises(HTTPException, match="immutable transport capture"):
        await flags._seal_verified_receipt(tid, active["id"])
    good = await save_receipt(
        ctf,
        flags,
        tid,
        active,
        {"status": "accepted", "correct_count": 1, "total_count": 2, "score": "50"},
    )
    await flags.receipt(tid, active["id"], system, good)
    await flags.receipt(tid, active["id"], system, good)
    state = await flags.board(tid, cid, lead)
    assert state["progress"] == {"correct_count": 1, "total_count": 2, "score": "50"}
    assert (await ctf.get_challenge(tid, str(cid)))["work_status"] == "in_progress"
    await flags.dispatch(
        tid,
        another["id"],
        system,
        {"request_id": str(uuid4()), "platform_call_id": "native-call-3"},
    )
    with pytest.raises(HTTPException, match="immutable platform dispatch"):
        await flags.capture(
            tid,
            system,
            {
                "request_id": str(uuid4()),
                "platform_call_id": active["platform_call_id"],
                "source": another["source"],
                "request_digest": another["request_digest"],
                "response_text": json.dumps(
                    {"status": "duplicate", "correct_count": 2, "total_count": 2, "score": "100"}
                ),
            },
        )
    wrong = await save_receipt(
        ctf,
        flags,
        tid,
        another,
        {"status": "duplicate", "correct_count": 2, "total_count": 2, "score": "100"},
    )
    with pytest.raises(HTTPException):
        await flags.receipt(tid, active["id"], system, wrong)
    await flags.receipt(tid, another["id"], system, wrong)
    assert (await flags.board(tid, cid, lead))["progress"]["score"] == "100"
    assert (await ctf.get_challenge(tid, str(cid)))["work_status"] == "completed"
    stable = await flags.board(tid, cid, lead)
    await ctf.repo.replay(tid)
    assert await flags.board(tid, cid, lead) == stable
    for raw in ("<html>upstream unavailable</html>", '{"status":"accepted","correct_count":', ""):
        malformed = await board.create_challenge(
            tid, lead, {"request_id": str(uuid4()), "title": "Malformed upstream result"}
        )
        malformed = await bind_owner(ctf, tid, malformed, owner)
        malformed_cid = UUID(malformed["id"])
        recorded = await flags.candidate(tid, malformed_cid, owner, candidate(malformed, artifact))
        pending = await flags.prepare(tid, malformed_cid, lead, submission(malformed, recorded))
        call_id = "malformed:" + str(uuid4())
        assert (
            await flags.dispatch(
                tid,
                pending["id"],
                system,
                {"request_id": str(uuid4()), "platform_call_id": call_id},
            )
        )["send_allowed"]
        captured = await flags.capture(
            tid,
            system,
            {
                "request_id": str(uuid4()),
                "platform_call_id": call_id,
                "source": pending["source"],
                "request_digest": pending["request_digest"],
                "response_text": raw,
            },
        )
        assert ctf.objects.values[captured["response_uri"]] == raw.encode()
        assert captured["response_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
        with pytest.raises(HTTPException) as invalid:
            await flags._seal_verified_receipt(tid, pending["id"])
        assert invalid.value.status_code == 422
        async with ctf.repo.engine.connect() as conn:
            unknown = await flags._submission(conn, tid, pending["id"])
            assert await flags._capture_record(conn, tid, call_id) == captured
        assert unknown["status"] == "unknown" and unknown["result"] is None
        assert not unknown["progress_applied"]
        assert (await ctf.get_challenge(tid, malformed["id"]))["work_status"] == "in_progress"
        assert ctf.objects.values[captured["response_uri"]] == raw.encode()
    async with client(ctf) as http:
        path = f"/api/tasks/{tid}/ctf/knowledge/{cid}/flags/submissions"
        assert (
            await http.post(path, headers=headers(owner), json=submission(item, second))
        ).status_code == 403
        assert (
            await http.post(
                path,
                headers=headers(lead),
                json={**submission(item, second), "accepted": True, "score": "999"},
            )
        ).status_code == 422


async def test_stop_wins_dispatch_race_and_late_receipt_cannot_restore_cancelled_work(
    ctf, monkeypatch
):
    tid, lead, owner, _, item, artifact, _, flags = await setup(ctf)
    cid, system = UUID(item["id"]), Principal("service", "scheduler")
    first = await flags.candidate(tid, cid, owner, candidate(item, artifact, "flag{sent}"))
    sent = await flags.prepare(tid, cid, lead, submission(item, first))
    await flags.dispatch(
        tid,
        sent["id"],
        system,
        {"request_id": str(uuid4()), "platform_call_id": "original-call:not-a-uuid"},
    )
    second = await flags.candidate(tid, cid, owner, candidate(item, artifact, "flag{prepared}"))
    prepared = await flags.prepare(tid, cid, lead, submission(item, second))
    entered, release = asyncio.Event(), asyncio.Event()
    original = ctf._emit

    async def pause_stop(conn, task_id, kind, actor, payload):
        if kind == "conclusion.requested":
            entered.set()
            await release.wait()
        return await original(conn, task_id, kind, actor, payload)

    monkeypatch.setattr(ctf, "_emit", pause_stop)
    stopping = asyncio.create_task(
        ctf.request_finish(
            tid,
            "user",
            str(uuid4()),
            {"end_reason": "user_stop", "summary": "Stop competing with dispatch"},
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    competing = asyncio.create_task(
        flags.dispatch(
            tid,
            prepared["id"],
            system,
            {"request_id": str(uuid4()), "platform_call_id": "not-sent"},
        )
    )
    try:
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(stopping, 5)
        answer = await asyncio.wait_for(competing, 5)
    finally:
        release.set()
        await asyncio.gather(stopping, competing, return_exceptions=True)
    assert (
        not answer["send_allowed"]
        and answer["submission"]["status"] == "cancelled"
        and "payload" not in answer
    )
    monkeypatch.setattr(ctf, "_emit", original)
    for turn in (await ctf.state(tid))["turns"]:
        if turn["status"] == "running":
            await ctf.finish_turn(tid, turn["agent_id"], turn["id"], turn["generation"], "stopped")
    await ctf.finalize_close(tid, True)
    async with ctf.repo.engine.begin() as conn:
        current = (
            await conn.execute(
                select(s.ctf_challenges.c.data).where(
                    s.ctf_challenges.c.task_id == tid, s.ctf_challenges.c.id == str(cid)
                )
            )
        ).scalar_one()
        cancelled = {**current, "work_status": "cancelled", "revision": current["revision"] + 1}
        await ctf._board_event(
            conn, tid, "service", "ctf.challenge.updated", str(uuid4()), "cancelled", cancelled
        )
    before = await ctf.state(tid)
    good = await save_receipt(
        ctf,
        flags,
        tid,
        sent,
        {"status": "accepted", "correct_count": 2, "total_count": 2, "score": "100"},
    )
    recorded = await flags.receipt(tid, sent["id"], system, good)
    assert (
        recorded["status"] == "accepted" and recorded["late"] and not recorded["progress_applied"]
    )
    after = await ctf.state(tid)
    assert (
        after["task"]["status"] == "stopped" and after["task"]["ctf_control"]["phase"] == "closed"
    )
    assert after["members"] == before["members"]
    assert (await ctf.get_challenge(tid, str(cid)))["work_status"] == "cancelled"
    assert len(
        [value for value in after["messages"] if value["notification_purpose"] == "flag_result"]
    ) == len(
        [value for value in before["messages"] if value["notification_purpose"] == "flag_result"]
    )
    archive = await ctf.sessions.export_archive(tid)
    assert len(archive["challenge_board"]["flag_candidates"]) == 2
    assert "flag{sent}" not in json.dumps(archive)
    assert any(
        row["data"]["upstream_response_uri"] == recorded["upstream_response_uri"]
        for row in archive["challenge_board"]["flag_submissions"]
    )
    await ctf.sessions.request_delete(tid)
    await ctf.sessions.purge(tid, ctf.objects)
    async with ctf.repo.engine.connect() as conn:
        for table in (s.flag_candidates, s.flag_submissions, s.board_announcements):
            assert (
                await conn.execute(select(table.c.id).where(table.c.task_id == tid))
            ).first() is None
