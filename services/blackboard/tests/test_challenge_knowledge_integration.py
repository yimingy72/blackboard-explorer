"""Knowledge authorization and event transactions against isolated PostgreSQL."""

import asyncio
import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import Principal, issue_agent_token, issue_user_token
from bbx_blackboard.challenge_board import ChallengeBoardService
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.service import BoardService
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import board_has_content
from fastapi import HTTPException
from sqlalchemy import func, select, update
from test_ctf_api import settings
from test_ctf_integration import board_team
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url

pytestmark = pytest.mark.integration


class Materials:
    def __init__(self):
        self.values = {}

    async def get(self, uri):
        return self.values[uri]

    async def exists(self, uri):
        return uri in self.values


def principal(tid, actor):
    return Principal(
        "agent",
        actor["actor"],
        task_id=tid,
        agent_id=actor["actor"],
        mode="ctf",
        generation=actor["generation"],
        turn_id=UUID(actor["turn_id"]),
    )


def headers(identity):
    token = issue_agent_token(
        settings(),
        identity.task_id,
        identity.agent_id,
        generation=identity.generation,
        turn_id=identity.turn_id,
    )
    return {"Authorization": f"Bearer {token}"}


async def challenge(ctf, tid, owner, title="Synthetic knowledge"):
    item = await ctf.create_challenge(tid, "user", str(uuid4()), title)
    return await ctf.update_challenge(
        tid,
        "user",
        item["id"],
        str(uuid4()),
        item["revision"],
        "assign",
        owner_id=owner["actor"],
    )


async def material(ctf, tid, actor):
    data = b"Synthetic captured evidence"
    uri = f"evidence/{tid}/{actor['actor']}/{uuid4().hex}-proof.txt"
    ctf.objects.values[uri] = data
    return await ctf.register_artifact(
        tid,
        actor["actor"],
        actor["turn_id"],
        actor["generation"],
        str(uuid4()),
        "/workspace/shared/proof.txt",
        uri,
        hashlib.sha256(data).hexdigest(),
        len(data),
        "proof.txt",
    )


def fact(item, artifact, statement="Synthetic observation", **extra):
    return {
        "request_id": str(uuid4()),
        "expected_revision": item["revision"],
        "kind": "observation",
        "statement": statement,
        "evidence": [
            {
                "type": "text",
                "summary": "Captured synthetic evidence",
                **{key: artifact[key] for key in ("uri", "path", "size", "sha256")},
            }
        ],
        **extra,
    }


async def setup(ctf):
    ctf.objects = Materials()
    tid, owner, other = await board_team(ctf)
    item = await challenge(ctf, tid, owner)
    artifact = await material(ctf, tid, owner)
    return tid, owner, other, item, artifact, ChallengeBoardService(ctf)


def client(ctf):
    app = create_app(settings(), engine=ctf.repo.engine, objects=ctf.objects)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    )


