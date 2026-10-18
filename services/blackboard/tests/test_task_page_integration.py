"""Server pagination searches all tasks while keeping counts and page in one snapshot."""

from collections import Counter
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_agent_token
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.settings import Settings
from bbx_blackboard.store import schema as s
from bbx_contracts.ctf import load_ctf_profile
from bbx_contracts.models import TaskStatus
from bbx_contracts.profile import load_profile
from pydantic import SecretStr
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url
from test_store_integration import task

pytestmark = pytest.mark.integration


async def test_task_page_searches_beyond_100_and_keeps_counts_and_authorization(
    board_service, monkeypatch
):
    root = Path(__file__).resolve().parents[3]
    profiles = ProfileStore(board_service.repo.engine)
    profile, _ = load_profile(root / "profiles/default")
    await profiles.create("default", profile, "test")
    await profiles.create("ctf", load_ctf_profile(root / "profiles/ctf"), "test")
    tid = await task(board_service)
    async with board_service.repo.engine.begin() as conn:
        original = dict(
            (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
        )
        rows = []
        statuses = list(TaskStatus)
        for index in range(137):
            rows.append(
                {
                    **original,
                    "id": uuid4(),
                    "name": f"Needle {index}",
                    "goal": f"Goal {index}" + (" literal %_x" if index == 17 else ""),
                    "mode": "ctf" if index % 2 else "blackboard",
                    "agent_profile": "ctf" if index % 2 else "default",
                    "status": statuses[index % len(statuses)].value,
                    "created_at": original["created_at"] + timedelta(seconds=index + 1),
                }
            )
        await conn.execute(insert(s.tasks), rows)
    config = Settings.model_construct(
        postgres_password=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        service_token=SecretStr("page-service"),
        agent_token_secret=SecretStr("page-fixture-signing-key-at-least-32"),
        admin_users=SecretStr("admin:test"),
    )
    app = create_app(config, engine=board_service.repo.engine)
    statements = []
    execute = AsyncConnection.execute

    async def capture(conn, statement, *args, **kwargs):
        statements.append(str(statement))
        return await execute(conn, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", capture)
    headers = {"Authorization": "Bearer page-service"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/tasks/page")).status_code == 401
        agent_headers = {"Authorization": f"Bearer {issue_agent_token(config, tid, 'agent-1')}"}
        assert (await client.get("/api/tasks/page", headers=agent_headers)).status_code == 403
        statements.clear()
        response = await client.get(
            "/api/tasks/page", params={"q": "needle", "offset": 120, "limit": 20}, headers=headers
        )
        assert response.status_code == 200, response.text
        assert len(statements) == 6
        page = response.json()
        assert page["total"] == 137 and len(page["items"]) == 17
        assert page["limit"] == 20 and page["offset"] == 120
        assert [item["id"] for item in page["items"]] == [
            str(row["id"]) for row in reversed(rows[:17])
        ]
        counts = {status.value: 0 for status in TaskStatus}
        counts.update(Counter(row["status"] for row in rows))
        assert page["status_counts"] == counts
        for status, group in (
            ("active", {"created", "provisioning", "running", "closing"}),
            ("ended", {"finished", "stopped"}),
            ("failed", {"failed"}),
        ):
            filtered = (
                await client.get(
                    "/api/tasks/page", params={"q": "needle", "status": status}, headers=headers
                )
            ).json()
            assert filtered["total"] == sum(counts[item] for item in group)
            assert filtered["status_counts"] == counts
            assert all(item["status"] in group for item in filtered["items"])
        by_mode = (
            await client.get(
                "/api/tasks/page", params={"q": "needle", "mode": "ctf"}, headers=headers
            )
        ).json()
        assert by_mode["total"] == sum(row["mode"] == "ctf" for row in rows)
        assert sum(by_mode["status_counts"].values()) == by_mode["total"]
        assert all(item["mode"] == "ctf" for item in by_mode["items"])
        for query, expected in ((str(rows[0]["id"]), rows[0]["id"]), ("%_", rows[17]["id"])):
            found = (
                await client.get("/api/tasks/page", params={"q": query}, headers=headers)
            ).json()
            assert found["total"] == 1 and found["items"][0]["id"] == str(expected)
        empty = (
            await client.get(
                "/api/tasks/page", params={"q": "needle", "offset": 10000}, headers=headers
            )
        ).json()
        assert empty["total"] == 137 and empty["items"] == []
        legacy = await client.get("/api/tasks", headers=headers)
        assert isinstance(legacy.json(), list) and len(legacy.json()) == 100
        for params in (
            {"limit": 0},
            {"limit": 101},
            {"offset": -1},
            {"status": "invalid"},
            {"mode": "invalid"},
        ):
            assert (
                await client.get("/api/tasks/page", params=params, headers=headers)
            ).status_code == 422
        committed = False

        async def concurrent_create(conn, statement, *args, **kwargs):
            nonlocal committed
            result = await capture(conn, statement, *args, **kwargs)
            if not committed and "GROUP BY tasks.status" in str(statement):
                committed = True
                async with board_service.repo.engine.begin() as writer:
                    await writer.execute(
                        insert(s.tasks).values(
                            **{
                                **rows[0],
                                "id": uuid4(),
                                "created_at": rows[-1]["created_at"] + timedelta(seconds=1),
                            }
                        )
                    )
            return result

        monkeypatch.setattr(AsyncConnection, "execute", concurrent_create)
        snapshot = (
            await client.get(
                "/api/tasks/page", params={"q": "needle", "limit": 100}, headers=headers
            )
        ).json()
        assert committed and snapshot["total"] == 137
        assert snapshot["items"][0]["id"] == str(rows[-1]["id"])
        latest = (
            await client.get("/api/tasks/page", params={"q": "needle"}, headers=headers)
        ).json()
        assert latest["total"] == 138
