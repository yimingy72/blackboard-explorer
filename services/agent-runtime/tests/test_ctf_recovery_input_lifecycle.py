"""Retire only completed, delivered, one-turn inputs from the model prefix."""

from copy import deepcopy
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from agent_framework import Agent, Content, Message, MessageInjectionMiddleware
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.context import message_signature
from bbx_runtime.ctf.middleware import (
    PENDING_KEY,
    CtfHistoryProvider,
    MailboxMiddleware,
    mailbox_message,
)
from bbx_runtime.ctf.provider_boundary import provider_call_boundary
from bbx_runtime.ctf.session import CtfCheckpoint, load_checkpoint
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
from test_ctf_context import text
from test_ctf_leaf_context import loaded
from test_ctf_provider_boundary import DispatchService

OLD_TURN = str(uuid4())
NEW_TURN = {"id": str(uuid4()), "generation": 2, "purpose": "execution"}


class ProofService(DispatchService):
    def __init__(self):
        super().__init__()
        self.proofs: list[dict[str, Any]] = []
        self.proof_error: Exception | None = None

    async def runtime(self, task_id, operation, **body):
        if operation == "recovery_input_proofs":
            self.calls.append((operation, deepcopy(body)))
            if self.proof_error:
                raise self.proof_error
            return {"proofs": [p for p in self.proofs if p["id"] in body["message_ids"]]}
        return await super().runtime(task_id, operation, **body)


async def fixture(*, sender="agent", kind="instruction", notification=None):
    service = ProofService()
    old = mailbox_message(
        {
            "id": str(uuid4()),
            "sender_kind": sender,
            "sender_id": "lead" if sender == "agent" else sender,
            "recipient_id": "member-1",
            "kind": kind,
            "purpose": "execution",
            "notification_purpose": notification,
            "source_turn_id": str(uuid4()) if sender == "system" else None,
            "body": "!" * 600,
        }
    )
    seed = text("goal", "Original task goal and permanent scope")
    checkpoint = await load_checkpoint(
        cast(Any, service), "task", "member-1", {"id": OLD_TURN, "generation": 1}, "role"
    )
    raw = [old, text("done", "Assignment completed", "assistant")]
    checkpoint.session.state.update(
        {
            "in_memory": {"messages": raw},
            "ctf_context_recovery_seed": seed.to_dict(),
            "ctf_context_recovery": {
                "turn_id": OLD_TURN,
                "message": seed.to_dict(),
                "protected_inputs": [old.to_dict()],
            },
            "ctf_history_view": {"excluded_before": 2, "epoch": 1, "total_messages": 2},
        }
    )
    await checkpoint.save()
    service.generation = 2
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", NEW_TURN, "role")
    source = old.additional_properties["ctf_source"]
    service.proofs = [
        {
            **source,
            "session_revision": 1,
            "delivered_turn_id": OLD_TURN,
            "claim_generation": 1,
            "turn_generation": 1,
            "turn_status": "finished",
            "end_reason": "completed",
        }
    ]
    value = CtfHistoryProvider(
        cast(Any, service), checkpoint, dict(NEW_TURN), service.profile.model, 1000
    )
    return service, value, old, seed


@pytest.mark.parametrize("resume", [False, True])
async def test_new_turn_retires_completed_assignment_and_keeps_goal_constraints_and_history(resume):
    service, value, old, seed = await fixture()
    durable = value.checkpoint.session.state
    constraint = mailbox_message(
        {
            "id": str(uuid4()),
            "sender_kind": "user",
            "sender_id": "user",
            "recipient_id": "member-1",
            "kind": "instruction",
            "purpose": "execution",
            "body": "Permanent user constraint: preserve all existing data",
        }
    )
    durable["ctf_context_recovery"]["protected_inputs"].append(constraint.to_dict())
    if resume:
        durable["ctf_context_blocked"] = {"turn_id": OLD_TURN}
    before = [m.to_dict() for m in durable["in_memory"]["messages"]]
    view = await loaded(value)
    assert [m.to_dict() for m in view] == [seed.to_dict(), constraint.to_dict()]
    fresh = text(str(uuid4()), "?" * 300)
    assert fresh in value.prepare_provider_messages([*view, fresh])
    assert durable["ctf_history_view"]["epoch"] == 2
    proof = durable["ctf_recovery_input_retirements"][0]
    assert proof["proof"]["id"] == old.message_id
    assert proof["proof"]["delivered_turn_id"] == OLD_TURN
    assert proof["proof"]["turn_generation"] == 1
    assert [m.to_dict() for m in durable["in_memory"]["messages"]] == before
    await value.checkpoint.save()
    restored = await load_checkpoint(cast(Any, service), "task", "member-1", NEW_TURN, "role")
    again = CtfHistoryProvider(cast(Any, service), restored, NEW_TURN, value.model, 1000)
    assert [m.to_dict() for m in await loaded(again)] == [m.to_dict() for m in view]
    assert restored.session.state["ctf_history_view"]["epoch"] == 2
    assert len(restored.session.state["ctf_recovery_input_retirements"]) == 1
    if resume:
        # A stale previous-turn marker cannot repeat an already saved transition.
        restored.session.state["ctf_context_blocked"] = {"turn_id": OLD_TURN}
        restarted = CtfHistoryProvider(cast(Any, service), restored, NEW_TURN, value.model, 1000)
        assert [m.to_dict() for m in await loaded(restarted)] == [m.to_dict() for m in view]
        assert restored.session.state["ctf_history_view"]["epoch"] == 2
        assert not restored.session.state.get("ctf_context_blocked")


