"""Usage receipts survive failed acknowledgments without a cleanup-only full snapshot."""

import copy
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from agent_framework import Agent, tool
from bbx_contracts.ctf import load_ctf_profile
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.middleware import CtfHistoryProvider, flush_usage_outbox
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.session import ToolResultCheckpointMiddleware
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_runtime import TURN, MemoryCtf


class BillingService(MemoryCtf):
    def __init__(self):
        super().__init__()
        self.billed: set[str] = set()
        self.lost_ack = False
        self.failed_request: str | None = None

    async def runtime(self, task_id, operation, **body):
        if operation == "bill_usage":
            self.operations.append(operation)
            if body["request_id"] == self.failed_request:
                raise httpx.ReadError("billing unavailable")
            self.billed.add(body["request_id"])
            if self.lost_ack:
                self.lost_ack = False
                raise httpx.ReadError("billing acknowledgment lost")
            return {}
        return await super().runtime(task_id, operation, **body)


def receipt(request_id):
    return {
        "request_id": request_id,
        "reservation_id": f"{TURN['id']}:1",
        "agent_id": "member-1",
        "turn_id": TURN["id"],
        "generation": TURN["generation"],
        "usage": {"cost": "0.12"},
    }


@pytest.mark.parametrize("purpose", ["execution", "review"])
async def test_successful_usage_cleanup_joins_next_required_checkpoint(purpose):
    service = BillingService()
    turn = {**TURN, "purpose": purpose}
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", turn, "role")
    key = "ctf_review_usage_outbox" if purpose == "review" else "ctf_usage_outbox"
    checkpoint.session.state[key] = [receipt("request-1")]
    await checkpoint.save()
    await flush_usage_outbox(cast(CtfClient, service), checkpoint, turn, defer_cleanup=True)
    assert service.operations.count("checkpoint") == 1
    assert service.saved is not None and service.saved["session"]["state"][key]
    assert checkpoint.session.state[key] == []
    await checkpoint.save()
    assert service.operations.count("checkpoint") == 2
    assert service.saved["session"]["state"][key] == []


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_restart_rebills_durable_receipt_once_even_without_cleanup_snapshot(lost_ack):
    service = BillingService()
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    checkpoint.session.state["ctf_usage_outbox"] = [receipt("request-1")]
    await checkpoint.save()
    service.lost_ack = lost_ack
    if lost_ack:
        with pytest.raises(httpx.ReadError):
            await flush_usage_outbox(cast(CtfClient, service), checkpoint, TURN, defer_cleanup=True)
        assert checkpoint.session.state["ctf_usage_outbox"]
    else:
        await flush_usage_outbox(cast(CtfClient, service), checkpoint, TURN, defer_cleanup=True)
    restarted = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    assert restarted.session.state["ctf_usage_outbox"]
    await flush_usage_outbox(cast(CtfClient, service), restarted, TURN, defer_cleanup=True)
    assert service.billed == {"request-1"}
    assert service.operations.count("bill_usage") == 2
    assert service.operations.count("checkpoint") == 1
    await restarted.save()
    assert service.saved is not None
    assert service.saved["session"]["state"]["ctf_usage_outbox"] == []


async def test_partial_billing_failure_retains_all_original_receipts_for_retry():
    service = BillingService()
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    entries: list[dict[str, Any]] = [receipt("request-1"), receipt("request-2")]
    checkpoint.session.state["ctf_usage_outbox"] = copy.deepcopy(entries)
    await checkpoint.save()
    service.failed_request = "request-2"
    with pytest.raises(httpx.ReadError):
        await flush_usage_outbox(cast(CtfClient, service), checkpoint, TURN, defer_cleanup=True)
    assert checkpoint.session.state["ctf_usage_outbox"] == entries
    assert service.billed == {"request-1"}
    service.failed_request = None
    await flush_usage_outbox(cast(CtfClient, service), checkpoint, TURN, defer_cleanup=True)
    assert service.billed == {"request-1", "request-2"}
    assert service.operations.count("checkpoint") == 1


async def test_recovery_flush_keeps_its_own_cleanup_checkpoint():
    service = BillingService()
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    checkpoint.session.state["ctf_usage_outbox"] = [receipt("request-1")]
    await checkpoint.save()
    await flush_usage_outbox(cast(CtfClient, service), checkpoint, TURN)
    assert service.operations.count("checkpoint") == 2
    assert service.saved is not None
    assert service.saved["session"]["state"]["ctf_usage_outbox"] == []


async def test_native_tool_loop_keeps_usage_and_tool_checkpoints_without_cleanup_duplicates():
    service = BillingService()
    checkpoint = await load_checkpoint(cast(CtfClient, service), "task", "member-1", TURN, "role")
    profile = load_ctf_profile(Path("profiles/ctf"))
    history = CtfHistoryProvider(cast(CtfClient, service), checkpoint, TURN, profile.model)

    @tool
    def inspect_note() -> str:
        """Read a synthetic note."""
        return "saved note"

    client = ScriptedChatClient(
        [ScriptStep(calls=(ScriptToolCall("inspect_note", {}),)), ScriptStep(text="done")]
    )
    async with Agent(
        client=client,
        tools=[inspect_note],
        context_providers=[history],
        middleware=[ToolResultCheckpointMiddleware(checkpoint)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        await agent.run([], session=checkpoint.session)
    await checkpoint.save()
    assert len(service.billed) == 2
    assert service.operations.count("checkpoint") == 4
    assert service.saved is not None
    assert service.saved["session"]["state"]["ctf_usage_outbox"] == []
