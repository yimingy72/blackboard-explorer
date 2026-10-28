"""Offline coverage of bounded indexes, per-item transactions and durable progress."""

import json
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.ctf_board import CtfBoardMixin
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from sqlalchemy import insert, update
from test_ctf_assignment_feedback import change_request, events, mailbox
from test_ctf_assignment_feedback import feedback as feedback_fixture
from test_ctf_assignment_feedback import storage as storage_fixture
from test_ctf_pagination import pagination_app as pagination_app_fixture

feedback = feedback_fixture
storage = storage_fixture
pagination_app = pagination_app_fixture


@pytest.mark.parametrize(
    "changes",
    [
        {"work_status": []},
        {"work_status": {}},
        {"max_bytes": True},
        {"max_bytes": 1000},
        {"owner_id": []},
        {"cursor": []},
    ],
)
async def test_summary_tool_invalid_query_is_422(pagination_app, changes):
    setup = pagination_app
    setup.backend.list_challenges = CtfBoardMixin().list_challenges
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/tasks/{setup.tid}/ctf/tools/list_challenges",
            json={"view": "summary", **changes},
            headers=setup.agent_headers,
        )
    assert response.status_code == 422


async def test_invalid_view_does_not_query_database():
    with pytest.raises(HTTPException) as error:
        await CtfBoardMixin().list_challenges(uuid4(), view=cast(Any, []))
    assert error.value.status_code == 422


@pytest.mark.parametrize("operation", ["create_challenges", "assign_challenges"])
async def test_batch_routes_validate_envelope_before_individual_challenge_routing(
    pagination_app, operation
):
    setup = pagination_app
    setup.backend.authorize_tool.return_value["member"]["role"] = "lead"
    setup.app.state.profile_store.get.return_value = {
        "worker_tools": {"lead": {"builtin": [operation]}},
    }
    method = AsyncMock(return_value={"results": []})
    setattr(setup.backend, operation, method)
    item = {"request_id": str(uuid4())}
    item.update(
        {"title": "Synthetic target"}
        if operation == "create_challenges"
        else {
            "challenge_id": str(setup.cid),
            "expected_revision": 1,
            "owner_id": "worker",
            "instructions": [{"recipient_id": "worker", "body": "Start the target"}],
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app),
        base_url="http://test",
    ) as client:
        path = f"/api/tasks/{setup.tid}/ctf/tools/{operation}"
        malformed = await client.post(
            path, json={"items": [item, {"request_id": "bad"}]}, headers=setup.agent_headers
        )
        assert malformed.status_code == 422
        method.assert_not_awaited()
        result = await client.post(path, json={"items": [item]}, headers=setup.agent_headers)
    assert result.status_code == 200
    assert method.await_count == 1
    assert method.await_args is not None
    assert method.await_args.kwargs["items"][0]["request_id"] == item["request_id"]


