"""Synthetic envd -> MinIO -> archived traffic API, without models or external targets."""

from __future__ import annotations

import asyncio
import hashlib
import shlex
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import docker
import httpx
import pytest
from bbx_blackboard import api, native_worker
from bbx_blackboard.native import NativeAnalysisCache, NativeWorker
from bbx_blackboard.settings import Settings as BlackboardSettings
from bbx_contracts.traffic import TrafficManifest, TrafficNativeSourceIndex, TrafficRecordDetail
from bbx_objects import ObjectStore
from bbx_runtime.clients import EnvdClient
from bbx_runtime.traffic.collector import TrafficCollector
from bbx_runtime.traffic.native import NativeArchive
from docker.errors import ImageNotFound, NotFound
from fastapi import HTTPException
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr
from testcontainers.core.container import DockerContainer

from services.envd.tests.test_traffic_integration import RAW, SERVER, TOKEN, command

pytestmark = pytest.mark.integration
ENVD_IMAGE = "bbx-traffic-envd:latest"
MINIO_IMAGE = "pgsty/minio:RELEASE.2026-04-17T00-00-00Z"
SERVICE_TOKEN = "traffic-archive-service-token"
MINIO_USER = "trafficarchive"
MINIO_PASSWORD = "traffic-archive-local-password"


async def healthy(url: str, headers: dict[str, str] | None = None) -> None:
    async with httpx.AsyncClient(trust_env=False, timeout=2, headers=headers) as client:
        for _ in range(150):
            try:
                if (await client.get(url)).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.2)
    raise AssertionError(f"Synthetic test service did not become healthy: {url}")


async def published(
    objects: ObjectStore, task_id: UUID, run: int, predicate: Callable[[TrafficManifest], bool]
) -> TrafficManifest:
    key = f"traffic/{task_id}/run-{run}/manifest.json"
    for _ in range(100):
        if await objects.exists(key):
            manifest = TrafficManifest.model_validate_json(await objects.get(key))
            if predicate(manifest):
                return manifest
        await asyncio.sleep(0.1)
    raise AssertionError(f"Traffic capture did not publish the expected run-{run} checkpoint")


async def setup_target(container: DockerContainer) -> None:
    certificate = (
        "openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost "
        "-addext subjectAltName=IP:127.0.0.1,IP:198.18.0.1,DNS:localhost "
        "-keyout /tmp/synthetic-key.pem "
        "-out /usr/local/share/ca-certificates/synthetic-target.crt >/dev/null 2>&1 "
        "&& cp /usr/local/share/ca-certificates/synthetic-target.crt /tmp/untrusted.crt "
        "&& update-ca-certificates >/dev/null 2>&1"
    )
    code, output = await asyncio.to_thread(container.exec, ["bash", "-c", certificate])
    assert code == 0, output
    launcher = (
        "import subprocess; subprocess.Popen(['python3','-c',"
        + repr(SERVER.replace("time.sleep(2)", "time.sleep(12)"))
        + "],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)"
    )
    code, output = await asyncio.to_thread(container.exec, ["python3", "-c", launcher])
    assert code == 0, output
    for _ in range(50):
        code, _ = await asyncio.to_thread(
            container.exec,
            [
                "python3",
                "-c",
                "import socket; socket.create_connection(('127.0.0.1',24441),1).close()",
            ],
        )
        if code == 0:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("Synthetic gateway target did not start")


