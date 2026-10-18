"""Model removal must not invalidate pinned tasks, credentials or prompt editing."""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.platform import ModelInput, PlatformStore
from bbx_blackboard.store import schema as s
from bbx_contracts.models import BUILTIN_TOOLS
from fastapi import HTTPException
from sqlalchemy import select
from test_platform import config
from test_store_integration import FakeObjects
from test_store_integration import board_service as board_service
from test_store_integration import database_url as database_url

pytestmark = pytest.mark.integration
PROFILES = Path(__file__).resolve().parents[3] / "profiles/default"


def model_input():
    return ModelInput.model_validate(
        {
            "label": "Removable model",
            "provider": "openai_chat",
            "model": "fixture-model",
            "base_url": "https://example.invalid/v1",
            "api_key": "fixture-secret",
            "price": {
                "currency": "CNY",
                "cache_hit_per_m": 0,
                "cache_miss_per_m": 1,
                "output_per_m": 2,
                "off_peak": False,
            },
        }
    )


async def test_model_deletion_preserves_tasks_and_rejects_stale_selection(board_service):
    settings = config()
    app = create_app(settings, engine=board_service.repo.engine, objects=FakeObjects())
    platform = app.state.platform_store
    profiles = app.state.profile_store
    await profiles.ensure_bundled(PROFILES, platform)
    name = "delete-" + uuid4().hex
    body = model_input()
    await platform.save("models", name, body, "test")
    await platform.save("models", name, body.model_copy(update={"model": "fixture-v2"}), "test")
    await platform.set_default_model(name)
    spec = {
        "goal": "Inspect fixture",
        "acceptance": [{"id": "A1", "desc": "Evidence"}],
        "budget": {"max_cost": "1", "max_minutes": 10},
        "agent_profile": "default",
        "model_id": name,
        "model_version": 1,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-service"},
    ) as client:
        created = await client.post("/api/tasks", json=spec)
        assert created.status_code == 200, created.text
        task = created.json()
        before = await board_service.state(UUID(task["id"]))
        snapshot = await profiles.get(task["agent_profile"], task["agent_profile_version"])
        path = f"/api/platform/models/{name}"
        assert (await client.delete(path)).status_code == 204
        assert (await client.delete(path)).status_code == 204
        assert name not in {
            item["name"] for item in (await client.get("/api/platform/models")).json()
        }
        assert await platform.default_model_name() == ""
        assert await board_service.state(UUID(task["id"])) == before
        assert await profiles.get(task["agent_profile"], task["agent_profile_version"]) == snapshot
        for version in [1, 2]:
            credential = await client.get(f"{path}/versions/{version}/credentials")
            assert credential.status_code == 200
            assert credential.json()["secret"] == "fixture-secret"
            stale = await client.post("/api/tasks", json={**spec, "model_version": version})
            assert stale.status_code == 404
        ctf = {
            "mode": "ctf",
            "agent_profile": "ctf",
            "goal": "Inspect fixture",
            "budget": spec["budget"],
            "model_id": name,
            "model_version": 1,
        }
        assert (await client.post("/api/tasks", json=ctf)).status_code == 404
        legacy = {
            **spec,
            "agent_profile": task["agent_profile"],
            "profile_version": task["agent_profile_version"],
        }
        legacy.pop("model_id")
        legacy.pop("model_version")
        assert (await client.post("/api/tasks", json=legacy)).status_code == 404
        assert (await client.post(path + "/default")).status_code == 404
        updated = body.model_dump(mode="json")
        updated["api_key"] = "fixture-secret"
        assert (await client.post(path, json=updated)).status_code == 409
        assert (await client.delete("/api/platform/models/does-not-exist")).status_code == 404


async def test_deleted_default_is_not_recreated_and_workers_remain_editable(board_service):
    app = create_app(config(), engine=board_service.repo.engine, objects=FakeObjects())
    platform = app.state.platform_store
    profiles = app.state.profile_store
    await profiles.ensure_bundled(PROFILES, platform)
    for model in await platform.list("models"):
        await platform.delete_model(model["name"])
    await profiles.ensure_bundled(PROFILES, platform)
    assert await platform.list("models") == []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-service"},
    ) as client:
        current = (await client.get("/api/settings/workers")).json()
        result = await client.put(
            "/api/settings/workers/explore",
            json={
                "expected_revision": current["revision"],
                "prompt": "Updated {{ goal }}",
                "tools": {"builtin": sorted(BUILTIN_TOOLS["explore"]), "mcp_servers": []},
            },
        )
        assert result.status_code == 200, result.text
        spec = {
            "goal": "Inspect fixture",
            "acceptance": [{"id": "A1", "desc": "Evidence"}],
            "budget": {"max_cost": "1", "max_minutes": 10},
            "agent_profile": "default",
        }
        assert (await client.post("/api/tasks", json=spec)).status_code == 422
        spec.pop("acceptance")
        assert (
            await client.post("/api/tasks", json={**spec, "mode": "ctf", "agent_profile": "ctf"})
        ).status_code == 422
    new_name = "replacement-" + uuid4().hex
    await platform.save("models", new_name, model_input(), "test")
    await platform.set_default_model(new_name)
    assert await platform.default_model_name() == new_name


async def test_model_delete_serializes_with_save_and_default_selection(board_service):
    platform = PlatformStore(board_service.repo.engine, "fixture-signing-key")
    name = "concurrent-" + uuid4().hex
    body = model_input()
    await platform.save("models", name, body, "test")
    await platform.set_default_model(name)
    results = await asyncio.gather(
        platform.save("models", name, body, "test"),
        platform.delete_model(name),
        platform.set_default_model(name),
        return_exceptions=True,
    )
    assert results[1] is None
    for result in results:
        if isinstance(result, BaseException):
            assert isinstance(result, HTTPException) and result.status_code in {404, 409}
    assert await platform.default_model_name() != name
    async with board_service.repo.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(s.platform_configs).where(s.platform_configs.c.name == name)
                )
            )
            .mappings()
            .all()
        )
        assert rows and all(row["deleted"] for row in rows)
    assert (await platform.credentials("models", name, 1))["secret"] == "fixture-secret"
