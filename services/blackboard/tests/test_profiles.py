"""Profile versions retain prompt bodies and skip unchanged content."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from bbx_blackboard.profiles import ProfileStore
from bbx_contracts.models import AgentProfile
from bbx_contracts.profile import load_profile
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
            return Result(self.rows[-1] if self.rows else None)
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
