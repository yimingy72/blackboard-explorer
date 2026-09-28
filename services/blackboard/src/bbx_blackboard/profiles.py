"""Versioned Agent profiles, independent of task event projections."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from bbx_contracts.models import AgentProfile
from bbx_contracts.profile import load_profile
from fastapi import HTTPException
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from bbx_blackboard.platform import PlatformStore
from bbx_blackboard.store import schema as s


def _content(profile: AgentProfile) -> dict[str, Any]:
    return profile.model_dump(mode="json")


def _digest(content: dict[str, Any]) -> str:
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _row_content(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in AgentProfile.model_fields}


class ProfileStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def list(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            stmt = select(
                s.agent_profiles.c.name,
                func.max(s.agent_profiles.c.version).label("latest_version"),
            ).group_by(s.agent_profiles.c.name)
            return [dict(row) for row in (await conn.execute(stmt)).mappings()]

    async def versions(self, name: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            stmt = (
                select(
                    s.agent_profiles.c.name,
                    s.agent_profiles.c.version,
                    s.agent_profiles.c.created_by,
                    s.agent_profiles.c.created_at,
                )
                .where(s.agent_profiles.c.name == name)
                .order_by(s.agent_profiles.c.version.desc())
            )
            return [dict(row) for row in (await conn.execute(stmt)).mappings()]

    async def get(self, name: str, version: int | None = None) -> dict[str, Any]:
        async with self.engine.connect() as conn:
            stmt = select(s.agent_profiles).where(s.agent_profiles.c.name == name)
            if version is not None:
                stmt = stmt.where(s.agent_profiles.c.version == version)
            else:
                stmt = stmt.order_by(s.agent_profiles.c.version.desc()).limit(1)
            row = (await conn.execute(stmt)).mappings().first()
            if row is None:
                raise KeyError(name)
            return dict(row)

    async def create(
        self,
        name: str,
        profile: AgentProfile,
        created_by: str,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        content = _content(profile)
        # Serialize version assignment for concurrent app startups or editors.
        lock = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            current = (
                (
                    await conn.execute(
                        select(s.agent_profiles)
                        .where(s.agent_profiles.c.name == name)
                        .order_by(s.agent_profiles.c.version.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if expected_version is not None and (
                current is None or current["version"] != expected_version
            ):
                raise HTTPException(
                    409, "设置已被其他操作更新，请重新加载后再保存，当前草稿未被覆盖"
                )
            if current is not None and _digest(_row_content(dict(current))) == _digest(content):
                return dict(current)
            version = current["version"] + 1 if current is not None else 1
            result = await conn.execute(
                insert(s.agent_profiles)
                .values(name=name, version=version, created_by=created_by, **content)
                .returning(s.agent_profiles)
            )
            return dict(result.mappings().one())

    async def _ensure_bundled_profile(self, name: str, profile: AgentProfile) -> dict[str, Any]:
        async with self.engine.connect() as conn:
            previous = (
                (
                    await conn.execute(
                        select(s.agent_profiles)
                        .where(
                            s.agent_profiles.c.name == name,
                            s.agent_profiles.c.created_by == "system",
                        )
                        .order_by(s.agent_profiles.c.version.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        if previous is not None and _digest(_row_content(dict(previous))) == _digest(
            _content(profile)
        ):
            return dict(previous)
        return await self.create(name, profile, "system")

    async def ensure_default(self, directory: Path) -> dict[str, Any]:
        profile, _ = load_profile(directory)
        return await self._ensure_bundled_profile("default", profile)

    async def ensure_bundled(self, directory: Path, platform: PlatformStore | None = None) -> None:
        default, _ = load_profile(directory)
        single, _ = load_profile(directory.parent / "single")
        if platform is not None:
            model = await platform.ensure_default_model(default.models.explore)
            for profile in (default, single):
                for role in ("explore", "derive", "close"):
                    setattr(profile.models, role, model.model_copy(deep=True))
        await self._ensure_bundled_profile("default", default)
        await self._ensure_bundled_profile("single", single)
