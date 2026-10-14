"""Bounded CTF board reads through tool and HTTP routes and disposable PostgreSQL."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_blackboard.ctf_board import CtfBoardMixin
from bbx_blackboard.settings import Settings
from bbx_blackboard.store import schema as s
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer


@pytest.fixture
def pagination_app(monkeypatch):
    tid, cid, turn = uuid4(), uuid4(), uuid4()
    config = Settings.model_construct(
        postgres_password=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        service_token=SecretStr("service-test"),
        agent_token_secret=SecretStr("test-secret-with-at-least-thirty-two-characters"),
        admin_users=SecretStr("alice:test"),
    )
    task = {"id": tid, "mode": "ctf", "agent_profile": "ctf", "agent_profile_version": 1}
    backend = SimpleNamespace(
        authorize_review=AsyncMock(side_effect=HTTPException(409, "Not a review turn")),
        authorize_reader=AsyncMock(),
        authorize_member=AsyncMock(return_value={"role": "teammate"}),
        authorize_tool=AsyncMock(
            return_value={
                "task": task,
                "member": {"id": "lead", "role": "teammate"},
                "purpose": "execution",
            }
        ),
        runtime_state=AsyncMock(return_value={"task": task, "members": []}),
        list_challenges=AsyncMock(
            return_value={"challenges": [{"id": str(cid), "tombstone": True}], "next_offset": 12}
        ),
        list_records=AsyncMock(return_value={"records": [{"body": "visible"}], "next_offset": 12}),
        get_challenge=AsyncMock(return_value={"id": str(cid)}),
    )
    app = api.create_app(config)
    app.state.ctf_service = backend
    app.state.profile_store = SimpleNamespace(
        get=AsyncMock(
            return_value={
                "worker_tools": {
                    "teammate": {"builtin": ["list_challenges", "get_challenge", "list_records"]}
                }
            }
        )
    )
    monkeypatch.setattr(api, "_task", AsyncMock(return_value=task))
    token = issue_agent_token(config, tid, "member-1", generation=2, turn_id=turn)
    return SimpleNamespace(
        app=app,
        backend=backend,
        tid=tid,
        cid=cid,
        agent_headers={"Authorization": f"Bearer {token}"},
        user_headers={"Authorization": f"Bearer {issue_user_token(config, 'alice')}"},
    )


@pytest.mark.parametrize("operation", ["list_challenges", "list_records"])
@pytest.mark.parametrize("route", ["http", "member_tool", "review_tool"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_routes_pass_pagination_to_database_method(
    pagination_app, operation, route, explicit
):
    setup = pagination_app
    params = {"limit": 2, "offset": 10} if explicit else {}
    expected: dict[str, int | None] = (
        {"limit": 2, "offset": 10} if explicit else {"limit": 100, "offset": 0}
    )
    args = [setup.tid]
    if operation == "list_records":
        args.append(str(setup.cid))
        expected["after_version"] = 41 if explicit else None
        if explicit:
            params["after_version"] = 41
    if route == "review_tool":
        setup.backend.authorize_tool.return_value["purpose"] = "review"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        prefix = f"/api/tasks/{setup.tid}/ctf"
        if route == "http":
            path = f"{prefix}/challenges"
            if operation == "list_records":
                path += f"/{setup.cid}/records"
            response = await client.get(path, params=params, headers=setup.user_headers)
        else:
            body: dict[str, Any] = dict(params)
            if operation == "list_records":
                body["challenge_id"] = str(setup.cid)
            response = await client.post(
                f"{prefix}/tools/{operation}", json=body, headers=setup.agent_headers
            )
    assert response.status_code == 200, response.text
    method = getattr(setup.backend, operation)
    method.assert_awaited_once_with(*args, **expected)
    assert response.json() == method.return_value


@pytest.mark.parametrize(
    "operation,params",
    [("list_records", {"after_version": value}) for value in [-1, True, 1.5, "2", None]]
    + [
        ("list_challenges", {"after_version": 0}),
        ("get_challenge", {"after_version": 0}),
        ("list_records", {"unknown": 1}),
        ("list_records", {"limit": True}),
        ("list_records", {"limit": 0}),
        ("list_records", {"limit": 501}),
        ("list_records", {"offset": -1}),
        ("list_records", {"offset": False}),
    ],
)
async def test_tool_rejects_invalid_or_unsupported_pagination(pagination_app, operation, params):
    setup = pagination_app
    body = dict(params)
    if operation != "list_challenges":
        body["challenge_id"] = str(setup.cid)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/tasks/{setup.tid}/ctf/tools/{operation}",
            json=body,
            headers=setup.agent_headers,
        )
    assert response.status_code == 422, response.text
    setup.backend.list_records.assert_not_awaited()
    setup.backend.list_challenges.assert_not_awaited()
    setup.backend.get_challenge.assert_not_awaited()


@pytest.mark.parametrize(
    "suffix,params",
    [("/records", {"after_version": value}) for value in [-1, "true", "1.5", "null"]]
    + [
        ("", {"after_version": 0}),
        ("/records", {"unknown": 1}),
        ("/records", {"limit": 0}),
        ("/records", {"limit": 501}),
        ("/records", {"offset": -1}),
        ("/records", {"offset": "false"}),
        ("/records", {"limit": "1.5"}),
    ],
)
async def test_http_rejects_invalid_or_unsupported_pagination(pagination_app, suffix, params):
    setup = pagination_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(setup.app), base_url="http://test"
    ) as client:
        prefix = f"/api/tasks/{setup.tid}/ctf/challenges"
        response = await client.get(
            f"{prefix}/{setup.cid}{suffix}", params=params, headers=setup.user_headers
        )
        assert response.status_code == 422, response.text
        response = await client.get(prefix, params={"after_version": 0}, headers=setup.user_headers)
        assert response.status_code == 422, response.text
    setup.backend.list_records.assert_not_awaited()
    setup.backend.list_challenges.assert_not_awaited()
    setup.backend.get_challenge.assert_not_awaited()


@pytest.mark.parametrize("after_version", [-1, True, 1.5, "2"])
async def test_record_service_rejects_invalid_version_before_query(after_version):
    with pytest.raises(HTTPException) as error:
        await CtfBoardMixin().list_records(uuid4(), str(uuid4()), after_version=after_version)
    assert error.value.status_code == 422


@pytest.fixture(scope="module")
def pagination_pg_url():
    container = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg")
    container.with_name(f"bbx-ctf-pagination-{uuid4().hex[:8]}")
    with container:
        yield container.get_connection_url()


@pytest.fixture
async def pagination_board(pagination_pg_url):
    engine = create_async_engine(pagination_pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda connection: s.ctf_challenges.create(connection, checkfirst=True))
        await conn.run_sync(lambda connection: s.ctf_records.create(connection, checkfirst=True))
    board = CtfBoardMixin()
    board.repo = SimpleNamespace(engine=engine)
    try:
        yield board
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("count,limit", [(0, 2), (1, 2), (2, 2), (3, 2), (4, 2), (7, 3)])
async def test_postgres_board_pages_and_legacy_full_reads(pagination_board, count, limit):
    board = pagination_board
    tid, other_tid, challenge_tid = uuid4(), uuid4(), uuid4()
    cid, other_cid = str(uuid4()), str(uuid4())
    challenges = [
        {"id": str(uuid4()), "tombstone": index == 0, "title": f"Challenge {index}"}
        for index in range(count)
    ]
    records = [
        {"id": str(uuid4()), "challenge_id": cid, "body": f"Record {index}"}
        for index in range(count)
    ]
    async with board.repo.engine.begin() as conn:
        for data in challenges:
            await conn.execute(
                insert(s.ctf_challenges).values(
                    task_id=challenge_tid, id=data["id"], data=data, revision=1
                )
            )
        for data in [{"id": cid}, {"id": other_cid}]:
            await conn.execute(
                insert(s.ctf_challenges).values(task_id=tid, id=data["id"], data=data, revision=1)
            )
        await conn.execute(
            insert(s.ctf_challenges).values(task_id=other_tid, id=cid, data={"id": cid}, revision=1)
        )
        for index, data in enumerate(records):
            await conn.execute(
                insert(s.ctf_records).values(
                    task_id=tid, id=data["id"], data=data, version=index * 7 + 3
                )
            )
        for scope_tid, scope_cid in [(other_tid, cid), (tid, other_cid)]:
            await conn.execute(
                insert(s.ctf_records).values(
                    task_id=scope_tid,
                    id=str(uuid4()),
                    data={"challenge_id": scope_cid, "body": "outside"},
                    version=1,
                )
            )
    expected_challenges = sorted(challenges, key=lambda data: data["id"])
    assert await board.list_challenges(challenge_tid) == {"challenges": expected_challenges}
    assert await board.list_records(tid, cid) == {"records": records}
    statements = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    event.listen(board.repo.engine.sync_engine, "before_cursor_execute", capture)
    try:
        for method, args, key, expected in [
            (board.list_challenges, [challenge_tid], "challenges", expected_challenges),
            (board.list_records, [tid, cid], "records", records),
        ]:
            gathered = []
            offset = 0
            while True:
                page = await method(*args, limit=limit, offset=offset)
                gathered.extend(page[key])
                assert page[key] == expected[offset : offset + limit]
                next_offset = offset + limit if len(expected) > offset + limit else None
                assert page["next_offset"] == next_offset
                if next_offset is None:
                    break
                offset = next_offset
            assert gathered == expected
            assert await method(*args, limit=limit, offset=len(expected)) == {
                key: [],
                "next_offset": None,
            }
    finally:
        event.remove(board.repo.engine.sync_engine, "before_cursor_execute", capture)
    queries = [(sql, params) for sql, params in statements if "LIMIT" in sql]
    assert queries
    assert all("OFFSET" in sql and params[-2] == limit + 1 for sql, params in queries)
    assert all("LIMIT" in sql for sql, _ in statements if "FROM ctf_records" in sql)
    assert all("LIMIT" in sql for sql, _ in statements if "ORDER BY ctf_challenges.id" in sql)


@pytest.mark.integration
async def test_postgres_after_version_uses_record_column_and_filtered_offsets(pagination_board):
    board = pagination_board
    tid, other_tid = uuid4(), uuid4()
    cid, other_cid = str(uuid4()), str(uuid4())
    versions = [3, 11, 18, 25, 46, 90]
    records = [
        {"id": str(uuid4()), "challenge_id": cid, "created_version": 999, "body": str(version)}
        for version in versions
    ]
    async with board.repo.engine.begin() as conn:
        for scope_tid, scope_cid in [(tid, cid), (tid, other_cid), (other_tid, cid)]:
            await conn.execute(
                insert(s.ctf_challenges).values(
                    task_id=scope_tid,
                    id=scope_cid,
                    data={"id": scope_cid, "tombstone": True},
                    revision=1,
                )
            )
        for version, data in zip(versions, records, strict=True):
            await conn.execute(
                insert(s.ctf_records).values(task_id=tid, id=data["id"], data=data, version=version)
            )
        for scope_tid, scope_cid in [(tid, other_cid), (other_tid, cid)]:
            await conn.execute(
                insert(s.ctf_records).values(
                    task_id=scope_tid,
                    id=str(uuid4()),
                    data={"challenge_id": scope_cid, "created_version": 18},
                    version=18,
                )
            )
    assert await board.list_records(tid, cid) == {"records": records}
    assert await board.list_records(tid, cid, after_version=11) == {"records": records[2:]}
    assert await board.list_records(tid, cid, limit=2, after_version=11) == {
        "records": records[2:4],
        "next_offset": 2,
    }
    assert await board.list_records(tid, cid, limit=2, offset=2, after_version=11) == {
        "records": records[4:],
        "next_offset": None,
    }
    assert await board.list_records(tid, cid, limit=2, offset=4, after_version=11) == {
        "records": [],
        "next_offset": None,
    }
    assert await board.list_records(tid, cid, limit=2, after_version=90) == {
        "records": [],
        "next_offset": None,
    }
    assert await board.list_records(tid, cid, after_version=0) == {"records": records}
    with pytest.raises(HTTPException) as error:
        await board.list_records(tid, str(uuid4()), limit=2, after_version=0)
    assert error.value.status_code == 404
