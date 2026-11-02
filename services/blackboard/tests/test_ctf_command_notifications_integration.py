"""Real PostgreSQL command result checkpoints, notification ACKs and later delivery."""

from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_integration import history, team

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def prepared(ctf):
    tid = await team(ctf)
    turn = await ctf.claim_turn(tid, "lead", "command-notification-test")
    messages = await ctf.claim_messages(tid, "lead", turn["id"], turn["generation"])
    await ctf.record_execution(tid, "lead", turn["id"], turn["generation"], "synthetic-boot")
    command = {
        "mode": "async",
        "turn_id": turn["id"],
        "command_id": str(uuid4()),
        "digest": "a" * 64,
        "state": "finished",
        "terminal_status": "completed",
        "result": "Synthetic saved result",
        "delivery": "notification",
        "completion_notified": False,
        "identity": {
            "task_id": str(tid),
            "member_id": "lead",
            "agent_id": "agent-1",
            "generation": turn["generation"],
            "boot_id": "synthetic-boot",
        },
    }
    payload: dict[str, Any] = history(messages)
    payload["state"]["ctf_commands"] = {f"{turn['id']}:fixture": command}
    await ctf.checkpoint(
        tid,
        "lead",
        turn["id"],
        turn["generation"],
        payload,
        "role",
        0,
        [{"message_id": item["id"], "claim_token": item["claim_token"]} for item in messages],
    )
    return tid, turn, command


@pytest.mark.parametrize("ack", [False, True])
async def test_lost_ack_reconciliation_and_later_turn_delivery_keep_distinct_proofs(ctf, ack):
    tid, origin, command = await prepared(ctf)
    receipt = await ctf.notify_command_result(
        tid, "lead", origin["id"], origin["generation"], command["command_id"]
    )
    with pytest.raises(HTTPException) as blocked:
        await ctf.finish_turn(tid, "lead", origin["id"], origin["generation"], "completed")
    assert cast(dict[str, Any], blocked.value.detail)["code"] == "ctf_command_pending"
    await ctf.finish_turn(tid, "lead", origin["id"], origin["generation"], "interrupted")
    delivery = await ctf.claim_turn(tid, "lead", "command-notification-test")
    assert delivery["generation"] > origin["generation"]
    messages = await ctf.claim_messages(tid, "lead", delivery["id"], delivery["generation"])
    assert any(item["id"] == receipt["id"] for item in messages)
    assert (
        await ctf.notify_command_result(
            tid, "lead", origin["id"], origin["generation"], command["command_id"]
        )
        == receipt
    )
    command["completion_notified"] = ack
    payload: dict[str, Any] = history(messages)
    payload["state"]["ctf_commands"] = {f"{origin['id']}:fixture": command}
    await ctf.checkpoint(
        tid,
        "lead",
        delivery["id"],
        delivery["generation"],
        payload,
        "role",
        1,
        [{"message_id": item["id"], "claim_token": item["claim_token"]} for item in messages],
    )
    await ctf.finish_turn(tid, "lead", delivery["id"], delivery["generation"], "completed")
    await ctf.post_message(tid, "user", "lead", "Next independent work", str(uuid4()))
    current = await ctf.claim_turn(tid, "lead", "command-notification-test")
    result = await ctf.recovery_input_proofs(
        tid, "lead", current["id"], current["generation"], [receipt["id"]]
    )
    if ack:
        proof = result["proofs"][0]
        assert proof["delivered_turn_id"] == delivery["id"]
        assert proof["command_turn_id"] == origin["id"]
        assert proof["command_generation"] == origin["generation"]
    else:
        assert result == {"proofs": []}


async def test_command_notice_rollback_retry_and_stop_do_not_lose_saved_results(ctf, monkeypatch):
    tid, turn, command = await prepared(ctf)
    original = ctf._message

    async def fail_after_insert(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("Synthetic mailbox rollback")

    monkeypatch.setattr(ctf, "_message", fail_after_insert)
    with pytest.raises(RuntimeError, match="mailbox rollback"):
        await ctf.notify_command_result(
            tid, "lead", turn["id"], turn["generation"], command["command_id"]
        )
    assert not any(
        item["notification_purpose"] == "command_result"
        for item in (await ctf.state(tid))["messages"]
    )
    monkeypatch.setattr(ctf, "_message", original)
    await ctf.request_finish(
        tid, "user", str(uuid4()), {"end_reason": "user_stop", "summary": "Stop"}
    )
    with pytest.raises(HTTPException) as stopped:
        await ctf.notify_command_result(
            tid, "lead", turn["id"], turn["generation"], command["command_id"]
        )
    assert cast(dict[str, Any], stopped.value.detail)["code"] == "ctf_command_notice_unavailable"
    saved = await ctf.sessions.get_session(tid, "lead")
    assert (
        saved["session"]["state"]["ctf_commands"][f"{turn['id']}:fixture"]["result"]
        == command["result"]
    )
    assert not any(
        item["notification_purpose"] == "command_result"
        for item in (await ctf.state(tid))["messages"]
    )
