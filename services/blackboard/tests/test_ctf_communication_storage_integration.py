"""Disposable PostgreSQL checks for observation plans and compact checkpoint proofs."""

import json
from typing import Any
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import text, update
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_integration import history, team

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def test_observation_lookup_uses_partial_index_with_large_synthetic_history(ctf):
    tid = await team(ctf)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO events (task_id, type, actor, object_id, payload) "
                "SELECT :tid, 'agent.trace.recorded', 'synthetic', "
                "'synthetic-observation-' || n, '{}'::jsonb "
                "FROM generate_series(1, 12000) AS n"
            ),
            {"tid": tid},
        )
        await conn.execute(text("ANALYZE events"))
        plan = (
            await conn.execute(
                text(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT payload FROM events "
                    "WHERE task_id=:tid AND type='agent.trace.recorded' AND object_id=:oid LIMIT 1"
                ),
                {"tid": tid, "oid": "synthetic-missing-observation"},
            )
        ).scalar_one()
    assert "ix_events_ctf_observation" in json.dumps(plan)


async def test_compact_checkpoint_delivery_can_prove_only_normal_prior_execution(ctf):
    tid = await team(ctf)
    lead = await ctf.claim_turn(tid, "lead", "communication-storage")
    member = await ctf.create_member(tid, "user", "Synthetic member", str(uuid4()))
    aid = member["id"]

    async def assign():
        mid = str(uuid4())
        await ctf.post_message(
            tid,
            "lead",
            aid,
            "Synthetic single-use arithmetic assignment",
            mid,
            "instruction",
            generation=lead["generation"],
            turn_id=lead["id"],
        )
        return mid

    old_mid = await assign()
    old = await ctf.claim_turn(tid, aid, "communication-storage")
    messages = await ctf.claim_messages(tid, aid, old["id"], old["generation"])
    payload: dict[str, Any] = history(messages)
    payload["state"]["bbx_checkpoint_id"] = str(uuid4())
    payload["state"]["synthetic_long_history"] = "x" * 200000
    receipt = await ctf.checkpoint(
        tid,
        aid,
        old["id"],
        old["generation"],
        payload,
        "Synthetic opening",
        0,
        [{"message_id": msg["id"], "claim_token": msg["claim_token"]} for msg in messages],
    )
    assert receipt["delivered_message_ids"] == [old_mid]
    assert receipt["checkpoint_id"] == payload["state"]["bbx_checkpoint_id"]
    assert len(json.dumps(receipt).encode()) < 600
    assert (await ctf.sessions.get_session(tid, aid))["session"] == payload
    await ctf.finish_turn(tid, aid, old["id"], old["generation"], "completed", "Synthetic result")
    fresh_mid = await assign()
    current = await ctf.claim_turn(tid, aid, "communication-storage")
    before = await ctf.state(tid)
    result = await ctf.recovery_input_proofs(
        tid, aid, current["id"], current["generation"], [old_mid, fresh_mid]
    )
    assert [proof["id"] for proof in result["proofs"]] == [old_mid]
    assert await ctf.state(tid) == before
    assert "body" not in result["proofs"][0]
    assert result["proofs"][0]["session_revision"] == receipt["revision"]
    with pytest.raises(HTTPException, match="Stale CTF generation"):
        await ctf.recovery_input_proofs(tid, aid, old["id"], old["generation"], [old_mid])
    for reason in ("interrupted", "step_limit", "context_blocked", "provider_transient"):
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.ctf_turns)
                .where(s.ctf_turns.c.task_id == tid, s.ctf_turns.c.id == old["id"])
                .values(end_reason=reason)
            )
        assert await ctf.recovery_input_proofs(
            tid, aid, current["id"], current["generation"], [old_mid]
        ) == {"proofs": []}