@pytest.mark.parametrize(
    "change",
    [
        "same_turn",
        "assignment",
        "active",
        "delivery",
        "pending",
        "unconfirmed",
        "signature",
        "duplicate_input",
        "missing_source",
        "unknown_tool",
        "repair",
        "unknown_command",
        "unknown_platform",
        "unrecorded_platform",
        "not_excluded",
    ],
)
async def test_current_uncertain_and_unfinished_inputs_stay_protected(change):
    service, value, old, _ = await fixture()
    durable = value.checkpoint.session.state
    if change == "same_turn":
        durable["ctf_context_recovery"]["turn_id"] = NEW_TURN["id"]
    elif change == "assignment":
        value.turn["assignment_message_ids"] = [old.message_id]
    elif change == "active":
        durable["ctf_active_input_ids"] = {"turn_id": NEW_TURN["id"], "ids": [old.message_id]}
    elif change == "delivery":
        value.checkpoint.stage_delivery(cast(str, old.message_id), "synthetic-claim")
    elif change == "pending":
        durable[PENDING_KEY] = [old.to_dict()]
    elif change == "unconfirmed":
        cast(CtfCheckpoint, value.checkpoint).confirmed_history = ()
    elif change == "signature":
        value._recovered_inputs()[0].contents[0].text = "changed"
    elif change == "duplicate_input":
        changed = deepcopy(value._recovered_inputs()[0])
        changed.contents[0].text = "Changed input with a duplicate ID must stay visible"
        durable["ctf_context_recovery"]["protected_inputs"].append(changed)
    elif change == "missing_source":
        value._recovered_inputs()[0].additional_properties = {}
    elif change in {"unknown_tool", "repair"}:
        raw = durable["in_memory"]["messages"]
        raw.append(
            Message(role="assistant", contents=[Content.from_function_call("unknown", "inspect")])
        )
        if change == "repair":
            result = Content.from_function_result("unknown", result="Unknown outcome")
            result.additional_properties["bbx_recovery_repair"] = "interrupted"
            raw.append(Message(role="tool", contents=[result]))
        cast(CtfCheckpoint, value.checkpoint).confirmed_history = tuple(
            message_signature(m) for m in raw
        )
        durable["ctf_history_view"]["excluded_before"] = len(raw)
    elif change == "unknown_command":
        durable["ctf_commands"] = {f"{OLD_TURN}:command": {"state": "prepared"}}
    elif change in {"unknown_platform", "unrecorded_platform"}:
        durable["ctf_platform_calls"] = {
            f"{OLD_TURN}:platform": {
                "state": "unknown" if change == "unknown_platform" else "received"
            }
        }
    elif change == "not_excluded":
        durable["ctf_history_view"]["excluded_before"] = 0
    try:
        await loaded(value)
    except RuntimeError as error:
        assert str(error) == "context_blocked"
    assert durable["ctf_context_recovery"]["protected_inputs"]
    assert not durable.get("ctf_recovery_input_retirements")


@pytest.mark.parametrize("state,retired", [("rejected", True), ("prepared", False)])
async def test_only_explicitly_rejected_platform_preflight_retires_old_input(state, retired):
    _, value, _, _ = await fixture()
    durable = value.checkpoint.session.state
    durable["ctf_platform_calls"] = {f"{OLD_TURN}:platform": {"state": state}}
    await loaded(value)
    assert bool(durable.get("ctf_recovery_input_retirements")) == retired


@pytest.mark.parametrize(
    "field,changed",
    [
        ("end_reason", "context_limit"),
        ("end_reason", "interrupted"),
        ("turn_status", "running"),
        ("session_revision", 0),
        ("session_revision", 100),
        ("turn_generation", 2),
        ("claim_generation", 7),
        ("recipient_id", "member-2"),
        ("delivered_turn_id", NEW_TURN["id"]),
        ("sender_id", "foreign"),
    ],
)
async def test_incomplete_or_mismatched_authoritative_proof_keeps_input(field, changed):
    service, value, _, _ = await fixture()
    service.proofs[0][field] = changed
    await loaded(value)
    assert value._recovered_inputs()
    assert not value.checkpoint.session.state.get("ctf_recovery_input_retirements")


