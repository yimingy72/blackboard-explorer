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
