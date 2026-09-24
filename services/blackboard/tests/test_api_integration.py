"""Full HTTP blackboard scenario against isolated PostgreSQL and MinIO."""

import asyncio
import io
import os
import socket
import tarfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import httpx
import pytest
import uvicorn
import zstandard
from alembic import command
from alembic.config import Config
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_agent_token
from bbx_blackboard.settings import Settings
from bbx_blackboard.simulator import run_demo
from bbx_objects import ObjectStore
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
SERVICE_TOKEN = "bbx-m1b-integration-service"


@pytest.fixture(scope="module")
def infrastructure():
    postgres = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg").with_name(
        f"bbx-m1b-postgres-{uuid4().hex[:8]}"
    )
    minio = (
        DockerContainer("pgsty/minio:RELEASE.2026-04-17T00-00-00Z")
        .with_name(f"bbx-m1b-minio-{uuid4().hex[:8]}")
        .with_exposed_ports(9000)
        .with_env("MINIO_ROOT_USER", "bbxm1buser")
        .with_env("MINIO_ROOT_PASSWORD", "bbxm1b-test-password")
        .with_command("server /data --console-address :9001")
    )
    with postgres, minio:
        database_url = postgres.get_connection_url()
        endpoint = f"{minio.get_container_host_ip()}:{minio.get_exposed_port(9000)}"
        deadline = time.monotonic() + 30
        while True:
            try:
                with urlopen(f"http://{endpoint}/minio/health/live", timeout=2) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        old_url = os.environ.get("BBX_DATABASE_URL")
        os.environ["BBX_DATABASE_URL"] = database_url
        try:
            command.upgrade(Config(str(ROOT / "services/blackboard/alembic.ini")), "head")
            yield database_url, endpoint
        finally:
            if old_url is None:
                os.environ.pop("BBX_DATABASE_URL", None)
            else:
                os.environ["BBX_DATABASE_URL"] = old_url


async def next_sse(lines: AsyncIterator[str]) -> dict[str, str]:
    event: dict[str, str] = {}
    async for line in lines:
        if not line:
            if event:
                return event
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        event[key] = value.lstrip(" ")
    raise AssertionError("SSE stream ended before an event")