async def test_full_http_knowledge_scope_and_untrusted_writer_fields(ctf):
    tid, owner, other, item, artifact, knowledge = await setup(ctf)
    own = principal(tid, owner)
    foreign = principal(tid, other)
    path = f"/api/tasks/{tid}/ctf/knowledge/{item['id']}"
    async with client(ctf) as http:
        payload = fact(item, artifact)
        response = await http.post(
            path + "/facts", headers={**headers(own), "X-Agent-Id": other["actor"]}, json=payload
        )
        assert response.status_code == 200, response.text
        posted = response.json()
        assert posted["object"]["author"] == owner["actor"]
        assert posted["object"]["assignment_ref"]["generation"] == own.generation
        assert posted["object"]["challenge_id"] == item["id"]
        assert posted["challenge_revision"] == item["revision"]
        direction = await http.post(
            path + "/intents",
            headers=headers(own),
            json={
                "request_id": str(uuid4()),
                "expected_revision": item["revision"],
                "statement": "Verify the synthetic result",
                "based_on": [posted["id"]],
                "expected": "A matching observation",
                "method": "Inspect captured evidence",
            },
        )
        assert direction.status_code == 200, direction.text
        assert direction.json()["object"]["holder"] is None
        assert direction.json()["object"]["relates_to"] == []
        assert (await http.get(path, headers=headers(own))).json()["facts"][posted["id"]][
            "statement"
        ] == payload["statement"]
        assert (
            await http.get(
                path + f"/objects/{direction.json()['id']}?depth=1", headers=headers(own)
            )
        ).json()["related"][posted["id"]]["author"] == own.agent_id
        assert len((await http.get(path + "/search?q=Synthetic", headers=headers(own))).json()) == 2
        snapshot = await http.get(path + "/snapshot", headers=headers(own))
        assert snapshot.status_code == 200 and posted["id"] in snapshot.text
        assert (
            await http.post(path + "/facts", headers=headers(foreign), json=fact(item, artifact))
        ).status_code == 403
        assert (await http.get(path, headers=headers(foreign))).status_code == 403
        assert (
            await http.post(
                path + "/facts",
                headers=headers(own),
                json={**fact(item, artifact), "agent_id": other["actor"]},
            )
        ).status_code == 422
        assert (
            await http.post(
                path + "/intents",
                headers=headers(own),
                json={
                    "request_id": str(uuid4()),
                    "expected_revision": item["revision"],
                    "statement": "No independent claim",
                    "based_on": [posted["id"]],
                    "expected": "A result",
                    "method": "Inspect",
                    "claim": True,
                },
            )
        ).status_code == 422
        helper = replace(own, name="helper-unbound", agent_id="helper-unbound", turn_id=uuid4())
        denied = await http.post(
            path + "/facts", headers=headers(helper), json=fact(item, artifact)
        )
        assert denied.status_code == 403 and "ChildRun" in denied.text
        assert (await http.get(path, headers=headers(helper))).status_code == 403
        assert (await http.post(path + "/facts", json=fact(item, artifact))).status_code == 401
        assert (
            await http.post(
                path + "/facts",
                headers={"Authorization": "Bearer service-test"},
                json=fact(item, artifact),
            )
        ).status_code == 403
        user = {"Authorization": f"Bearer {issue_user_token(settings(), 'alice')}"}
        assert (await http.get(path, headers=user)).status_code == 200
        assert (
            await http.post(path + "/facts", headers=user, json=fact(item, artifact))
        ).status_code == 403
    async with ctf.repo.engine.connect() as conn:
        assert (
            await conn.execute(
                select(func.count()).select_from(s.agent_runs).where(s.agent_runs.c.task_id == tid)
            )
        ).scalar_one() == 0
    assert len((await knowledge.board(tid, UUID(item["id"]), own))["facts"]) == 1


async def test_cross_task_and_challenge_references_do_not_leak(ctf):
    tid, owner, other, item, artifact, knowledge = await setup(ctf)
    own, peer = principal(tid, owner), principal(tid, other)
    private = await challenge(ctf, tid, other, "Private challenge")
    private_artifact = await material(ctf, tid, other)
    await ctf.append_record(
        tid,
        **other,
        challenge_id=private["id"],
        request_id=str(uuid4()),
        body="Explicitly associated private evidence",
        artifact_ids=[private_artifact["id"]],
    )
    private_fact = await knowledge.write(
        tid,
        UUID(private["id"]),
        peer,
        fact(private, private_artifact, "Private marker"),
        kind="fact",
    )
    private_intent = await knowledge.write(
        tid,
        UUID(private["id"]),
        peer,
        {
            "request_id": str(uuid4()),
            "expected_revision": private["revision"],
            "statement": "Private direction",
            "based_on": [private_fact["id"]],
            "expected": "A result",
            "method": "Inspect private material",
        },
        kind="intent",
    )
    foreign_tid, foreign_owner, _ = await board_team(ctf)
    foreign_artifact = await material(ctf, foreign_tid, foreign_owner)
    file_id = str(uuid4())
    shared = {
        "id": file_id,
        "filename": "rules.txt",
        "path": "/workspace/shared/inputs/rules.txt",
        "uri": f"inputs/{tid}/{file_id}/rules.txt",
        "size": 5,
        "sha256": hashlib.sha256(b"rules").hexdigest(),
    }
    ctf.objects.values[shared["uri"]] = b"rules"
    async with ctf.repo.engine.begin() as conn:
        task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                initial_attachments=[shared],
                ctf_control={**task["ctf_control"], "public_input_ids": [file_id]},
            )
        )
    await knowledge.write(
        tid, UUID(private["id"]), peer, fact(private, shared, "Shared rules"), kind="fact"
    )
    shared_fact = await knowledge.write(
        tid, UUID(item["id"]), own, fact(item, shared, "Shared rules"), kind="fact"
    )
    async with client(ctf) as http:
        path = f"/api/tasks/{tid}/ctf/knowledge/{item['id']}"
        assert (
            await http.get(path + f"/objects/{private_fact['id']}?depth=1", headers=headers(own))
        ).status_code == 404
        assert (await http.get(path + "/search?q=Private", headers=headers(own))).json() == []
        assert (
            "Private marker" not in (await http.get(path + "/snapshot", headers=headers(own))).text
        )
        assert (
            await http.post(
                path + "/facts", headers=headers(own), json=fact(item, foreign_artifact)
            )
        ).status_code == 403
        assert (
            await http.post(
                path + "/facts", headers=headers(own), json=fact(item, private_artifact)
            )
        ).status_code == 403
        denied_ref = await http.post(
            path + "/facts",
            headers=headers(own),
            json=fact(item, artifact, derived_from=[private_fact["id"]], kind="inference"),
        )
        assert denied_ref.status_code == 422 and denied_ref.json()["code"] == "invalid_reference"
        denied_close = await http.post(
            path + "/facts",
            headers=headers(own),
            json=fact(
                item,
                artifact,
                resolves=private_intent["id"],
                result="confirmed",
            ),
        )
        assert (
            denied_close.status_code == 422 and denied_close.json()["code"] == "invalid_reference"
        )
        assert (
            await http.get(
                f"/api/tasks/{foreign_tid}/ctf/knowledge/{item['id']}", headers=headers(own)
            )
        ).status_code == 403
        events = (await http.get(f"/api/tasks/{tid}/events", headers=headers(own))).json()
        secret = [event for event in events if event["version"] == private_fact["board_version"]]
        assert len(secret) == 1 and secret[0]["type"] == "ctf.cursor" and secret[0]["payload"] == {}
        assert max(event["version"] for event in events) >= private_fact["board_version"]
    assert set((await knowledge.board(tid, UUID(item["id"]), own))["facts"]) == {shared_fact["id"]}