async def captured_command(
    url: str,
    linux_agent: str,
    text: str,
    *,
    identity: dict[str, Any] | None = None,
    privileged: bool = False,
) -> dict[str, Any]:
    if identity is None:
        return await command(url, linux_agent, text, privileged=privileged)
    command_id = str(uuid4())
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {TOKEN}", "X-Agent-Id": linux_agent},
        timeout=30,
        trust_env=False,
    ) as http:
        async with streamable_http_client(f"{url}/mcp", http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool(
                    "execute_command",
                    {"command": text, "privileged": privileged},
                    meta={"ctf": {**identity, "command_id": command_id}},
                )
                assert not result.isError and result.structuredContent is not None, result
                assert result.structuredContent["execution"]["command_id"] == command_id
                return result.structuredContent


async def agent_traffic(
    url: str,
    linux_agent: str,
    canonical: str,
    run: int,
    *,
    include_raw: bool,
    identity: dict[str, Any] | None = None,
    long_stream: bool = False,
) -> set[str]:
    command_ids = set()
    path = f"/round-{run}/{canonical}?token=synthetic-private-query"
    for scheme, port in (("http", 24441), ("https", 24442)):
        result = await captured_command(
            url,
            linux_agent,
            f"sudo -n curl --noproxy '' -fsS {scheme}://198.18.0.1:{port}{shlex.quote(path)}",
            identity=identity,
        )
        assert result["exit_code"] == 0, result
        assert "first-synthetic-chunk" in result["stdout"]
        command_ids.add(result["execution"]["command_id"])
    if long_stream:
        result = await captured_command(
            url,
            linux_agent,
            "curl --noproxy '' -fsS "
            f"https://198.18.0.1:24442/stream/round-{run}/{canonical} >/dev/null",
            identity=identity,
        )
        assert result["exit_code"] == 0, result
        command_ids.add(result["execution"]["command_id"])
    if include_raw:
        owned_raw = RAW.replace("synthetic-", f"bbx-archive-{canonical}-")
        result = await captured_command(
            url,
            linux_agent,
            "python3 -c " + shlex.quote(owned_raw),
            identity=identity,
            privileged=True,
        )
        assert result["exit_code"] == 0, result
        assert result["execution"]["network"]["agent_id"] == canonical
        command_ids.add(result["execution"]["command_id"])
    return command_ids


def traffic_app(
    monkeypatch: pytest.MonkeyPatch,
    objects: ObjectStore,
    task_id: UUID,
    mode: str,
    run: int,
):
    settings = BlackboardSettings.model_construct(
        postgres_password=SecretStr("unused-test-postgres"),
        minio_root_password=SecretStr(MINIO_PASSWORD),
        service_token=SecretStr(SERVICE_TOKEN),
        agent_token_secret=SecretStr("traffic-archive-signing-key-at-least-32-bytes"),
        admin_users=SecretStr("local-test:unused-password"),
    )
    app = api.create_app(settings, objects=objects)

    async def task(_request: Any, requested: UUID) -> dict[str, Any]:
        if requested != task_id:
            raise HTTPException(404, "Task not found")
        return {
            "id": task_id,
            "mode": mode,
            "status": "running" if run == 1 else "finished",
            "run_number": run,
        }

    monkeypatch.setattr(api, "_task", task)
    return app


class DockerNativeWorkers:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.cache = NativeAnalysisCache(worker_factory=self.start)

    async def start(self, home: Path) -> NativeWorker:
        docker_binary = shutil.which("docker")
        assert docker_binary is not None
        name = "bbx-traffic-native-analysis-" + uuid4().hex[:8]
        self.names.append(name)
        owner = home.stat()
        process = await asyncio.create_subprocess_exec(
            docker_binary,
            "run",
            "-i",
            "--pull=never",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{owner.st_uid}:{owner.st_gid}",
            "--pids-limit",
            "64",
            "--memory",
            "1g",
            "--cpus",
            "1",
            "-v",
            f"{home}:{home}:rw",
            "-v",
            f"{Path(native_worker.__file__).resolve()}:/native_worker.py:ro",
            "-w",
            str(home),
            "-e",
            f"HOME={home}",
            "-e",
            f"XDG_CONFIG_HOME={home}",
            "-e",
            f"WIRESHARK_CONFIG_DIR={home}",
            "-e",
            "LC_ALL=C",
            "-e",
            "TZ=UTC",
            "--entrypoint",
            "/opt/envd/venv/bin/python",
            ENVD_IMAGE,
            "/native_worker.py",
            "/usr/bin/sharkd",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024**2,
            start_new_session=True,
        )
        return await NativeWorker.attach(process)

    async def close(self) -> None:
        await self.cache.close()
        client = docker.from_env()
        try:
            for name in self.names:
                try:
                    container = await asyncio.to_thread(client.containers.get, name)
                except NotFound:
                    continue
                await asyncio.to_thread(container.remove, force=True, v=True)
        finally:
            client.close()


async def native_query(
    client: httpx.AsyncClient,
    path: str,
    agent: str,
    expression: str,
    run: int = 1,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    response = await client.post(
        path + "/native/queries",
        json={
            "run": run,
            "agent_id": agent,
            "display_filter": expression,
        },
    )
    assert response.status_code == 202, response.text
    query_id = response.json()["id"]
    async with asyncio.timeout(20):
        while True:
            response = await client.get(path + f"/native/queries/{query_id}")
            assert response.status_code == 200, response.text
            state = response.json()
            assert state["status"] != "error", state
            if state["revision"] is not None and state["matched_packets"]:
                page = await client.get(
                    path + f"/native/queries/{query_id}/packets",
                    params={"revision": state["revision"], "limit": 200},
                )
                if page.status_code == 409:
                    continue
                assert page.status_code == 200, page.text
                rows = page.json()["records"]
                assert rows and all(row["agent_id"] == agent for row in rows)
                row = rows[0]
                detail = await client.get(
                    path
                    + f"/native/queries/{query_id}/sources/{row['source_id']}"
                    + f"/frames/{row['source_number']}",
                    params={"revision": state["revision"]},
                )
                if detail.status_code == 409:
                    continue
                assert detail.status_code == 200, detail.text
                return state, detail.json(), rows
            await asyncio.sleep(0.1)


async def live_packet_preview(
    monkeypatch: pytest.MonkeyPatch,
    objects: ObjectStore,
    task_id: UUID,
    mode: str,
    control: EnvdClient,
    url: str,
    linux_agent: str,
    canonical: str,
    identity: dict[str, Any] | None,
) -> None:
    code = (
        "import socket,time; "
        "receiver=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
        "receiver.bind(('127.0.0.1',42424)); "
        "sender=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
        "[(sender.sendto(b'live-packet-preview',('127.0.0.1',42424)), "
        "receiver.recv(128), time.sleep(0.1)) for _ in range(100)]"
    )
    running = asyncio.create_task(
        captured_command(
            url,
            linux_agent,
            "python3 -c " + shlex.quote(code),
            identity=identity,
        )
    )
    app = traffic_app(monkeypatch, objects, task_id, mode, 1)
    workers = DockerNativeWorkers()
    app.state.native_cache = workers.cache
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://live-test",
            trust_env=False,
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        ) as client:
            path = f"/api/tasks/{task_id}/traffic"
            async with asyncio.timeout(4):
                after = 0
                while True:
                    response = await client.get(
                        path,
                        params={
                            "run": 1,
                            "kind": "packet",
                            "agent_id": canonical,
                            "protocol": "udp",
                            "q": "42424",
                            "after": after,
                            "limit": 200,
                        },
                    )
                    assert response.status_code == 200, response.text
                    data = response.json()
                    after = data["next_after"]
                    if data["records"]:
                        row = data["records"][0]
                        assert not running.done(), "Packet preview waited for command completion"
                        sealed = (await control.traffic("artifacts"))["items"]
                        assert not any(item.get("agent_id") == canonical for item in sealed), (
                            "Packet preview waited for the ten-second raw fragment to close"
                        )
                        detail = await client.get(path + "/records/" + row["id"], params={"run": 1})
                        assert detail.status_code == 200, detail.text
                        decoded = TrafficRecordDetail.model_validate(detail.json())
                        assert decoded.packet_detail is not None and decoded.packet_detail.layers
                        assert decoded.packet_detail.raw_hex
                        assert decoded.packet is not None and decoded.packet.number >= 1
                        break
                    await asyncio.sleep(0.1)
            await published(
                objects,
                task_id,
                1,
                lambda item: any(
                    ref.agent_id == canonical and ref.packet_count for ref in item.native
                ),
            )
            _, detail, _ = await native_query(
                client,
                path,
                canonical,
                "udp.dstport == 42424 && frame.len > 0",
            )
            assert not running.done(), "Native filtering waited for the live command to finish"
            assert detail["layers"] and detail["data_sources"]
            assert any(
                b"live-packet-preview" in bytes.fromhex(item["raw_hex"])
                for item in detail["data_sources"]
            )
        result = await running
        assert result["exit_code"] == 0, result
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await workers.close()


async def restart_native_uploader(capture: TrafficCollector) -> dict[str, TrafficNativeSourceIndex]:
    async with capture.native_lock:
        previous = capture.native_archive
        assert previous is not None and previous.refs
        restored = NativeArchive(capture.envd, capture.objects, capture.task_id, capture.run)
        await restored.restore(list(previous.refs.values()))
        assert restored.refs == previous.refs and restored.indices == previous.indices
        capture.native_archive = restored
        return dict(restored.indices)


async def archived_api(
    monkeypatch: pytest.MonkeyPatch,
    objects: ObjectStore,
    task_id: UUID,
    manifests: dict[int, TrafficManifest],
    mode: str,
    owners: tuple[str, ...],
    command_ids: dict[str, set[str]],
) -> None:
    app = traffic_app(monkeypatch, objects, task_id, mode, 2)
    workers = DockerNativeWorkers()
    app.state.native_cache = workers.cache
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    path = f"/api/tasks/{task_id}/traffic"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://archived-test",
            trust_env=False,
            headers=headers,
        ) as client:
            assert (await client.get(path, headers={"Authorization": ""})).status_code == 401
            assert (await client.get(f"/api/tasks/{uuid4()}/traffic")).status_code == 404
            assert (await client.get(path, params={"run": 3})).status_code == 404
            for run in (1, 2):
                listing = await client.get(path, params={"run": run, "limit": 200})
                assert listing.status_code == 200, listing.text
                body = listing.json()
                assert body["availability"] == "recorded"
                assert body["capture"]["finished_at"] is not None
                assert body["capture"]["record_count"] == manifests[run].record_count
                assert len(body["files"]) == len(manifests[run].files)
            for agent in owners:
                for protocol in ("http", "https", "tcp", "udp", "icmp"):
                    response = await client.get(
                        path,
                        params={"run": 1, "agent_id": agent, "protocol": protocol, "limit": 200},
                    )
                    assert response.status_code == 200, response.text
                    records = response.json()["records"]
                    assert records, (agent, protocol)
                    assert all(
                        item["agent_id"] == agent and item["attribution"] == "exact"
                        for item in records
                    )
                decoded = await client.get(
                    path,
                    params={
                        "run": 1,
                        "agent_id": agent,
                        "protocol": "https",
                        "kind": "http",
                        "q": "/round-1/",
                    },
                )
                row = next(
                    item for item in decoded.json()["records"] if item["plaintext"] == "decoded"
                )
                response = await client.get(path + "/records/" + row["id"], params={"run": 1})
                assert response.status_code == 200, response.text
                detail = TrafficRecordDetail.model_validate(response.json())
                assert detail.agent_id == agent and detail.observation is not None
                assert detail.observation.scope == "wire"
                assert detail.observation.attributes["command_id"] in command_ids[agent]
                assert (
                    detail.response is not None
                    and "first-synthetic-chunk" in detail.response.body_preview
                )
                assert "synthetic-private-query" not in response.text
                assert "synthetic-cookie=private" not in response.text
                owned = await client.get(path, params={"run": 1, "agent_id": agent})
                files = owned.json()["files"]
                assert files and all(
                    item["agent_id"] == agent and item["source_id"] for item in files
                )
                packets = bytearray()
                for item in files:
                    async with client.stream(
                        "GET", path + "/files/" + item["id"], params={"run": 1}
                    ) as download:
                        assert download.status_code == 200
                        content = b"".join([part async for part in download.aiter_bytes()])
                        assert len(content) == item["size_bytes"]
                        assert content[:4] == b"\x0a\x0d\x0d\x0a"
                        stored = await objects.get(f"traffic/{task_id}/run-1/pcap/{item['id']}")
                        assert hashlib.sha256(content).digest() == hashlib.sha256(stored).digest()
                        assert download.headers["cache-control"] == "no-store"
                        packets.extend(content)
                assert f"bbx-archive-{agent}-".encode() in packets
                other = next(owner for owner in owners if owner != agent)
                assert f"bbx-archive-{other}-".encode() not in packets
            current = (
                await client.get(
                    path, params={"q": "/round-2/", "protocol": "https", "kind": "http"}
                )
            ).json()
            assert current["run"] == 2 and current["records"]
            assert all("/round-2/" in item["url"] for item in current["records"])
            previous = (
                await client.get(path, params={"run": 1, "q": "/round-2/", "kind": "http"})
            ).json()
            assert previous["records"] == []

            for owner in owners:
                state, detail, _ = await native_query(
                    client, path, owner, "http.response.code == 200"
                )
                assert state["engine_version"] and detail["layers"] and detail["data_sources"]
                assert all(item["decryption"] == "available" for item in state["coverage"])
            _, detail, _ = await native_query(
                client,
                path,
                owners[0],
                "http.response.code == 200 && http.content_length >= 100000",
            )
            assert any(item["length"] >= 100000 for item in detail["data_sources"]), detail
            assert any(item["id"] > 0 for item in detail["data_sources"])
            assert (
                len(
                    {
                        part.segment_id
                        for part in TrafficNativeSourceIndex.model_validate_json(
                            await objects.get(
                                f"traffic/{task_id}/run-1/native/{detail['source_id']}/indexes/"
                                + next(
                                    ref.revision
                                    for ref in manifests[1].native
                                    if ref.source_id == detail["source_id"]
                                )
                                + ".json"
                            )
                        ).parts
                    }
                )
                >= 3
            )
            await native_query(client, path, owners[0], "http.response.code == 200", run=2)
            invalid = await client.post(
                path + "/native/check", json={"display_filter": "http && ("}
            )
            assert invalid.status_code == 422
    finally:
        await workers.close()


