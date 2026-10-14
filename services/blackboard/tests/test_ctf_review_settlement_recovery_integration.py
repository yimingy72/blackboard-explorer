"""Joined local review recovery never bypasses saved history, usage or writer fences."""

from typing import Any
from uuid import uuid4

import pytest
from bbx_blackboard.store.repository import now
from fastapi import HTTPException
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_integration import history
from test_ctf_review_integration import terminal

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def saved_review(ctf, tid, aid, *, receipt=True, observation=True):
    message = await ctf.post_message(tid, "user", aid, "Explain saved arithmetic", str(uuid4()))
    turn = await ctf.claim_review_turn(tid, aid, "joined-runtime")
    messages = await ctf.claim_messages(tid, aid, turn["id"], turn["generation"])
    session: dict[str, Any] = history(messages)
    usage_id = str(uuid4())
    session["state"].update(
        ctf_steps={turn["id"]: 1},
        ctf_review_usage_outbox=[
            {
                "request_id": usage_id,
                "turn_id": turn["id"],
                "agent_id": aid,
                "generation": turn["generation"],
                "reservation_id": f"{turn['id']}:1",
                "usage": {"cost": "0.25", "output_tokens": 7},
            }
        ],
    )
    await ctf.checkpoint(
        tid,
        aid,
        turn["id"],
        turn["generation"],
        session,
        "Saved review instructions",
        0,
        [{"message_id": message["id"], "claim_token": messages[0]["claim_token"]}],
    )
    if receipt:
        await ctf.bill_usage(
            tid, aid, turn["id"], turn["generation"], usage_id, {"cost": "0.25", "output_tokens": 7}
        )
    if observation:
        await ctf.record_observation(
            tid,
            aid,
            turn["id"],
            turn["generation"],
            "model_output",
            {"step": 1, "text": "Saved arithmetic explanation"},
            str(uuid4()),
        )
    return turn, message


def proof(turn, **changes):
    return {
        "runtime_instance": "joined-runtime",
        "failed_turn_id": turn["id"],
        "agent_id": turn["agent_id"],
        "generation": turn["generation"],
        "joined": True,
        "answer": "Original arithmetic explanation",
        "failure": {
            "error_type": "CtfControlTransportExhausted",
            "phase": "review_settlement",
            "occurred_at": now().isoformat(),
            "correlation_id": str(uuid4()),
            "summary": "Review settlement control transport retries exhausted.",
        },
        **changes,
    }


async def test_targeted_review_recovery_preserves_answer_session_usage_and_peer(ctf):
    tid, peer_id = await terminal(ctf, remove=True)
    target, message = await saved_review(ctf, tid, "lead")
    peer, _ = await saved_review(ctf, tid, peer_id)
    before = await ctf.state(tid)
    saved = await ctf.sessions.get_session(tid, "lead")
    arguments = proof(target)
    result = await ctf.recover_reviews(tid, **arguments)
    assert result["recovered"] == 1
    assert result["turn"]["end_reason"] == "interrupted"
    assert result["turn"]["final_answer"] == arguments["answer"]
    assert result["turn"]["usage"] == next(
        turn["usage"] for turn in before["turns"] if turn["id"] == target["id"]
    )
    assert await ctf.sessions.get_session(tid, "lead") == saved
    after = await ctf.state(tid)
    assert next(turn for turn in after["turns"] if turn["id"] == peer["id"]) == next(
        turn for turn in before["turns"] if turn["id"] == peer["id"]
    )
    for key in ("status", "ctf_control", "ctf_conclusion", "usage"):
        assert after["task"][key] == before["task"][key]
    lead = next(member for member in after["members"] if member["id"] == "lead")
    assert lead["current_turn_id"] is None and lead["generation"] == target["generation"] + 1
    replies = [item for item in after["messages"] if item["kind"] == "review_result"]
    assert len(replies) == 1 and replies[0]["body"] == arguments["answer"]
    assert replies[0]["reply_to"] == message["id"]
    repeat = await ctf.recover_reviews(tid, **arguments)
    assert repeat == {"recovered": 0, "turn": result["turn"]}
    assert (await ctf.state(tid))["messages"] == after["messages"]
    with pytest.raises(HTTPException):
        await ctf.checkpoint(
            tid, "lead", target["id"], target["generation"], saved["session"], "Late", 1, []
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"joined": False},
        {"runtime_instance": "other-runtime"},
        {"generation": -1},
        {"agent_id": "other"},
    ],
)
async def test_targeted_recovery_rejects_writer_proof_mismatch(ctf, changes):
    tid, aid = await terminal(ctf)
    turn, _ = await saved_review(ctf, tid, aid)
    before = await ctf.state(tid)
    with pytest.raises(HTTPException):
        await ctf.recover_reviews(tid, **proof(turn, **changes))
    assert await ctf.state(tid) == before