async def test_summary_pages_preserve_every_identity_and_bound_old_large_fields(feedback):
    service, tid = feedback.service, feedback.tid
    rows = []
    for index in range(64):
        cid = str(uuid4())
        data = {
            **feedback.challenge,
            "id": cid,
            "title": "题目" * (10000 if index == 0 else 70),
            "external_id": "来源" * (10000 if index == 0 else 50),
            "description": "Full original statement. " * 2000,
            "owner_id": "alice" if index % 2 else "bob",
            "work_status": "blocked" if index % 3 else "in_progress",
        }
        rows.append(data)
    with feedback.database.begin() as conn:
        conn.execute(
            insert(s.ctf_challenges),
            [{"task_id": tid, "id": row["id"], "revision": 1, "data": row} for row in rows],
        )
    seen, cursor = [], None
    while True:
        result = await service.list_challenges(tid, view="summary", cursor=cursor)
        assert len(json.dumps(result, ensure_ascii=False).encode()) <= 6000
        assert result["view"] == "summary" and result["challenges"]
        assert all("description" not in item for item in result["challenges"])
        seen.extend(result["challenges"])
        cursor = result["next_cursor"]
        if cursor is None:
            assert result["next_offset"] is None
            break
    assert {row["id"] for row in seen} == {row["id"] for row in rows} | {feedback.challenge["id"]}
    assert len(seen) == 65
    assert all(row["revision"] == 1 for row in seen)
    long = next(row for row in seen if row["id"] == rows[0]["id"])
    assert set(long["truncated_fields"]) == {"title", "external_id"}
    assert long["detail_ref"] == rows[0]["id"]
    assert (await service.get_challenge(tid, rows[0]["id"]))["external_id"] == rows[0][
        "external_id"
    ]
    # The full service/Web path remains unprojected, including legacy offset semantics.
    full = await service.list_challenges(tid, limit=2, offset=1)
    assert "view" not in full and full["next_offset"] == 3
    assert all("description" in row for row in full["challenges"])
    filtered = await service.list_challenges(
        tid,
        view="summary",
        owner_id="alice",
        work_status="blocked",
        max_bytes=1024,
    )
    assert len(json.dumps(filtered, ensure_ascii=False).encode()) <= 1024
    assert filtered["challenges"] and filtered["next_cursor"]
    assert all(
        row["owner_id"] == "alice" and row["work_status"] == "blocked"
        for row in filtered["challenges"]
    )
    with pytest.raises(HTTPException) as error:
        await service.list_challenges(
            tid, view="summary", cursor=filtered["next_cursor"], owner_id="bob"
        )
    assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        await service.list_challenges(tid, view="summary", cursor=filtered["next_cursor"], offset=1)


async def test_batch_create_partial_success_replay_and_source_conflict(feedback):
    service, tid = feedback.service, feedback.tid
    items = [
        {
            "request_id": str(uuid4()),
            "title": "独立题",
            "external_id": source,
            "description": "Long source. " * 2000,
        }
        for source in ("provider:one", "provider:one", "provider:three")
    ]
    result = await service.create_challenges(tid, "user", items)
    assert [item["status"] for item in result["results"]] == ["ok", "error", "ok"]
    first = result["results"][0]["challenge"]["id"]
    assert result["results"][1]["error"] == {
        "status_code": 409,
        "detail": {"code": "external_id_conflict", "challenge_ids": [first]},
    }
    assert "Long source" not in json.dumps(result)
    before = events(feedback)
    assert await service.create_challenges(tid, "user", items) == result
    assert events(feedback) == before
    changed = await service.create_challenges(tid, "user", [{**items[0], "title": "Changed"}])
    assert changed["results"][0]["error"]["status_code"] == 409
    assert events(feedback) == before


@pytest.mark.parametrize(
    "failure", [RuntimeError("Synthetic uncertain transport"), HTTPException(503, "Unavailable")]
)
async def test_batch_validation_precedes_writes_and_unknown_error_keeps_committed_receipt(
    feedback, monkeypatch, failure
):
    service, tid = feedback.service, feedback.tid
    first = {"request_id": str(uuid4()), "title": "First"}
    before = events(feedback)
    with pytest.raises(HTTPException) as error:
        await service.create_challenges(
            tid, "user", [first, {"request_id": "invalid", "title": "Second"}]
        )
    assert error.value.status_code == 422 and events(feedback) == before
    original = service.create_challenge

    async def fail_second(tid, actor, **kwargs):
        if kwargs["title"] == "Second":
            raise failure
        return await original(tid, actor, **kwargs)

    second = {"request_id": str(uuid4()), "title": "Second"}
    monkeypatch.setattr(service, "create_challenge", fail_second)
    with pytest.raises(type(failure)):
        await service.create_challenges(tid, "user", [first, second])
    assert len(events(feedback)) == len(before) + 1
    monkeypatch.setattr(service, "create_challenge", original)
    replay = await service.create_challenges(tid, "user", [first, second])
    assert all(item["status"] == "ok" for item in replay["results"])
    assert len(events(feedback)) == len(before) + 2


