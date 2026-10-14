"""Explicit owner delivery is one CAS transaction, without model calls."""

from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from ctf_solution_fixtures import solution
from fastapi import HTTPException
from sqlalchemy import update
from test_ctf_integration import board_team
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url

pytestmark = pytest.mark.integration


async def owned_challenge(ctf):
    tid, owner, helper = await board_team(ctf)
    challenge = await ctf.create_challenge(tid, **owner, request_id=str(uuid4()), title="Delivery")
    challenge = await ctf.update_challenge(
        tid,
        **owner,
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=1,
        action="claim",
    )
    return tid, owner, helper, challenge


def declaration(challenge, **fields):
    if fields.get("work_status") == "completed":
        fields.setdefault("solution", solution())
    return dict(
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=challenge["revision"],
        status="candidate",
        summary="Delivered",
        **fields,
    )


@pytest.mark.parametrize("actor", ["owner", "lead"])
@pytest.mark.parametrize("status", ["candidate", "unknown"])
async def test_owner_or_lead_atomic_delivery_and_idempotency(ctf, actor, status):
    tid, owner, _, challenge = await owned_challenge(ctf)
    identity = owner if actor == "owner" else {"actor": "user"}
    if actor == "lead":
        from test_ctf_integration import ctf_turn

        turn = await ctf_turn(ctf, tid)
        identity = {"actor": "lead", "generation": turn["generation"], "turn_id": turn["id"]}
    payload = declaration(challenge, work_status="completed")
    payload["status"] = status
    result = await ctf.record_candidate(tid, **identity, **payload)
    assert result["work_status"] == "completed"
    assert result["verification"]["status"] == status
    assert result["verification"]["source"] == "agent"
    assert await ctf.record_candidate(tid, **identity, **payload) == result
    current = await ctf.get_challenge(tid, challenge["id"])
    assert current["work_status"] == "completed"
    assert current["revision"] == challenge["revision"] + 1
    assert len((await ctf.list_records(tid, challenge["id"]))["records"]) == 2
    with pytest.raises(HTTPException, match="Request ID conflict"):
        await ctf.record_candidate(
            tid, **identity, **{**payload, "work_status": "blocked", "solution": None}
        )


async def test_collaborator_may_record_candidate_but_cannot_deliver_owner_work(ctf):
    tid, owner, helper, challenge = await owned_challenge(ctf)
    challenge = await ctf.update_challenge(
        tid,
        "user",
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=challenge["revision"],
        action="collaborators",
        collaborator_ids=[helper["actor"]],
    )
    payload = declaration(challenge)
    with pytest.raises(HTTPException, match="Only the owner or Lead"):
        await ctf.record_candidate(
            tid, **helper, **payload, work_status="completed", solution=solution()
        )
    assert await ctf.get_challenge(tid, challenge["id"]) == challenge
    assert not (await ctf.list_records(tid, challenge["id"]))["records"]
    await ctf.record_candidate(tid, **helper, **payload)
    current = await ctf.get_challenge(tid, challenge["id"])
    assert current["work_status"] == "in_progress"
    assert current["verification"]["status"] == "candidate"


async def test_unassigned_member_cannot_bypass_record_permission_with_candidate(ctf):
    tid, owner, helper, challenge = await owned_challenge(ctf)
    record = dict(challenge_id=challenge["id"], request_id=str(uuid4()), body="Offline findings")
    candidate = declaration(challenge)
    with pytest.raises(HTTPException, match="ask Lead to add you to collaborator_ids"):
        await ctf.append_record(tid, **helper, **record)
    with pytest.raises(HTTPException, match="Only assigned members"):
        await ctf.record_candidate(tid, **helper, **candidate)
    assert await ctf.get_challenge(tid, challenge["id"]) == challenge
    assert not (await ctf.list_records(tid, challenge["id"]))["records"]
    challenge = await ctf.update_challenge(
        tid,
        "user",
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=challenge["revision"],
        action="collaborators",
        collaborator_ids=[helper["actor"]],
    )
    await ctf.append_record(tid, **helper, **record)
    await ctf.record_candidate(
        tid, **helper, **{**candidate, "expected_revision": challenge["revision"]}
    )
    assert len((await ctf.list_records(tid, challenge["id"]))["records"]) == 2


