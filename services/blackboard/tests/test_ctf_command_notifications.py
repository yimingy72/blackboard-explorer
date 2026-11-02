"""Persisted async results gate mailbox writes, retries and successful settlement."""

import json
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import insert, select, update
from test_ctf_assignment_feedback import feedback as feedback_fixture
from test_ctf_assignment_feedback import mailbox
from test_ctf_communication_storage import storage as storage_fixture
from test_ctf_pagination import pagination_app as pagination_app_fixture

feedback = feedback_fixture
storage = storage_fixture
pagination_app = pagination_app_fixture


async def test_command_notify_runtime_route_is_service_only_and_accepts_no_result_payload(
    pagination_app,
):
    setup = pagination_app
    setup.backend.notify_command_result = AsyncMock(
        return_value={"id": str(uuid4()), "recipient_id": "lead"}
    )
    body = {
        "agent_id": "lead",
        "turn_id": str(uuid4()),
        "generation": 2,
        "command_id": str(uuid4()),
    }
    headers = {"Authorization": "Bearer service-test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        path = f"/api/tasks/{setup.tid}/ctf/runtime/notify_command_result"
        denied = await client.post(path, json=body, headers=setup.agent_headers)
        assert denied.status_code == 403
        for bad in (
            {**body, "result": "forged"},
            {**body, "generation": True},
            {"command_id": body["command_id"]},
        ):
            response = await client.post(path, json=bad, headers=headers)
            assert response.status_code == 422
        setup.backend.notify_command_result.assert_not_awaited()
        response = await client.post(path, json=body, headers=headers)
    assert response.status_code == 200
    setup.backend.notify_command_result.assert_awaited_once_with(setup.tid, **body)


def entry(feedback, **changes):
    return {
        "mode": "async",
        "turn_id": feedback.current_id,
        "command_id": str(uuid4()),
        "digest": "a" * 64,
        "state": "finished",
        "terminal_status": "completed",
        "result": "Synthetic stdout must stay out of the mailbox",
        "delivery": "notification",
        "completion_notified": False,
        "identity": {
            "task_id": str(feedback.tid),
            "member_id": "lead",
            "agent_id": "agent-1",
            "generation": 2,
            "boot_id": "synthetic-boot",
        },
        **changes,
    }


async def save(feedback, command):
    with feedback.database.begin() as conn:
        member = (
            conn.execute(select(s.ctf_members).where(s.ctf_members.c.id == "lead")).mappings().one()
        )
        prior = conn.execute(select(s.agent_sessions.c.revision)).scalar_one_or_none() or 0
        conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.id == "lead")
            .values(
                execution={
                    "boot_id": "synthetic-boot",
                    "generation": member["generation"],
                    "drained": False,
                },
            )
        )
    return await feedback.service.checkpoint(
        feedback.tid,
        "lead",
        member["current_turn_id"],
        member["generation"],
        {"state": {"ctf_commands": {f"{command['turn_id']}:fixture": command}}},
        "role",
        prior,
        [],
    )


async def notify(feedback, command):
    return await feedback.service.notify_command_result(
        feedback.tid,
        "lead",
        command["turn_id"],
        command["identity"]["generation"],
        command["command_id"],
    )


async def test_notification_uses_confirmed_result_and_retries_without_repeating_stdout(feedback):
    command = entry(feedback)
    await save(feedback, command)
    message = await notify(feedback, command)
    assert await notify(feedback, command) == message
    notice = mailbox(feedback)[0]
    assert notice["sender_kind"] == "system" and message["recipient_id"] == "lead"
    assert notice["source_turn_id"] is None and notice["notification_purpose"] == "command_result"
    assert command["result"] not in notice["body"]
    assert json.loads(notice["body"])["result_ref"] == {
        "tool": "read_command_result",
        "command_id": command["command_id"],
    }
    command["completion_notified"] = True
    await save(feedback, command)
    assert await notify(feedback, command) == message
    assert len(mailbox(feedback)) == 1


