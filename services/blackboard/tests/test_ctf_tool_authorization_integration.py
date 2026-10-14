"""Count authenticated tool SQL against disposable PostgreSQL with synthetic tasks."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.auth import issue_agent_token
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.store import schema as s
from bbx_contracts.ctf import load_ctf_profile
from sqlalchemy import event, update
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_turn, team
from test_ctf_integration import ctf_url as ctf_url
from test_ctf_review_integration import terminal

pytestmark = pytest.mark.integration


@pytest.fixture
async def tool_http(ctf):
    tid = await team(ctf)
    turn = await ctf_turn(ctf, tid)
    profiles = ProfileStore(ctf.repo.engine)
    profile = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")
    name = f"tool-authority-{uuid4().hex}"
    await profiles.create(name, profile, "test")
    profile.worker_tools["lead"].builtin.remove("list_challenges")
    latest = await profiles.create(name, profile, "test")
    assert latest["version"] == 2
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(agent_profile=name, agent_profile_version=1)
        )
    app = api.create_app(settings())
    app.state.engine = ctf.repo.engine
    app.state.ctf_service = ctf
    app.state.profile_store = profiles
    # A regression must fail before querying unrelated scheduling tables.
    ctf.runtime_state = AsyncMock(
        side_effect=AssertionError("Tools must not load scheduling state")
    )
    ctf.state = AsyncMock(side_effect=AssertionError("Tools must not load complete history"))
    token = issue_agent_token(
        settings(), tid, "lead", generation=turn["generation"], turn_id=turn["id"]
    )
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(ctf.repo.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(
                client=client,
                tid=tid,
                turn=turn,
                ctf=ctf,
                headers={"Authorization": f"Bearer {token}"},
                statements=statements,
                profile=name,
            )
    finally:
        event.remove(ctf.repo.engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.parametrize("operation,expected", [("list_challenges", 7), ("list_team", 8)])
async def test_execution_tool_queries_only_authority_profile_and_requested_data(
    tool_http, operation, expected
):
    setup = tool_http
    response = await setup.client.post(
        f"/api/tasks/{setup.tid}/ctf/tools/{operation}", headers=setup.headers, json={}
    )
    assert response.status_code == 200, response.text
    sql = setup.statements
    assert len(sql) == expected, sql
    assert sum(" for update" in statement for statement in sql) == 1
    assert sum("from ctf_turns " in statement for statement in sql) == 1
    assert all("ctf_turns.id =" in statement for statement in sql if "from ctf_turns " in statement)
    assert not any("from ctf_messages " in statement for statement in sql)
    assert sum("from ctf_members " in statement for statement in sql) == (
        2 if operation == "list_team" else 1
    )
    assert sum("from agent_profiles " in statement for statement in sql) == 1
    assert not any(
        "from sessions " in statement or "from ctf_records " in statement for statement in sql
    )
    setup.ctf.runtime_state.assert_not_awaited()
    setup.ctf.state.assert_not_awaited()


async def test_execution_tool_rechecks_profile_generation_pending_operation_and_budget(tool_http):
    setup = tool_http
    path = f"/api/tasks/{setup.tid}/ctf/tools/list_challenges"
    assert (await setup.client.post(path, headers=setup.headers, json={})).status_code == 200
    async with setup.ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks).where(s.tasks.c.id == setup.tid).values(agent_profile_version=2)
        )
    setup.statements.clear()
    assert (await setup.client.post(path, headers=setup.headers, json={})).status_code == 403
    assert not any("from ctf_challenges " in statement for statement in setup.statements)
    async with setup.ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.tasks).where(s.tasks.c.id == setup.tid).values(agent_profile_version=1)
        )
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == setup.tid, s.ctf_members.c.id == "lead")
            .values(pending_operation={"operation": "stop"})
        )
    setup.statements.clear()
    assert (await setup.client.post(path, headers=setup.headers, json={})).status_code == 409
    assert not any("from ctf_challenges " in statement for statement in setup.statements)
    async with setup.ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == setup.tid, s.ctf_members.c.id == "lead")
            .values(pending_operation=None, generation=setup.turn["generation"] + 1)
        )
    assert (await setup.client.post(path, headers=setup.headers, json={})).status_code == 409
    async with setup.ctf.repo.engine.begin() as conn:
        await conn.execute(
            update(s.ctf_members)
            .where(s.ctf_members.c.task_id == setup.tid, s.ctf_members.c.id == "lead")
            .values(generation=setup.turn["generation"])
        )
        await conn.execute(
            update(s.tasks).where(s.tasks.c.id == setup.tid).values(usage={"cost": "10"})
        )
    response = await setup.client.post(path, headers=setup.headers, json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ctf_budget_exhausted"


async def test_review_tool_has_one_authority_transaction_without_execution_profile_or_budget(ctf):
    tid, aid = await terminal(ctf, remove=True)
    await ctf.post_message(tid, "user", aid, "Explain synthetic stored evidence", str(uuid4()))
    turn = await ctf.claim_review_turn(tid, aid, "test-review-runtime")
    assert turn is not None
    app = api.create_app(settings())
    app.state.engine = ctf.repo.engine
    app.state.ctf_service = ctf
    app.state.profile_store = SimpleNamespace(
        get=AsyncMock(side_effect=AssertionError("Review does not use worker tools"))
    )
    token = issue_agent_token(
        settings(), tid, aid, generation=turn["generation"], turn_id=turn["id"]
    )
    headers = {"Authorization": f"Bearer {token}"}
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(ctf.repo.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            path = f"/api/tasks/{tid}/ctf/tools/"
            response = await client.post(path + "list_challenges", headers=headers, json={})
            assert response.status_code == 200, response.text
            assert len(statements) == 7, statements
            assert sum(" for update" in sql for sql in statements) == 1
            assert sum("from ctf_members " in sql for sql in statements) == 1
            assert sum("from ctf_turns " in sql for sql in statements) == 1
            assert not any(
                "from ctf_messages " in sql or "from agent_profiles " in sql for sql in statements
            )
            for operation in ("send_message", "list_team", "read_turn_result"):
                assert (
                    await client.post(path + operation, headers=headers, json={})
                ).status_code == 403
            async with ctf.repo.engine.begin() as conn:
                await conn.execute(
                    update(s.tasks).where(s.tasks.c.id == tid).values(status="running")
                )
            assert (
                await client.post(path + "list_challenges", headers=headers, json={})
            ).status_code == 409
    finally:
        event.remove(ctf.repo.engine.sync_engine, "before_cursor_execute", record)