async def test_batch_assign_partial_cas_and_scoped_idempotent_mail(feedback):
    service, tid = feedback.service, feedback.tid
    created = await service.create_challenges(
        tid,
        "user",
        [{"request_id": str(uuid4()), "title": f"Target {index}"} for index in range(3)],
    )
    items = [
        {
            "request_id": str(uuid4()),
            "challenge_id": result["challenge"]["id"],
            "expected_revision": 99 if index == 1 else 1,
            "owner_id": "alice",
            "instructions": [{"recipient_id": "alice", "body": "Start this independent target"}],
        }
        for index, result in enumerate(created["results"])
    ]
    result = await service.assign_challenges(tid, "user", items)
    assert [row["status"] for row in result["results"]] == ["ok", "error", "ok"]
    messages = mailbox(feedback)
    assert {row["challenge_id"] for row in messages} == {
        items[0]["challenge_id"],
        items[2]["challenge_id"],
    }
    assert all(row["kind"] == "instruction" for row in messages)
    assert result == await service.assign_challenges(tid, "user", items)
    assert mailbox(feedback) == messages


async def owned(feedback):
    challenge = await feedback.service.update_challenge(
        feedback.tid, "user", **change_request(feedback)
    )
    return await feedback.service.update_challenge(
        feedback.tid,
        "user",
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=challenge["revision"],
        action="collaborators",
        collaborator_ids=["bob"],
    )


def record_request(feedback, challenge, stage="result"):
    return {
        "actor": "alice",
        "generation": 1,
        "turn_id": feedback.turns["alice"],
        "challenge_id": challenge["id"],
        "request_id": str(uuid4()),
        "body": "Synthetic observation",
        "progress": {"stage": stage, "title": "Observed", "summary": "A reproducible result"},
    }


async def test_important_progress_notifies_once_without_using_turn_result_slot(feedback):
    service, tid = feedback.service, feedback.tid
    challenge = await owned(feedback)
    with feedback.database.begin() as conn:
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX result_slot "
            "ON ctf_messages(task_id, source_turn_id, notification_purpose)"
        )
    attempt = record_request(feedback, challenge, "attempt")
    await service.append_record(tid, **attempt)
    assert mailbox(feedback) == []
    for stage in ("breakthrough", "result"):
        request = record_request(feedback, challenge, stage)
        record = await service.append_record(tid, **request)
        assert await service.append_record(tid, **request) == record
    messages = mailbox(feedback)
    assert len(messages) == 4
    assert {row["recipient_id"] for row in messages} == {"lead", "bob"}
    assert all(
        row["source_turn_id"] is None and row["challenge_id"] == challenge["id"] for row in messages
    )
    assert all(json.loads(row["body"])["created_version"] > 0 for row in messages)
    state = await service.runtime_state(tid)
    assert set(state["ready_member_ids"]) == {"lead", "bob"}
    claimed = await service.claim_messages(tid, "lead", feedback.current_id, 2)
    assert len(claimed) == 2 and all(row["challenge_id"] == challenge["id"] for row in claimed)


async def test_progress_and_mail_rollback_together(feedback, monkeypatch):
    service, tid = feedback.service, feedback.tid
    challenge = await owned(feedback)
    before = events(feedback)
    original = service._message
    count = 0

    async def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("Synthetic mail failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_message", fail_second)
    request = record_request(feedback, challenge)
    with pytest.raises(RuntimeError, match="mail failure"):
        await service.append_record(tid, **request)
    assert events(feedback) == before and mailbox(feedback) == []
    monkeypatch.setattr(service, "_message", original)
    await service.append_record(tid, **request)
    assert len(mailbox(feedback)) == 2


