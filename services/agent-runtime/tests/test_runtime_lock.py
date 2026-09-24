"""The single-instance lock excludes peers and releases its database session."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from bbx_runtime.scheduler.lock import RuntimeLock
from bbx_runtime.settings import SchedulerSettings
from pydantic import SecretStr


def lock_settings():
    return SchedulerSettings.model_construct(postgres_password=SecretStr("test-only"))


async def test_lock_releases_session_and_detects_connection_loss(monkeypatch):
    connection = Mock()
    connection.fetchval = AsyncMock(return_value=True)
    connection.close = AsyncMock()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr("bbx_runtime.scheduler.lock.asyncpg.connect", connect)
    async with RuntimeLock(lock_settings()) as instance:
        connection.add_termination_listener.call_args.args[0](connection)
        await asyncio.wait_for(instance.lost.wait(), 0.1)
    connection.close.assert_awaited_once()
    connection.remove_termination_listener.assert_called_once()


async def test_second_instance_cannot_keep_a_session(monkeypatch):
    connection = Mock(fetchval=AsyncMock(return_value=False), close=AsyncMock())
    monkeypatch.setattr(
        "bbx_runtime.scheduler.lock.asyncpg.connect", AsyncMock(return_value=connection)
    )
    with pytest.raises(RuntimeError, match="already holds"):
        async with RuntimeLock(lock_settings()):
            pytest.fail("Second runtime must never start")
    connection.close.assert_awaited_once()


async def test_lock_ping_detects_a_silent_connection_failure(monkeypatch):
    connection = Mock(fetchval=AsyncMock(side_effect=[True, ConnectionError()]), close=AsyncMock())
    monkeypatch.setattr(
        "bbx_runtime.scheduler.lock.asyncpg.connect", AsyncMock(return_value=connection)
    )
    monkeypatch.setattr("bbx_runtime.scheduler.lock.PING_SECONDS", 0.001)
    async with RuntimeLock(lock_settings()) as instance:
        await asyncio.wait_for(instance.lost.wait(), 0.1)
    assert connection.fetchval.await_count == 2
