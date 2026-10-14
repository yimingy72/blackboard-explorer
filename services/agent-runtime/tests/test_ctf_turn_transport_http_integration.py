"""Committed turn settlement loss is reconciled over real PostgreSQL and ASGI HTTP."""

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from bbx_runtime.ctf.runner import CtfRunner
from test_ctf_cooperation_integration import cooperation_board as cooperation_board
from test_ctf_cooperation_integration import create_task
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

pytestmark = pytest.mark.integration


async def test_committed_finish_response_drop_keeps_answer_usage_and_unknown_reservation(
    cooperation_board,
):
    board, ctf, _, configured, probe, _ = cooperation_board
    tid = await create_task(board, ctf, configured, "10")
    turn = await ctf.runtime(tid, "claim_turn", agent_id="lead", runtime_instance="settle-loss")
    await ctf.runtime(
        tid,
        "bill_usage",
        agent_id="lead",
        turn_id=turn["id"],
        generation=turn["generation"],
        request_id=str(uuid4()),
        usage={"cost": "0.2"},
    )
    reservation_id = f"{turn['id']}:synthetic-sent"
    await ctf.runtime(
        tid,
        "authorize_member",
        agent_id="lead",
        turn_id=turn["id"],
        generation=turn["generation"],
        reservation_id=reservation_id,
        reservation_cost="0.4",
    )
    await ctf.runtime(
        tid,
        "mark_reservation_sent",
        agent_id="lead",
        turn_id=turn["id"],
        generation=turn["generation"],
        reservation_id=reservation_id,
    )
    dropped = False

    async def drop_committed_response(response):
        nonlocal dropped
        if response.request.url.path.endswith("/finish_turn") and not dropped:
            assert response.status_code == 200
            await response.aread()
            dropped = True
            raise httpx.RemoteProtocolError(
                "synthetic loss after committed settlement", request=response.request
            )

    assert probe.http is not None
    probe.http.event_hooks["response"].append(drop_committed_response)
    try:
        runner = CtfRunner(ctf, cast(Any, SimpleNamespace()))
        assert await runner.settle(
            tid, {"id": "lead"}, turn, "completed", "synthetic complete answer"
        )
    finally:
        probe.http.event_hooks["response"].remove(drop_committed_response)
    assert dropped
    assert probe.operations[("POST", "finish_turn")] == 1
    state = await ctf.state(tid)
    saved = next(item for item in state["turns"] if item["id"] == turn["id"])
    assert saved["status"] == "finished"
    assert saved["end_reason"] == "completed"
    assert saved["final_answer"] == "synthetic complete answer"
    assert saved["usage"]["cost"] == "0.2"
    assert state["task"]["usage"]["cost"] == "0.2"
    assert (
        state["task"]["ctf_control"]["budget_unknown_reservations"][reservation_id]["cost"] == "0.4"
    )
    assert not runner.pending_settlements
    assert state["task"]["ctf_control"]["phase"] == "running"


