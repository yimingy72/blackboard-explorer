"""New publication validates capabilities without changing legacy profile loading."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from bbx_blackboard import api
from bbx_blackboard.profiles import _content
from bbx_contracts.ctf import load_ctf_profile
from test_ctf_api import settings


def profile():
    return load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")


async def test_new_task_missing_readback_is_rejected_before_any_snapshot_or_execution(monkeypatch):
    value = profile()
    value.worker_tools["teammate"].builtin.remove("read_saved_tool_result")
    store = SimpleNamespace(get=AsyncMock(return_value={}), create=AsyncMock())
    app = api.create_app(settings())
    app.state.profile_store = store
    app.state.ctf_service = SimpleNamespace(create_task=AsyncMock(), provision=AsyncMock())
    monkeypatch.setattr(api, "ctf_profile", lambda _: value)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/tasks",
            headers={"Authorization": "Bearer service-test"},
            json={
                "mode": "ctf",
                "goal": "Inspect material",
                "budget": {"max_cost": 1, "max_minutes": 1},
            },
        )
    assert response.status_code == 422
    assert "teammate: read_saved_tool_result" in response.json()["detail"]
    store.create.assert_not_awaited()
    app.state.ctf_service.provision.assert_not_awaited()


async def test_each_role_can_repair_legacy_settings_without_rewriting_the_other():
    value = profile()
    for tools in value.worker_tools.values():
        tools.builtin.remove("read_saved_tool_result")
    row = {"name": "ctf", "version": 3, **_content(value)}
    saved = {}

    async def create(name, selected, author, **kwargs):
        saved.update(selected.model_dump(mode="json"))
        return {"name": name, "version": 4, **_content(selected)}

    store = SimpleNamespace(get=AsyncMock(return_value=row), create=AsyncMock(side_effect=create))
    app = api.create_app(settings())
    app.state.profile_store = store
    selected = value.worker_tools["lead"].model_dump(mode="json")
    selected["builtin"].append("read_saved_tool_result")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        before = await client.get(
            "/api/settings/ctf/workers", headers={"Authorization": "Bearer service-test"}
        )
        assert before.status_code == 200
        response = await client.put(
            "/api/settings/ctf/workers/lead",
            headers={"Authorization": "Bearer service-test"},
            json={"expected_revision": 3, "prompt": value.prompt_templates.lead, "tools": selected},
        )
    assert response.status_code == 200, response.text
    assert saved["worker_tools"]["teammate"] == value.worker_tools["teammate"].model_dump(
        mode="json"
    )
    assert "read_saved_tool_result" in saved["worker_tools"]["lead"]["builtin"]


async def test_explicit_publication_rejects_missing_readback_before_writes(monkeypatch):
    from contextlib import asynccontextmanager
    from typing import cast

    from bbx_blackboard import ctf_profile_publish
    from bbx_blackboard.profiles import ProfileStore
    from test_ctf_profile_publish import profile_rows

    rows = profile_rows()
    rows["ctf"]["worker_tools"]["lead"]["builtin"].remove("read_saved_tool_result")

    @asynccontextmanager
    async def transaction():
        yield object()

    store = SimpleNamespace(
        engine=SimpleNamespace(begin=transaction), create_in_connection=AsyncMock()
    )
    monkeypatch.setattr(
        ctf_profile_publish, "_latest", AsyncMock(side_effect=lambda _conn, name: rows[name])
    )
    with pytest.raises(ValueError, match="readback"):
        await ctf_profile_publish.publish_ctf_profiles(
            cast(ProfileStore, store),
            apply=True,
            expected_versions={name: row["version"] for name, row in rows.items()},
        )
    store.create_in_connection.assert_not_awaited()
