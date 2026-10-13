"""Context recovery is bounded, audited, and never replays deferred inputs."""

from typing import Any
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from sqlalchemy import select, update
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_integration import history, team

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def block(ctf, tid, aid, revision):
    turn = await ctf.claim_turn(tid, aid, "context-control")
    assert turn is not None
    messages = await ctf.claim_messages(tid, aid, turn["id"], turn["generation"])
    session: dict[str, Any] = history(messages)
    session["state"]["ctf_context_blocked"] = {
        "turn_id": turn["id"],
        "message_ids": [*[m["id"] for m in messages], None, ""],
        "branch": "protected_input",
        "eligible": 0,
    }
    await ctf.checkpoint(tid, aid, turn["id"], turn["generation"], session, "prompt", revision, [])
    await ctf.finish_turn(tid, aid, turn["id"], turn["generation"], "context_blocked")
    return turn


async def decisions(ctf, tid):
    async with ctf.repo.engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    select(s.events.c.payload)
                    .where(
                        s.events.c.task_id == tid, s.events.c.type == "ctf.coordination.reminded"
                    )
                    .order_by(s.events.c.version)
                )
            ).scalars()
        )


@pytest.mark.parametrize("lead", [False, True])
async def test_context_exhaustion_notifies_once_without_reset_or_self_wakeup(ctf, lead):
    tid = await team(ctf)
    aid = "lead" if lead else (await ctf.create_member(tid, "user", "Blocked", str(uuid4())))["id"]
    if not lead:
        await ctf.post_message(
            tid, "user", aid, "Investigate fake input", str(uuid4()), "instruction"
        )
    await block(ctf, tid, aid, 0)
    initial = (await ctf.state(tid))["task"]["ctf_control"]["context_blocked_members"][aid]
    assert None not in initial["message_ids"] and "" not in initial["message_ids"]
    assert None not in initial["input_message_ids"]
    turn = await block(ctf, tid, aid, 1)
    await ctf.finish_turn(tid, aid, turn["id"], turn["generation"], "context_blocked")
    state = await ctf.state(tid)
    notifications = [
        m for m in state["messages"] if m["notification_purpose"] == "context_recovery_exhausted"
    ]
    assert len(notifications) == 1
    assert notifications[0]["recipient_id"] == "lead"
    assert notifications[0]["deferred"] is lead
    assert aid not in (await ctf.runtime_state(tid))["ready_member_ids"]
    events = await decisions(ctf, tid)
    assert [item["status"] for item in events] == [
        "recovery_requested",
        "recovery_deferred",
        "recovery_exhausted",
    ]
    assert events[1]["reason"] == "context_blocked"
    assert events[-1]["reason"] == "retry_limit" and events[-1]["limit"] == 1
    assert len(state["task"]["ctf_control"]["context_blocked_history"]) == 2
    await ctf.post_message(
        tid, "user", aid, "Try a smaller explicit input", str(uuid4()), "instruction"
    )
    await block(ctf, tid, aid, 2)
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["turn_retries"][aid]["context_blocked"] == 1
    assert len([m for m in state["messages"] if m["notification_purpose"] == "turn_recovery"]) == 1


@pytest.mark.parametrize(
    "reason", ["lifecycle", "pending_operation", "phase", "replacement", "budget"]
)
async def test_declined_recovery_has_auditable_reason_without_spending_allowance(ctf, reason):
    tid = await team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "context-control")
    state = await ctf.state(tid)
    member = state["members"][0]
    task = state["task"]
    values = {}
    if reason == "lifecycle":
        member = {**member, "lifecycle": "stopped"}
    elif reason == "pending_operation":
        member = {**member, "pending_operation": {"kind": "stop"}}
    elif reason == "phase":
        values["ctf_control"] = {**task["ctf_control"], "phase": "closing"}
    elif reason == "replacement":
        values["ctf_control"] = {**task["ctf_control"], "replacement": {"phase": "begin"}}
    else:
        values["usage"] = {"cost": "10"}
    async with ctf.repo.engine.begin() as conn:
        if values:
            await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(**values))
        await ctf._retry_limited_turn(conn, tid, member, turn, "context_blocked")
    events = await decisions(ctf, tid)
    assert len(events) == 1
    assert events[0]["status"] == "recovery_disabled"
    assert events[0]["reason"] == reason
    state = await ctf.state(tid)
    assert not state["task"]["ctf_control"].get("turn_retries")
    assert not any(m["notification_purpose"] == "turn_recovery" for m in state["messages"])


