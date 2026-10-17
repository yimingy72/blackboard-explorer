"""Task overviews stay bounded by the page, not by task history."""

from pathlib import Path
from typing import Any

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.ctf import CtfService
from bbx_blackboard.ctf_api import public_member
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.settings import Settings
from bbx_contracts.ctf import load_ctf_profile
from bbx_contracts.profile import load_profile
from pydantic import SecretStr, TypeAdapter
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncConnection
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url
from test_store_integration import task

pytestmark = pytest.mark.integration


async def test_overviews_batch_both_modes_without_reading_task_histories(
    board_service, monkeypatch
):
    root = Path(__file__).resolve().parents[3]
    profiles = ProfileStore(board_service.repo.engine)
    ordinary_profile, _ = load_profile(root / "profiles/default")
    await profiles.create("default", ordinary_profile, "test")
    await profiles.create("ctf", load_ctf_profile(root / "profiles/ctf"), "test")
    ordinary_ids = [await task(board_service) for _ in range(3)]
    active = {}
    for tid in ordinary_ids:
        aid = await board_service.register_agent(tid, "explore")
        active[tid] = (await board_service.state(tid))["agents"][aid]
    ctf = CtfService(board_service.repo.engine)
    ctf_ids = []
    members = {}
    for _ in range(2):
        tid = await ctf.create_task(
            {
                "mode": "ctf",
                "goal": "Synthetic overview",
                "budget": {"max_cost": "1", "max_minutes": 10},
            }
        )
        await ctf.provision(tid)
        ctf_ids.append(tid)
        members[tid] = [public_member(member) for member in (await ctf.state(tid))["members"]]
    # Large unrelated histories must never be fetched for list/detail responses.
    from bbx_blackboard.store import schema as s

    async with board_service.repo.engine.begin() as conn:
        await conn.execute(
            insert(s.agent_sessions).values(
                task_id=ordinary_ids[0],
                agent_id=active[ordinary_ids[0]]["id"],
                session={"history": "x" * 1_000_000},
                opening_instructions="",
                origin="native",
                revision=1,
            )
        )
    config = Settings.model_construct(
        postgres_password=SecretStr("fixture"),
        minio_root_password=SecretStr("fixture"),
        service_token=SecretStr("overview-service"),
        agent_token_secret=SecretStr("overview-test-signing-key-at-least-32"),
        admin_users=SecretStr("tester:fixture"),
    )
    app = create_app(config, engine=board_service.repo.engine)
    statements = []
    execute = AsyncConnection.execute

    async def capture(conn, statement, *args, **kwargs):
        statements.append(str(statement))
        return await execute(conn, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer overview-service"},
    ) as client:
        response = await client.get("/api/tasks", params={"limit": 500})
        assert response.status_code == 200, response.text
        assert len(statements) == 5
        assert not any(
            f"FROM {name}" in sql
            for sql in statements
            for name in (
                "facts",
                "intents",
                "tool_calls",
                "agent_sessions",
                "agent_messages",
                "ctf_messages",
                "ctf_turns",
                "ctf_records",
                "ctf_challenges",
                "events",
            )
        )
        assert any(
            "agent_profiles.models" in sql and "prompt_templates" not in sql for sql in statements
        )
        listed = {item["id"]: item for item in response.json()}
        for tid in ordinary_ids + ctf_ids:
            statements.clear()
            detail = await client.get(f"/api/tasks/{tid}")
            assert detail.status_code == 200, detail.text
            assert detail.json() == listed[str(tid)]
            if tid in active:
                assert detail.json()["agents"] == TypeAdapter(list[dict[str, Any]]).dump_python(
                    [active[tid]], mode="json"
                )
                assert detail.json()["members"] == []
            else:
                assert detail.json()["members"] == TypeAdapter(list[dict[str, Any]]).dump_python(
                    members[tid], mode="json"
                )
                assert detail.json()["agents"] == []
        for count in (1, 2, 3):
            statements.clear()
            page = await client.get("/api/tasks", params={"offset": 2, "limit": count})
            assert len(page.json()) == count
            assert len(statements) == 4

        committed = False
        target = ordinary_ids[0]

        async def concurrent_finish(conn, statement, *args, **kwargs):
            nonlocal committed
            result = await capture(conn, statement, *args, **kwargs)
            if not committed and str(statement).startswith("SELECT tasks.id,"):
                committed = True
                async with board_service.repo.engine.begin() as writer:
                    await writer.execute(
                        update(s.tasks)
                        .where(s.tasks.c.id == target)
                        .values(
                            usage={"cost": "7"},
                        )
                    )
                    await writer.execute(
                        update(s.agent_runs)
                        .where(
                            s.agent_runs.c.task_id == target,
                        )
                        .values(status="finished")
                    )
            return result

        monkeypatch.setattr(AsyncConnection, "execute", concurrent_finish)
        snapshot = await client.get("/api/tasks", params={"limit": 500})
        item = next(item for item in snapshot.json() if item["id"] == str(target))
        assert committed and item["usage"] == {} and len(item["agents"]) == 1
        latest = (await client.get(f"/api/tasks/{target}")).json()
        assert latest["usage"] == {"cost": "7"} and latest["agents"] == []
        empty = await client.get("/api/tasks", params={"offset": 100000})
        assert empty.status_code == 200 and empty.json() == []


async def test_platform_list_projects_only_latest_connection_versions(board_service, monkeypatch):
    from bbx_blackboard.platform import McpInput, PlatformStore

    store = PlatformStore(board_service.repo.engine, "synthetic-platform-encryption-key")
    for name in ("overview-tools-a", "overview-tools-b"):
        for version in range(1, 7):
            await store.save(
                "mcp-servers",
                name,
                McpInput(label=f"Version {version}", url=f"https://example.invalid/v{version}"),
                "test",
            )
    public = store.public
    projected = []

    def capture(row):
        projected.append((row["name"], row["version"]))
        return public(row)

    monkeypatch.setattr(store, "public", capture)
    listed = await store.list("mcp-servers")
    assert len(projected) == len(listed)
    assert [(item["name"], item["version"]) for item in listed] == [
        ("overview-tools-a", 6),
        ("overview-tools-b", 6),
    ]