async def test_assignment_handoff_and_replay_preserve_original_authors(ctf):
    tid, owner, other, item, artifact, knowledge = await setup(ctf)
    first, successor = principal(tid, owner), principal(tid, other)
    cid = UUID(item["id"])
    initial = await knowledge.write(tid, cid, first, fact(item, artifact), kind="fact")
    direction = await knowledge.write(
        tid,
        cid,
        first,
        {
            "request_id": str(uuid4()),
            "expected_revision": item["revision"],
            "statement": "Unfinished direction",
            "based_on": [initial["id"]],
            "expected": "Verified result",
            "method": "Inspect again",
        },
        kind="intent",
    )
    handed = await ctf.update_challenge(
        tid, "user", item["id"], str(uuid4()), item["revision"], "assign", owner_id=other["actor"]
    )
    closing = fact(handed, artifact, resolves=direction["id"], result="confirmed")
    with pytest.raises(HTTPException) as denied:
        await knowledge.write(tid, cid, first, closing, kind="fact")
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as stale:
        assert successor.generation is not None
        await knowledge.write(
            tid, cid, replace(successor, generation=successor.generation - 1), closing, kind="fact"
        )
    assert stale.value.status_code == 403
    with pytest.raises(HTTPException) as stale_turn:
        await knowledge.write(tid, cid, replace(successor, turn_id=uuid4()), closing, kind="fact")
    assert stale_turn.value.status_code == 409
    complete = await knowledge.write(tid, cid, successor, closing, kind="fact")
    board = await knowledge.board(tid, cid, successor)
    assert board["intents"][direction["id"]]["holder"] is None
    assert board["intents"][direction["id"]]["author"] == owner["actor"]
    assert board["intents"][direction["id"]]["closed_by"] == complete["id"]
    assert (await ctf.get_challenge(tid, item["id"]))["work_status"] == "in_progress"
    old_events = await BoardService(ctf.repo.engine, ctf.objects).events(tid, 0, owner["actor"])
    assert (
        next(event for event in old_events if event["version"] == initial["board_version"])["type"]
        == "ctf.cursor"
    )
    await ctf.repo.replay(tid)
    assert await knowledge.board(tid, cid, successor) == board
    assert await knowledge.write(tid, cid, successor, closing, kind="fact") == complete