async def test_missing_delivery_proof_keeps_original_input():
    service, value, old, _ = await fixture()
    service.proofs = []
    view = await loaded(value)
    assert any(message.message_id == old.message_id for message in view)
    assert not value.checkpoint.session.state.get("ctf_recovery_input_retirements")


@pytest.mark.parametrize(
    "sender,kind,notification,retired",
    [
        ("user", "instruction", None, False),
        ("agent", "message", None, False),
        ("system", "instruction", None, False),
        ("system", "instruction", "context_continuation", True),
        ("system", "instruction", "turn_recovery", True),
        ("system", "turn_finished", "assignment_result", True),
        ("system", "notification", "turn_recovery", False),
        ("system", "assignment_result", "turn_finished", False),
        ("system", "turn_finished", "turn_finished", False),
        ("system", "instruction", "assignment_result", False),
        ("system", "turn_finished", "turn_recovery", False),
        ("user", "turn_finished", "assignment_result", False),
    ],
)
async def test_only_explicitly_scoped_input_sources_retire(sender, kind, notification, retired):
    _, value, _, _ = await fixture(sender=sender, kind=kind, notification=notification)
    await loaded(value)
    assert bool(value.checkpoint.session.state.get("ctf_recovery_input_retirements")) == retired


async def test_proof_read_failure_is_diagnosed_and_fences_still_raise():
    service, value, _, _ = await fixture()
    service.proof_error = httpx.RemoteProtocolError("synthetic private response")
    await loaded(value)
    assert value._recovered_inputs()
    diagnostic = value.checkpoint.session.state["ctf_recovery_input_check"]
    assert diagnostic["status"] == "proof_unavailable"
    assert "private" not in str(diagnostic)
    denied_service, denied, _, _ = await fixture()
    denied_service.proof_error = RemoteError(409, "fenced")
    with pytest.raises(RemoteError):
        await loaded(denied)


async def test_native_provider_saves_retirement_before_first_dispatch():
    service, value, old, _ = await fixture()
    fresh = str(uuid4())
    service.post(fresh, "New assignment")
    mailbox = MailboxMiddleware(cast(Any, service), value.checkpoint, NEW_TURN, 10)
    client = ScriptedChatClient([ScriptStep(text="done", expect_contains="New assignment")])
    original = client._inner_get_response

    def dispatch(**kwargs):
        assert service.saved is not None
        durable = service.saved["session"]["state"]
        assert durable["ctf_context_recovery"]["protected_inputs"] == []
        assert durable["ctf_history_view"]["epoch"] == 2
        assert durable["ctf_recovery_input_retirements"][0]["proof"]["id"] == old.message_id
        return original(**kwargs)

    client._inner_get_response = dispatch
    with provider_call_boundary(client, mailbox.enter_provider, value.prepare_provider_messages):
        async with Agent(
            client=client,
            instructions="Synthetic goal",
            context_providers=[value],
            middleware=[mailbox, MessageInjectionMiddleware()],
            require_per_service_call_history_persistence=True,
        ) as agent:
            await agent.run([], session=value.checkpoint.session, stream=False)
    assert len(client.received_messages) == 1
    assert all(m.message_id != old.message_id for m in client.received_messages[0])
    assert any(m.message_id == fresh for m in client.received_messages[0])


async def test_delivery_proof_requests_are_bounded_without_losing_a_large_recovery_prefix():
    service, value, old, seed = await fixture()
    raw = []
    proof = service.proofs[0]
    service.proofs = []
    for _ in range(101):
        message = deepcopy(old)
        message.message_id = str(uuid4())
        message.additional_properties["ctf_source"]["id"] = message.message_id
        raw.append(message)
        service.proofs.append({**proof, "id": message.message_id})
    durable = value.checkpoint.session.state
    durable["ctf_context_recovery"]["protected_inputs"] = [m.to_dict() for m in raw]
    raw.append(text("done", "Completed", "assistant"))
    durable["in_memory"]["messages"] = raw
    durable["ctf_history_view"]["excluded_before"] = len(raw)
    await value.checkpoint.save()
    assert [m.to_dict() for m in await loaded(value)] == [seed.to_dict()]
    requests = [body for operation, body in service.calls if operation == "recovery_input_proofs"]
    assert [len(body["message_ids"]) for body in requests] == [100, 1]
    assert len(durable["ctf_recovery_input_retirements"]) == 101
