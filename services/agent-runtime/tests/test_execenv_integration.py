"""Real envd lifecycle across direct and proxy Docker networks."""

from __future__ import annotations

import asyncio
import io
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
from bbx_runtime.execenv import ExecEnvManager
from bbx_runtime.settings import Settings
from docker.errors import ImageNotFound
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr
from testcontainers.core.container import DockerContainer

pytestmark = pytest.mark.integration


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
            assert "KILL" in envd_container.attrs["HostConfig"]["CapAdd"]
            assert envd_container.attrs["HostConfig"]["CapDrop"] == ["ALL"]
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
                        result = await session.call_tool(
                            "execute_command",
                            {"command": "echo m2a-proof > evidence.txt; cat evidence.txt"},
                        )
                        assert not result.isError
                        assert "m2a-proof" in str(result.structuredContent)

            archive = await manager.archive_to_store(handle)
            assert archive.uri == f"workspace/{task_id}.tar.zst"
            assert archive.size > 0 and archive.fallback == "none"
            compressed = await objects.get(archive.uri)
            raw = zstandard.ZstdDecompressor().decompress(compressed, max_output_size=20_000_000)
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as content:
                assert any(
                    name.endswith("agents/agent-1/evidence.txt") for name in content.getnames()
                )
            assert (await manager.find(task_id)) == handle
            await manager.destroy(task_id)
            restored = await manager.provision(task_id, profile, archive.uri)
            async with EnvdClient(restored.base_url, restored.token) as envd:
                restored_file = await envd.read_file("/workspace/agents/agent-1/evidence.txt")
                assert restored_file == b"m2a-proof\n"
                assert (await envd.restore_status())["restored"]
            second = await manager.archive_to_store(restored, 2)
            assert second.uri == f"workspace/{task_id}/run-2.tar.zst"
            assert await objects.exists(second.uri)
    finally:
        if manager is not None:
            await manager.destroy(task_id)
        await asyncio.to_thread(network.remove)
        assert not docker_client.containers.list(
            all=True, filters={"label": f"bbx.task-id={task_id}"}
        )
