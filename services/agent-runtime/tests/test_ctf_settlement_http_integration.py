"""Settlement transport loss crosses real PG/HTTP/native MAF without cancelling peers."""

import asyncio
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.settings import Settings
from pydantic import SecretStr
from test_ctf_cooperation_integration import HeldScriptedClient, create_task, wait_until
from test_ctf_cooperation_integration import cooperation_board as cooperation_board
from test_ctf_vertical_integration import SimulatedDrain
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

pytestmark = pytest.mark.integration


class MemberDrain(SimulatedDrain):
    """Expose the member drain boundary without claiming real process termination."""

    async def stop_member(self, task_id, member):
        return {"drained": True}


@pytest.mark.parametrize("lost_operation", ["runtime-state", "finish_turn"])
async def test_settlement_transport_loss_keeps_peer_running_and_does_not_replay(
    cooperation_board, monkeypatch, lost_operation
):
    board, ctf, _, configured, probe, _ = cooperation_board
    tid = await create_task(board, ctf, configured, "10")
    entered = []
    releases = {mid: asyncio.Event() for mid in ("lead", "member-1")}
    clients = {}

    def factory(member, _profile):
        mid = member["id"]
        assert mid not in clients, "Control recovery must not recreate the model client"
        client = HeldScriptedClient(mid, entered, releases[mid])
        client.steps[0] = replace(client.steps[0], expect_contains=None)
        clients[mid] = client
        return client

    settings = Settings.model_construct(deepseek_api_key=SecretStr("unused-scripted"))
    envd = MemberDrain()
    runner = CtfRunner(ctf, settings, factory, envd=envd)
    coordinator = CtfCoordinator(board, settings, runner=runner, envd=envd)
    setup_turn = await ctf.runtime(
        tid, "claim_turn", agent_id="lead", runtime_instance=coordinator.instance
    )
    await ctf.tool(
        tid,
        setup_turn["token"],
        "create_member",
        display_name="Held peer",
        request_id=str(uuid4()),
    )
    assignment = await ctf.tool(
        tid,
        setup_turn["token"],
        "post_message",
        recipient_id="member-1",
        body="Compute arithmetic for member-1",
        message_id=str(uuid4()),
        kind="instruction",
    )
    settlement_started = False
    settle = runner.settle

    async def enter_settlement(task_id, member, turn, reason, answer):
        nonlocal settlement_started
        if member["id"] == "lead":
            assert reason == "completed" and answer == "Completed arithmetic for lead"
            assert len(clients["lead"].received_messages) == 1
            settlement_started = True
        return await settle(task_id, member, turn, reason, answer)

    monkeypatch.setattr(runner, "settle", enter_settlement)
    assert probe.http is not None
    transport = probe.http._transport
    send = transport.handle_async_request
    operations = Counter()
    injected = False
    affected = None

    async def lose_response(request):
        nonlocal injected
        operation = request.url.path.rsplit("/", 1)[-1]
        targeted = settlement_started and asyncio.current_task() is affected
        if targeted:
            operations[(request.method, operation)] += 1
        response = await send(request)
        if targeted and not injected and operation == lost_operation:
            assert response.status_code == 200
            await response.aclose()
            injected = True
            raise httpx.RemoteProtocolError("Scripted settlement response loss", request=request)
        return response

    monkeypatch.setattr(transport, "handle_async_request", lose_response)
    try:
        await coordinator.tick(tid)
        affected, affected_turn = coordinator.active[(tid, "lead")]
        peer, peer_turn = coordinator.active[(tid, "member-1")]
        await wait_until(lambda: set(entered) == {"lead", "member-1"})
        assert not affected.done() and not peer.done()
        releases["lead"].set()
        await asyncio.wait_for(affected, 10)
        assert injected
        assert not peer.done() and not peer.cancelled()
        state = await ctf.state(tid)
        finished = next(turn for turn in state["turns"] if turn["id"] == affected_turn["id"])
        assert finished["end_reason"] == "completed"
        assert finished["final_answer"] == "Completed arithmetic for lead"
        assert Decimal(finished["usage"]["cost"]) == Decimal("0.0002")
        assert len(finished["usage_requests"]) == 1
        assert finished["checkpoint_revision"] > 0
        assert state["task"]["ctf_control"]["phase"] == "running"
        assert state["task"]["ctf_conclusion"] is None
        assert Decimal(state["task"]["usage"]["cost"]) == Decimal("0.0002")
        assert operations[("POST", "finish_turn")] == 1
        if lost_operation == "runtime-state":
            assert operations[("GET", "runtime-state")] == 2
        else:
            assert operations[("GET", "state")] == 1
        assert not runner.pending_checkpoints and not runner.pending_settlements
        assert await coordinator.tick(tid)
        assert not peer.done() and not peer.cancelled()
        assert {mid: len(client.received_messages) for mid, client in clients.items()} == {
            "lead": 1,
            "member-1": 0,
        }
        releases["member-1"].set()
        await asyncio.wait_for(peer, 10)
        final = await ctf.state(tid)
        assert final["task"]["ctf_control"]["phase"] == "running"
        assert len(final["turns"]) == 2
        assert all(turn["end_reason"] == "completed" for turn in final["turns"])
        assert Decimal(final["task"]["usage"]["cost"]) == Decimal("0.0004")
        assert not final["task"]["ctf_control"].get("budget_reservations")
        assert not final["task"]["ctf_control"].get("budget_unknown_reservations")
        receipts = [
            message
            for message in final["messages"]
            if message["notification_purpose"] == "assignment_result"
        ]
        assert len(receipts) == 1 and receipts[0]["source_turn_id"] == peer_turn["id"]
        delivered = next(
            message for message in final["messages"] if message["id"] == assignment["id"]
        )
        assert delivered["status"] == "delivered"
        assert delivered["delivered_turn_id"] == peer_turn["id"]
        assert all(len(client.received_messages) == 1 for client in clients.values())
        saved = await ctf.get_agent_session(tid, "lead")
        assert "Completed arithmetic for lead" in str(saved["session"]["state"]["in_memory"])
    finally:
        for release in releases.values():
            release.set()
        await coordinator.detach(tid, deleting=True)


