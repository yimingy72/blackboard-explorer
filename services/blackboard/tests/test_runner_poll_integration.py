"""Runner poll notifications and lease boundaries against disposable PostgreSQL."""

import asyncio
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4

import asyncpg
import jwt
import pytest
from bbx_blackboard import runner_control
from bbx_blackboard.runner_auth import RunnerIdentity
from bbx_blackboard.sse import SSEDispatcher
from bbx_blackboard.store import schema as s
from bbx_contracts.runner import RunnerFence, runner_json_text, runner_payload_digest
from fastapi import HTTPException
from sqlalchemy import update
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_runner_control_integration import (
    acquire,
    auth,
    connect,
    control,
    poll_query,
    post_input,
    setup,
)
from test_sse import until

pytestmark = pytest.mark.integration


@pytest.fixture
async def dispatcher(ctf_url):
    async def connect():
        return await asyncpg.connect(ctf_url.replace("postgresql+asyncpg:", "postgresql:"))

    dispatcher = SSEDispatcher(settings(), connect=connect)
    await dispatcher.start()
    try:
        await asyncio.wait_for(dispatcher._ready.wait(), 2)
        yield dispatcher
    finally:
        await dispatcher.stop()


async def test_poll_reads_after_subscribing_and_wakes_only_after_commit(
    ctf, dispatcher, monkeypatch
):
    async with connect(ctf) as (app, http):
        tid, owner, binding, prepare = await setup(http, ctf)
        app.state.dispatcher = dispatcher
        service = app.state.runner_control
        channel = f"bbx_task_{tid.hex}"
        original_poll = service._poll

        async def race(*args):
            assert channel in dispatcher._subscribers and channel in dispatcher._listening
            await post_input(http, ctf, tid, owner, binding, "Committed between subscribe and read")
            return await original_poll(*args)

        monkeypatch.setattr(service, "_poll", race)
        response = await http.get(
            control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
        )
        assert response.status_code == 200, response.text
        commands = response.json()["commands"]
        assert [item["command_seq"] for item in commands] == [prepare["command_seq"], 2]
        monkeypatch.setattr(service, "_poll", original_poll)
        checked = asyncio.Event()
        original_identity = service._identity
        reads = 0

        async def identity(*args, **kwargs):
            nonlocal reads
            reads += 1
            value = await original_identity(*args, **kwargs)
            checked.set()
            return value

        monkeypatch.setattr(service, "_identity", identity)
        async with dispatcher.subscribe(tid) as committed:
            waiting = asyncio.create_task(
                http.get(
                    control(tid) + "/commands",
                    headers=auth(owner),
                    params={**poll_query(owner, after=2), "wait_seconds": 15},
                )
            )
            try:
                await asyncio.wait_for(checked.wait(), 1)
                await until(lambda: ctf.repo.engine.pool.checkedout() == 0)
                await asyncio.sleep(1.05)
                assert reads == 1 and not waiting.done()
                assert ctf.repo.engine.pool.checkedout() == 0
                payload = runner_json_text({"kind": "close", "binding_id": binding["binding_id"]})
                for commit in (False, True):
                    async with ctf.repo.engine.connect() as conn:
                        transaction = await conn.begin()
                        await service.repo.lock(conn, tid)
                        current = await service._current_owner(conn, tid)
                        command = await service._enqueue(
                            conn,
                            tid,
                            current,
                            str(uuid4()),
                            payload,
                            runner_payload_digest(payload),
                        )
                        await asyncio.sleep(0.03)
                        assert committed.empty() and not waiting.done()
                        if commit:
                            await transaction.commit()
                        else:
                            await transaction.rollback()
                    if not commit:
                        await asyncio.sleep(0.03)
                        assert committed.empty() and reads == 1
                await asyncio.wait_for(committed.get(), 1)
                response = await asyncio.wait_for(waiting, 1)
                assert response.status_code == 200, response.text
                assert response.json()["commands"][0]["request_id"] == command["request_id"]
                assert response.json()["commands"][0]["command_seq"] == 3
            finally:
                waiting.cancel()
                with suppress(asyncio.CancelledError):
                    await waiting
        assert channel not in dispatcher._subscribers
        cancelled = asyncio.create_task(
            http.get(
                control(tid) + "/commands",
                headers=auth(owner),
                params={**poll_query(owner, after=3), "wait_seconds": 15},
            )
        )
        try:
            await until(lambda: channel in dispatcher._subscribers)
            await until(lambda: ctf.repo.engine.pool.checkedout() == 0)
        finally:
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(cancelled, 0.5)
        assert channel not in dispatcher._subscribers and channel not in dispatcher._listening


@pytest.mark.parametrize("boundary", ["permit", "expires_at", "hard_deadline"])
async def test_poll_without_ready_listener_respects_boundaries_and_fallback(
    ctf, monkeypatch, boundary
):
    monkeypatch.setattr(runner_control, "PERMIT_SECONDS", 0.6)
    async with connect(ctf) as (app, http):
        tid, owner, binding, command = await setup(http, ctf)
        service = app.state.runner_control
        identity = RunnerIdentity(
            RunnerFence.model_validate(owner["fence"]),
            jwt.decode(owner["token"], options={"verify_signature": False})["token_version"],
        )
        unavailable = SSEDispatcher(settings())
        if boundary != "permit":
            async with ctf.repo.engine.begin() as conn:
                await conn.execute(
                    update(s.runner_owners)
                    .where(s.runner_owners.c.task_id == tid)
                    .values({boundary: service.clock() + timedelta(seconds=0.1)})
                )
            with pytest.raises(HTTPException, match="owner fence"):
                await asyncio.wait_for(
                    service.poll(tid, identity, 1000, 5, dispatcher=unavailable), 0.5
                )
        else:
            _, lease = await acquire(http, tid, owner, binding, command)
            renewed = await asyncio.wait_for(
                service.poll(tid, identity, 1000, 5, dispatcher=unavailable), 0.5
            )
            assert renewed["lease_renewals"][0]["lease_revision"] > lease["lease_revision"]
            monkeypatch.setattr(runner_control, "POLL_RECHECK_SECONDS", 0.1)
            waiting = asyncio.create_task(
                service.poll(tid, identity, 1000, 5, dispatcher=unavailable)
            )
            try:
                await until(lambda: bool(unavailable._subscribers))
                await until(lambda: ctf.repo.engine.pool.checkedout() == 0)
                async with ctf.repo.engine.begin() as conn:
                    await conn.execute(
                        update(s.tasks).where(s.tasks.c.id == tid).values(status="stopped")
                    )
                with pytest.raises(HTTPException, match="control is stopped"):
                    await asyncio.wait_for(waiting, 0.5)
            finally:
                waiting.cancel()
                with suppress(asyncio.CancelledError, HTTPException):
                    await waiting
        assert not unavailable._subscribers
        assert ctf.repo.engine.pool.checkedout() == 0
