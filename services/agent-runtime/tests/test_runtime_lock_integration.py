"""Prove advisory-lock exclusivity and reconnect handoff against PostgreSQL."""

import asyncio

import asyncpg
import pytest
from bbx_runtime.scheduler.lock import RuntimeLock
from bbx_runtime.settings import SchedulerSettings
from pydantic import SecretStr
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration


async def test_runtime_lock_excludes_peer_then_recovers_after_connection_loss(
    runtime_infrastructure,
):
    database_url, _ = runtime_infrastructure
    url = make_url(database_url)
    settings = SchedulerSettings.model_construct(
        postgres_host=url.host,
        postgres_port=url.port,
        postgres_user=url.username,
        postgres_password=SecretStr(url.password or "test"),
        postgres_db=url.database,
    )
    async with RuntimeLock(settings) as first:
        with pytest.raises(RuntimeError, match="already holds"):
            async with RuntimeLock(settings):
                pytest.fail("A second runtime acquired the global lock")
        assert first.connection is not None
        pid = first.connection.get_server_pid()
        probe = await asyncpg.connect(database_url.replace("postgresql+asyncpg", "postgresql"))
        try:
            assert await probe.fetchval("SELECT pg_terminate_backend($1)", pid)
            await asyncio.wait_for(first.lost.wait(), 3)
        finally:
            await probe.close()
    async with RuntimeLock(settings) as replacement:
        assert not replacement.lost.is_set()
