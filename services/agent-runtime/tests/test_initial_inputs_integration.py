"""Initial uploads survive real Ubuntu restore, restarts, and selected archives."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

import docker
import httpx
import pytest
import zstandard
from bbx_contracts.profile import load_profile
from bbx_objects import ObjectStore
from bbx_runtime.clients import EnvdClient
from bbx_runtime.execenv import ExecEnvHandle, ExecEnvManager
from bbx_runtime.settings import Settings
from docker.errors import ImageNotFound
from pydantic import SecretStr
from test_execenv_integration import command
from testcontainers.core.container import DockerContainer

pytestmark = pytest.mark.integration


def attachment(task_id: UUID, filename: str, body: bytes) -> dict:
    key = str(uuid4())
    return {
        "id": key,
        "filename": filename,
        "path": f"/workspace/shared/inputs/{key}/{filename}",
        "uri": f"inputs/{task_id}/{key}/{filename}",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def archive_files(compressed: bytes) -> dict[str, bytes]:
    raw = zstandard.ZstdDecompressor().decompress(compressed, max_output_size=20_000_000)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        result = {}
        for member in archive:
            if member.isfile():
                file = archive.extractfile(member)
                assert file is not None
                result[member.name] = file.read()
        return result


async def check_originals(handle: ExecEnvHandle, originals: list[dict], bodies: dict[str, bytes]):
    async with EnvdClient(handle.base_url, handle.token) as envd:
        assert (await envd.restore_status())["restored"] is True
        for original in originals:
            restored = await envd.read_file(original["path"])
            assert restored == bodies[original["uri"]]
            assert len(restored) == original["size"]
            assert hashlib.sha256(restored).hexdigest() == original["sha256"]
            parent = original["path"].rsplit("/", 1)[0]
            assert (await envd.stat(f"{parent}/would-execute.sh"))["exists"] is False
        assert (await envd.stat("/workspace/shared/unexpected_execution"))["exists"] is False


async def test_initial_inputs_restore_restart_and_archive_resume(monkeypatch):
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")
    docker_client = docker.from_env()
    try:
        docker_client.images.get("bbx-exec-env:latest")
    except ImageNotFound:
        docker_client.close()
        pytest.fail("Build bbx-exec-env:latest with make image-exec-env first")

    task_id = uuid4()
    prefix = f"bbx-task-inputs-{task_id.hex[:8]}"
    network = await asyncio.to_thread(
        docker_client.networks.create, f"{prefix}-exec", driver="bridge", internal=False
    )
    minio_name = f"{prefix}-minio"
    minio = (
        DockerContainer("pgsty/minio:RELEASE.2026-04-17T00-00-00Z")
        .with_name(minio_name)
        .with_exposed_ports(9000)
        .with_env("MINIO_ROOT_USER", "taskinputsuser")
        .with_env("MINIO_ROOT_PASSWORD", "taskinputs-test-password")
        .with_command("server /data --console-address :9001")
    )
    manager = None
    try:
        with minio:
            endpoint = f"{minio.get_container_host_ip()}:{minio.get_exposed_port(9000)}"
            async with httpx.AsyncClient(trust_env=False) as http:
                for _ in range(100):
                    try:
                        if (
                            await http.get(f"http://{endpoint}/minio/health/live")
                        ).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.2)
                else:
                    pytest.fail("Temporary MinIO did not become healthy")
            objects = ObjectStore(
                endpoint,
                "taskinputsuser",
                "taskinputs-test-password",
                f"taskinputs{task_id.hex[:12]}",
            )
            await objects.ensure_bucket()
            settings = Settings.model_construct(
                envd_token_secret=SecretStr("task-inputs-integration-only-secret"),
                exec_network=network.name,
                exec_access_mode="relay",
                exec_egress_mode="direct",
            )
            profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
            manager = ExecEnvManager(settings, docker_client=docker_client, objects=objects)
            monkeypatch.setattr(manager, "_name", lambda _task: f"{prefix}-envd")
            monkeypatch.setattr(manager, "_relay_name", lambda _task: f"{prefix}-relay")

            uploaded = io.BytesIO()
            with zipfile.ZipFile(uploaded, "w") as archive:
                archive.writestr("would-execute.sh", "touch /workspace/shared/unexpected_execution")
            zip_body = uploaded.getvalue()
            text_body = "初始资料\n".encode() + b"\x00\xff"
            originals = [
                attachment(task_id, "source.zip", zip_body),
                attachment(task_id, "初始:数据.txt", text_body),
            ]
            bodies = {originals[0]["uri"]: zip_body, originals[1]["uri"]: text_body}
            for uri, body in bodies.items():
                await objects.put(uri, body)
            handle = await manager.provision(task_id, profile)
            async with EnvdClient(handle.base_url, handle.token) as envd:
                assert (await envd.restore_status())["restored"] is False
            await manager.ensure_initial_inputs(handle, originals)
            await check_originals(handle, originals, bodies)
            empty = {
                "format": "bbx.task-archive.v1",
                "task_id": str(task_id),
                "run_number": 1,
                "state": {
                    "task": {"id": str(task_id), "initial_attachments": originals},
                    "facts": {},
                    "intents": {},
                    "agents": {},
                },
                "events": [],
                "sessions": [],
                "messages": [],
                "task_runs": [],
            }
            no_fact_archive = await manager.archive_task(task_id, empty)
            no_fact_bytes = await objects.get(no_fact_archive.uri)
            initial_files = archive_files(no_fact_bytes)
            for original in originals:
                assert (
                    initial_files[original["path"].removeprefix("/workspace/")]
                    == bodies[original["uri"]]
                )
            initial_manifest = json.loads(initial_files[".bbx/manifest.json"])
            assert initial_manifest["initial_attachments"] == originals
            assert {row["uri"] for row in initial_manifest["restored_files"]} == set(bodies)
            assert not any("would-execute.sh" in name for name in initial_files)

            await manager.create_user(handle, "agent-1")
            result = await command(
                handle,
                "printf 'agent-result\\n' > registered.txt; printf 'scratch\\n' > transient.tmp",
            )
            assert result["exit_code"] == 0
            envd_container = docker_client.containers.get(handle.container_id)
            await asyncio.to_thread(envd_container.stop, timeout=10)
            restarted = await manager.provision(task_id, profile)
            assert restarted.container_id == handle.container_id
            await manager.ensure_initial_inputs(restarted, originals)
            await check_originals(restarted, originals, bodies)
            async with EnvdClient(restarted.base_url, restarted.token) as envd:
                result_bytes = await envd.read_file("/workspace/agents/agent-1/registered.txt")
                assert result_bytes == b"agent-result\n"
                assert (
                    await envd.read_file("/workspace/agents/agent-1/transient.tmp") == b"scratch\n"
                )
            evidence_uri = f"evidence/{task_id}/agent-1/registered.txt"
            await objects.put(evidence_uri, result_bytes)
            selected = {
                **empty,
                "run_number": 2,
                "state": {
                    **empty["state"],
                    "facts": {
                        "F1": {
                            "version": 1,
                            "evidence": [
                                {
                                    "path": "/workspace/agents/agent-1/registered.txt",
                                    "uri": evidence_uri,
                                    "size": len(result_bytes),
                                }
                            ],
                        }
                    },
                },
            }
            selected_archive = await manager.archive_task(task_id, selected)
            assert selected_archive.uri == f"workspace/{task_id}/run-2.tar.zst"
            selected_files = archive_files(await objects.get(selected_archive.uri))
            assert selected_files["agents/agent-1/registered.txt"] == result_bytes
            assert not any(
                "transient.tmp" in name or "would-execute.sh" in name for name in selected_files
            )
            assert await objects.get(no_fact_archive.uri) == no_fact_bytes
            await manager.destroy(task_id)
            assert not docker_client.containers.list(
                all=True, filters={"label": f"bbx.task-id={task_id}"}
            )
            resumed = await manager.provision(task_id, profile, selected_archive.uri)
            assert resumed.container_id != restarted.container_id
            await manager.ensure_initial_inputs(resumed, originals)
            await check_originals(resumed, originals, bodies)
            async with EnvdClient(resumed.base_url, resumed.token) as envd:
                assert (
                    await envd.read_file("/workspace/agents/agent-1/registered.txt") == result_bytes
                )
                assert (await envd.stat("/workspace/agents/agent-1/transient.tmp"))[
                    "exists"
                ] is False
            for uri, body in bodies.items():
                assert await objects.get(uri) == body
    finally:
        try:
            if manager is not None:
                await manager.destroy(task_id)
            await asyncio.to_thread(network.remove)
            assert not docker_client.containers.list(
                all=True, filters={"label": f"bbx.task-id={task_id}"}
            )
            assert not docker_client.containers.list(all=True, filters={"name": minio_name})
            assert not docker_client.networks.list(names=[network.name])
        finally:
            docker_client.close()
