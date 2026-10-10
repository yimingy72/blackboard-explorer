"""Profile versions retain prompt bodies and skip unchanged content."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from bbx_blackboard.profiles import ProfileStore
from bbx_contracts.ctf import CtfAgentProfile, load_ctf_profile
from bbx_contracts.models import AgentProfile
from bbx_contracts.profile import load_profile
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.dml import Insert


class Result:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    def mappings(self) -> "Result":
        return self

    def first(self) -> dict[str, Any] | None:
        return self.row

    def one(self) -> dict[str, Any]:
        assert self.row is not None
        return self.row


class Connection:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def execute(self, statement: Any, _params: Any = None) -> Result:
        if isinstance(statement, Insert):
            row = {
                **statement.compile().params,
                "created_at": datetime.now(UTC),
            }
            self.rows.append(row)
            return Result(row)
        if getattr(statement, "is_select", False):
            name = statement.compile().params.get("name_1")
            rows = [row for row in self.rows if row["name"] == name]
            return Result(rows[-1] if rows else None)
        return Result(None)


class Engine:
    def __init__(self) -> None:
        self.connection = Connection()

    @asynccontextmanager
    async def begin(self):
        yield self.connection


@pytest.mark.asyncio
async def test_profile_body_change_creates_version() -> None:
    directory = Path(__file__).resolve().parents[3] / "profiles/default"
    profile, _ = load_profile(directory)
    engine = Engine()
    store = ProfileStore(cast(AsyncEngine, engine))
    first = await store.create("default", profile, "system")
    same = await store.create("default", profile, "system")
    assert first["version"] == same["version"] == 1
    assert len(engine.connection.rows) == 1

    edited = profile.model_dump()
    edited["prompt_templates"]["explore"] += "\nExtra instruction."
    second = await store.create("default", AgentProfile.model_validate(edited), "system")
    assert second["version"] == 2
    assert len(engine.connection.rows) == 2
    assert first["prompt_templates"]["explore"] != second["prompt_templates"]["explore"]


@pytest.mark.parametrize("name", ["default", "single", "ctf"])
@pytest.mark.parametrize("created_by", ["user", "system"])
async def test_bundled_initialization_preserves_existing_named_settings(name, created_by):
    directory = Path(__file__).resolve().parents[3] / "profiles" / name
    profile = load_ctf_profile(directory) if name == "ctf" else load_profile(directory)[0]
    edited = profile.model_dump()
    if name == "ctf":
        edited["model"]["model"] = "ui-selected-model"
        edited["model"]["price"]["billing_mode"] = "fixed"
        edited["prompt_templates"]["lead"] += "\nUser-authored instruction."
        configured = CtfAgentProfile.model_validate(edited)
    else:
        edited["models"]["explore"]["model"] = "ui-selected-model"
        edited["models"]["explore"]["price"]["billing_mode"] = "fixed"
        edited["prompt_templates"]["explore"] += "\nUser-authored instruction."
        configured = AgentProfile.model_validate(edited)
    engine = Engine()
    store = ProfileStore(cast(AsyncEngine, engine))
    await store.create("other-profile", profile, "system")
    initial = await store._ensure_bundled_profile(name, profile)
    assert initial["version"] == 1
    saved = await store.create(name, configured, created_by, expected_version=1)
    preserved = await store._ensure_bundled_profile(name, profile)
    assert preserved == saved
    assert len([row for row in engine.connection.rows if row["name"] == name]) == 2


async def test_explicit_profile_publish_keeps_expected_version_conflicts():
    profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    engine = Engine()
    store = ProfileStore(cast(AsyncEngine, engine))
    first = await store._ensure_bundled_profile("default", profile)
    with pytest.raises(HTTPException) as stale:
        await store.create("default", profile, "system", expected_version=0)
    assert stale.value.status_code == 409
    assert len(engine.connection.rows) == 1
    edited = profile.model_dump()
    edited["prompt_templates"]["explore"] += "\nExplicit template update."
    updated = AgentProfile.model_validate(edited)
    saved = await store.create("default", updated, "system", expected_version=first["version"])
    assert saved["version"] == 2
    with pytest.raises(HTTPException) as stale:
        await store.create("default", updated, "system", expected_version=first["version"])
    assert stale.value.status_code == 409
    assert len(engine.connection.rows) == 2