async def test_review_claim_response_loss_reclaims_fenced_token_and_runs_one_native_review(
    cooperation_board,
):
    from bbx_blackboard.auth import issue_user_token
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall

    board, ctf, _, configured, probe, _ = cooperation_board
    tid = await create_task(board, ctf, configured, "10")
    await ctf.runtime(
        tid,
        "request_finish",
        actor="user",
        request_id=str(uuid4()),
        conclusion={"end_reason": "user_stop", "summary": "Synthetic terminal review"},
    )
    await ctf.runtime(tid, "finalize_close", drained=True)
    transport = board._http._transport
    assert isinstance(transport, httpx.ASGITransport)
    app = cast(Any, transport.app)
    uri = f"workspace/{tid}.tar.zst"
    app.state.objects.files[uri] = b"synthetic archive marker"
    await board.record_archive(tid, uri, len(app.state.objects.files[uri]), "none")
    await board.record_cleanup(tid)
    message_id = str(uuid4())
    response = await board._http.post(
        f"/api/tasks/{tid}/agents/lead/messages",
        json={"id": message_id, "content": "Inspect synthetic saved task"},
        headers={"Authorization": f"Bearer {issue_user_token(app.state.settings, 'admin')}"},
    )
    assert response.status_code == 200, response.text
    dropped = False

    async def drop_claim_response(response):
        nonlocal dropped
        if response.request.url.path.endswith("/claim_review_turn") and not dropped:
            assert response.status_code == 200
            await response.aread()
            dropped = True
            raise httpx.RemoteProtocolError(
                "synthetic committed review claim response loss", request=response.request
            )

    assert probe.http is not None
    probe.http.event_hooks["response"].append(drop_claim_response)
    try:
        turn = await ctf.runtime(
            tid, "claim_review_turn", agent_id="lead", runtime_instance="same-review-runtime"
        )
    finally:
        probe.http.event_hooks["response"].remove(drop_claim_response)
    assert turn and turn["token"]
    assert probe.operations[("POST", "claim_review_turn")] == 2
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("list_challenges"),)),
            ScriptStep(text="synthetic complete review answer"),
        ]
    )
    runner = CtfRunner(ctf, cast(Any, SimpleNamespace()), client_factory=lambda *_: client)
    await runner.run(tid, {"id": "lead", "display_name": "Lead", "role": "lead"}, turn)
    state = await ctf.state(tid)
    reviews = [item for item in state["turns"] if item["purpose"] == "review"]
    assert len(reviews) == 1 and reviews[0]["id"] == turn["id"]
    assert reviews[0]["generation"] == turn["generation"]
    assert reviews[0]["end_reason"] == "completed"
    assert reviews[0]["final_answer"] == "synthetic complete review answer"
    assert len(client.received_options) == 2
    replies = [
        message
        for message in state["messages"]
        if message["notification_purpose"] == f"review_result:{message_id}"
    ]
    assert len(replies) == 1 and replies[0]["body"] == "synthetic complete review answer"


async def test_claim_readback_to_reclaim_race_never_creates_new_review_turn(cooperation_board):
    from bbx_blackboard.auth import issue_user_token

    board, ctf, _, configured, probe, _ = cooperation_board
    tid = await create_task(board, ctf, configured, "10")
    await ctf.runtime(
        tid,
        "request_finish",
        actor="user",
        request_id=str(uuid4()),
        conclusion={"end_reason": "user_stop", "summary": "Synthetic terminal review"},
    )
    await ctf.runtime(tid, "finalize_close", drained=True)
    transport = board._http._transport
    assert isinstance(transport, httpx.ASGITransport)
    app = cast(Any, transport.app)
    uri = f"workspace/{tid}.tar.zst"
    app.state.objects.files[uri] = b"synthetic archive marker"
    await board.record_archive(tid, uri, len(app.state.objects.files[uri]), "none")
    await board.record_cleanup(tid)
    message_id = str(uuid4())
    response = await board._http.post(
        f"/api/tasks/{tid}/agents/lead/messages",
        json={"id": message_id, "content": "Inspect synthetic saved task"},
        headers={"Authorization": f"Bearer {issue_user_token(app.state.settings, 'admin')}"},
    )
    assert response.status_code == 200, response.text
    dropped, retired = False, False
    old_turn_id = None

    async def interrupt_between_proof_and_reclaim(response):
        nonlocal dropped, retired, old_turn_id
        path = response.request.url.path
        if path.endswith("/claim_review_turn") and not dropped:
            await response.aread()
            dropped = True
            raise httpx.RemoteProtocolError(
                "synthetic committed claim response loss", request=response.request
            )
        if path.endswith("/runtime-state") and dropped and not retired:
            await response.aread()
            turn = next(item for item in response.json()["turns"] if item["purpose"] == "review")
            old_turn_id = turn["id"]
            retired = True
            await ctf.runtime(
                tid,
                "finish_turn",
                agent_id="lead",
                turn_id=turn["id"],
                generation=turn["generation"],
                end_reason="interrupted",
                answer="synthetic external interruption",
            )

    assert probe.http is not None
    probe.http.event_hooks["response"].append(interrupt_between_proof_and_reclaim)
    try:
        turn = await ctf.runtime(
            tid, "claim_review_turn", agent_id="lead", runtime_instance="same-review-runtime"
        )
    finally:
        probe.http.event_hooks["response"].remove(interrupt_between_proof_and_reclaim)
    assert turn is None and dropped and retired
    state = await ctf.state(tid)
    reviews = [item for item in state["turns"] if item["purpose"] == "review"]
    assert len(reviews) == 1 and reviews[0]["id"] == old_turn_id
    assert reviews[0]["status"] == "finished"
    assert state["members"][0]["current_turn_id"] is None
    assert probe.operations[("POST", "claim_review_turn")] == 2