@pytest.mark.parametrize("missing", ["checkpoint", "usage", "observation"])
async def test_targeted_recovery_keeps_unconfirmed_review_barriers(ctf, missing):
    tid, aid = await terminal(ctf)
    turn, _ = await saved_review(
        ctf, tid, aid, receipt=missing != "usage", observation=missing != "observation"
    )
    if missing == "checkpoint":
        await ctf.checkpoint_failed(tid, aid, turn["id"], turn["generation"])
    before = await ctf.state(tid)
    saved = await ctf.sessions.get_session(tid, aid)
    with pytest.raises(HTTPException):
        await ctf.recover_reviews(tid, **proof(turn))
    assert await ctf.state(tid) == before
    assert await ctf.sessions.get_session(tid, aid) == saved


async def test_targeted_recovery_reads_original_finished_result_after_late_write(ctf):
    tid, aid = await terminal(ctf)
    turn, message = await saved_review(ctf, tid, aid)
    original = await ctf.finish_turn(
        tid, aid, turn["id"], turn["generation"], "completed", "Confirmed original explanation"
    )
    before = await ctf.state(tid)
    result = await ctf.recover_reviews(tid, **proof(turn))
    assert result == {"recovered": 0, "turn": original}
    assert await ctf.state(tid) == before
    replies = [item for item in before["messages"] if item["kind"] == "review_result"]
    assert len(replies) == 1 and replies[0]["body"] == "Confirmed original explanation"
    assert replies[0]["reply_to"] == message["id"]


async def test_cross_instance_review_retires_marker_into_fault_history(ctf):
    tid, aid = await terminal(ctf)
    turn, _ = await saved_review(ctf, tid, aid, receipt=False)
    await ctf.checkpoint_failed(tid, aid, turn["id"], turn["generation"])
    before = await ctf.state(tid)
    saved = await ctf.sessions.get_session(tid, aid)
    result = await ctf.recover_reviews(tid, "replacement-runtime")
    assert result == {"recovered": 1}
    after = await ctf.state(tid)
    interrupted = next(item for item in after["turns"] if item["id"] == turn["id"])
    assert interrupted["status"] == "finished" and interrupted["end_reason"] == "interrupted"
    control = after["task"]["ctf_control"]
    assert not control["unresolved_checkpoints"]
    fault = control["checkpoint_failures"][turn["id"]]
    assert fault["purpose"] == "review" and fault["generation"] == turn["generation"]
    assert fault["checkpoint_revision"] == interrupted["checkpoint_revision"]
    assert fault["reason"] == "Turn exited before its final checkpoint was confirmed"
    assert await ctf.sessions.get_session(tid, aid) == saved
    assert after["task"]["usage"] == before["task"]["usage"]
    assert control.get("review_usage", {}) == before["task"]["ctf_control"].get("review_usage", {})
    assert after["task"]["ctf_conclusion"] == before["task"]["ctf_conclusion"]
    assert await ctf.recover_reviews(tid, "replacement-runtime") == {"recovered": 0}
    with pytest.raises(HTTPException):
        await ctf.checkpoint(
            tid, aid, turn["id"], turn["generation"], saved["session"], "Late", 1, []
        )


async def test_review_claim_returns_only_same_instance_current_generation(ctf):
    tid, aid = await terminal(ctf)
    turn, _ = await saved_review(ctf, tid, aid)
    before = await ctf.state(tid)
    assert await ctf.claim_review_turn(tid, aid, "joined-runtime") == next(
        item for item in before["turns"] if item["id"] == turn["id"]
    )
    assert await ctf.claim_review_turn(tid, aid, "another-runtime") is None
    assert await ctf.state(tid) == before
    await ctf.checkpoint_failed(tid, aid, turn["id"], turn["generation"])
    assert await ctf.claim_review_turn(tid, aid, "joined-runtime") is None
