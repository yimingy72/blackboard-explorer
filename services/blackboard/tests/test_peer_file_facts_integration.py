"""File evidence is immutable and only becomes a Fact after successful capture."""

import asyncio
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bbx_blackboard.runner_control import row
from bbx_blackboard.runner_gateway import RunnerGateway
from bbx_blackboard.store import schema as s
from bbx_contracts.runner import runner_json_text, runner_payload_digest
from fastapi import HTTPException
from sqlalchemy import insert, select, update
from starlette.datastructures import Headers
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_worker_pool_integration import pool_task, publish, reconcile, synthetic_principal
from test_worker_pool_lifecycle_integration import change_challenge

pytestmark = pytest.mark.integration
KEY = "synthetic-file-report-key"
PATH = "/workspace/shared/challenges/sample/artifacts/solve.py"


class FileObjects:
    def __init__(self):
        self.values = {}
        self.fail = False
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()

    async def put(self, uri, body, **_kwargs):
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise OSError("Synthetic upload failure")
        self.values[uri] = body.read()


async def file_call(ctf, gateway, tid, *, revision=None):
    iid = hashlib.sha256(uuid4().bytes).hexdigest()
    async with ctf.repo.engine.begin() as conn:
        await ctf.repo.lock(conn, tid)
        task = await row(conn, s.tasks, tid)
        active = task["ctf_control"]["native_worker_active"]["member-1"]
        challenge = await ctf._challenge(conn, tid, active["work_id"])
        resource = (
            (
                await conn.execute(
                    select(s.task_runner_resources).where(
                        s.task_runner_resources.c.task_id == tid,
                    )
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(
            update(s.task_runner_resources)
            .where(
                s.task_runner_resources.c.task_id == tid,
            )
            .values(
                data={**resource["data"], "owner_epoch": active["epoch"]},
                sandbox_host={"report_key_hash": hashlib.sha256(KEY.encode()).hexdigest()},
            )
        )
        arguments = runner_json_text(
            {
                "path": PATH,
                "fact": {
                    "expected_revision": revision or challenge["revision"],
                    "kind": "observation",
                    "statement": "Saved the current script snapshot",
                },
            }
        )
        await conn.execute(
            insert(s.runner_calls).values(
                task_id=tid,
                id="tool:" + iid,
                kind="tool",
                binding_id=active["binding_id"],
                owner_epoch=active["epoch"],
                state="dispatched",
                concurrency_active=True,
                provider_key="sandbox",
                created_at=datetime.now(UTC),
                data={
                    "invocation_id": iid,
                    "tool_name": "read_file",
                    "sdk_admitted": True,
                    "business": {"worker_pool": True},
                    "binding": {"agent_id": "member-1"},
                    "authorized_wait_until_ms": 9999999999999,
                    "arguments_digest": runner_payload_digest(arguments),
                    "arguments_ciphertext": gateway.cipher.encrypt(arguments.encode()).decode(),
                    "sandbox_binding": {
                        "agent_id": "member-1",
                        "challenge_id": active["work_id"],
                        "assignment_id": active["assignment_ref"],
                        "role": "worker",
                    },
                },
            )
        )
    return iid


def request(objects, content):
    async def stream():
        yield content

    return SimpleNamespace(
        headers=Headers({"content-type": "application/octet-stream"}),
        app=SimpleNamespace(state=SimpleNamespace(objects=objects)),
        stream=stream,
    )


async def board_rows(ctf, tid):
    async with ctf.repo.engine.connect() as conn:
        facts = list(
            (await conn.execute(select(s.facts).where(s.facts.c.task_id == tid))).mappings()
        )
        records = list(
            (
                await conn.execute(
                    select(s.ctf_records).where(
                        s.ctf_records.c.task_id == tid,
                    )
                )
            ).mappings()
        )
        return facts, records


async def test_file_versions_and_atomic_stale_fact_rejection(ctf):
    async with pool_task(ctf, 1) as (tid, pool):
        challenge = await publish(ctf, tid)
        await reconcile(ctf, tid, pool)
        await synthetic_principal(ctf, pool, tid, "member-1")
        gateway = RunnerGateway(ctf.repo.engine, settings())
        objects = FileObjects()
        saved = []
        for content in (b"first version", b"second version"):
            iid = await file_call(ctf, gateway, tid)
            proof = await gateway.sandbox.upload_board_evidence(
                request(objects, content), tid, iid, KEY
            )
            assert proof["sha256"] == hashlib.sha256(content).hexdigest()
            assert proof["size"] == len(content)
            assert len((await board_rows(ctf, tid))[0]) == len(saved)
            result = {"structuredContent": {"result": {"artifact": proof}}, "isError": False}
            response = await gateway.sandbox.complete(tid, iid, KEY, result)
            assert "fact" in response["result"]["structuredContent"]["result"]
            assert await gateway.sandbox.complete(tid, iid, KEY, result) == response
            saved.append(proof)
        assert saved[0]["uri"] != saved[1]["uri"]
        assert objects.values[saved[0]["uri"]] == b"first version"
        facts, records = await board_rows(ctf, tid)
        assert len(facts) == 2 and len(records) == 4
        assert all(item["evidence"][0]["path"] == PATH for item in facts)

        iid = await file_call(ctf, gateway, tid)
        proof = await gateway.sandbox.upload_board_evidence(
            request(objects, b"third"), tid, iid, KEY
        )
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await change_challenge(ctf, conn, tid, challenge["id"], description="Updated material")
        result = await gateway.sandbox.complete(
            tid,
            iid,
            KEY,
            {
                "structuredContent": {"result": {"artifact": proof}},
                "isError": False,
            },
        )
        assert result["result"]["isError"] is True
        assert [len(rows) for rows in await board_rows(ctf, tid)] == [2, 4]
        async with ctf.repo.engine.connect() as conn:
            assert (await row(conn, s.runner_calls, tid, "tool:" + iid))["state"] == "completed"


async def test_upload_failure_or_failed_host_read_never_posts_a_fact(ctf):
    async with pool_task(ctf, 1) as (tid, pool):
        await publish(ctf, tid)
        await reconcile(ctf, tid, pool)
        await synthetic_principal(ctf, pool, tid, "member-1")
        gateway, objects = RunnerGateway(ctf.repo.engine, settings()), FileObjects()
        iid = await file_call(ctf, gateway, tid)
        objects.fail = True
        with pytest.raises(OSError, match="upload failure"):
            await gateway.sandbox.upload_board_evidence(request(objects, b"partial"), tid, iid, KEY)
        objects.fail = False
        proof = await gateway.sandbox.upload_board_evidence(
            request(objects, b"captured"), tid, iid, KEY
        )
        result = await gateway.sandbox.complete(
            tid,
            iid,
            KEY,
            {
                "structuredContent": {"result": {"artifact": proof}},
                "isError": True,
            },
        )
        assert result["result"]["isError"] is True
        assert await board_rows(ctf, tid) == ([], [])


async def test_upload_does_not_hold_task_lock_and_rechecks_original_assignment(ctf):
    async with pool_task(ctf, 1) as (tid, pool):
        await publish(ctf, tid)
        await reconcile(ctf, tid, pool)
        await synthetic_principal(ctf, pool, tid, "member-1")
        gateway, objects = RunnerGateway(ctf.repo.engine, settings()), FileObjects()
        iid = await file_call(ctf, gateway, tid)
        objects.release.clear()
        uploading = asyncio.create_task(
            gateway.sandbox.upload_board_evidence(
                request(objects, b"saved bytes"),
                tid,
                iid,
                KEY,
            )
        )
        try:
            await asyncio.wait_for(objects.started.wait(), 5)
            async with asyncio.timeout(5), ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                task = await row(conn, s.tasks, tid)
                active = task["ctf_control"]["native_worker_active"]
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(
                        ctf_control={
                            **task["ctf_control"],
                            "native_worker_active": {
                                **active,
                                "member-1": {**active["member-1"], "releasing": True},
                            },
                        }
                    )
                )
        finally:
            objects.release.set()
        with pytest.raises(HTTPException, match="original scoped"):
            await uploading
        assert await board_rows(ctf, tid) == ([], [])