async def test_transaction_dedupe_stop_and_ordinary_holder_compatibility(ctf, monkeypatch):
    tid, owner, _, item, artifact, knowledge = await setup(ctf)
    own, cid = principal(tid, owner), UUID(item["id"])
    payload = fact(item, artifact)
    results = await asyncio.gather(
        *(knowledge.write(tid, cid, own, payload, kind="fact") for _ in range(3))
    )
    assert results[0] == results[1] == results[2]
    with pytest.raises(HTTPException) as conflict:
        await knowledge.write(
            tid, cid, own, {**payload, "statement": "Changed request"}, kind="fact"
        )
    assert conflict.value.status_code == 409
    notify = knowledge.repo.notify

    async def fail_notify(*args, **kwargs):
        raise RuntimeError("Injected transaction failure")

    monkeypatch.setattr(knowledge.repo, "notify", fail_notify)
    with pytest.raises(RuntimeError, match="Injected"):
        await knowledge.write(tid, cid, own, fact(item, artifact, "Rolled back"), kind="fact")
    monkeypatch.setattr(knowledge.repo, "notify", notify)
    assert len((await knowledge.board(tid, cid, own))["facts"]) == 1
    async with ctf.repo.engine.connect() as conn:
        ordinary = await knowledge.repo.load(conn, tid, include_tool_calls=False)
        assert (
            not ordinary.facts and not ordinary.intents and not await board_has_content(conn, tid)
        )
    board = BoardService(ctf.repo.engine, ctf.objects)
    normal = await board.create_task(
        {
            "goal": "Ordinary compatibility",
            "acceptance": [{"id": "A1", "desc": "Proof"}],
            "budget": {"max_cost": "10", "max_minutes": 60},
            "agent_profile": "default",
        }
    )
    await board.transition(normal, "provisioning")
    await board.transition(normal, "running")
    alice = await board.register_agent(normal, "explore")
    bob = await board.register_agent(normal, "explore")
    observation = {
        key: value
        for key, value in fact(item, artifact).items()
        if key not in {"request_id", "expected_revision"}
    }
    uri = f"evidence/{normal}/{alice}/proof.txt"
    ctf.objects.values[uri] = b"Synthetic captured evidence"
    observation["evidence"] = [{**observation["evidence"][0], "uri": uri}]
    first = await board.post_fact(normal, alice, observation)
    direction = await board.post_intent(
        normal,
        alice,
        {
            "statement": "Ordinary direction",
            "based_on": [first["id"]],
            "expected": "Proof",
            "method": "Inspect",
            "relates_to": ["A1"],
            "claim": True,
        },
    )
    with pytest.raises(RuleViolation) as holder:
        await board.post_fact(
            normal, bob, {**observation, "resolves": direction["id"], "result": "confirmed"}
        )
    assert holder.value.code == "not_holder"
    await board.post_fact(
        normal, alice, {**observation, "resolves": direction["id"], "result": "confirmed"}
    )
    normal_state = await board.state(normal)
    assert all(
        item["challenge_id"] is None and item["assignment_ref"] is None
        for item in normal_state["facts"].values()
    )
    assert normal_state["intents"][direction["id"]]["status"] == "closed"
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(ctf_options={"dispatch_mode": "automatic"})
        )
    with pytest.raises(HTTPException) as unbound:
        await knowledge.write(tid, cid, own, fact(item, artifact), kind="fact")
    assert unbound.value.status_code == 403
    reference = str(uuid4())
    snapshot = {"work_id": item["id"], "scope_revision": 1, "assignment_ref": reference}
    async with ctf.repo.engine.begin() as conn:
        task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
        control = {
            **task["ctf_control"],
            "dispatch_members": {own.agent_id: reference},
            "dispatch_assignments": {reference: snapshot},
        }
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(ctf_control=control))
    bound = await knowledge.write(
        tid, cid, own, fact(item, artifact, "Bound assignment"), kind="fact"
    )
    assert bound["object"]["assignment_ref"]["assignment_id"] == reference
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **control,
                    "dispatch_assignments": {reference: {**snapshot, "readonly_intake": True}},
                }
            )
        )
    with pytest.raises(HTTPException) as readonly:
        await knowledge.write(tid, cid, own, fact(item, artifact), kind="fact")
    assert readonly.value.status_code == 403
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(ctf_control=control))
    entered, released = asyncio.Event(), asyncio.Event()
    exists = ctf.objects.exists

    async def blocked_exists(uri):
        entered.set()
        await released.wait()
        return await exists(uri)

    monkeypatch.setattr(ctf.objects, "exists", blocked_exists)
    waiting = asyncio.create_task(
        knowledge.write(tid, cid, own, fact(item, artifact, "Storage pending"), kind="fact")
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        stopped_request = await asyncio.wait_for(
            ctf.request_finish(
                tid,
                "user",
                str(uuid4()),
                {"end_reason": "user_stop", "summary": "Stop while storage is pending"},
            ),
            timeout=5,
        )
        assert stopped_request["phase"] == "closing"
    except BaseException:
        released.set()
        await asyncio.gather(waiting, return_exceptions=True)
        raise
    released.set()
    with pytest.raises(HTTPException) as late:
        await waiting
    assert late.value.status_code == 409
    monkeypatch.setattr(ctf.objects, "exists", exists)
    assert len((await knowledge.board(tid, cid, own))["facts"]) == 2
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(status="stopped"))
    with pytest.raises(HTTPException) as stopped:
        await knowledge.write(tid, cid, own, payload, kind="fact")
    assert stopped.value.status_code == 409


