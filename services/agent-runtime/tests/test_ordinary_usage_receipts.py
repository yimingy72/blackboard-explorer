"""Native MAF preserves successful usage across billing failures and cancellation."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from agent_framework import Content, tool
from bbx_contracts.heartbeat import UNKNOWN_USAGE_WARNING, ReceiptHeartbeatRequest
from bbx_runtime.billing import _usage_known
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.session import SessionCheckpoint
from bbx_runtime.testing.scripted_client import (
    ScriptedChatClient,
    ScriptStep,
    ScriptToolCall,
    ScriptUsage,
)
from bbx_runtime.usage_receipts import flush_ordinary_usage
from test_model_recovery import RECEIPT, InterruptedClient, RecoveryService, invoke, runner_for
from usage_receipt_fixtures import heartbeat_ack


class ReceiptService(RecoveryService):
    def __init__(self):
        super().__init__("derive")
        self.confirmed: set[str] = set()
        self.charged_output = 0
        self.before_commit = 0
        self.lose_ack = False

    async def heartbeat(self, task_id, agent_id, **kwargs):
        assert self.saved is not None
        state = self.saved["session"]["state"]
        assert RECEIPT in str(state["in_memory"]["messages"])
        receipts = state["bbx_usage_outbox"]
        assert any(r["request"]["request_id"] == kwargs["request_id"] for r in receipts)
        if self.before_commit:
            self.before_commit -= 1
            raise httpx.ReadError("Synthetic heartbeat before commit")
        if kwargs["request_id"] not in self.confirmed:
            self.confirmed.add(kwargs["request_id"])
            self.charged_output += kwargs["usage"].output_tokens
            await super().heartbeat(task_id, agent_id, **kwargs)
        if self.lose_ack:
            self.lose_ack = False
            raise httpx.ReadError("Synthetic heartbeat committed but acknowledgment lost")
        return heartbeat_ack(task_id, agent_id, kwargs)


def client():
    return ScriptedChatClient(
        [
            ScriptStep(
                text=RECEIPT,
                usage=ScriptUsage(prompt_cache_miss_tokens=1000, completion_tokens=300),
            )
        ]
    )


@pytest.mark.parametrize("failure", ["before_commit", "lost_ack"])
async def test_successful_response_is_saved_before_billing_and_bounded_retry_never_replays_model(
    failure,
):
    service = ReceiptService()
    service.before_commit = 1 if failure == "before_commit" else 0
    service.lose_ack = failure == "lost_ack"
    runner, _ = runner_for(service)
    model = client()
    result = await invoke(runner, model)
    assert result.end_reason == "normal"
    assert model._index == 1
    assert service.charged_output == 300 and len(service.beats) == 1
    assert service.saved is not None
    assert service.saved["session"]["state"]["bbx_usage_outbox"] == []
    assert service.saved["revision"] == 3


async def test_persistent_billing_failure_leaves_exact_receipt_and_restart_can_settle_once():
    service = ReceiptService()
    service.before_commit = 10
    runner, _ = runner_for(service)
    model = client()
    assert (await invoke(runner, model)).end_reason == "runtime_error"
    assert model._index == 1 and service.charged_output == 0
    assert service.saved is not None
    receipts = deepcopy(service.saved["session"]["state"]["bbx_usage_outbox"])
    assert len(receipts) == 1 and receipts[0]["request"]["usage"]["output_tokens"] == 300
    assert (
        sum(
            call.args[2]["kind"] == "model_output"
            for call in service.record_agent_trace.await_args_list
        )
        == 1
    )
    checkpoint = await SessionCheckpoint.load(cast(BlackboardClient, service), "task", "agent-1")
    assert checkpoint is not None
    service.before_commit = 0
    await flush_ordinary_usage(checkpoint)
    assert service.charged_output == 300
    # Crash before a cleanup snapshot leaves the same durable receipt, not a new call.
    restarted = await SessionCheckpoint.load(cast(BlackboardClient, service), "task", "agent-1")
    assert restarted is not None
    await flush_ordinary_usage(restarted)
    assert service.charged_output == 300 and len(service.beats) == 1


async def test_cancel_after_complete_response_during_trace_keeps_history_and_usage():
    service = ReceiptService()
    traced = asyncio.Event()
    stop_trace = asyncio.Event()

    async def trace(_task, _agent, body, **_kwargs):
        if body["kind"] == "model_output":
            traced.set()
            await stop_trace.wait()
        return []

    service.record_agent_trace = AsyncMock(side_effect=trace)
    runner, _ = runner_for(service)
    model = client()
    pending = asyncio.create_task(invoke(runner, model))
    await asyncio.wait_for(traced.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert model._index == 1 and service.charged_output == 300
    assert service.saved is not None
    assert RECEIPT in str(service.saved["session"]["state"]["in_memory"]["messages"])


async def test_incomplete_model_stream_does_not_invent_a_usage_receipt():
    service = ReceiptService()
    runner, _ = runner_for(service)
    model = InterruptedClient(
        [ScriptStep()], {0: RuntimeError("Synthetic incomplete stream")}, partial_tool=True
    )
    assert (await invoke(runner, model)).end_reason == "runtime_error"
    assert model._index == 1 and service.charged_output == 0 and not service.beats
    assert service.saved is not None
    assert not service.saved["session"]["state"].get("bbx_usage_outbox")


async def test_failure_before_provider_dispatch_does_not_create_unknown_usage():
    class NotDispatched(ScriptedChatClient):
        def _inner_get_response(self, **kwargs):
            raise RuntimeError("Synthetic failure before provider dispatch")

    service = ReceiptService()
    runner, _ = runner_for(service)
    model = NotDispatched([ScriptStep()])
    assert (await invoke(runner, model)).end_reason == "runtime_error"
    assert model._index == 0 and not service.beats and service.charged_output == 0
    assert service.saved is not None
    assert not service.saved["session"]["state"].get("bbx_usage_outbox")


async def test_lost_snapshot_ack_reads_back_protocol_and_bills_the_same_receipt_once():
    class LostSnapshot(ReceiptService):
        lost = False

        async def put_agent_session(self, *args, **kwargs):
            result = await super().put_agent_session(*args, **kwargs)
            if kwargs["session"]["state"].get("bbx_usage_outbox") and not self.lost:
                self.lost = True
                raise httpx.ReadError("Synthetic committed snapshot acknowledgment lost")
            return result

    service = LostSnapshot()
    runner, _ = runner_for(service)
    model = client()
    assert (await invoke(runner, model)).end_reason == "normal"
    assert model._index == 1 and service.charged_output == 300 and len(service.beats) == 1
    assert service.saved is not None and service.saved["revision"] == 3


async def test_old_backend_without_receipt_protocol_never_receives_additive_heartbeat():
    class OldBackend(ReceiptService):
        async def put_agent_session(self, *args, **kwargs):
            result = await super().put_agent_session(*args, **kwargs)
            result.pop("usage_receipt_protocol", None)
            assert self.saved is not None
            self.saved.pop("usage_receipt_protocol", None)
            return result

    service = OldBackend()
    service.heartbeat = AsyncMock(
        side_effect=AssertionError("Legacy additive billing must not run")
    )
    runner, _ = runner_for(service)
    model = client()
    assert (await invoke(runner, model)).end_reason == "runtime_error"
    assert model._index == 1 and service.charged_output == 0
    service.heartbeat.assert_not_awaited()
    assert service.saved is not None
    assert (
        service.saved["session"]["state"]["bbx_usage_outbox"][0]["request"]["usage"][
            "output_tokens"
        ]
        == 300
    )


async def test_client_uses_receipt_envelope_while_legacy_wire_body_stays_flat():
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        service = BlackboardClient("http://synthetic", "synthetic-token", http)
        body: dict[str, Any] = dict(
            steps=1, context_tokens=1000, usage={"output_tokens": 300}, last_seen_version=0
        )
        await service.heartbeat(
            "task",
            "agent-1",
            **body,
            requested_at=datetime.now(UTC),
            request_id=str(uuid4()),
            expected_run_number=1,
        )
        await service.heartbeat("task", "agent-1", **body)
    assert set(sent[0]) == {"receipt"}
    assert ReceiptHeartbeatRequest.model_validate(sent[0]).receipt.request_id is not None
    assert sent[1] == body


@pytest.mark.parametrize("details", [None, {"output_token_count": 300}])
async def test_success_with_incomplete_usage_preserves_step_and_blocks_further_dispatch(
    details, monkeypatch
):
    class IncompleteUsage(ScriptedChatClient):
        def _inner_get_response(self, **kwargs):
            original = cast(Any, super()._inner_get_response(**kwargs))
            assert kwargs["stream"]

            async def updates():
                async for update in original:
                    update.contents = [c for c in update.contents if c.type != "usage"]
                    if details is not None:
                        update.contents.append(Content.from_usage(details))
                    yield update

            return self._build_response_stream(updates())

    called = 0

    @tool
    async def synthetic_tool() -> str:
        nonlocal called
        called += 1
        return "must not execute"

    monkeypatch.setattr("bbx_runtime.runner.make_board_tools", lambda _ctx: [synthetic_tool])
    service = ReceiptService()
    runner, _ = runner_for(service)
    model = IncompleteUsage(
        [
            ScriptStep(text=RECEIPT, calls=(ScriptToolCall("synthetic_tool"),)),
            ScriptStep(text="must not run"),
        ]
    )
    assert (await invoke(runner, model)).end_reason == "runtime_error"
    assert model._index == 1 and len(service.beats) == 1
    assert called == 0
    assert (
        sum(
            call.args[2]["kind"] == "model_output"
            for call in service.record_agent_trace.await_args_list
        )
        == 1
    )
    assert service.beats[0]["steps"] == 1
    assert service.charged_output == (300 if details else 0)
    assert service.saved is not None
    saved = service.saved["session"]["state"]
    assert RECEIPT in str(saved["in_memory"]["messages"])
    assert saved["bbx_usage_outbox"][0]["request"]["usage_known"] is False
    # A confirmed but incomplete receipt survives a cold restart and cannot bill twice.
    checkpoint = await SessionCheckpoint.load(cast(BlackboardClient, service), "task", "agent-1")
    assert checkpoint is not None
    with pytest.raises(ValueError, match=UNKNOWN_USAGE_WARNING) as failure:
        await flush_ordinary_usage(checkpoint)
    assert vars(failure.value)["bbx_usage_settlement_error"] is True
    assert len(service.beats) == 1
    assert checkpoint.session.state["bbx_usage_outbox"]


async def test_trace_failure_cannot_hide_a_retained_usage_settlement_error():
    service = ReceiptService()
    service.before_commit = 10

    async def trace(_task, _agent, body, **_kwargs):
        if body["kind"] == "model_output":
            raise RuntimeError("Synthetic output trace failure")
        return []

    service.record_agent_trace = AsyncMock(side_effect=trace)
    runner, _ = runner_for(service)
    model = client()
    result = await invoke(runner, model)
    assert result.end_reason == "runtime_error"
    assert result.receipt["reason"] == "费用登记待恢复"
    assert result.receipt["error"] == {
        "category": "usage_settlement",
        "scope": "infrastructure",
        "transient": True,
    }
    assert model._index == 1 and service.saved is not None
    assert service.saved["session"]["state"]["bbx_usage_outbox"]
    assert (
        sum(
            call.args[2]["kind"] == "model_output"
            for call in service.record_agent_trace.await_args_list
        )
        == 1
    )


@pytest.mark.parametrize(
    ("details", "provider", "known"),
    [
        (None, "openai", False),
        ({}, "openai", False),
        ({"input_token_count": 0, "output_token_count": 0}, "openai", True),
        ({"output_token_count": 3}, "openai", False),
        (
            {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 2, "output_token_count": 3},
            "openai",
            True,
        ),
        (
            {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 2, "output_token_count": 3},
            "anthropic",
            False,
        ),
    ],
)
def test_usage_completeness_requires_explicit_input_and_output_counters(details, provider, known):
    assert _usage_known(details, provider) is known
