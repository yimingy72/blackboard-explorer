"""Reusing an Explore session cannot let the previous execution write its control state."""

from datetime import datetime
from uuid import uuid4

import pytest
from bbx_blackboard.conversations import Conversations
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import update
from test_final_receipt_fence_integration import finish_http as finish_http
from test_ordinary_usage_receipts_integration import bill, prepared, save
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url
from test_store_integration import fact, intent, projections, task

pytestmark = pytest.mark.integration


async def test_reactivated_explore_rejects_stale_and_missing_execution_fences(finish_http):
    service, board, http, _ = finish_http
    from pathlib import Path

    await ProfileStore(service.repo.engine).ensure_default(
        Path(__file__).resolve().parents[3] / "profiles/default"
    )
    tid = await task(service)
    registration = await board.register_agent(tid, "explore")
    aid = registration["agent_id"]
    initial = (await service.state(tid))["agents"][aid]
    assert datetime.fromisoformat(registration["started_at"]) == initial["started_at"]
    conversations = Conversations(service.repo.engine)
    await conversations.put_session(tid, aid, {"original": True}, "opening", "native", 0, [])
    await service.post_fact(tid, aid, fact())
    iid = (await service.post_intent(tid, aid, intent(claim=True)))["id"]
    receipt = {
        "accepted": False,
        "error": {"category": "connection", "scope": "provider", "transient": True},
    }
    await service.finish_agent(tid, aid, receipt, "runtime_error")
    registration = await board.register_agent(tid, "explore", intent_id=iid)
    assert registration["agent_id"] == aid
    await service.claim_for(tid, iid, aid)
    current = (await service.state(tid))["agents"][aid]
    assert datetime.fromisoformat(registration["started_at"]) == current["started_at"]
    assert current["started_at"] != initial["started_at"]
    before = await service.state(tid)
    session = await conversations.get_session(tid, aid)
    path = f"/api/tasks/{tid}/agents/{aid}"
    headers = {"Authorization": "Bearer final-receipt-service"}
    for fence in ({}, {"expected_started_at": initial["started_at"].isoformat()}):
        responses = [
            await http.post(
                path + "/finish",
                headers=headers,
                json={
                    "receipt": receipt,
                    "end_reason": "runtime_error",
                    "expected_run_number": 1,
                    "expected_agent_run_id": aid,
                    **fence,
                },
            ),
            await http.patch(
                path,
                headers=headers,
                json={
                    "steps": 9,
                    "context_tokens": 9999,
                    "usage": {},
                    "last_seen_version": 9999,
                    **fence,
                },
            ),
            await http.put(
                path + "/session",
                headers=headers,
                json={
                    "session": {"stale": True},
                    "opening_instructions": "old",
                    "origin": "native",
                    "expected_revision": 1,
                    **fence,
                },
            ),
        ]
        assert [response.status_code for response in responses] == [422, 422, 409]
        assert all("stale_agent_execution" in response.text for response in responses)
        assert await service.state(tid) == before
        assert await conversations.get_session(tid, aid) == session
    fence = {"expected_started_at": current["started_at"].isoformat()}
    response = await http.put(
        path + "/session",
        headers=headers,
        json={
            "session": {"new": True},
            "opening_instructions": "new",
            "origin": "native",
            "expected_revision": 1,
            **fence,
        },
    )
    assert response.status_code == 200 and response.json()["revision"] == 2
    response = await http.patch(
        path,
        headers=headers,
        json={
            "steps": 1,
            "context_tokens": 100,
            "usage": {},
            "last_seen_version": 1,
            **fence,
        },
    )
    assert response.status_code == 200
    receipt = {"accepted": True, "data": {"result": "new execution"}}
    body = {
        "receipt": receipt,
        "end_reason": "normal",
        "expected_run_number": 1,
        "expected_agent_run_id": aid,
        **fence,
    }
    assert (await http.post(path + "/finish", headers=headers, json=body)).status_code == 200
    duplicate = await http.post(path + "/finish", headers=headers, json=body)
    assert duplicate.status_code == 200 and duplicate.json() == []
    # Terminal read-only review remains available with its own valid message claim.
    mid = uuid4()
    await conversations.post_message(tid, aid, mid, "Explain the result")
    claim = await conversations.claim(tid, aid, mid, "review")
    await conversations.put_session(
        tid,
        aid,
        {"review": True},
        "review",
        "native",
        2,
        [],
        review_claim={"id": mid, "claim_token": claim["claim_token"]},
    )
    reply = await conversations.complete(
        tid,
        aid,
        mid,
        claim["claim_token"],
        {"review": "complete"},
        "review",
        "native",
        3,
        "Explanation",
        {},
    )
    assert reply["content"] == "Explanation"
    before_replay = await projections(service, tid)
    await service.replay(tid)
    assert await projections(service, tid) == before_replay


@pytest.mark.parametrize("identified_execution", [False, True])
async def test_old_explore_usage_receipt_only_accounts_after_reactivation(
    board_service, identified_execution
):
    service = board_service
    tid, aid, receipt, amount = await prepared(service, persisted=False)
    original = (await service.state(tid))["agents"][aid]
    if identified_execution:
        receipt.request.expected_started_at = original["started_at"]
    await service.post_fact(tid, aid, fact())
    iid = (await service.post_intent(tid, aid, intent(claim=True)))["id"]
    await service.finish_agent(tid, aid, {"accepted": False}, "runtime_error")
    await save(service, tid, aid, receipt)
    assert await service.register_agent(tid, "explore", intent_id=iid) == aid
    await service.claim_for(tid, iid, aid)
    current = (await service.state(tid))["agents"][aid]
    first = await bill(service, tid, aid, receipt)
    duplicate = await bill(service, tid, aid, receipt)
    assert first["cost"] == amount and duplicate["events"] == []
    assert first["events"][0]["payload"]["accounting_only"] is True
    after = await service.state(tid)
    for field in (
        "steps",
        "context_tokens",
        "last_seen_version",
        "last_heartbeat_at",
        "status",
        "started_at",
    ):
        assert after["agents"][aid][field] == current[field]
    assert after["intents"][iid]["holder"] == aid
    assert after["task"]["usage"]["output_tokens"] == receipt.request.usage.output_tokens


async def test_missing_fence_detects_legacy_reactivation_without_previous_receipt(board_service):
    service = board_service
    tid = await task(service)
    aid = await service.register_agent(tid, "explore", is_seed=True)
    await service.finish_agent(tid, aid, {}, "runtime_error")
    assert await service.register_agent(tid, "explore", is_seed=True) == aid
    async with service.repo.engine.begin() as conn:
        await conn.execute(
            update(s.agent_runs)
            .where(
                s.agent_runs.c.task_id == tid,
                s.agent_runs.c.id == aid,
            )
            .values(previous_receipt=None)
        )
    with pytest.raises(RuleViolation) as stale_finish:
        await service.finish_agent(tid, aid, {}, "runtime_error")
    assert stale_finish.value.code == "stale_agent_execution"
    with pytest.raises(HTTPException) as stale_checkpoint:
        await Conversations(service.repo.engine).put_session(
            tid,
            aid,
            {},
            "old",
            "native",
            0,
            [],
        )
    assert stale_checkpoint.value.status_code == 409