async def test_delivery_stale_revision_or_unregistered_artifact_writes_nothing(ctf):
    tid, owner, _, challenge = await owned_challenge(ctf)
    payload = declaration(challenge, work_status="completed")
    for patch in ({"expected_revision": 1}, {"evidence_refs": [str(uuid4())]}):
        with pytest.raises(HTTPException):
            await ctf.record_candidate(tid, **owner, **{**payload, **patch})
        assert await ctf.get_challenge(tid, challenge["id"]) == challenge
        assert not (await ctf.list_records(tid, challenge["id"]))["records"]


async def test_delivery_rolls_back_record_when_challenge_write_fails(ctf, monkeypatch):
    tid, owner, _, challenge = await owned_challenge(ctf)
    original = ctf._board_event

    async def fail_update(conn, tid, actor, kind, request_id, digest, result):
        if kind == "ctf.challenge.updated":
            raise RuntimeError("scripted write failure")
        return await original(conn, tid, actor, kind, request_id, digest, result)

    monkeypatch.setattr(ctf, "_board_event", fail_update)
    payload = declaration(challenge, work_status="completed")
    with pytest.raises(RuntimeError, match="scripted write failure"):
        await ctf.record_candidate(tid, **owner, **payload)
    assert await ctf.get_challenge(tid, challenge["id"]) == challenge
    assert not (await ctf.list_records(tid, challenge["id"]))["records"]
    monkeypatch.setattr(ctf, "_board_event", original)
    await ctf.record_candidate(tid, **owner, **payload)
    assert len((await ctf.list_records(tid, challenge["id"]))["records"]) == 2


@pytest.mark.parametrize("source", ["user", "platform"])
@pytest.mark.parametrize("work_status", [None, "completed"])
async def test_candidate_delivery_preserves_trusted_acceptance(ctf, source, work_status):
    tid, owner, _, challenge = await owned_challenge(ctf)
    await ctf.manual_verification(
        tid,
        "authenticated-user",
        challenge["id"],
        str(uuid4()),
        challenge["revision"],
        "accepted",
        "Confirmed",
    )
    challenge = await ctf.get_challenge(tid, challenge["id"])
    if source == "platform":
        challenge["verification"]["source"] = "platform"
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.ctf_challenges)
                .where(s.ctf_challenges.c.task_id == tid, s.ctf_challenges.c.id == challenge["id"])
                .values(data=challenge)
            )
    result = await ctf.record_candidate(
        tid, **owner, **declaration(challenge, work_status=work_status)
    )
    assert result["verification"]["status"] == "candidate"
    current = await ctf.get_challenge(tid, challenge["id"])
    assert current["verification"] == challenge["verification"]
    assert current["work_status"] == (work_status or challenge["work_status"])


async def test_candidate_text_does_not_implicitly_complete_work(ctf):
    tid, owner, _, challenge = await owned_challenge(ctf)
    payload = declaration(challenge)
    payload["summary"] = "Candidate CTF{offline-test-example}"
    await ctf.record_candidate(tid, **owner, **payload)
    current = await ctf.get_challenge(tid, challenge["id"])
    assert current["work_status"] == "in_progress"
    assert current["verification"]["status"] == "candidate"


async def test_closed_challenge_requires_explicit_reopen(ctf):
    tid, owner, _, challenge = await owned_challenge(ctf)
    await ctf.record_candidate(tid, **owner, **declaration(challenge, work_status="completed"))
    challenge = await ctf.get_challenge(tid, challenge["id"])
    with pytest.raises(HTTPException, match="Reopen the challenge first"):
        await ctf.record_candidate(tid, **owner, **declaration(challenge, work_status="blocked"))