@pytest.mark.parametrize(
    "change", ["missing", "state", "result", "identity", "boot", "revision", "closing", "stopping"]
)
async def test_unconfirmed_foreign_unknown_and_stopping_results_never_notify(feedback, change):
    command = entry(feedback)
    if change == "state":
        command["state"] = "unknown"
    elif change == "result":
        command.pop("result")
    elif change == "identity":
        command["identity"]["member_id"] = "member-1"
    elif change == "boot":
        command["identity"]["boot_id"] = "other-boot"
    if change != "missing":
        await save(feedback, command)
    with feedback.database.begin() as conn:
        if change == "revision":
            conn.execute(
                update(s.ctf_turns)
                .where(s.ctf_turns.c.id == feedback.current_id)
                .values(checkpoint_revision=99)
            )
        elif change == "closing":
            conn.execute(update(s.tasks).values(ctf_control={"phase": "closing"}))
        elif change == "stopping":
            conn.execute(
                update(s.ctf_members)
                .where(s.ctf_members.c.id == "lead")
                .values(run_state="stopping")
            )
    with pytest.raises(HTTPException) as caught:
        await notify(feedback, command)
    assert caught.value.status_code == 409
    assert mailbox(feedback) == []


async def test_existing_notice_can_ack_across_generation_but_new_old_notice_cannot_be_created(
    feedback,
):
    command = entry(feedback)
    await save(feedback, command)
    message = await notify(feedback, command)
    with feedback.database.begin() as conn:
        conn.execute(update(s.ctf_members).where(s.ctf_members.c.id == "lead").values(generation=3))
    # This is only readback of the earlier commit, even while the execution fence changed.
    assert await notify(feedback, command) == message
    assert len(mailbox(feedback)) == 1
    missing_notice = {**command, "command_id": str(uuid4())}
    with feedback.database.begin() as conn:
        conn.execute(
            update(s.agent_sessions).values(
                session={
                    "state": {
                        "ctf_commands": {
                            f"{command['turn_id']}:fixture": missing_notice,
                        }
                    }
                }
            )
        )
    with pytest.raises(HTTPException) as caught:
        await notify(feedback, missing_notice)
    assert cast(dict[str, Any], caught.value.detail)["code"] == "ctf_command_notice_unavailable"
    assert len(mailbox(feedback)) == 1


async def test_preoccupied_notice_id_does_not_confirm_an_unrelated_message(feedback):
    command = entry(feedback)
    await save(feedback, command)
    mid = feedback.service._command_notice_id(
        feedback.tid, "lead", feedback.current_id, 2, command["command_id"]
    )
    await feedback.service.post_message(feedback.tid, "user", "lead", "Unrelated", mid)
    with pytest.raises(HTTPException, match="notification identity conflict"):
        await notify(feedback, command)
    assert len(mailbox(feedback)) == 1 and mailbox(feedback)[0]["sender_kind"] == "user"


@pytest.mark.parametrize(
    "state,delivery,notified,allowed",
    [
        ("running", "notification", False, False),
        ("unknown", "notification", False, False),
        ("finished", "notification", False, False),
        ("finished", "notification", True, True),
        ("finished", "tool_result", False, True),
    ],
)
async def test_completed_settlement_requires_terminal_result_and_delivery_ack(
    feedback, state, delivery, notified, allowed
):
    command = entry(feedback, state=state, delivery=delivery, completion_notified=notified)
    await save(feedback, command)
    if allowed:
        result = await feedback.service.finish_turn(
            feedback.tid, "lead", feedback.current_id, 2, "completed"
        )
        assert result["status"] == "finished"
    else:
        with pytest.raises(HTTPException) as caught:
            await feedback.service.finish_turn(
                feedback.tid, "lead", feedback.current_id, 2, "completed"
            )
        assert cast(dict[str, Any], caught.value.detail)["code"] == "ctf_command_pending"
        # Interrupted/stop cleanup remains possible; this gate only protects success settlement.
        result = await feedback.service.finish_turn(
            feedback.tid, "lead", feedback.current_id, 2, "interrupted"
        )
        assert result["end_reason"] == "interrupted"


@pytest.mark.parametrize("ack", [False, True])
async def test_retirement_proof_separates_command_origin_from_later_completed_delivery(
    feedback, ack
):
    origin = feedback.old_id
    delivered_turn = str(uuid4())
    command = entry(feedback, turn_id=origin, completion_notified=ack)
    command["identity"]["generation"] = 1
    await save(feedback, command)
    mid = feedback.service._command_notice_id(
        feedback.tid, "lead", origin, 1, command["command_id"]
    )
    with feedback.database.begin() as conn:
        conn.execute(update(s.ctf_members).where(s.ctf_members.c.id == "lead").values(generation=3))
        conn.execute(
            update(s.ctf_turns).where(s.ctf_turns.c.id == feedback.current_id).values(generation=3)
        )
        conn.execute(
            insert(s.ctf_turns).values(
                task_id=feedback.tid,
                id=delivered_turn,
                agent_id="lead",
                generation=2,
                purpose="execution",
                status="finished",
                end_reason="completed",
            )
        )
        conn.execute(
            insert(s.ctf_messages).values(
                task_id=feedback.tid,
                id=mid,
                sender_kind="system",
                sender_id="system",
                recipient_id="lead",
                kind="notification",
                notification_purpose="command_result",
                purpose="execution",
                body="Reference only",
                status="delivered",
                session_revision=1,
                delivered_turn_id=delivered_turn,
                claim_turn_id=delivered_turn,
                claim_generation=2,
                recipient_sequence=1,
            )
        )
    result = await feedback.service.recovery_input_proofs(
        feedback.tid, "lead", feedback.current_id, 3, [mid]
    )
    if not ack:
        assert result == {"proofs": []}
    else:
        proof = result["proofs"][0]
        assert proof["delivered_turn_id"] == delivered_turn
        assert proof["command_turn_id"] == origin and proof["command_generation"] == 1
        assert proof["command_id"] == command["command_id"]
        assert "body" not in proof and "result" not in proof


