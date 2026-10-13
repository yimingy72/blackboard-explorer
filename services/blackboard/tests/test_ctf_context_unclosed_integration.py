"""Unclosed context decisions retain quotas, references and terminal diagnostics."""

import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import update
from test_ctf_context_recovery_control_integration import block, decisions
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_integration import team
from test_ctf_reliability_integration import idle_board
from test_ctf_review_integration import terminal

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def extended_block(ctf, tid, aid, revision):
    # The actual settlement reads the confirmed Session diagnostic.
    turn = await ctf.claim_turn(tid, aid, "context-unclosed")
    messages = await ctf.claim_messages(tid, aid, turn["id"], turn["generation"])
    from test_ctf_integration import history

    session: dict[str, Any] = history(messages)
    session["state"]["ctf_context_blocked"] = {
        "turn_id": turn["id"],
        "message_ids": [message["id"] for message in messages],
        "branch": "remaining_over_limit",
        "eligible": 2,
        "cut": 1,
        "estimated_input_tokens": 324150,
        "limit_tokens": 320000,
    }
    await ctf.checkpoint(tid, aid, turn["id"], turn["generation"], session, "prompt", revision, [])
    await ctf.finish_turn(tid, aid, turn["id"], turn["generation"], "context_blocked")
    return turn


async def test_exhaustion_notice_lists_context_branch_and_deferred_recovery_id_without_input_body(
    ctf,
):
    tid = await team(ctf)
    aid = (await ctf.create_member(tid, "user", "Owner", str(uuid4())))["id"]
    await ctf.post_message(
        tid, "user", aid, "Private synthetic input never in notice", str(uuid4()), "instruction"
    )
    await extended_block(ctf, tid, aid, 0)
    second = await extended_block(ctf, tid, aid, 1)
    state = await ctf.state(tid)
    recovery = next(
        message
        for message in state["messages"]
        if message["notification_purpose"] == "turn_recovery"
    )
    notice = next(
        message
        for message in state["messages"]
        if message["notification_purpose"] == "context_recovery_exhausted"
    )
    details = json.loads(notice["body"][notice["body"].index("{") :])
    assert details["blocked_members"] == [
        {
            "agent_id": aid,
            "branch": "remaining_over_limit",
            "eligible": 2,
            "cut": 1,
            "estimated_input_tokens": 324150,
            "limit_tokens": 320000,
        }
    ]
    assert recovery["id"] in [message["id"] for message in details["deferred_messages"]]
    assert "Private synthetic" not in notice["body"]
    assert len(notice["body"]) <= 20000
    assert (await decisions(ctf, tid))[-1]["status"] == "recovery_exhausted"
    await ctf.finish_turn(tid, aid, second["id"], second["generation"], "context_blocked")
    assert (
        len(
            [
                message
                for message in (await ctf.state(tid))["messages"]
                if message["notification_purpose"] == "context_recovery_exhausted"
            ]
        )
        == 1
    )


async def test_idle_notice_references_deferred_work_and_blocked_branch_without_releasing_it(ctf):
    tid, _ = await idle_board(ctf)
    aid = (await ctf.create_member(tid, "user", "Owner", str(uuid4())))["id"]
    message = await ctf.post_message(
        tid, "user", aid, "Private synthetic deferred body", str(uuid4()), "instruction"
    )
    state = await ctf.state(tid)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages)
            .where(s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == message["id"])
            .values(deferred=True)
        )
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **state["task"]["ctf_control"],
                    "context_blocked_members": {
                        aid: {
                            "branch": "remaining_over_limit",
                            "eligible": 0,
                            "cut": 0,
                            "estimated_input_tokens": 321000,
                            "limit_tokens": 320000,
                            "input_message_ids": [message["id"]],
                        }
                    },
                }
            )
        )
    instant = state["task"]["created_at"]
    assert (await ctf.idle_watchdog(tid, instant=instant))["action"] == "waiting"
    assert (await ctf.idle_watchdog(tid, instant=instant + timedelta(seconds=61)))[
        "action"
    ] == "nudged"
    state = await ctf.state(tid)
    notice = next(item for item in state["messages"] if item["body"].startswith("The team is idle"))
    assert message["id"] in notice["body"] and "remaining_over_limit" in notice["body"]
    assert "Private synthetic deferred body" not in notice["body"]
    assert next(item for item in state["messages"] if item["id"] == message["id"])["deferred"]