async def test_candidate_and_trusted_verification_notify_without_fabricating_acceptance(feedback):
    service, tid = feedback.service, feedback.tid
    challenge = await owned(feedback)
    request = dict(
        actor="alice",
        generation=1,
        turn_id=feedback.turns["alice"],
        challenge_id=challenge["id"],
        request_id=str(uuid4()),
        expected_revision=challenge["revision"],
        status="candidate",
        summary="Observed correct:true is not trusted acceptance",
    )
    candidate = await service.record_candidate(tid, **request)
    assert await service.record_candidate(tid, **request) == candidate
    current = await service.get_challenge(tid, challenge["id"])
    assert current["verification"]["status"] == "candidate"
    assert len(mailbox(feedback)) == 2
    # This internal path models an already-authorized trusted platform result.
    async with service.repo.engine.begin() as conn:
        await service._verification_record(
            conn,
            tid,
            "system",
            str(uuid4()),
            "synthetic",
            current,
            {"status": "accepted", "source": "platform", "summary": "Trusted fixture"},
            "Trusted fixture",
        )
    assert len(mailbox(feedback)) == 5
    assert (await service.get_challenge(tid, challenge["id"]))["verification"][
        "source"
    ] == "platform"


@pytest.mark.parametrize("phase", ["closing", "closed"])
async def test_late_verification_is_saved_but_does_not_wake_stopped_task(feedback, phase):
    service, tid = feedback.service, feedback.tid
    challenge = await owned(feedback)
    with feedback.database.begin() as conn:
        conn.execute(
            update(s.tasks).values(
                status="stopped" if phase == "closed" else "running",
                ctf_control={"phase": phase},
            )
        )
    async with service.repo.engine.begin() as conn:
        await service._verification_record(
            conn,
            tid,
            "system",
            str(uuid4()),
            "synthetic",
            challenge,
            {"status": "unknown", "source": "platform"},
            "Late fixture",
        )
    assert mailbox(feedback) == []
    assert (await service.get_challenge(tid, challenge["id"]))["verification"][
        "status"
    ] == "unknown"


async def test_removed_recipients_do_not_break_progress_and_message_scope_is_immutable(feedback):
    service, tid = feedback.service, feedback.tid
    challenge = await owned(feedback)
    with feedback.database.begin() as conn:
        conn.execute(
            update(s.ctf_members).where(s.ctf_members.c.id == "bob").values(lifecycle="removed")
        )
    await service.append_record(tid, **record_request(feedback, challenge))
    assert [row["recipient_id"] for row in mailbox(feedback)] == ["lead"]
    mid = str(uuid4())
    await service.post_message(tid, "user", "alice", "Scoped", mid, challenge_id=challenge["id"])
    other = await service.create_challenge(tid, "user", str(uuid4()), "Other")
    with pytest.raises(HTTPException, match="Message ID conflict"):
        await service.post_message(tid, "user", "alice", "Scoped", mid, challenge_id=other["id"])
    with pytest.raises(HTTPException) as error:
        await service.post_message(
            tid, "user", "alice", "Cross-task", str(uuid4()), challenge_id=str(uuid4())
        )
    assert error.value.status_code == 404
    await service.post_message(tid, "user", "alice", "Legacy", str(uuid4()))
    assert mailbox(feedback)[-1]["challenge_id"] is None


async def test_stale_verification_notice_distinguishes_record_from_current_board(feedback):
    service, tid = feedback.service, feedback.tid
    challenge = await owned(feedback)
    async with service.repo.engine.begin() as conn:
        await service._verification_record(
            conn,
            tid,
            "system",
            str(uuid4()),
            "synthetic",
            challenge,
            {"status": "accepted", "source": "platform"},
            "Late acceptance",
            expected_revision=challenge["revision"] - 1,
        )
    assert (await service.get_challenge(tid, challenge["id"]))["verification"] == {}
    for row in mailbox(feedback):
        notice = json.loads(row["body"])
        assert notice["summary_applied"] is False
        assert notice["record_verification_status"] == "accepted"
        assert notice["verification_status"] is None


async def test_maximum_batch_failure_receipt_stays_bounded(feedback, monkeypatch):
    async def reject(*args, **kwargs):
        raise HTTPException(409, "错误" * 10000)

    monkeypatch.setattr(feedback.service, "create_challenge", reject)
    result = await feedback.service.create_challenges(
        feedback.tid,
        "user",
        [{"request_id": str(uuid4()), "title": f"Target {index}"} for index in range(20)],
    )
    assert len(result["results"]) == 20
    assert all(row["error"]["status_code"] == 409 for row in result["results"])
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 16000
