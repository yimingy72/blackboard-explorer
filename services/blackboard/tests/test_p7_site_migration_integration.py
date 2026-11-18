"""Only an exact profile may move at the P7 empty-secondary cutpoint."""

from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.site_migration import (
    CutoverBlocked,
    configuration_digest,
    import_secondary_profile,
)
from bbx_blackboard.store import schema as s
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def postgres_cutpoint(monkeypatch):
    with ExitStack() as stack:
        urls = []
        for role in ("primary", "secondary"):
            pg = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg")
            pg.with_name(f"bbx-p7-cutpoint-{role}-{uuid4().hex[:8]}")
            stack.enter_context(pg)
            url = pg.get_connection_url()
            monkeypatch.setenv("BBX_DATABASE_URL", url)
            command.upgrade(Config(str(ROOT / "services/blackboard/alembic.ini")), "0032")
            urls.append(url)
        yield urls


async def test_exact_profile_copy_rejects_new_secondary_rows_and_target_conflict(postgres_cutpoint):
    primary, secondary = [create_async_engine(url) for url in postgres_cutpoint]
    primary_profiles, secondary_profiles = ProfileStore(primary), ProfileStore(secondary)
    try:
        await primary_profiles.ensure_default(ROOT / "profiles/default")
        retained = await primary_profiles.get("default", 1)
        await secondary_profiles.ensure_bundled(ROOT / "profiles/default")
        original = await secondary_profiles.get("ctf", 1)
        identity = text("SELECT system_identifier::text FROM pg_control_system()")
        async with primary.connect() as target, secondary.connect() as source:
            cutpoint = {
                "primary_system_id": str(await target.scalar(identity)),
                "secondary_system_id": str(await source.scalar(identity)),
                "secondary_config_sha256": await configuration_digest(source),
            }

        async def apply(*, check_only=False):
            async with primary.begin() as target, secondary.begin() as source:
                return await import_secondary_profile(
                    target, source, **cutpoint, check_only=check_only
                )

        async with secondary.begin() as source:
            await source.execute(s.app_settings.insert().values(key="new-config", value={}))
        with pytest.raises(CutoverBlocked, match="configuration differs"):
            await apply()
        group = uuid4()
        async with secondary.begin() as source:
            await source.execute(
                s.app_settings.delete().where(s.app_settings.c.key == "new-config")
            )
            await source.execute(
                s.task_input_groups.insert().values(
                    id=group, owner="fixture", expires_at=func.now()
                )
            )
        with pytest.raises(CutoverBlocked, match="Secondary has business data"):
            await apply()
        async with primary.connect() as target:
            assert not (
                await target.execute(
                    select(s.agent_profiles.c.name).where(s.agent_profiles.c.name == "ctf")
                )
            ).first()
        async with secondary.begin() as source:
            await source.execute(
                s.task_input_groups.delete().where(s.task_input_groups.c.id == group)
            )

        assert await apply(check_only=True) is True
        with pytest.raises(KeyError):
            await primary_profiles.get("ctf", 1)
        assert await apply() is True
        assert await primary_profiles.get("ctf", 1) == original
        assert await primary_profiles.get("default", 1) == retained
        assert await apply() is False
        async with primary.begin() as target:
            await target.execute(
                s.agent_profiles.update()
                .where(s.agent_profiles.c.name == "ctf")
                .values(created_by="different")
            )
        with pytest.raises(CutoverBlocked, match="conflicts"):
            await apply()
        assert (await primary_profiles.get("ctf", 1))["created_by"] == "different"
        assert await secondary_profiles.get("ctf", 1) == original
    finally:
        await primary.dispose()
        await secondary.dispose()