async def test_terminal_review_reads_only_its_history_and_cannot_write(ctf):
    tid, owner, other, item, artifact, knowledge = await setup(ctf)
    own, peer = principal(tid, owner), principal(tid, other)
    first = await knowledge.write(tid, UUID(item["id"]), own, fact(item, artifact), kind="fact")
    private = await challenge(ctf, tid, other, "Another worker's topic")
    private_artifact = await material(ctf, tid, other)
    private_fact = await knowledge.write(
        tid,
        UUID(private["id"]),
        peer,
        fact(private, private_artifact, "Other history"),
        kind="fact",
    )
    await ctf.update_challenge(
        tid, "user", item["id"], str(uuid4()), item["revision"], "assign", owner_id=other["actor"]
    )
    await ctf.request_finish(
        tid,
        "user",
        str(uuid4()),
        {"end_reason": "user_stop", "summary": "Review historical evidence"},
    )
    for actor in (owner, other):
        await ctf.finish_turn(tid, actor["actor"], actor["turn_id"], actor["generation"], "stopped")
    await ctf.finalize_close(tid, True)
    uri = f"workspace/{tid}.tar.zst"
    ctf.objects.values[uri] = b"archive marker"
    await ctf.record_archive(tid, uri, len(ctf.objects.values[uri]))
    await ctf.record_cleanup(tid)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid, s.ctf_members.c.id == owner["actor"])
            .values(lifecycle="removed")
        )
    await ctf.post_message(tid, "user", owner["actor"], "Explain my original topic", str(uuid4()))
    turn = await ctf.claim_review_turn(tid, owner["actor"], "knowledge-review")
    assert turn is not None and turn["purpose"] == "review"
    review = principal(
        tid, {"actor": owner["actor"], "generation": turn["generation"], "turn_id": turn["id"]}
    )
    assert review.purpose == "solver"
    path = f"/api/tasks/{tid}/ctf/knowledge/{item['id']}"
    async with client(ctf) as http:
        response = await http.get(path, headers=headers(review))
        assert response.status_code == 200, response.text
        assert first["id"] in response.json()["facts"]
        assert (
            await http.get(
                f"/api/tasks/{tid}/ctf/knowledge/{private['id']}", headers=headers(review)
            )
        ).status_code == 403
        assert (
            await http.post(path + "/facts", headers=headers(review), json=fact(item, artifact))
        ).status_code == 409
        assert (await http.get(path, headers=headers(own))).status_code in {403, 409}
        assert (await http.get(path, headers=headers(peer))).status_code in {403, 409}
        events = (await http.get(f"/api/tasks/{tid}/events", headers=headers(review))).json()
        assert (
            next(event for event in events if event["version"] == first["board_version"])["type"]
            == "fact.posted"
        )
        assert (
            next(event for event in events if event["version"] == private_fact["board_version"])[
                "type"
            ]
            == "ctf.cursor"
        )
    async with ctf.repo.engine.connect() as conn:
        member = (
            (
                await conn.execute(
                    select(s.ctf_members).where(
                        s.ctf_members.c.task_id == tid, s.ctf_members.c.id == owner["actor"]
                    )
                )
            )
            .mappings()
            .one()
        )
        assert member["lifecycle"] == "removed" and member["generation"] == review.generation