async def test_same_run_stop_resume_and_fresh_instruction_do_not_reset_context_allowance(ctf):
    tid = await team(ctf)
    aid = (await ctf.create_member(tid, "user", "Owner", str(uuid4())))["id"]
    await ctf.post_message(
        tid, "user", aid, "Synthetic original instruction", str(uuid4()), "instruction"
    )
    await block(ctf, tid, aid, 0)
    second = await block(ctf, tid, aid, 1)
    run_number = (await ctf.state(tid))["task"]["run_number"]
    await ctf.record_execution(tid, aid, None, second["generation"], "synthetic-boot")
    request = str(uuid4())
    await ctf.request_member_operation(tid, "user", aid, "stop", request)
    await ctf.complete_member_operation(
        tid,
        aid,
        request,
        {"boot_id": "synthetic-boot", "generation": second["generation"], "drained": True},
    )
    await ctf.request_member_operation(tid, "user", aid, "resume", str(uuid4()))
    await ctf.post_message(
        tid, "user", aid, "Smaller explicit instruction", str(uuid4()), "instruction"
    )
    await block(ctf, tid, aid, 2)
    state = await ctf.state(tid)
    assert state["task"]["run_number"] == run_number
    assert state["task"]["ctf_control"]["turn_retries"][aid]["context_blocked"] == 1
    assert (
        len(
            [
                message
                for message in state["messages"]
                if message["notification_purpose"] == "turn_recovery"
            ]
        )
        == 1
    )
    assert (await decisions(ctf, tid))[-1]["status"] == "recovery_exhausted"


async def test_confirmation_cannot_bypass_old_context_input_or_revive_cancelled_recovery(ctf):
    tid = await team(ctf)
    lead = await ctf.claim_turn(tid, "lead", "lead-authority")
    aid = (await ctf.create_member(tid, "user", "Owner", str(uuid4())))["id"]
    original = await ctf.post_message(
        tid, "user", aid, "Original synthetic instruction", str(uuid4()), "instruction"
    )
    await block(ctf, tid, aid, 0)
    state = await ctf.state(tid)
    recovery = next(
        message
        for message in state["messages"]
        if message["notification_purpose"] == "turn_recovery"
    )
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages)
            .where(s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == recovery["id"])
            .values(status="cancelled")
        )
    args = {"actor": "lead", "generation": lead["generation"], "turn_id": lead["id"]}
    await ctf.confirm_messages(tid, message_ids=[original["id"]], **args)
    assert await ctf.claim_turn(tid, aid, "context-unclosed") is None
    with pytest.raises(HTTPException):
        await ctf.confirm_messages(tid, message_ids=[recovery["id"]], **args)
    fresh = await ctf.post_message(
        tid, "user", aid, "Smaller fresh input", str(uuid4()), "instruction"
    )
    turn = await ctf.claim_turn(tid, aid, "context-unclosed")
    claimed = await ctf.claim_messages(tid, aid, turn["id"], turn["generation"])
    assert [message["id"] for message in claimed] == [fresh["id"]]
    state = await ctf.state(tid)
    assert (
        next(message for message in state["messages"] if message["id"] == recovery["id"])["status"]
        == "cancelled"
    )
    assert next(message for message in state["messages"] if message["id"] == original["id"])[
        "deferred"
    ]


async def test_terminal_cleanup_is_idempotent_preserves_history_and_counts_and_does_not_reopen(
    ctf, monkeypatch
):
    tid = await team(ctf)
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "partial", "summary": "Synthetic stop"}
    )
    await ctf.finalize_close(tid, True)
    state = await ctf.state(tid)
    control = {
        **state["task"]["ctf_control"],
        "turn_retries": {"lead": {"context_blocked": 1}},
        "context_blocked_members": {
            "lead": {
                "turn_id": "legacy-turn",
                "branch": "legacy",
                "message_ids": ["input", None, ""],
                "input_message_ids": [None, "input"],
            }
        },
        "context_blocked_history": {"older-turn": {"branch": "old-history"}},
    }
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(ctf_control=control))
    assert await ctf.cleanup_terminal_context_blocks(tid) == {
        "task_id": str(tid),
        "changed": True,
        "cleared_members": 1,
    }
    after = await ctf.state(tid)
    archived = after["task"]["ctf_control"]
    assert archived["context_blocked_members"] == {}
    assert archived["context_blocked_history"]["legacy-turn"]["message_ids"] == ["input"]
    assert (
        archived["context_blocked_history"]["older-turn"]
        == control["context_blocked_history"]["older-turn"]
    )
    assert archived["turn_retries"] == control["turn_retries"]
    assert after["task"]["status"] == state["task"]["status"]
    assert after["messages"] == state["messages"] and after["turns"] == state["turns"]
    before_events = len(await decisions(ctf, tid))
    from unittest.mock import AsyncMock

    from bbx_blackboard import ctf as module

    patched = AsyncMock(wraps=module.patch)
    monkeypatch.setattr(module, "patch", patched)
    assert not (await ctf.cleanup_terminal_context_blocks(tid))["changed"]
    patched.assert_not_awaited()
    assert len(await decisions(ctf, tid)) == before_events