@pytest.mark.asyncio
async def test_full_http_demo_and_sse_resume(infrastructure) -> None:
    database_url, endpoint = infrastructure
    parsed = make_url(database_url)
    settings = Settings(
        postgres_host=parsed.host or "127.0.0.1",
        postgres_port=parsed.port or 5432,
        postgres_user=parsed.username or "test",
        postgres_password=SecretStr(parsed.password or "test"),
        postgres_db=parsed.database or "test",
        minio_root_user="bbxm1buser",
        minio_root_password=SecretStr("bbxm1b-test-password"),
        minio_endpoint=f"http://{endpoint}",
        minio_bucket=f"bbxm1b{uuid4().hex[:12]}",
        service_token=SecretStr(SERVICE_TOKEN),
        agent_token_secret=SecretStr("bbx-m1b-integration-agent-secret"),
        admin_users=SecretStr("admin:integration-only"),
        profiles_dir=ROOT / "profiles/default",
    )
    objects = ObjectStore(
        endpoint,
        "bbxm1buser",
        "bbxm1b-test-password",
        settings.minio_bucket,
    )
    await objects.ensure_bucket()
    engine = create_async_engine(settings.database_url)
    app = create_app(settings, engine=engine, objects=objects)
    server_socket = socket.socket()
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    host, port = server_socket.getsockname()
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, lifespan="on", log_level="warning")
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))
    try:
        for _ in range(200):
            if server.started:
                break
            if server_task.done():
                await server_task
                raise AssertionError("Uvicorn exited before starting")
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("Uvicorn did not start")
        base_url = f"http://{host}:{port}"
        summary = await asyncio.to_thread(run_demo, base_url, SERVICE_TOKEN, 0)
        tid = summary["task_id"]
        headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        async with httpx.AsyncClient(base_url=base_url, headers=headers, trust_env=False) as client:
            state = (await client.get(f"/api/tasks/{tid}/state")).json()
            assert state["task"]["status"] == "finished"
            assert state["task"]["acceptance_state"]["A1"]["status"] == "met"
            assert set(state["facts"]) == set(summary["fact_ids"])
            assert state["intents"][summary["intent_id"]]["result"] == "confirmed"
            assert state["intents"][summary["intent_id"]]["attempts"] == 0
            assert state["facts"][summary["fact_ids"][1]]["status"] == "proposed"
            assert len(state["agents"]) == 7
            assert all(agent["status"] == "finished" for agent in state["agents"].values())
            assert sorted(status for _, status in summary["claim_results"]) == [200, 422]
            versions = summary["event_versions"]
            assert versions == sorted(set(versions))
            types = summary["event_types"]
            for required in (
                "intent.claimed",
                "intent.closed",
                "fact.disputed",
                "fact.undisputed",
                "acceptance.reverted",
                "task.report",
                "task.finished",
                "agent.finished",
                "agent.conclude_requested",
            ):
                assert required in types
            assert types.count("acceptance.judged") == 4
            assert types.count("agent.finished") == len(state["agents"])
            assert types.count("agent.conclude_requested") == 3
            assert types.index("agent.conclude_requested") < types.index("task.finished")
            assert types[-1] == "agent.finished"
            assert (await client.get(f"/api/tasks/{tid}/report")).text.startswith("# Final report")

            async with client.stream(
                "GET", f"/api/tasks/{tid}/stream", params={"since": 0}
            ) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                for version, kind in zip(versions, types, strict=True):
                    item = await asyncio.wait_for(next_sse(lines), 10)
                    assert int(item["id"]) == version
                    assert item["event"] == kind

            other = (
                await client.post(
                    "/api/tasks",
                    json={
                        "goal": "Other task",
                        "acceptance": [{"id": "A1", "desc": "Separate evidence"}],
                        "budget": {"max_cost": "1", "max_minutes": 10},
                        "agent_profile": "default",
                    },
                )
            ).json()["id"]
            other_agent = (
                await client.post(f"/api/tasks/{other}/agents", json={"task_type": "explore"})
            ).json()
            denied = await client.get(
                "/api/evidence",
                params={"uri": summary["evidence_uris"][0]},
                headers={"Authorization": f"Bearer {other_agent['token']}"},
            )
            assert denied.status_code == 403

            stream_task = (
                await client.post(
                    "/api/tasks",
                    json={
                        "goal": "SSE task",
                        "acceptance": [{"id": "A1", "desc": "Observe transitions"}],
                        "budget": {"max_cost": "1", "max_minutes": 10},
                        "agent_profile": "default",
                    },
                )
            ).json()["id"]
            path = f"/api/tasks/{stream_task}/stream"
            async with client.stream("GET", path, params={"since": 0}) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                created = await asyncio.wait_for(next_sse(lines), 10)
                assert created["event"] == "task.created"
                assert int(created["id"]) > 0
                started = await client.post(f"/api/tasks/{stream_task}/start")
                assert started.status_code == 200
                provisioning = await asyncio.wait_for(next_sse(lines), 10)
                assert provisioning["event"] == "task.provisioning"
                assert int(provisioning["id"]) > int(created["id"])
            running = await client.post(
                f"/api/tasks/{stream_task}/status", json={"status": "running"}
            )
            assert running.status_code == 200
            async with client.stream(
                "GET", path, headers={"Last-Event-ID": provisioning["id"]}
            ) as response:
                assert response.status_code == 200
                resumed = await asyncio.wait_for(next_sse(response.aiter_lines()), 10)
                assert resumed["event"] == "task.running"
                assert int(resumed["id"]) > int(provisioning["id"])
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, 15)
        server_socket.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_workspace_api_reads_registered_minio_archive_with_task_auth(infrastructure) -> None:
    database_url, endpoint = infrastructure
    parsed = make_url(database_url)
    settings = Settings(
        postgres_host=parsed.host or "127.0.0.1",
        postgres_port=parsed.port or 5432,
        postgres_user=parsed.username or "test",
        postgres_password=SecretStr(parsed.password or "test"),
        postgres_db=parsed.database or "test",
        minio_root_user="bbxm1buser",
        minio_root_password=SecretStr("bbxm1b-test-password"),
        minio_endpoint=f"http://{endpoint}",
        minio_bucket=f"bbxm4workspace{uuid4().hex[:12]}",
        service_token=SecretStr(SERVICE_TOKEN),
        agent_token_secret=SecretStr("bbx-m4-workspace-agent-signing-test-secret"),
        admin_users=SecretStr("admin:integration-only"),
        profiles_dir=ROOT / "profiles/default",
    )
    objects = ObjectStore(endpoint, "bbxm1buser", "bbxm1b-test-password", settings.minio_bucket)
    await objects.ensure_bucket()
    engine = create_async_engine(settings.database_url)
    app = create_app(settings, engine=engine, objects=objects)
    try:
        service = app.state.board_service
        task_id = await service.create_task(
            {
                "goal": "Inspect archived workspace",
                "acceptance": [{"id": "A1", "desc": "Read source"}],
                "budget": {"max_cost": "1", "max_minutes": 5},
                "agent_profile": "default",
            }
        )
        for status in ("provisioning", "running", "closing", "finished"):
            await service.transition(task_id, status)
        uri = f"workspace/{task_id}.tar.zst"
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w") as tar:
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            tar.addfile(root)
            source = tarfile.TarInfo("./shared/orders.py")
            data = b"def reserve():\n    return True\n"
            source.size = len(data)
            tar.addfile(source, io.BytesIO(data))
            link = tarfile.TarInfo("./shared/python")
            link.type = tarfile.SYMTYPE
            link.linkname = "/usr/bin/python3"
            tar.addfile(link)
        compressed = zstandard.ZstdCompressor().compress(content.getvalue())
        await objects.put(uri, compressed, content_type="application/zstd")
        await service.record_archive(task_id, uri, len(compressed), "none")

        same_agent = issue_agent_token(settings, task_id, "agent-1")
        other_agent = issue_agent_token(settings, uuid4(), "agent-2")
        base = f"/api/tasks/{task_id}/workspace"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
        ) as client:
            assert (await client.get(f"{base}/tree")).status_code == 401
            assert (
                await client.get(f"{base}/tree", headers={"Authorization": f"Bearer {other_agent}"})
            ).status_code == 403
            headers = {"Authorization": f"Bearer {same_agent}"}
            tree = await client.get(f"{base}/tree", headers=headers)
            assert tree.status_code == 200, tree.text
            entries = {entry["path"]: entry for entry in tree.json()["entries"]}
            assert entries["shared/orders.py"]["size"] == len(data)
            assert entries["shared/python"]["kind"] == "link"
            preview = await client.get(
                f"{base}/file", params={"path": "shared/orders.py"}, headers=headers
            )
            assert preview.json() == {
                "path": "shared/orders.py",
                "size": len(data),
                "text": data.decode(),
                "truncated": False,
                "binary": False,
            }
            assert (
                await client.get(f"{base}/file", params={"path": "shared/python"}, headers=headers)
            ).status_code == 400
    finally:
        await app.state.workspace_cache.close()
        await engine.dispose()