@pytest.mark.parametrize("mode", ["blackboard", "ctf"])
async def test_envd_fragments_survive_container_destruction_and_read_through_traffic_api(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")
    docker_client = docker.from_env()
    try:
        for image in (ENVD_IMAGE, MINIO_IMAGE):
            try:
                docker_client.images.get(image)
            except ImageNotFound:
                pytest.skip(f"Required prebuilt integration image is missing: {image}")
        suffix, task_id = uuid4().hex[:8], uuid4()
        members = (
            {"agent-1": "lead", "agent-2": "member-1"}
            if mode == "ctf"
            else {"agent-1": "agent-1", "agent-2": "agent-2"}
        )
        envd = (
            DockerContainer(ENVD_IMAGE)
            .with_name(f"bbx-traffic-archive-envd-{suffix}")
            .with_env("ENVD_TOKEN", TOKEN)
            .with_env("ENVD_MODE", mode)
            .with_env("ENVD_TASK_ID", str(task_id))
            .with_env("ENVD_TRAFFIC_ENABLED", "true")
            .with_exposed_ports(8080)
            .with_kwargs(
                cap_add=["NET_ADMIN", "SYS_ADMIN"],
                sysctls={"net.ipv4.ip_forward": "1", "net.ipv6.conf.all.forwarding": "1"},
                devices=["/dev/net/tun:/dev/net/tun:rwm"],
                mem_limit="2g",
                pids_limit=256,
            )
        )
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ):
            envd.with_env(key, "")
        minio = (
            DockerContainer(MINIO_IMAGE)
            .with_name(f"bbx-traffic-archive-minio-{suffix}")
            .with_exposed_ports(9000)
            .with_env("MINIO_ROOT_USER", MINIO_USER)
            .with_env("MINIO_ROOT_PASSWORD", MINIO_PASSWORD)
            .with_command("server /data --console-address :9001")
        )
        captures: list[TrafficCollector] = []
        destroyed = False
        with minio:
            endpoint = f"{minio.get_container_host_ip()}:{minio.get_exposed_port(9000)}"
            await healthy(f"http://{endpoint}/minio/health/live")
            objects = ObjectStore(endpoint, MINIO_USER, MINIO_PASSWORD, f"traffic-archive-{suffix}")
            await objects.ensure_bucket()
            try:
                await asyncio.to_thread(envd.start)
                container_id = envd.get_container_id()
                url = f"http://{envd.get_container_host_ip()}:{envd.get_exposed_port(8080)}"
                await healthy(url + "/health", {"Authorization": f"Bearer {TOKEN}"})
                await setup_target(envd)
                manifests = {}
                round_one_commands: dict[str, set[str]] = {}
                for run in (1, 2):
                    control = EnvdClient(url, TOKEN)
                    capture = TrafficCollector(control, objects, str(task_id), run, poll_seconds=1)
                    captures.append(capture)
                    await capture.start()
                    initial = await published(
                        objects, task_id, run, lambda item: item.finished_at is None
                    )
                    assert initial.status == "collecting"
                    identities: dict[str, dict[str, Any]] = {}
                    recovered: dict[str, TrafficNativeSourceIndex] = {}
                    for linux_agent, canonical in members.items():
                        await control.create_user(linux_agent)
                        if mode == "ctf":
                            status = await control.ctf_status()
                            identity = {
                                "task_id": str(task_id),
                                "boot_id": status["boot_id"],
                                "agent_id": linux_agent,
                                "member_id": canonical,
                                "generation": run,
                            }
                            registered = await control.ctf_operation("register", identity)
                            assert registered["member_id"] == canonical
                            assert registered["generation"] == run
                            identities[linux_agent] = identity
                        if run == 1 and linux_agent == next(iter(members)):
                            await live_packet_preview(
                                monkeypatch,
                                objects,
                                task_id,
                                mode,
                                control,
                                url,
                                linux_agent,
                                canonical,
                                identities.get(linux_agent),
                            )
                            recovered = await restart_native_uploader(capture)
                    agents = tuple(members) if run == 1 else (next(iter(members)),)
                    results = await asyncio.gather(
                        *[
                            agent_traffic(
                                url,
                                linux_agent,
                                members[linux_agent],
                                run,
                                include_raw=run == 1,
                                identity=identities.get(linux_agent),
                                long_stream=run == 1 and linux_agent == next(iter(members)),
                            )
                            for linux_agent in agents
                        ]
                    )
                    if run == 1:
                        round_one_commands.update(
                            {
                                members[agent]: commands
                                for agent, commands in zip(agents, results, strict=True)
                            }
                        )
                    for identity in identities.values():
                        drained = await control.ctf_operation("stop", identity)
                        assert drained["drained"] is True and drained["generation"] == run
                    await published(objects, task_id, run, lambda item: item.record_count > 0)
                    await capture.close()
                    captures.remove(capture)
                    manifest = await published(
                        objects, task_id, run, lambda item: item.finished_at is not None
                    )
                    assert manifest.status == "complete", manifest.error
                    assert manifest.https == "enabled" and manifest.dropped_packets == 0
                    assert manifest.captured_bytes > 0 and manifest.files
                    for source_id, previous_index in recovered.items():
                        ref = next(item for item in manifest.native if item.source_id == source_id)
                        final_index = TrafficNativeSourceIndex.model_validate_json(
                            await objects.get(
                                capture.prefix + f"native/{source_id}/indexes/{ref.revision}.json"
                            )
                        )
                        assert final_index.packet_count >= previous_index.packet_count
                        assert (
                            final_index.parts[: len(previous_index.parts)] == previous_index.parts
                        )
                    manifests[run] = manifest
                    async with EnvdClient(url, TOKEN) as check:
                        assert (await check.traffic("chunks"))["items"] == []
                        assert (await check.traffic("artifacts"))["items"] == []
                assert {item.agent_id for item in manifests[1].files} >= set(members.values())
                await asyncio.to_thread(envd.stop)
                destroyed = True
                with pytest.raises(NotFound):
                    await asyncio.to_thread(docker_client.containers.get, container_id)
                await archived_api(
                    monkeypatch,
                    objects,
                    task_id,
                    manifests,
                    mode,
                    tuple(members.values()),
                    round_one_commands,
                )
            finally:
                for capture in captures:
                    await capture.close(discard=True)
                if not destroyed:
                    await asyncio.to_thread(envd.stop)
    finally:
        docker_client.close()