async def test_terminal_review_reads_only_own_saved_result_without_resuming_execution(feedback):
    command = entry(feedback, result="证据😀\n" * 100)
    await save(feedback, command)
    foreign_id = str(uuid4())
    with feedback.database.begin() as conn:
        conn.execute(update(s.tasks).values(status="stopped", ctf_control={"phase": "closed"}))
        conn.execute(
            update(s.ctf_turns)
            .where(s.ctf_turns.c.id == feedback.current_id)
            .values(purpose="review")
        )
        conn.execute(
            update(s.ctf_members).where(s.ctf_members.c.id == "lead").values(lifecycle="removed")
        )
        conn.execute(
            insert(s.agent_sessions).values(
                task_id=feedback.tid,
                agent_id="bob",
                revision=1,
                session={
                    "state": {"ctf_commands": {"other:1": {**command, "command_id": foreign_id}}}
                },
            )
        )
    result = await feedback.service.read_command_result(
        feedback.tid,
        "lead",
        feedback.current_id,
        2,
        command["command_id"],
        offset=2,
        limit=3,
    )
    assert result == {
        "command_id": command["command_id"],
        "state": "completed",
        "result": command["result"][2:5],
        "next_offset": 5,
    }
    with pytest.raises(HTTPException) as foreign:
        await feedback.service.read_command_result(
            feedback.tid, "lead", feedback.current_id, 2, foreign_id
        )
    assert foreign.value.status_code == 404
    command["state"] = "unknown"
    with feedback.database.begin() as conn:
        conn.execute(
            update(s.agent_sessions)
            .where(s.agent_sessions.c.agent_id == "lead")
            .values(
                session={"state": {"ctf_commands": {f"{command['turn_id']}:fixture": command}}},
            )
        )
    unknown = await feedback.service.read_command_result(
        feedback.tid, "lead", feedback.current_id, 2, command["command_id"]
    )
    assert unknown["state"] == "unknown" and "result" not in unknown
    assert mailbox(feedback) == []


async def test_review_command_result_route_rejects_execution_and_foreign_reader_arguments(
    pagination_app,
):
    setup = pagination_app
    setup.backend.authorize_tool.return_value["purpose"] = "review"
    setup.backend.read_command_result = AsyncMock(
        return_value={
            "command_id": "saved",
            "state": "completed",
            "result": "Saved output",
            "next_offset": None,
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        prefix = f"/api/tasks/{setup.tid}/ctf/tools/"
        response = await client.post(
            prefix + "read_command_result",
            json={"command_id": "saved"},
            headers=setup.agent_headers,
        )
        assert response.status_code == 200
        for operation in ("start_command", "cancel_command", "execute_command"):
            assert (
                await client.post(
                    prefix + operation, json={"command_id": "saved"}, headers=setup.agent_headers
                )
            ).status_code == 403
        foreign = await client.post(
            prefix + "read_command_result",
            json={"command_id": "saved", "agent_id": "lead"},
            headers=setup.agent_headers,
        )
        assert foreign.status_code == 422
    args = setup.backend.read_command_result.await_args
    assert args is not None and args.kwargs["agent_id"] == "member-1"
    setup.backend.read_command_result.assert_awaited_once()


@pytest.mark.parametrize("page", [{"offset": True}, {"offset": -1}, {"limit": 0}, {"limit": 16001}])
async def test_review_command_result_rejects_invalid_page_before_reading(feedback, page):
    with pytest.raises(HTTPException) as error:
        await feedback.service.read_command_result(
            feedback.tid, "lead", feedback.current_id, 2, "saved", **page
        )
    assert error.value.status_code == 422
