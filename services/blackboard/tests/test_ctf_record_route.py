"""Exact shared record reads retain existing task-reader authority."""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from test_ctf_pagination import pagination_app as pagination_app


@pytest.mark.parametrize("reader", ["user", "agent"])
async def test_record_route_reads_one_scoped_record(pagination_app, reader):
    setup = pagination_app
    rid = uuid4()
    setup.backend.get_record = AsyncMock(return_value={"id": str(rid), "body": "Synthetic note"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/tasks/{setup.tid}/ctf/challenges/{setup.cid}/records/{rid}",
            headers=getattr(setup, f"{reader}_headers"),
        )
    assert response.status_code == 200
    assert response.json() == setup.backend.get_record.return_value
    setup.backend.get_record.assert_awaited_once_with(setup.tid, str(setup.cid), str(rid))


@pytest.mark.parametrize(
    "case,expected", [("anonymous", 401), ("other_task", 403), ("query", 422), ("invalid_id", 422)]
)
async def test_record_route_rejects_invalid_or_unauthorized_reads(pagination_app, case, expected):
    setup = pagination_app
    setup.backend.get_record = AsyncMock()
    tid = uuid4() if case == "other_task" else setup.tid
    rid = "not-a-uuid" if case == "invalid_id" else str(uuid4())
    headers = {} if case == "anonymous" else setup.agent_headers
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/tasks/{tid}/ctf/challenges/{setup.cid}/records/{rid}",
            headers=headers,
            params={"unexpected": "value"} if case == "query" else {},
        )
    assert response.status_code == expected
    setup.backend.get_record.assert_not_awaited()


@pytest.mark.parametrize("review", [False, True])
async def test_record_tool_reads_the_reference_directly_in_execution_and_review(
    pagination_app, review
):
    setup = pagination_app
    rid = str(uuid4())
    setup.backend.get_record = AsyncMock(return_value={"id": rid, "body": "Exact shared evidence"})
    setup.app.state.profile_store.get.return_value["worker_tools"]["teammate"]["builtin"].append(
        "get_record"
    )
    if review:
        setup.backend.authorize_tool.return_value["purpose"] = "review"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/tasks/{setup.tid}/ctf/tools/get_record",
            json={"challenge_id": str(setup.cid), "record_id": rid},
            headers=setup.agent_headers,
        )
        bad = await client.post(
            f"/api/tasks/{setup.tid}/ctf/tools/get_record",
            json={"challenge_id": str(setup.cid), "record_id": "not-a-record"},
            headers=setup.agent_headers,
        )
    assert response.status_code == 200 and response.json()["body"] == "Exact shared evidence"
    assert bad.status_code == 422
    setup.backend.get_record.assert_awaited_once_with(setup.tid, str(setup.cid), rid)
    setup.backend.list_records.assert_not_awaited()
    setup.backend.list_challenges.assert_not_awaited()


async def test_review_can_read_back_only_its_own_saved_long_record(pagination_app):
    setup = pagination_app
    setup.backend.authorize_tool.return_value["purpose"] = "review"
    setup.backend.read_saved_tool_result = AsyncMock(return_value={"result": "Saved middle"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        path = f"/api/tasks/{setup.tid}/ctf/tools/read_saved_tool_result"
        result = await client.post(
            path, json={"call_id": "record-call"}, headers=setup.agent_headers
        )
        foreign = await client.post(
            path, json={"call_id": "record-call", "actor": "lead"}, headers=setup.agent_headers
        )
    assert result.status_code == 200 and foreign.status_code == 422
    setup.backend.read_saved_tool_result.assert_awaited_once_with(
        setup.tid, actor="member-1", call_id="record-call", offset=0, limit=2000
    )