async def test_terminal_cleanup_rejects_running_task_and_real_active_review(ctf):
    tid = await team(ctf)
    with pytest.raises(HTTPException, match="terminal"):
        await ctf.cleanup_terminal_context_blocks(tid)
    closed, aid = await terminal(ctf)
    await ctf.post_message(closed, "user", aid, "Explain synthetic saved work", str(uuid4()))
    review = await ctf.claim_review_turn(closed, aid, "cleanup-review")
    assert review is not None
    with pytest.raises(HTTPException, match="active execution or review"):
        await ctf.cleanup_terminal_context_blocks(closed)


async def test_recovery_diagnostics_bound_ids_and_ignore_non_numeric_or_unknown_branch_content(ctf):
    tid, _ = await idle_board(ctf)
    aid = (await ctf.create_member(tid, "user", "Owner", str(uuid4())))["id"]
    for _ in range(55):
        await ctf.post_message(
            tid, "user", aid, "Private synthetic body", str(uuid4()), "instruction"
        )
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages)
            .where(s.ctf_messages.c.task_id == tid, s.ctf_messages.c.recipient_id == aid)
            .values(deferred=True)
        )
        summary = await ctf._recovery_diagnostics(
            conn,
            tid,
            {
                "context_blocked_members": {
                    aid: {
                        "branch": {"private": "synthetic"},
                        "eligible": "private",
                        "cut": -1,
                        "estimated_input_tokens": 321000,
                        "limit_tokens": 320000,
                    }
                }
            },
        )
    assert len(summary["deferred_messages"]) == 50
    assert summary["deferred_messages_truncated"] is True
    assert summary["blocked_members"][0]["branch"] == "unknown"
    assert summary["blocked_members"][0]["eligible"] is None
    assert summary["blocked_members"][0]["cut"] is None
    assert "Private synthetic body" not in json.dumps(summary)


async def test_lead_confirmation_releases_deferred_recovery_not_in_old_blocked_input_ids(ctf):
    tid = await team(ctf)
    lead = await ctf.claim_turn(tid, "lead", "lead-confirmation")
    aid = (await ctf.create_member(tid, "user", "Owner", str(uuid4())))["id"]
    await ctf.post_message(
        tid, "user", aid, "Synthetic original input", str(uuid4()), "instruction"
    )
    await block(ctf, tid, aid, 0)
    state = await ctf.state(tid)
    recovery = next(
        message
        for message in state["messages"]
        if message["notification_purpose"] == "turn_recovery"
    )
    async with ctf.repo.engine.begin() as conn:
        await ctf._task(conn, tid)
        await ctf._defer_inputs(conn, tid, aid, [recovery["id"]], "lead_review_required")
    assert (await decisions(ctf, tid))[-1]["status"] == "recovery_deferred"
    assert await ctf.claim_turn(tid, aid, "context-unclosed") is None
    await ctf.confirm_messages(
        tid,
        actor="lead",
        message_ids=[recovery["id"]],
        generation=lead["generation"],
        turn_id=lead["id"],
    )
    turn = await ctf.claim_turn(tid, aid, "context-unclosed")
    messages = await ctf.claim_messages(tid, aid, turn["id"], turn["generation"])
    assert [message["id"] for message in messages] == [recovery["id"]]
    control = (await ctf.state(tid))["task"]["ctf_control"]
    assert control["turn_retries"][aid]["context_blocked"] == 1
