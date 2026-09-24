"""A dedicated PostgreSQL session guards the single runtime instance."""

import asyncio

import asyncpg

from bbx_runtime.settings import SchedulerSettings

LOCK_KEY = 0x42425852554E
PING_SECONDS = 2.0
PING_TIMEOUT = 2.0


class RuntimeLock:
    def __init__(self, settings: SchedulerSettings) -> None:
        self.settings = settings
        self.connection: asyncpg.Connection | None = None
        self.lost = asyncio.Event()
        self.monitor: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "RuntimeLock":
        cfg = self.settings
        connection = await asyncpg.connect(
            host=cfg.postgres_host,
            port=cfg.postgres_port,
            user=cfg.postgres_user,
            password=cfg.postgres_password.get_secret_value(),
            database=cfg.postgres_db,
            timeout=10,
            command_timeout=10,
        )
        self.connection = connection
        try:
            acquired = await connection.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY)
            if not acquired:
                raise RuntimeError("Another agent-runtime already holds the instance lock")
            connection.add_termination_listener(self._terminated)
            self.monitor = asyncio.create_task(self._monitor(), name="runtime-lock-monitor")
        except BaseException:
            await connection.close()
            self.connection = None
            raise
        return self

    def _terminated(self, _connection: asyncpg.Connection) -> None:
        self.lost.set()

    async def _monitor(self) -> None:
        assert self.connection is not None
        try:
            while not self.lost.is_set():
                await asyncio.sleep(PING_SECONDS)
                async with asyncio.timeout(PING_TIMEOUT):
                    await self.connection.fetchval("SELECT 1")
        except Exception:
            self.lost.set()

    async def __aexit__(self, *_args: object) -> None:
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.connection is not None:
            self.connection.remove_termination_listener(self._terminated)
            await self.connection.close()
            self.connection = None
