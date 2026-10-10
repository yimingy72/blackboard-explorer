"""Terminal review retains identity and history without execution authority or billing."""

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_ctf_integration import history, team

pytestmark = pytest.mark.integration


class ReviewObjects:
    def __init__(self):
        self.files = {}

    async def put(self, key, data, **kwargs):
        self.files[key] = data

    async def exists(self, key):
        return key in self.files


async def terminal(ctf, *, remove=False):
    tid = await team(ctf)
    if remove:
        member = await ctf.create_member(tid, "user", "Archived teammate", str(uuid4()))
        aid = member["id"]
        await ctf.record_execution(tid, aid, None, 0, "simulated-boot")
        request_id = str(uuid4())
        await ctf.request_member_operation(tid, "user", aid, "remove", request_id)
        await ctf.complete_member_operation(
            tid, aid, request_id, {"boot_id": "simulated-boot", "generation": 0, "drained": True}
        )
    else:
        aid = "lead"
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Review saved evidence"}
    )
    await ctf.finalize_close(tid, True)
    objects = ReviewObjects()
    ctf.objects = objects
    uri = f"workspace/{tid}.tar.zst"
    objects.files[uri] = b"simulated archive marker"
    await ctf.record_archive(tid, uri, len(objects.files[uri]))
    await ctf.record_cleanup(tid)
    return tid, aid


@pytest.mark.parametrize("removed", [False, True])
async def test_terminal_review_removed_history_billing_and_resume_delete_barrier(ctf, removed):
    tid, aid = await terminal(ctf, remove=removed)
    before = await ctf.state(tid)
    original = next(member for member in before["members"] if member["id"] == aid)
    user_message = await ctf.post_message(
        tid, "user", aid, "Explain the stored result", str(uuid4())
    )
    assert user_message["purpose"] == "review" and user_message["kind"] == "review"
    turns = await asyncio.gather(
        *(ctf.claim_review_turn(tid, aid, "review-runtime") for _ in range(2))
    )
    assert sum(turn is not None for turn in turns) == 1
    turn = next(turn for turn in turns if turn is not None)
    assert turn["purpose"] == "review"
    with pytest.raises(HTTPException):
        await ctf.resume(tid, uuid4(), "1", 1)
    with pytest.raises(HTTPException):
        await ctf.sessions.request_delete(tid)
    messages = await ctf.claim_messages(tid, aid, turn["id"], turn["generation"])
    assert [message["id"] for message in messages] == [user_message["id"]]
    assert all(message["purpose"] == "review" for message in messages)
    deliveries = [
        {"message_id": message["id"], "claim_token": message["claim_token"]} for message in messages
    ]
    await ctf.checkpoint(
        tid,
        aid,
        turn["id"],
        turn["generation"],
        history(messages),
        "Preserved review instructions",
        0,
        deliveries,
    )
    usage_id = str(uuid4())
    await ctf.bill_usage(
        tid, aid, turn["id"], turn["generation"], usage_id, {"cost": "0.25", "output_tokens": 7}
    )
    await ctf.bill_usage(
        tid, aid, turn["id"], turn["generation"], usage_id, {"cost": "0.25", "output_tokens": 7}
    )
    with pytest.raises(HTTPException):
        await ctf.authorize_member(tid, aid, turn["generation"], turn["id"])
    await ctf.finish_turn(
        tid, aid, turn["id"], turn["generation"], "completed", "Historical explanation only"
    )
    # Settlement retries must not duplicate the correlated review reply.
    await ctf.finish_turn(
        tid, aid, turn["id"], turn["generation"], "completed", "Historical explanation only"
    )
    after = await ctf.state(tid)
    member = next(item for item in after["members"] if item["id"] == aid)
    assert member["lifecycle"] == original["lifecycle"]
    assert member["run_state"] == original["run_state"]
    assert member["usage"] == original["usage"]
    assert after["task"]["usage"] == before["task"]["usage"]
    assert Decimal(str(after["task"]["ctf_control"]["review_usage"]["cost"])) == Decimal("0.25")
    replies = [message for message in after["messages"] if message["kind"] == "review_result"]
    assert len(replies) == 1 and replies[0]["reply_to"] == user_message["id"]
    assert replies[0]["recipient_id"] == aid and replies[0]["status"] == "delivered"
    execution_before = [
        message for message in before["messages"] if message["purpose"] == "execution"
    ]
    execution_after = [
        message for message in after["messages"] if message["purpose"] == "execution"
    ]
    assert execution_after == execution_before
    assert await ctf.claim_review_turn(tid, aid, "review-runtime") is None
    await ctf.resume(tid, uuid4(), "1", 1)
    resumed = await ctf.state(tid)
    assert (
        next(item for item in resumed["members"] if item["id"] == aid)["lifecycle"]
        == original["lifecycle"]
    )


async def test_review_recovery_requeues_unconfirmed_input_and_fences_late_checkpoint(ctf):
    tid, aid = await terminal(ctf)
    message = await ctf.post_message(tid, "user", aid, "Review after process restart", str(uuid4()))
    old = await ctf.claim_review_turn(tid, aid, "old-runtime")
    leased = await ctf.claim_messages(tid, aid, old["id"], old["generation"])
    assert leased[0]["id"] == message["id"]
    assert (await ctf.recover_reviews(tid, "new-runtime"))["recovered"] == 1
    with pytest.raises(HTTPException):
        await ctf.checkpoint(tid, aid, old["id"], old["generation"], history(leased), "Old", 0, [])
    new = await ctf.claim_review_turn(tid, aid, "new-runtime")
    assert new["generation"] > old["generation"]
    fresh = await ctf.claim_messages(tid, aid, new["id"], new["generation"])
    assert [item["id"] for item in fresh] == [message["id"]]
    assert (await ctf.recover_reviews(tid, "new-runtime"))["recovered"] == 0
    deliveries = [{"message_id": item["id"], "claim_token": item["claim_token"]} for item in fresh]
    await ctf.checkpoint(
        tid, aid, new["id"], new["generation"], history(fresh), "New", 0, deliveries
    )
    await ctf.finish_turn(tid, aid, new["id"], new["generation"], "completed", "Recovered review")
    state = await ctf.state(tid)
    assert len([item for item in state["messages"] if item["kind"] == "review_result"]) == 1
