"""Real envd lifecycle across direct and proxy Docker networks."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path
from uuid import uuid4

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
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr
from testcontainers.core.container import DockerContainer

pytestmark = pytest.mark.integration


async def command(handle: ExecEnvHandle, value: str) -> dict:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {handle.token}", "X-Agent-Id": "agent-1"},
        trust_env=False,
        timeout=30,
    ) as http:
        async with streamable_http_client(f"{handle.base_url}/mcp", http_client=http) as (
            reader,
            writer,
            _,
        ):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("execute_command", {"command": value})
                assert not result.isError
                assert result.structuredContent is not None
                return result.structuredContent


@pytest.mark.asyncio
@pytest.mark.parametrize("egress_mode, internal", [("direct", False), ("proxy", True)])
async def test_execenv_lifecycle_with_archive(
    monkeypatch: pytest.MonkeyPatch, egress_mode: str, internal: bool
) -> None:
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")
    docker_client = docker.from_env()
    try:
        docker_client.images.get("bbx-exec-env:latest")
    except ImageNotFound:
        pytest.skip("bbx-exec-env:latest is not available; run make image-exec-env")

    task_id = uuid4()
    network = await asyncio.to_thread(
        docker_client.networks.create,
        f"bbx-m2a-exec-{task_id.hex[:8]}",
        driver="bridge",
        internal=internal,
    )
    network.reload()
    assert network.attrs["Internal"] is internal
    minio = (
        DockerContainer("pgsty/minio:RELEASE.2026-04-17T00-00-00Z")
        .with_name(f"bbx-m2a-minio-{task_id.hex[:8]}")
        .with_exposed_ports(9000)
        .with_env("MINIO_ROOT_USER", "bbxm2auser")
        .with_env("MINIO_ROOT_PASSWORD", "bbxm2a-password")
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
                    pytest.fail("MinIO did not become healthy")
            objects = ObjectStore(
                endpoint, "bbxm2auser", "bbxm2a-password", f"bbxm2a{task_id.hex[:12]}"
            )
            await objects.ensure_bucket()
            settings = Settings.model_construct(
                deepseek_api_key=SecretStr("fake-deepseek"),
                minio_root_password=SecretStr("bbxm2a-password"),
                service_token=SecretStr("fake-service"),
                envd_token_secret=SecretStr("integration-only-secret"),
                exec_network=network.name,
                exec_access_mode="relay",
                exec_egress_mode=egress_mode,
            )
            profile, _ = load_profile(Path(__file__).resolve().parents[3] / "profiles/default")
            manager = ExecEnvManager(settings, docker_client=docker_client, objects=objects)
            handle = await manager.provision(task_id, profile)
            assert handle.base_url.startswith("http://127.0.0.1:")
            assert handle.relay_id is not None
            envd_container = docker_client.containers.get(handle.container_id)
            envd_container.reload()
            assert list(envd_container.attrs["NetworkSettings"]["Networks"]) == [network.name]
            assert envd_container.attrs["HostConfig"]["PortBindings"] in (None, {})
            assert envd_container.attrs["HostConfig"]["CapAdd"] == ["NET_ADMIN"]
            assert not envd_container.attrs["HostConfig"]["CapDrop"]
            assert not envd_container.attrs["HostConfig"]["Privileged"]
            assert not envd_container.attrs["HostConfig"]["SecurityOpt"]
            assert (
                envd_container.attrs["HostConfig"]["Devices"][0]["PathInContainer"]
                == "/dev/net/tun"
            )
            assert envd_container.labels["bbx.task-id"] == str(task_id)
            assert "PRIVILEGED_PREFIXES=" in envd_container.attrs["Config"]["Env"]
            proxy_vars = [
                item
                for item in envd_container.attrs["Config"]["Env"]
                if item.split("=", 1)[0].lower() in {"http_proxy", "https_proxy"}
            ]
            assert bool(proxy_vars) is internal

            assert (await manager.create_user(handle, "agent-1"))["home"].endswith("agent-1")
            async with EnvdClient(handle.base_url, handle.token) as envd:
                assert (await envd.health())["status"] == "ok"
            result = await command(
                handle,
                "sudo -n id -u; echo m2a-proof > evidence.txt; "
                "dd if=/dev/zero of=transient.bin bs=1M count=8 2>/dev/null; cat evidence.txt",
            )
            assert result["exit_code"] == 0 and "m2a-proof" in result["stdout"]

            evidence_uri = f"evidence/{task_id}/agent-1/proof"
            dependency_uri = f"evidence/{task_id}/agent-1/dependency"
            await objects.put(evidence_uri, b"m2a-proof\n")
            await objects.put(dependency_uri, b'{"input": 1}\n')
            data = {
                "format": "bbx.task-archive.v1",
                "task_id": str(task_id),
                "run_number": 1,
                "state": {
                    "task": {"id": str(task_id)},
                    "facts": {
                        "F1": {
                            "version": 1,
                            "evidence": [
                                {
                                    "path": "/workspace/agents/agent-1/evidence.txt",
                                    "uri": evidence_uri,
                                    "size": 10,
                                },
                                {
                                    "path": "/workspace/shared/input.json",
                                    "uri": dependency_uri,
                                    "size": 13,
                                },
                            ],
                        }
                    },
                    "intents": {},
                    "agents": {},
                },
                "events": [],
                "sessions": [],
                "messages": [],
                "task_runs": [],
            }
            archive = await manager.archive_task(task_id, data)
            assert archive.uri == f"workspace/{task_id}.tar.zst"
            assert archive.size > 0 and archive.fallback == "none"
            compressed = await objects.get(archive.uri)
            raw = zstandard.ZstdDecompressor().decompress(compressed, max_output_size=20_000_000)
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as content:
                assert "agents/agent-1/evidence.txt" in content.getnames()
                assert not any("transient.bin" in name for name in content.getnames())
                assert content.extractfile("agents/agent-1/evidence.txt").read() == b"m2a-proof\n"  # type: ignore[union-attr]
                assert content.extractfile("shared/input.json").read() == b'{"input": 1}\n'  # type: ignore[union-attr]
                manifest = json.loads(content.extractfile(".bbx/manifest.json").read())  # type: ignore[union-attr]
                restored_files = {item["path"]: item for item in manifest["restored_files"]}
                assert (
                    restored_files["agents/agent-1/evidence.txt"]["sha256"]
                    == hashlib.sha256(b"m2a-proof\n").hexdigest()
                )
                assert restored_files["shared/input.json"]["size"] == 13
                audit_uri = f"toolcalls/{task_id}/runtime-audit-run-1.txt"
                assert audit_uri in manifest["references"]
                task_snapshot = json.loads(content.extractfile(".bbx/task.json").read())  # type: ignore[union-attr]
                assert task_snapshot["runtime_audit"]["result_uri"] == audit_uri
            audit_bytes = await objects.get(audit_uri)
            audit = json.loads(audit_bytes)
            assert audit["task_id"] == str(task_id) and audit["run_number"] == 1
            assert "agent-1" in audit["sudo"] and "COMMAND=" in audit["sudo"]
            assert "sudo -n id -u" in audit["commands"]
            assert (
                task_snapshot["runtime_audit"]["sha256"] == hashlib.sha256(audit_bytes).hexdigest()
            )
            assert (await manager.find(task_id)) == handle
            await manager.destroy(task_id)
            restored = await manager.provision(task_id, profile, archive.uri)
            restored_sudo = await command(restored, "sudo -n id -u")
            assert restored_sudo["exit_code"] == 0
            assert restored_sudo["stdout"] == "<command_output>0\n</command_output>"
            async with EnvdClient(restored.base_url, restored.token) as envd:
                restored_file = await envd.read_file("/workspace/agents/agent-1/evidence.txt")
                assert restored_file == b"m2a-proof\n"
                assert await envd.read_file("/workspace/shared/input.json") == b'{"input": 1}\n'
                assert (await envd.restore_status())["restored"]
            data["run_number"] = 2
            second = await manager.archive_task(task_id, data)
            assert second.uri == f"workspace/{task_id}/run-2.tar.zst"
            assert await objects.exists(second.uri)
            assert await objects.exists(audit_uri)
            assert await objects.exists(f"toolcalls/{task_id}/runtime-audit-run-2.txt")
    finally:
        if manager is not None:
            await manager.destroy(task_id)
        await asyncio.to_thread(network.remove)
        assert not docker_client.containers.list(
            all=True, filters={"label": f"bbx.task-id={task_id}"}
        )
