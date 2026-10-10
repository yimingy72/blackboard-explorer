"""CTF transactions against disposable PostgreSQL, without model or envd calls."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ctf_url():
    root = Path(__file__).resolve().parents[3]
    container = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg")
    container.with_name(f"bbx-ctf-db-{uuid4().hex[:8]}")
    with container:
        url = container.get_connection_url()
        old = os.environ.get("BBX_DATABASE_URL")
        os.environ["BBX_DATABASE_URL"] = url
        try:
            command.upgrade(Config(str(root / "services/blackboard/alembic.ini")), "head")
            yield url
        finally:
            if old is None:
                os.environ.pop("BBX_DATABASE_URL", None)
            else:
                os.environ["BBX_DATABASE_URL"] = old


@pytest.fixture
async def ctf(ctf_url):
    engine = create_async_engine(ctf_url)
    try:
        yield CtfService(engine)
    finally:
        await engine.dispose()


async def team(ctf):
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Solve a fake challenge",
            "budget": {"max_cost": "10", "max_minutes": 60},
            "ctf_options": {"max_teammates": 1},
        }
    )
    await ctf.provision(tid)
    await ctf.start(tid)
    return tid


async def ctf_turn(ctf, tid, recipient="lead", body="Compute 2 + 2"):
    await ctf.post_message(tid, "user", recipient, body, str(uuid4()), "instruction")
    turn = await ctf.claim_turn(tid, recipient, "budget-test")
    assert turn is not None
    return turn


async def test_budget_reservations_atomically_bound_parallel_calls(ctf):
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Bound fake arithmetic calls",
            "budget": {"max_cost": "1", "max_minutes": 60},
            "ctf_options": {"max_teammates": 2},
        }
    )
    await ctf.provision(tid)
    await ctf.start(tid)
    alice = await ctf.create_member(tid, "user", "Alice", str(uuid4()))
    bob = await ctf.create_member(tid, "user", "Bob", str(uuid4()))
    alice_turn, bob_turn = await asyncio.gather(
        ctf_turn(ctf, tid, alice["id"], "Read local note"),
        ctf_turn(ctf, tid, bob["id"], "Compute 2 + 2"),
    )
    results = await asyncio.gather(
        ctf.authorize_member(
            tid,
            alice["id"],
            alice_turn["generation"],
            alice_turn["id"],
            reservation_id="alice:1",
            reservation_cost="0.60",
        ),
        ctf.authorize_member(
            tid,
            bob["id"],
            bob_turn["generation"],
            bob_turn["id"],
            reservation_id="bob:1",
            reservation_cost="0.60",
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, HTTPException) for result in results) == 1
    state = await ctf.state(tid)
    reservations = state["task"]["ctf_control"]["budget_reservations"]
    assert sum(float(item["cost"]) for item in reservations.values()) == 0.6

    winner, winner_turn = (
        (alice, alice_turn) if not isinstance(results[0], Exception) else (bob, bob_turn)
    )
    winner_reservation = "alice:1" if winner["id"] == alice["id"] else "bob:1"
    await ctf.bill_usage(
        tid,
        winner["id"],
        winner_turn["id"],
        winner_turn["generation"],
        "usage-1",
        {"cost": "0.20"},
        reservation_id=winner_reservation,
    )
    await ctf.bill_usage(
        tid,
        winner["id"],
        winner_turn["id"],
        winner_turn["generation"],
        "usage-1",
        {"cost": "0.20"},
        reservation_id=winner_reservation,
    )
    state = await ctf.state(tid)
    assert state["task"]["usage"]["cost"] == "0.20"
    assert "budget_reservations" not in state["task"]["ctf_control"]


async def test_budget_reservation_releases_on_cancel_and_recovery(ctf):
    tid = await team(ctf)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(budget={"max_cost": "1", "max_minutes": 60})
        )
    turn = await ctf_turn(ctf, tid)
    await ctf.authorize_member(
        tid, "lead", turn["generation"], turn["id"], reservation_id="lead:1", reservation_cost="0.8"
    )
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["budget_reservations"]
    await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "stopped")
    assert "budget_reservations" not in (await ctf.state(tid))["task"]["ctf_control"]

    second = await ctf_turn(ctf, tid, body="Read the saved fake note")
    await ctf.authorize_member(
        tid,
        "lead",
        second["generation"],
        second["id"],
        reservation_id="lead:2",
        reservation_cost="0.8",
    )
    await ctf.mark_reservation_sent(tid, "lead", second["generation"], second["id"], "lead:2")
    restarted = CtfService(ctf.repo.engine)
    await restarted.recover(tid)
    recovered = await restarted.state(tid)
    assert "budget_reservations" not in recovered["task"]["ctf_control"]
    assert (
        recovered["task"]["ctf_control"]["budget_unknown_reservations"]["lead:2"]["cost"] == "0.8"
    )
    assert recovered["members"][0]["generation"] == second["generation"] + 1
    with pytest.raises(HTTPException, match="Stale CTF generation"):
        await restarted.bill_usage(
            tid,
            "lead",
            second["id"],
            second["generation"],
            "late-usage",
            {"cost": "0.8"},
            reservation_id="lead:2",
        )
    next_turn = await ctf_turn(restarted, tid, body="Read another local note")
    with pytest.raises(HTTPException, match="Task budget exhausted"):
        await restarted.authorize_member(
            tid,
            "lead",
            next_turn["generation"],
            next_turn["id"],
            reservation_id="lead:3",
            reservation_cost="0.3",
        )
    await restarted.finish_turn(tid, "lead", next_turn["id"], next_turn["generation"], "stopped")


async def test_failed_start_is_terminal_and_not_requeued(ctf):
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Start a fake worker",
            "budget": {"max_cost": "1", "max_minutes": 5},
        }
    )
    await ctf.provision(tid)
    await ctf.fail_start(tid, "fake worker unavailable")
    state = await ctf.state(tid)
    assert state["task"]["status"] == "failed"
    assert state["task"]["ctf_control"]["phase"] == "closed"
    assert state["task"]["ctf_conclusion"]["end_reason"] == "system_failure"
    with pytest.raises(HTTPException, match="Task cannot start"):
        await ctf.start(tid)


def history(messages):
    return {
        "state": {
            "in_memory": {"messages": [{"role": "user", "message_id": m["id"]} for m in messages]}
        }
    }


async def test_claim_checkpoint_recovery_and_replay(ctf):
    tid = await team(ctf)
    claims = await asyncio.gather(*(ctf.claim_turn(tid, "lead", "runtime") for _ in range(2)))
    turn = next(t for t in claims if t)
    assert sum(t is not None for t in claims) == 1
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    retried = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    assert retried[0]["claim_token"] == messages[0]["claim_token"]
    deliveries = [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages]
    # Pending snapshots are durable, but do not acknowledge delivered history.
    pending = {"state": {"pending_messages": history(messages)["state"]["in_memory"]["messages"]}}
    with pytest.raises(HTTPException):
        await ctf.checkpoint(
            tid, "lead", turn["id"], turn["generation"], pending, "prompt", 0, deliveries
        )
    await ctf.checkpoint(tid, "lead", turn["id"], turn["generation"], pending, "prompt", 0, [])
    await ctf.recover(tid)
    with pytest.raises(HTTPException):
        await ctf.checkpoint(
            tid, "lead", turn["id"], turn["generation"], history(messages), "prompt", 1, deliveries
        )
    turn = await ctf.claim_turn(tid, "lead", "new-runtime")
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    deliveries = [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages]
    saved = await ctf.checkpoint(
        tid, "lead", turn["id"], turn["generation"], history(messages), "prompt", 1, deliveries
    )
    assert saved["revision"] == 2
    before = await ctf.state(tid)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == tid)
            .values(display_name="damaged")
        )
    await ctf.repo.replay(tid)
    after = await ctf.state(tid)
    assert after == before
    with pytest.raises(HTTPException):
        await ctf.checkpoint(
            tid, "lead", turn["id"], turn["generation"], history(messages), "prompt", 1, deliveries
        )
    await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "completed", "done")
    assert not [m for m in (await ctf.state(tid))["messages"] if m["kind"] == "turn_finished"]


async def test_quota_results_usage_and_closing(ctf):
    tid = await team(ctf)
    request = str(uuid4())
    member = await ctf.create_member(tid, "user", "Alice", request)
    assert (await ctf.create_member(tid, "user", "Alice", request))["id"] == member["id"]
    with pytest.raises(HTTPException):
        await ctf.create_member(tid, "user", "Bob", str(uuid4()))
    mid = str(uuid4())
    msg = await ctf.post_message(tid, "user", member["id"], "Investigate", mid, "instruction")
    assert (await ctf.post_message(tid, "user", member["id"], "Investigate", mid, "instruction"))[
        "id"
    ] == msg["id"]
    with pytest.raises(HTTPException):
        await ctf.post_message(tid, "user", member["id"], "Changed", mid, "instruction")
    turn = await ctf.claim_turn(tid, member["id"], "runtime")
    messages = await ctf.claim_messages(tid, member["id"], turn["id"], turn["generation"])
    await ctf.checkpoint(
        tid,
        member["id"],
        turn["id"],
        turn["generation"],
        history(messages),
        "prompt",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
    )
    for _ in range(2):
        await ctf.bill_usage(
            tid, member["id"], turn["id"], turn["generation"], "billing", {"cost": "0.1"}
        )
    for _ in range(2):
        await ctf.finish_turn(
            tid, member["id"], turn["id"], turn["generation"], "completed", "Found evidence"
        )
    state = await ctf.state(tid)
    assert state["task"]["usage"]["cost"] == "0.1"
    assert (
        len([m for m in state["messages"] if m["notification_purpose"] == "assignment_result"]) == 1
    )
    close_id = str(uuid4())
    await ctf.request_finish(tid, "user", close_id, {"end_reason": "user_stop", "summary": "Stop"})
    deferred = await ctf.post_message(tid, "user", "lead", "One more thing", str(uuid4()))
    assert deferred["deferred"]
    assert await ctf.claim_turn(tid, "lead", "runtime") is None
    with pytest.raises(HTTPException):
        await ctf.finalize_close(tid, False)
    assert (await ctf.finalize_close(tid, True))["status"] == "stopped"
    async with ctf.repo.engine.connect() as conn:
        private_events = (
            (
                await conn.execute(
                    select(s.events).where(
                        s.events.c.task_id == tid, s.events.c.type == "ctf.message.posted"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert all(event["addressed_to"] for event in private_events)


async def test_failed_stream_result_and_outbox_recovery(ctf):
    tid = await team(ctf)
    member = await ctf.create_member(tid, "user", "Alice", str(uuid4()))
    mid = str(uuid4())
    await ctf.post_message(tid, "user", member["id"], "Investigate", mid, "instruction")
    first = await ctf.claim_turn(tid, member["id"], "first")
    messages = await ctf.claim_messages(tid, member["id"], first["id"], first["generation"])
    await ctf.bill_usage(
        tid, member["id"], first["id"], first["generation"], "request", {"cost": "0.1"}
    )
    # Stream drained pending in memory, then failed before delivery checkpoint.
    await ctf.recover(tid)
    second = await ctf.claim_turn(tid, member["id"], "second")
    reclaimed = await ctf.claim_messages(tid, member["id"], second["id"], second["generation"])
    assert reclaimed[0]["id"] == messages[0]["id"]
    assert reclaimed[0]["claim_token"] != messages[0]["claim_token"]
    await ctf.bill_usage(
        tid, member["id"], second["id"], second["generation"], "request", {"cost": "0.1"}
    )
    await ctf.checkpoint(
        tid,
        member["id"],
        second["id"],
        second["generation"],
        history(reclaimed),
        "prompt",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in reclaimed],
    )
    reply = await ctf.post_message(
        tid,
        member["id"],
        "lead",
        "Evidence found",
        str(uuid4()),
        reply_to=mid,
        generation=second["generation"],
        turn_id=second["id"],
    )
    await ctf.finish_turn(
        tid, member["id"], second["id"], second["generation"], "completed", "Evidence found"
    )
    state = await ctf.state(tid)
    assert state["task"]["usage"]["cost"] == "0.1"
    results = [m for m in state["messages"] if m["notification_purpose"] == "assignment_result"]
    assert len(results) == 2
    assert "interrupted" in next(m["body"] for m in results if m["source_turn_id"] == first["id"])
    completed = next(m["body"] for m in results if m["source_turn_id"] == second["id"])
    assert reply["id"] in completed
    assert "Evidence found" not in completed


async def test_context_limit_continuation_is_idempotent_and_respects_budget_and_stop(ctf):
    tid = await team(ctf)
    instruction = await ctf.post_message(
        tid, "user", "lead", "Continue the investigation", str(uuid4()), "instruction"
    )
    turn = await ctf.claim_turn(tid, "lead", "runtime")
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    assert instruction["id"] in {message["id"] for message in messages}
    await ctf.checkpoint(
        tid,
        "lead",
        turn["id"],
        turn["generation"],
        history(messages),
        "prompt",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
    )
    await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "context_limit")
    continuation = await ctf.enqueue_continuation(tid, "lead", turn["id"], turn["generation"])
    duplicate = await ctf.enqueue_continuation(tid, "lead", turn["id"], turn["generation"])
    assert continuation["id"] == duplicate["id"]
    assert continuation["notification_purpose"] == "context_continuation"
    assert await ctf.claim_turn(tid, "lead", "runtime")

    stopped_tid = await team(ctf)
    stop_message = await ctf.post_message(
        stopped_tid, "user", "lead", "Stop after this turn", str(uuid4()), "instruction"
    )
    stopped_turn = await ctf.claim_turn(stopped_tid, "lead", "runtime")
    await ctf.finish_turn(
        stopped_tid, "lead", stopped_turn["id"], stopped_turn["generation"], "context_limit"
    )
    await ctf.request_finish(
        stopped_tid,
        "user",
        str(uuid4()),
        {"end_reason": "user_stop", "summary": "Stop"},
    )
    with pytest.raises(HTTPException, match="Task is not running"):
        await ctf.enqueue_continuation(
            stopped_tid, "lead", stopped_turn["id"], stopped_turn["generation"]
        )

    budget_tid = await team(ctf)
    budget_message = await ctf.post_message(
        budget_tid, "user", "lead", "Use the budget", str(uuid4()), "instruction"
    )
    budget_turn = await ctf.claim_turn(budget_tid, "lead", "runtime")
    await ctf.finish_turn(
        budget_tid, "lead", budget_turn["id"], budget_turn["generation"], "context_limit"
    )
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks).where(s.tasks.c.id == budget_tid).values(usage={"cost": "10"})
        )
    with pytest.raises(HTTPException, match="Task budget exhausted"):
        await ctf.enqueue_continuation(
            budget_tid, "lead", budget_turn["id"], budget_turn["generation"]
        )
    assert budget_message["id"] != stop_message["id"]


async def test_long_settlement_and_private_reply_do_not_lose_lead_result(ctf):
    tid = await team(ctf)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(ctf_options={"max_teammates": 2, "max_steps": 60, "context_threshold": 128000})
        )
    alice = await ctf.create_member(tid, "user", "Alice", str(uuid4()))
    bob = await ctf.create_member(tid, "user", "Bob", str(uuid4()))
    mid = str(uuid4())
    await ctf.post_message(tid, "user", alice["id"], "Investigate", mid, "instruction")
    turn = await ctf.claim_turn(tid, alice["id"], "runtime")
    messages = await ctf.claim_messages(tid, alice["id"], turn["id"], turn["generation"])
    await ctf.checkpoint(
        tid,
        alice["id"],
        turn["id"],
        turn["generation"],
        history(messages),
        "prompt",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
    )
    await ctf.post_message(
        tid,
        alice["id"],
        bob["id"],
        "Private reply",
        str(uuid4()),
        reply_to=mid,
        generation=turn["generation"],
        turn_id=turn["id"],
    )
    answer = "Large final output\n" * 2000
    await ctf.finish_turn(tid, alice["id"], turn["id"], turn["generation"], "completed", answer)
    state = await ctf.state(tid)
    result = next(m for m in state["messages"] if m["notification_purpose"] == "assignment_result")
    assert len(result["body"]) <= 20000
    assert "Large final output" in result["body"]
    assert (await ctf.turn_result(tid, turn["id"], "lead"))["final_answer"] == answer
    with pytest.raises(HTTPException):
        await ctf.turn_result(tid, turn["id"], bob["id"])


async def test_unconfirmed_final_checkpoint_blocks_closing_after_recovery(ctf):
    tid = await team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "runtime")
    await ctf.request_finish(
        tid,
        "lead",
        str(uuid4()),
        {"end_reason": "goal_claimed", "summary": "Done", "lead_claim": True},
        generation=turn["generation"],
        turn_id=turn["id"],
    )
    await ctf.checkpoint_failed(tid, "lead", turn["id"], turn["generation"])
    await ctf.recover(tid)
    with pytest.raises(HTTPException, match="checkpoint"):
        await ctf.finalize_close(tid, True)
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["phase"] == "closing"
    assert state["task"]["ctf_control"]["unresolved_checkpoints"] == [turn["id"]]


async def test_initial_workflow_is_delivered_with_source_and_survives_start(ctf):
    import json

    goal = "Perform offline analysis. " * 1200
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": goal,
            "domain_context": "Use the custom workflow, without a flag submission.",
            "completion_requirements": "Deliver analysis and reproduction instructions.",
            "budget": {"max_cost": "10", "max_minutes": 60},
        }
    )
    await ctf.provision(tid)
    await ctf.start(tid)
    state = await ctf.state(tid)
    assert (
        state["task"]["ctf_control"]["completion_requirements"]
        == "Deliver analysis and reproduction instructions."
    )
    turn = await ctf.claim_turn(tid, "lead", "runtime")
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    assert len(messages) > 1
    assert all(m["sender_kind"] == "user" and m["kind"] == "instruction" for m in messages)
    restored = json.loads("".join(m["body"].split("\n", 1)[1] for m in messages))
    assert restored["goal"] == goal
    assert "custom workflow" in restored["domain_context"]
    assert "reproduction instructions" in restored["completion_requirements"]


class ObservationObjects:
    def __init__(self):
        self.files = {}
        self.after_upload: Callable[[], Awaitable[None]] | None = None

    async def put(self, uri, data, content_type="application/octet-stream"):
        assert content_type == "application/json"
        self.files[uri] = data
        if self.after_upload:
            await self.after_upload()

    async def exists(self, uri):
        return uri in self.files


async def test_observations_are_persistent_private_idempotent_and_fenced(ctf):
    import json

    objects = ObservationObjects()
    ctf.objects = objects
    tid = await team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "runtime")
    args = (tid, "lead", turn["id"], turn["generation"])
    first = await ctf.record_observation(*args, "initial_context", {"instructions": "Lead"}, "one")
    assert (
        await ctf.record_observation(*args, "initial_context", {"instructions": "Changed"}, "two")
        == first
    )
    assert len(objects.files) == 1
    tool = {"tool": "execute_command", "arguments": {"command": "echo fake"}, "result": "x" * 25000}
    saved = await ctf.record_observation(*args, "tool_result", tool, "call-id")
    assert await ctf.record_observation(*args, "tool_result", tool, "call-id") == saved
    assert json.loads(objects.files[saved["uri"]]) == tool
    assert await ctf.authorize_object(tid, "lead", saved["uri"])
    assert not await ctf.authorize_object(tid, "member-1", saved["uri"])
    async with ctf.repo.engine.connect() as conn:
        log = (
            (await conn.execute(select(s.tool_calls).where(s.tool_calls.c.task_id == tid)))
            .mappings()
            .one()
        )
        assert log["result_uri"] == saved["uri"]
    diagnostic = {
        "category": "invalid_response",
        "event_type": "response.incomplete",
        "incomplete_reason": "max_output_tokens",
    }
    error = await ctf.record_observation(*args, "model_error", diagnostic, "model-error-1")
    assert await ctf.record_observation(*args, "model_error", diagnostic, "model-error-1") == error
    assert json.loads(objects.files[error["uri"]]) == diagnostic
    assert await ctf.authorize_object(tid, "lead", error["uri"])
    assert not await ctf.authorize_object(tid, "member-1", error["uri"])
    await ctf.repo.replay(tid)

    async def revoke():
        await ctf.recover(tid)

    objects.after_upload = revoke
    with pytest.raises(HTTPException, match="Stale"):
        await ctf.record_observation(*args, "model_output", {"answer": "late"}, "late")
    assert not await ctf.authorize_object(tid, "lead", list(objects.files)[-1])
    count = len(objects.files)
    with pytest.raises(HTTPException, match="Stale"):
        await ctf.record_observation(*args, "model_output", {"answer": "late"}, "later")
    assert len(objects.files) == count


async def test_member_stop_resume_remove_requires_matching_drain_and_keeps_history(ctf):
    tid = await team(ctf)
    member = await ctf.create_member(tid, "user", "Alice", str(uuid4()))
    aid = member["id"]
    mid = str(uuid4())
    await ctf.post_message(tid, "user", aid, "Investigate", mid, "instruction")
    turn = await ctf.claim_turn(tid, aid, "runtime")
    await ctf.record_execution(tid, aid, turn["id"], turn["generation"], "boot-one")
    stop_id = str(uuid4())
    stopping = await ctf.request_member_operation(tid, "user", aid, "stop", stop_id)
    assert stopping["run_state"] == "stopping"
    assert (await ctf.request_member_operation(tid, "user", aid, "stop", stop_id))[
        "pending_operation"
    ] == stopping["pending_operation"]
    with pytest.raises(HTTPException):
        await ctf.authorize_member(tid, aid, turn["generation"], turn["id"])
    assert await ctf.claim_messages(tid, aid, turn["id"], turn["generation"]) == []
    await ctf.checkpoint(tid, aid, turn["id"], turn["generation"], history([]), "prompt", 0, [])
    await ctf.bill_usage(tid, aid, turn["id"], turn["generation"], "final", {"cost": "0.1"})
    await ctf.recover(tid)
    with pytest.raises(HTTPException):
        await ctf.finish_turn(tid, aid, turn["id"], turn["generation"], "stopped")
    with pytest.raises(HTTPException):
        await ctf.complete_member_operation(
            tid,
            aid,
            stop_id,
            {"boot_id": "new-boot", "generation": turn["generation"], "drained": True},
        )
    with pytest.raises(HTTPException):
        await ctf.create_member(tid, "user", "Bob", str(uuid4()))
    proof = {"boot_id": "boot-one", "generation": turn["generation"], "drained": True}
    stopped = await ctf.complete_member_operation(tid, aid, stop_id, proof)
    assert stopped["lifecycle"] == "stopped"
    assert await ctf.claim_turn(tid, aid, "runtime") is None
    await ctf.post_message(tid, "user", aid, "Queued while stopped", str(uuid4()))
    with pytest.raises(HTTPException):
        await ctf.create_member(tid, "user", "Bob", str(uuid4()))
    await ctf.request_member_operation(tid, "user", aid, "resume", str(uuid4()))
    resumed = await ctf.claim_turn(tid, aid, "runtime")
    assert resumed["generation"] > turn["generation"]
    await ctf.record_execution(tid, aid, resumed["id"], resumed["generation"], "boot-one")
    remove_id = str(uuid4())
    await ctf.request_member_operation(tid, "user", aid, "remove", remove_id)
    removed = await ctf.complete_member_operation(
        tid,
        aid,
        remove_id,
        {"boot_id": "boot-one", "generation": resumed["generation"], "drained": True},
    )
    assert removed["lifecycle"] == "removed"
    assert (await ctf.complete_member_operation(tid, aid, remove_id, {}))["lifecycle"] == "removed"
    with pytest.raises(HTTPException):
        await ctf.post_message(tid, "user", aid, "Too late", str(uuid4()))
    with pytest.raises(HTTPException):
        await ctf.create_member(tid, "user", "Alice", str(uuid4()))
    assert (await ctf.create_member(tid, "user", "Bob", str(uuid4())))["id"] != aid
    state = await ctf.state(tid)
    assert all(m["status"] == "cancelled" for m in state["messages"] if m["recipient_id"] == aid)
    assert await ctf.sessions.get_session(tid, aid)


async def test_idle_generation_zero_stop_and_old_boot_registration(ctf):
    tid = await team(ctf)
    aid = (await ctf.create_member(tid, "user", "Alice", str(uuid4())))["id"]
    op = str(uuid4())
    await ctf.request_member_operation(tid, "user", aid, "stop", op)
    await ctf.record_execution(tid, aid, None, 0, "boot-one")
    with pytest.raises(HTTPException):
        await ctf.record_execution(tid, aid, None, 0, "boot-two")
    await ctf.complete_member_operation(
        tid, aid, op, {"boot_id": "boot-one", "generation": 0, "drained": True}
    )
    remove_id = str(uuid4())
    pending = await ctf.request_member_operation(tid, "user", aid, "remove", remove_id)
    assert pending["pending_operation"]["target_generation"] == 0
    await ctf.complete_member_operation(
        tid, aid, remove_id, {"boot_id": "boot-one", "generation": 0, "drained": True}
    )


async def test_execution_replacement_is_ordered_frozen_and_keeps_destroy_evidence(ctf):
    tid = await team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "runtime")
    await ctf.record_execution(tid, "lead", turn["id"], turn["generation"], "old-boot")
    identity = {"old_container_id": "exact-container", "old_boot_id": "old-boot"}
    await ctf.execution_replacement(tid, phase="begin", **identity)
    with pytest.raises(HTTPException):
        await ctf.authorize_member(tid, "lead", turn["generation"], turn["id"])
    await ctf.checkpoint(tid, "lead", turn["id"], turn["generation"], history([]), "prompt", 0, [])
    with pytest.raises(HTTPException):
        await ctf.execution_replacement(tid, phase="destroyed", **identity)
    with pytest.raises(HTTPException):
        await ctf.execution_replacement(
            tid, phase="archive_saved", archive_uri="workspace/another/archive.tar.zst", **identity
        )
    uri = f"workspace/{tid}/replacement-exact-container.tar.zst"
    saved = await ctf.execution_replacement(tid, phase="archive_saved", archive_uri=uri, **identity)
    assert (
        await ctf.execution_replacement(tid, phase="archive_saved", archive_uri=uri, **identity)
        == saved
    )
    with pytest.raises(HTTPException):
        await ctf.execution_replacement(
            tid, phase="archive_saved", archive_uri=f"workspace/{tid}/different.tar.zst", **identity
        )
    await ctf.execution_replacement(tid, phase="destroyed", **identity)
    with pytest.raises(HTTPException):
        await ctf.execution_replacement(tid, phase="ready", new_boot_id="old-boot", **identity)
    ready = await ctf.execution_replacement(tid, phase="ready", new_boot_id="new-boot", **identity)
    assert (await ctf.execution_replacement(tid, phase="begin", **identity)) == ready
    state = await ctf.state(tid)
    member = next(m for m in state["members"] if m["id"] == "lead")
    assert member["execution"]["drained"]
    assert member["execution"]["container_destroyed"] == "exact-container"
    assert member["execution"]["replacement_boot_id"] == "new-boot"
    with pytest.raises(HTTPException):
        await ctf.authorize_member(tid, "lead", turn["generation"], turn["id"])
    await ctf.recover(tid)
    with pytest.raises(HTTPException):
        await ctf.checkpoint(
            tid, "lead", turn["id"], turn["generation"], history([]), "prompt", 1, []
        )


async def test_recovery_wakes_lead_once_after_delivered_input(ctf):
    tid = await team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "runtime")
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    await ctf.checkpoint(
        tid,
        "lead",
        turn["id"],
        turn["generation"],
        history(messages),
        "prompt",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
    )
    await ctf.recover(tid)
    await ctf.recover(tid)
    state = await ctf.state(tid)
    notices = [
        m for m in state["messages"] if m["sender_kind"] == "system" and m["kind"] == "instruction"
    ]
    assert len(notices) == 1
    assert turn["id"] in notices[0]["body"]
    assert "Do not replay tools" in notices[0]["body"]
    resumed = await ctf.claim_turn(tid, "lead", "new-runtime")
    assert resumed is not None
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Stop"}
    )
    await ctf.recover(tid)
    state = await ctf.state(tid)
    assert (
        len(
            [
                m
                for m in state["messages"]
                if m["sender_kind"] == "system" and m["kind"] == "instruction"
            ]
        )
        == 1
    )


class ArtifactObjects(ObservationObjects):
    async def get(self, uri):
        return self.files[uri]


async def board_team(ctf):
    tid = await team(ctf)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks).where(s.tasks.c.id == tid).values(ctf_options={"max_teammates": 2})
        )
    actors = []
    for name in ("Owner", "Helper"):
        member = await ctf.create_member(tid, "user", name, str(uuid4()))
        aid = member["id"]
        await ctf.post_message(tid, "user", aid, "Work", str(uuid4()), "instruction")
        turn = await ctf.claim_turn(tid, aid, "runtime")
        actors.append({"actor": aid, "generation": turn["generation"], "turn_id": turn["id"]})
    return tid, *actors


async def test_challenge_cas_owner_help_and_shared_records(ctf):
    tid, owner, helper = await board_team(ctf)
    challenge = await ctf.create_challenge(
        tid, **owner, request_id=str(uuid4()), title="Offline analysis"
    )
    cid = challenge["id"]
    request = str(uuid4())
    claimed = await ctf.update_challenge(
        tid, **owner, challenge_id=cid, request_id=request, expected_revision=1, action="claim"
    )
    assert (
        await ctf.update_challenge(
            tid, **owner, challenge_id=cid, request_id=request, expected_revision=1, action="claim"
        )
        == claimed
    )
    with pytest.raises(HTTPException) as conflict:
        await ctf.update_challenge(
            tid,
            **helper,
            challenge_id=cid,
            request_id=str(uuid4()),
            expected_revision=1,
            action="claim",
        )
    assert conflict.value.detail == {"code": "revision_conflict", "current_revision": 2}
    with pytest.raises(HTTPException):
        await ctf.append_record(
            tid, **helper, challenge_id=cid, request_id=str(uuid4()), body="Unauthorized"
        )
    challenge = await ctf.update_challenge(
        tid,
        actor="user",
        challenge_id=cid,
        request_id=str(uuid4()),
        expected_revision=2,
        action="collaborators",
        collaborator_ids=[helper["actor"]],
    )
    note = await ctf.append_record(
        tid,
        **helper,
        challenge_id=cid,
        request_id=str(uuid4()),
        body="Observed a failure condition",
    )
    assert note["author_id"] == helper["actor"]
    with pytest.raises(HTTPException):
        await ctf.update_challenge(
            tid,
            **helper,
            challenge_id=cid,
            request_id=str(uuid4()),
            expected_revision=3,
            action="set_status",
            work_status="completed",
        )
    help_args = dict(
        challenge_id=cid,
        request_id=str(uuid4()),
        expected_revision=3,
        body="Need another approach",
        attempted_routes="Read headers",
        observations_and_basis="Header mismatch",
        failure_conditions="No valid header",
        current_blocker="Unknown format",
        help_needed="Inspect format",
        no_artifacts_reason="No scripts yet; only inline inspection",
    )
    help_record = await ctf.request_help(tid, **owner, **help_args)
    assert await ctf.request_help(tid, **owner, **help_args) == help_record
    state = await ctf.state(tid)
    challenge = await ctf.get_challenge(tid, cid)
    assert challenge["owner_id"] == owner["actor"] and challenge["work_status"] == "blocked"
    assert len([m for m in state["messages"] if m["kind"] == "help_request"]) == 1
    assert len((await ctf.list_records(tid, cid))["records"]) == 2
    stale_help = {**help_args, "request_id": str(uuid4())}
    with pytest.raises(HTTPException):
        await ctf.request_help(tid, **owner, **stale_help)
    assert len((await ctf.list_records(tid, cid))["records"]) == 2
    completed = await ctf.update_challenge(
        tid,
        **owner,
        challenge_id=cid,
        request_id=str(uuid4()),
        expected_revision=4,
        action="set_status",
        work_status="completed",
    )
    assert completed["verification"] == {}
    assert completed["owner_id"] == owner["actor"]
    await ctf.repo.replay(tid)
    assert await ctf.get_challenge(tid, cid) == completed


async def test_artifact_registry_checks_content_and_keeps_versions(ctf):
    import hashlib

    tid, owner, helper = await board_team(ctf)
    ctf.objects = ArtifactObjects()
    path = "/workspace/shared/ctf/script.py"
    refs = []
    for content in (b"print(1)", b"print(2)"):
        request = str(uuid4())
        uri = f"evidence/{tid}/{request}/script.py"
        ctf.objects.files[uri] = content
        args = dict(
            agent_id=owner["actor"],
            turn_id=owner["turn_id"],
            generation=owner["generation"],
            request_id=request,
            path=path,
            uri=uri,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            filename="script.py",
        )
        registered = await ctf.register_artifact(tid, **args)
        assert await ctf.register_artifact(tid, **args) == registered
        assert await ctf.lookup_artifact(tid, owner["actor"], request) == registered
        assert await ctf.authorize_object(tid, helper["actor"], uri)
        refs.append(registered)
    assert refs[1]["created_version"] > refs[0]["created_version"]
    assert (await ctf.state(tid))["artifacts"] == refs
    with pytest.raises(HTTPException):
        await ctf.register_artifact(tid, **{**args, "request_id": str(uuid4()), "sha256": "0" * 64})
    challenge = await ctf.create_challenge(
        tid, **owner, request_id=str(uuid4()), title="With artifacts"
    )
    await ctf.update_challenge(
        tid,
        **owner,
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=1,
        action="claim",
    )
    record = await ctf.append_record(
        tid,
        **owner,
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        body="Reproduction script",
        artifact_ids=[refs[0]["id"]],
    )
    assert record["artifact_refs"] == [refs[0]]
    with pytest.raises(HTTPException):
        await ctf.append_record(
            tid,
            **owner,
            challenge_id=challenge["id"],
            request_id=str(uuid4()),
            body="Unknown artifact",
            artifact_ids=[str(uuid4())],
        )
    other = await team(ctf)
    assert not await ctf.authorize_shared_artifact(other, refs[0]["uri"])


async def test_challenge_close_reopen_delete_and_finish_gate(ctf):
    tid, owner, _ = await board_team(ctf)
    lead = await ctf.claim_turn(tid, "lead", "runtime")
    conclusion = {"end_reason": "goal_claimed", "summary": "Complete", "lead_claim": True}
    challenge = await ctf.create_challenge(
        tid, actor="user", request_id=str(uuid4()), title="Required work"
    )
    cid = challenge["id"]
    with pytest.raises(HTTPException, match="Unfinished"):
        await ctf.request_finish(
            tid, "lead", str(uuid4()), conclusion, lead["generation"], lead["id"]
        )
    challenge = await ctf.update_challenge(
        tid, **owner, challenge_id=cid, request_id=str(uuid4()), expected_revision=1, action="claim"
    )
    challenge = await ctf.update_challenge(
        tid,
        **owner,
        challenge_id=cid,
        request_id=str(uuid4()),
        expected_revision=2,
        action="release",
    )
    assert challenge["owner_id"] is None and challenge["work_status"] == "pending"
    challenge = await ctf.update_challenge(
        tid,
        actor="user",
        challenge_id=cid,
        request_id=str(uuid4()),
        expected_revision=3,
        action="assign",
        owner_id=owner["actor"],
    )
    challenge = await ctf.update_challenge(
        tid,
        **owner,
        challenge_id=cid,
        request_id=str(uuid4()),
        expected_revision=4,
        action="set_status",
        work_status="completed",
    )
    with pytest.raises(HTTPException):
        await ctf.request_help(
            tid,
            **owner,
            challenge_id=cid,
            request_id=str(uuid4()),
            expected_revision=5,
            body="Retry",
            attempted_routes="Inspect",
            observations_and_basis="Observed",
            failure_conditions="Unknown",
            current_blocker="Blocked",
            help_needed="Help",
            no_artifacts_reason="No files",
        )
    challenge = await ctf.update_challenge(
        tid,
        **owner,
        challenge_id=cid,
        request_id=str(uuid4()),
        expected_revision=5,
        action="reopen",
    )
    assert challenge["owner_id"] == owner["actor"]
    with pytest.raises(HTTPException):
        await ctf.update_challenge(
            tid,
            **owner,
            challenge_id=cid,
            request_id=str(uuid4()),
            expected_revision=6,
            action="delete",
        )
    await ctf.update_challenge(
        tid,
        actor="user",
        challenge_id=cid,
        request_id=str(uuid4()),
        expected_revision=6,
        action="delete",
    )
    assert (await ctf.get_challenge(tid, cid))["tombstone"]
    assert (
        await ctf.request_finish(
            tid, "lead", str(uuid4()), conclusion, lead["generation"], lead["id"]
        )
    )["phase"] == "closing"


async def platform_profile(ctf, tid):
    from bbx_blackboard.profiles import _content
    from bbx_contracts.ctf import CtfAgentProfile, load_ctf_profile
    from sqlalchemy import insert

    data = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf").model_dump(
        mode="json"
    )
    for role in ("lead", "teammate"):
        data["worker_tools"][role]["mcp_servers"] = [
            {
                "name": "fake-platform",
                "version": 1,
                "allowed_tools": ["submit", "status", "start"]
                if role == "lead"
                else ["submit", "status"],
            }
        ]
    data["platform_tools"] = [
        {
            "server_name": "fake-platform",
            "server_version": 1,
            "tool_name": tool,
            "purpose": purpose,
            "result_adapter": "fake_ctf_v1",
            "read_only": tool == "status",
        }
        for tool, purpose in (("start", "management"), ("submit", "submit"), ("status", "status"))
    ]
    profile = CtfAgentProfile.model_validate(data)
    name = f"ctf-test-{uuid4()}"
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            insert(s.agent_profiles).values(name=name, version=1, **_content(profile))
        )
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(agent_profile=name, agent_profile_version=1)
        )


async def test_platform_verification_provenance_hash_and_cas(ctf):
    import hashlib
    import json

    tid, owner, helper = await board_team(ctf)
    await platform_profile(ctf, tid)
    ctf.objects = ArtifactObjects()
    challenge = await ctf.create_challenge(
        tid, **owner, request_id=str(uuid4()), title="Submission"
    )
    cid = challenge["id"]
    await ctf.update_challenge(
        tid, **owner, challenge_id=cid, request_id=str(uuid4()), expected_revision=1, action="claim"
    )
    await ctf.set_verification_required(
        tid, "user", cid, str(uuid4()), 2, True, "User workflow requires submission"
    )
    candidate = await ctf.record_candidate(
        tid,
        **owner,
        challenge_id=cid,
        request_id=str(uuid4()),
        expected_revision=3,
        status="candidate",
        summary="Found candidate",
    )
    assert candidate["verification"]["source"] == "agent"
    identity = dict(
        agent_id=owner["actor"],
        turn_id=owner["turn_id"],
        generation=owner["generation"],
        challenge_id=cid,
        server_name="fake-platform",
        server_version=1,
        tool_name="submit",
    )
    with pytest.raises(HTTPException):
        await ctf.authorize_platform_call(
            tid, **{**identity, "tool_name": "start"}, expected_revision=4
        )
    with pytest.raises(HTTPException):
        await ctf.authorize_platform_call(
            tid,
            **{**identity, "agent_id": helper["actor"], "turn_id": helper["turn_id"]},
            expected_revision=4,
        )
    for revision, status in ((4, "rejected"), (5, "accepted"), (6, "unknown")):
        await ctf.authorize_platform_call(tid, **identity, expected_revision=revision)
        request_id = str(uuid4())
        uri = f"evidence/{tid}/{request_id}/response.json"
        content = {
            "structuredContent": {
                "status": status,
                "target_id": "fake-target",
                "submission_id": request_id,
            }
        }
        if status == "unknown":
            content = {"status": "unknown", "error": "transport outcome unknown"}
        raw = json.dumps(content).encode()
        ctf.objects.files[uri] = raw
        args = dict(
            **identity,
            expected_revision=revision,
            request_id=request_id,
            call_id=str(uuid4()),
            response_uri=uri,
            response_sha256=hashlib.sha256(raw).hexdigest(),
        )
        await ctf.begin_platform_call(
            tid, **identity, expected_revision=revision, call_id=args["call_id"]
        )
        with pytest.raises(HTTPException):
            await ctf.record_platform_result(tid, **{**args, "response_sha256": "0" * 64})
        record = await ctf.record_platform_result(tid, **args)
        assert await ctf.record_platform_result(tid, **args) == record
        assert record["verification"]["status"] == status
        assert record["verification"]["source"] == "platform"
        assert record["verification"]["test_only"] is True
        assert await ctf.authorize_object(tid, helper["actor"], uri)
    confirmed = await ctf.manual_verification(
        tid, "alice", cid, str(uuid4()), 7, "accepted", "Checked independently"
    )
    assert confirmed["verification"]["source"] == "user"
    assert confirmed["verification"]["user_id"] == "alice"
    # A late platform response is still recorded, but cannot overwrite newer user evidence.
    late = await ctf.record_platform_result(tid, **{**args, "request_id": str(uuid4())})
    assert not late["summary_applied"]
    assert (await ctf.get_challenge(tid, cid))["verification"]["source"] == "user"


async def test_dispatched_platform_result_settles_after_closing_or_stopping(ctf):
    import hashlib
    import json

    for task_closing in (False, True):
        tid, owner, _ = await board_team(ctf)
        await platform_profile(ctf, tid)
        ctf.objects = ArtifactObjects()
        challenge = await ctf.create_challenge(
            tid, **owner, request_id=str(uuid4()), title="In flight"
        )
        cid = challenge["id"]
        await ctf.update_challenge(
            tid,
            **owner,
            challenge_id=cid,
            request_id=str(uuid4()),
            expected_revision=1,
            action="claim",
        )
        identity = dict(
            agent_id=owner["actor"],
            turn_id=owner["turn_id"],
            generation=owner["generation"],
            challenge_id=cid,
            server_name="fake-platform",
            server_version=1,
            tool_name="submit",
            expected_revision=2,
        )
        call_id = str(uuid4())
        await ctf.begin_platform_call(tid, **identity, call_id=call_id)
        if task_closing:
            await ctf.request_finish(
                tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Stop"}
            )
        else:
            await ctf.request_member_operation(tid, "user", owner["actor"], "stop", str(uuid4()))
        with pytest.raises(HTTPException):
            await ctf.begin_platform_call(tid, **identity, call_id=str(uuid4()))
        request = str(uuid4())
        uri = f"evidence/{tid}/{request}/result.json"
        raw = json.dumps(
            {"status": "accepted", "target_id": "retained-external-id", "submission_id": call_id}
        ).encode()
        ctf.objects.files[uri] = raw
        result = await ctf.record_platform_result(
            tid,
            **identity,
            request_id=request,
            call_id=call_id,
            response_uri=uri,
            response_sha256=hashlib.sha256(raw).hexdigest(),
        )
        assert result["verification"]["external_ref"] == "retained-external-id"
        assert result["verification"]["status"] == "accepted"
        with pytest.raises(HTTPException):
            await ctf.record_platform_result(
                tid,
                **identity,
                request_id=str(uuid4()),
                call_id=str(uuid4()),
                response_uri=uri,
                response_sha256=hashlib.sha256(raw).hexdigest(),
            )


async def test_terminal_archive_resume_and_scoped_purge(ctf):
    class Objects:
        def __init__(self):
            self.files = {}

        async def put(self, key, data, **kwargs):
            self.files[key] = data

        async def exists(self, key):
            return key in self.files

        async def list(self, prefix):
            return [key for key in self.files if key.startswith(prefix)]

        async def remove(self, key):
            self.files.pop(key, None)

    objects = Objects()
    ctf.objects = objects
    tid = await team(ctf)
    other = await team(ctf)
    objects.files[f"evidence/{other}/keep/data"] = b"keep"
    turn = await ctf.claim_turn(tid, "lead", "archive-runtime")
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    await ctf.checkpoint(
        tid,
        "lead",
        turn["id"],
        turn["generation"],
        history(messages),
        "persisted prompt",
        0,
        [{"message_id": m["id"], "claim_token": m["claim_token"]} for m in messages],
    )
    await ctf.finish_turn(tid, "lead", turn["id"], turn["generation"], "completed", "Saved")
    await ctf.post_message(tid, "user", "lead", "Check this after resuming", str(uuid4()))
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Paused for review"}
    )
    await ctf.finalize_close(tid, True)
    request_id = uuid4()
    with pytest.raises(HTTPException):
        await ctf.resume(tid, request_id, "2", 5)
    snapshot = await ctf.export_archive(tid)
    assert snapshot["format"] == "bbx.task-archive.v1"
    assert snapshot["state"]["task"]["mode"] == "ctf"
    assert snapshot["messages"] and snapshot["state"]["task"]["ctf_conclusion"]
    uri = f"workspace/{tid}.tar.zst"
    objects.files[uri] = b"archive"
    await ctf.record_archive(tid, uri, 7)
    await ctf.record_cleanup(tid)
    before_archive_replay = await ctf.state(tid)
    await ctf.repo.replay(tid)
    assert await ctf.state(tid) == before_archive_replay
    assert before_archive_replay["task"]["ctf_control"]["cleanup"]["phase"] == "complete"
    await ctf.resume(tid, request_id, "2", 5)
    await ctf.resume(tid, request_id, "2", 5)
    with pytest.raises(HTTPException):
        await ctf.resume(tid, uuid4(), "2", 5)
    state = await ctf.state(tid)
    assert state["task"]["budget"]["max_cost"] == "12"
    assert state["task"]["run_number"] == 2
    async with ctf.repo.engine.connect() as conn:
        sessions_before = list(
            (
                await conn.execute(
                    select(s.agent_sessions).where(s.agent_sessions.c.task_id == tid)
                )
            ).mappings()
        )
    assert sessions_before
    await ctf.repo.replay(tid)
    assert await ctf.state(tid) == state
    assert state["task"]["ctf_conclusion"] is None
    async with ctf.repo.engine.connect() as conn:
        assert (
            list(
                (
                    await conn.execute(
                        select(s.agent_sessions).where(s.agent_sessions.c.task_id == tid)
                    )
                ).mappings()
            )
            == sessions_before
        )
    assert sum(not m["deferred"] for m in state["messages"] if m["status"] == "queued") == 1
    await ctf.start(tid)
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Delete"}
    )
    await ctf.finalize_close(tid, True)
    await ctf.sessions.request_delete(tid)
    await ctf.sessions.purge(tid, objects)
    assert list(objects.files) == [f"evidence/{other}/keep/data"]
    async with ctf.repo.engine.connect() as conn:
        for table in (
            s.ctf_members,
            s.ctf_turns,
            s.ctf_messages,
            s.ctf_challenges,
            s.ctf_records,
            s.agent_sessions,
            s.events,
        ):
            assert not (await conn.execute(select(table).where(table.c.task_id == tid))).first()
        assert (await conn.execute(select(s.tasks).where(s.tasks.c.id == other))).first()