async def test_joined_review_recovery_accepts_real_native_checkpoint_usage_and_trace(
    cooperation_board, monkeypatch
):
    from uuid import UUID

    from bbx_runtime.ctf.coordinator import failure_diagnostic
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptUsage

    board, ctf, _, configured, probe, _ = cooperation_board
    tid = await create_task(board, ctf, configured, "10")
    await ctf.runtime(
        tid,
        "request_finish",
        actor="system",
        request_id=str(uuid4()),
        conclusion={"end_reason": "user_stop", "summary": "Review arithmetic"},
    )
    await ctf.runtime(tid, "finalize_close", drained=True)
    assert probe.http is not None
    transport = probe.http._transport
    # Only lifecycle/user-input setup accesses the fixture service directly.
    service = transport.app.state.ctf_service
    objects = transport.app.state.objects
    archive_uri = f"workspace/{tid}.tar.zst"
    await objects.put(archive_uri, b"Synthetic archive marker")
    await board.record_archive(tid, archive_uri, 24, "none")
    await board.record_cleanup(tid)
    await service.post_message(
        UUID(tid), "user", "lead", "Explain the saved arithmetic", str(uuid4())
    )
    asgi_send = transport.handle_async_request
    claims = 0

    async def lose_claim_response(request):
        nonlocal claims
        response = await asgi_send(request)
        if request.url.path.endswith("/runtime/claim_review_turn"):
            claims += 1
            if claims == 1:
                assert response.status_code == 200
                await response.aclose()
                raise httpx.RemoteProtocolError(
                    "Scripted review claim response loss", request=request
                )
        return response

    monkeypatch.setattr(transport, "handle_async_request", lose_claim_response)
    turn = await ctf.runtime(
        tid, "claim_review_turn", agent_id="lead", runtime_instance="joined-http-runtime"
    )
    assert claims == 2 and turn["token"]
    assert (await ctf.tool(tid, turn["token"], "list_challenges"))["challenges"] == []
    assert len((await ctf.state(tid))["turns"]) == 1
    client = ScriptedChatClient(
        [
            ScriptStep(
                text="Arithmetic explanation retained",
                usage=ScriptUsage(prompt_cache_miss_tokens=100, completion_tokens=100),
            )
        ]
    )
    settings = Settings.model_construct(deepseek_api_key=SecretStr("unused-scripted"))
    runner = CtfRunner(ctf, settings, lambda *_: client)
    writes = 0

    async def lose_unsubmitted_finish(request):
        nonlocal writes
        if request.url.path.endswith("/runtime/finish_turn"):
            writes += 1
            raise httpx.RemoteProtocolError("Scripted unsubmitted review write", request=request)
        return await asgi_send(request)

    monkeypatch.setattr(transport, "handle_async_request", lose_unsubmitted_finish)
    await runner.run(tid, {"id": "lead", "display_name": "Lead", "role": "lead"}, turn)
    key = (tid, "lead")
    assert writes == 1 and key in runner.pending_settlements
    assert not runner.pending_checkpoints
    saved = await ctf.get_agent_session(tid, "lead")
    assert saved["session"]["state"]["ctf_steps"][turn["id"]] == 1
    assert not saved["session"]["state"]["ctf_review_usage_outbox"]
    error = httpx.RemoteProtocolError("Synthetic transport failure")
    arguments = {
        "runtime_instance": "joined-http-runtime",
        "failed_turn_id": turn["id"],
        "agent_id": "lead",
        "generation": turn["generation"],
        "joined": True,
        "answer": runner.pending_settlements[key][3],
        "failure": failure_diagnostic("review_settlement", error, task_id=tid),
    }
    result = await ctf.runtime(tid, "recover_reviews", **arguments)
    assert result["recovered"] == 1
    assert result["turn"]["end_reason"] == "interrupted"
    assert result["turn"]["final_answer"] == "Arithmetic explanation retained"
    assert result["turn"]["usage"]["cost"] == "0.0002"
    assert await ctf.get_agent_session(tid, "lead") == saved
    state = await ctf.state(tid)
    assert state["task"]["ctf_control"]["phase"] == "closed"
    assert state["task"]["ctf_control"]["review_usage"]["cost"] == "0.0002"
    assert (
        next(member for member in state["members"] if member["id"] == "lead")["current_turn_id"]
        is None
    )
    assert await ctf.runtime(tid, "recover_reviews", **arguments) == {
        "recovered": 0,
        "turn": result["turn"],
    }
    assert writes == 1 and len(client.received_messages) == 1
