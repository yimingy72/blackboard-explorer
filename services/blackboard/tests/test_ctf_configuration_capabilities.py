"""New publication validates capabilities without changing legacy profile loading."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from bbx_blackboard import api
from bbx_contracts.ctf import load_ctf_profile
from test_ctf_api import settings


def profile():
    return load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf")


async def test_unpublished_ctf_has_no_editable_draft_entry():
    app = api.create_app(settings())
    app.state.profile_store = SimpleNamespace(get=AsyncMock(), create=AsyncMock())
    headers = {"Authorization": "Bearer service-test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        before = await client.get("/api/settings/ctf/workers", headers=headers)
        assert before.json() == {"published": False}
        result = await client.put("/api/settings/ctf/workers/lead", headers=headers, json={})
        assert result.status_code in {404, 405}
    app.state.profile_store.get.assert_not_awaited()
    app.state.profile_store.create.assert_not_awaited()


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