async def test_legacy_deferred_recovery_waits_for_fresh_input_and_keeps_block_until_claim(ctf):
    tid = await team(ctf)
    aid = (await ctf.create_member(tid, "user", "Blocked", str(uuid4())))["id"]
    await ctf.post_message(tid, "user", aid, "Fake original input", str(uuid4()), "instruction")
    await block(ctf, tid, aid, 0)
    state = await ctf.state(tid)
    recovery = next(m for m in state["messages"] if m["notification_purpose"] == "turn_recovery")
    assert not recovery["deferred"]
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_messages)
            .where(s.ctf_messages.c.task_id == tid, s.ctf_messages.c.id == recovery["id"])
            .values(deferred=True)
        )
    assert await ctf.claim_turn(tid, aid, "context-control") is None
    assert aid in (await ctf.state(tid))["task"]["ctf_control"]["context_blocked_members"]
    fresh = await ctf.post_message(
        tid, "user", aid, "Smaller new input", str(uuid4()), "instruction"
    )
    assert fresh["dispatch_status"] == "context_recovery_requested"
    assert aid in (await ctf.state(tid))["task"]["ctf_control"]["context_blocked_members"]
    turn = await ctf.claim_turn(tid, aid, "context-control")
    messages = await ctf.claim_messages(tid, aid, turn["id"], turn["generation"])
    assert [m["id"] for m in messages] == [fresh["id"]]
    control = (await ctf.state(tid))["task"]["ctf_control"]
    assert aid not in control["context_blocked_members"]
    assert control["turn_retries"][aid]["context_blocked"] == 1


@pytest.mark.parametrize("already_closed", [False, True])
async def test_terminal_context_control_is_cleared_but_legacy_diagnostics_remain(
    ctf, already_closed
):
    tid = await team(ctf)
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "partial", "summary": "Stop"}
    )
    if already_closed:
        await ctf.finalize_close(tid, True)
    state = await ctf.state(tid)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **state["task"]["ctf_control"],
                    "context_blocked_members": {
                        "lead": {
                            "turn_id": "legacy-turn",
                            "message_ids": ["input", None, ""],
                            "input_message_ids": [None, "input"],
                            "branch": "legacy",
                        }
                    },
                }
            )
        )
    await ctf.finalize_close(tid, True)
    control = (await ctf.state(tid))["task"]["ctf_control"]
    assert control["context_blocked_members"] == {}
    entry = control["context_blocked_history"]["legacy-turn"]
    assert entry["message_ids"] == entry["input_message_ids"] == ["input"]
    assert entry["branch"] == "legacy"


async def test_member_removal_audits_cancelled_recovery_without_resetting_allowance(ctf):
    tid = await team(ctf)
    aid = (await ctf.create_member(tid, "user", "Blocked", str(uuid4())))["id"]
    await ctf.post_message(tid, "user", aid, "Investigate fake input", str(uuid4()), "instruction")
    turn = await block(ctf, tid, aid, 0)
    await ctf.record_execution(tid, aid, None, turn["generation"], "fake-boot")
    operation = str(uuid4())
    await ctf.request_member_operation(tid, "user", aid, "remove", operation)
    await ctf.complete_member_operation(
        tid,
        aid,
        operation,
        {"boot_id": "fake-boot", "generation": turn["generation"], "drained": True},
    )
    events = await decisions(ctf, tid)
    assert events[-1]["status"] == "recovery_cancelled"
    assert events[-1]["reason"] == "member_removed"
    state = await ctf.state(tid)
    recovery = next(m for m in state["messages"] if m["notification_purpose"] == "turn_recovery")
    assert recovery["status"] == "cancelled"
    assert state["task"]["ctf_control"]["turn_retries"][aid]["context_blocked"] == 1
    assert turn["id"] in state["task"]["ctf_control"]["context_blocked_history"]
