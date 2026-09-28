"""Reconcile one isolated task, preserve events, replay, and bill its next run correctly."""

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from bbx_blackboard.api import create_app
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.settings import Settings
from bbx_blackboard.store import schema as s
from bbx_contracts.profile import load_profile
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


class Objects:
    async def exists(self, uri: str) -> bool:
        return True


async def test_reconcile_estimate_is_audited_idempotent_and_preserves_tokens():
    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg").with_name(
        f"bbx-continuation-billing-{uuid4().hex[:8]}"
    ) as postgres:
        url = postgres.get_connection_url()
        old = os.environ.get("BBX_DATABASE_URL")
        os.environ["BBX_DATABASE_URL"] = url
        try:
            await asyncio.to_thread(
                command.upgrade, Config(str(ROOT / "services/blackboard/alembic.ini")), "head"
            )
        finally:
            if old is None:
                os.environ.pop("BBX_DATABASE_URL", None)
            else:
                os.environ["BBX_DATABASE_URL"] = old
        engine = create_async_engine(url)
        settings = Settings(
            postgres_password=SecretStr("test"),
            minio_root_password=SecretStr("test"),
            service_token=SecretStr("billing-test"),
            agent_token_secret=SecretStr("billing-agent-test"),
            admin_users=SecretStr("admin:test"),
        )
        app = create_app(settings, engine=engine, objects=Objects())
        service = app.state.board_service
        profile, _ = load_profile(ROOT / "profiles/default")
        for role in ("explore", "derive", "close"):
            getattr(profile.models, role).price.billing_mode = "fixed"
        await ProfileStore(engine).create("default", profile, "test")
        tid = await service.create_task(
            {
                "goal": "Count rows",
                "acceptance": [{"id": "A1", "desc": "Report row count"}],
                "budget": {"max_cost": "20", "max_minutes": 60},
                "agent_profile": "default",
            }
        )
        await service.transition(tid, "provisioning")
        await service.transition(tid, "running")
        aid = await service.register_agent(tid, "explore", is_seed=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer billing-test"},
        ) as client:
            progress = {
                "steps": 1,
                "context_tokens": 2_000_000,
                "last_seen_version": 0,
                "usage": {
                    "cache_hit_tokens": 1_000_000,
                    "cache_miss_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                    "reasoning_tokens": 500_000,
                    "cost": "9999",
                },
            }
            response = await client.patch(f"/api/tasks/{tid}/agents/{aid}", json=progress)
            assert response.status_code == 200, response.text
            assert Decimal(str(response.json()["cost"])) == Decimal("10.04")
            assert (await client.get(f"/api/tasks/{tid}/cost-reconciliation")).status_code == 409
            await service.finish_agent(tid, aid, {}, "runtime_error")
            await service.transition(tid, "failed", reason="test")
            async with engine.begin() as conn:
                await conn.execute(
                    update(s.events)
                    .where(s.events.c.task_id == tid, s.events.c.type == "agent.progress")
                    .values(
                        created_at=datetime(2026, 9, 28, 5, 30, tzinfo=UTC),
                        payload={
                            **progress,
                            "agent_id": aid,
                            "usage": {**progress["usage"], "cost": "10.04"},
                        },
                    )
                )
            preview = (await client.get(f"/api/tasks/{tid}/cost-reconciliation")).json()
            assert Decimal(preview["cost"]) == Decimal("5.02")
            assert not preview["applied"]
            body = {"expected_version": preview["version"]}
            first = await client.post(f"/api/tasks/{tid}/cost-reconciliation", json=body)
            second = await client.post(f"/api/tasks/{tid}/cost-reconciliation", json=body)
            assert first.status_code == second.status_code == 200
            assert first.json()["version"] == second.json()["version"]
            state = await service.state(tid)
            assert Decimal(state["task"]["usage"]["cost"]) == Decimal("5.02")
            assert state["agents"][aid]["usage"]["output_tokens"] == 1_000_000
            assert state["task"]["billing_mode"] == "deepseek_schedule"
            await service.replay(tid)
            assert Decimal((await service.state(tid))["task"]["usage"]["cost"]) == Decimal("5.02")
            async with engine.connect() as conn:
                logs = (
                    (
                        await conn.execute(
                            select(s.events).where(
                                s.events.c.task_id == tid, s.events.c.type == "cost.reconciled"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(logs) == 1 and logs[0]["payload"]["previous_cost"] == "10.04"
        await engine.dispose()
