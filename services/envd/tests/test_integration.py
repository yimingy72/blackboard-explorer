"""Container checks for envd's security and transport boundaries."""

import asyncio
import io
import json
import os
import tarfile
import time
import uuid
from contextlib import ExitStack

import httpx
import pytest
import zstandard
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

pytestmark = pytest.mark.integration
TOKEN = "integration-only-token"

# Publish the test client's route without giving envd an external network.
RELAY = """
import select
import socket
import socketserver

class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection(('envd', 8080), timeout=10) as upstream:
            upstream.settimeout(None)
            peers = {self.request: upstream, upstream: self.request}
            while True:
                ready, _, _ = select.select(list(peers), [], [], 30)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)

socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer.daemon_threads = True
with socketserver.ThreadingTCPServer(('0.0.0.0', 8080), Relay) as server:
    server.serve_forever()
"""


@pytest.fixture(scope="module")
def envd_url():
    os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"
    network = Network(docker_network_kw={"internal": True})
    network.name = f"bbx-m2env-{uuid.uuid4().hex[:8]}"
    with network:
        container = (
            DockerContainer("bbx-exec-env:latest")
            .with_name(f"bbx-m2env-envd-{uuid.uuid4().hex[:8]}")
            .with_network(network)
            .with_network_aliases("envd")
            .with_env("ENVD_TOKEN", TOKEN)
            .with_env("EVIDENCE_MAX_BYTES", "100000")
            .with_kwargs(
                cap_add=["NET_ADMIN"],
                devices=["/dev/net/tun:/dev/net/tun:rwm"],
                nano_cpus=2_000_000_000,
                mem_limit="4g",
                pids_limit=256,
            )
        )
        with ExitStack() as stack:
            stack.enter_context(container)
            relay = stack.enter_context(
                DockerContainer("bbx-exec-env:latest")
                .with_name(f"bbx-m2env-relay-{uuid.uuid4().hex[:8]}")
                .with_command(["python", "-c", RELAY])
                .with_exposed_ports(8080)
                .with_kwargs(cap_drop=["ALL"], security_opt=["no-new-privileges"])
            )
            network.connect(relay.get_container_id())
            url = f"http://{relay.get_container_host_ip()}:{relay.get_exposed_port(8080)}"
            with httpx.Client(
                headers={"Authorization": f"Bearer {TOKEN}"}, timeout=15, trust_env=False
            ) as client:
                for _ in range(50):
                    try:
                        if client.get(f"{url}/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.2)
                else:
                    pytest.fail("envd did not become healthy")
            yield url


async def command(url: str, agent_id: str, value: str, **kwargs):
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Agent-Id": agent_id}
    async with httpx.AsyncClient(headers=headers, timeout=30, trust_env=False) as client:
        async with streamable_http_client(f"{url}/mcp", http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                return await session.call_tool("execute_command", {"command": value, **kwargs})


def result_data(result):
    assert not result.isError, result
    assert result.structuredContent is not None
    return result.structuredContent


@pytest.mark.asyncio
async def test_envd_container(envd_url: str) -> None:
    url = envd_url
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False
    ) as client:
        assert (await client.get(f"{url}/health")).json() == {"status": "ok", "runtime_audit": True}
        async with httpx.AsyncClient(trust_env=False) as unauthenticated:
            assert (await unauthenticated.get(f"{url}/health")).status_code == 401
            assert (await unauthenticated.post(f"{url}/mcp")).status_code == 401
            assert (await unauthenticated.get(f"{url}/audit")).status_code == 401
        for agent_id in ("agent-1", "agent-2"):
            response = await client.post(f"{url}/users", json={"agent_id": agent_id})
            assert response.status_code == 200, response.text
            assert (
                await client.post(f"{url}/users", json={"agent_id": agent_id})
            ).status_code == 200
        assert (await client.post(f"{url}/users", json={"agent_id": "bad;id"})).status_code == 400

        own = result_data(await command(url, "agent-2", "echo private > own.txt; cat own.txt"))
        assert "private" in own["stdout"]
        denied = result_data(
            await command(
                url,
                "agent-1",
                "cat /workspace/agents/agent-2/own.txt; "
                "echo nope >> /workspace/agents/agent-2/own.txt",
            )
        )
        assert denied["exit_code"] != 0
        assert "private" in denied["stdout"]
        result_data(await command(url, "agent-1", "echo sticky > /workspace/shared/owned.txt"))
        sticky = result_data(await command(url, "agent-2", "rm /workspace/shared/owned.txt"))
        assert sticky["exit_code"] != 0

        timeout = result_data(
            await command(url, "agent-1", "sleep 30 & echo $! > child.pid; wait", timeout_sec=1)
        )
        assert timeout["exit_code"] == 124
        await asyncio.sleep(1)
        child = result_data(await command(url, "agent-1", "kill -0 $(cat child.pid)"))
        assert child["exit_code"] != 0
        root_timeout = result_data(
            await command(
                url,
                "agent-1",
                "sleep 30 & echo $! > root-child.pid; wait",
                timeout_sec=1,
                privileged=True,
            )
        )
        assert root_timeout["exit_code"] == 124
        await asyncio.sleep(1)
        root_child = result_data(
            await command(url, "agent-1", "sudo -n kill -0 $(cat root-child.pid)")
        )
        assert root_child["exit_code"] != 0

        large = result_data(await command(url, "agent-1", "python3 -c 'print(\"x\" * 80000)'"))
        assert large["truncated"] is True
        assert len(large["stdout"]) < 66000
        full = await client.get(f"{url}/files", params={"path": large["full_output_path"]})
        assert full.status_code == 200 and len(full.content) > 80000
        assert (
            await client.get(f"{url}/files", params={"path": "/workspace/../etc/passwd"})
        ).status_code == 400
        result_data(await command(url, "agent-1", "ln -s /etc/passwd external-link"))
        assert (
            await client.get(
                f"{url}/files", params={"path": "/workspace/agents/agent-1/external-link"}
            )
        ).status_code == 400
        assert (
            await client.get(f"{url}/stat", params={"path": "/workspace/agents/agent-1/missing"})
        ).json() == {"exists": False}
        result_data(
            await command(
                url, "agent-1", 'python3 -c \'open("too-big", "wb").write(b"x" * 100001)\''
            )
        )
        assert (
            await client.get(f"{url}/files", params={"path": "/workspace/agents/agent-1/too-big"})
        ).status_code == 413

        allowed = result_data(await command(url, "agent-1", "id -u; whoami", privileged=True))
        assert allowed["stdout"] == "<command_output>0\nroot\n</command_output>"
        assert allowed["execution"]["privileged_requested"] is True
        assert allowed["execution"]["agent_id"] == "agent-1"
        assert allowed["execution"]["command_id"]
        audit_owner = result_data(
            await command(
                url,
                "agent-1",
                "stat -c '%U %a' /var/log/bbx/commands.jsonl",
                privileged=True,
            )
        )
        assert audit_owner["stdout"] == "<command_output>root 600\n</command_output>"
        assert (
            result_data(await command(url, "agent-1", "cat /var/log/bbx/commands.jsonl"))[
                "exit_code"
            ]
            != 0
        )
        assert (
            result_data(await command(url, "agent-1", "sudo -n id -u"))["stdout"]
            == "<command_output>0\n</command_output>"
        )
        assert result_data(await command(url, "agent-1", "id -u"))["stdout"] != allowed["stdout"]
        failed = result_data(await command(url, "agent-1", "exit 17", privileged=True))
        assert failed["exit_code"] == 17
        tun = result_data(
            await command(
                url,
                "agent-1",
                "ip tuntap add dev bbx-test mode tun; ip tuntap del dev bbx-test mode tun",
                privileged=True,
            )
        )
        assert tun["exit_code"] == 0, tun
        token_env = result_data(
            await command(url, "agent-1", "printenv ENVD_TOKEN ENVD_TOKEN_SECRET")
        )
        assert token_env["exit_code"] != 0
        assert token_env["stdout"] == "<command_output></command_output>"
        privileged_token_env = result_data(
            await command(url, "agent-1", "printenv ENVD_TOKEN ENVD_TOKEN_SECRET", privileged=True)
        )
        assert privileged_token_env["exit_code"] != 0
        concurrent = await asyncio.gather(
            command(url, "agent-1", "sleep 0.1; echo one"),
            command(url, "agent-2", "sleep 0.1; echo two", privileged=True),
        )
        concurrent_ids = {result_data(item)["execution"]["command_id"] for item in concurrent}
        assert len(concurrent_ids) == 2
        snapshot = (await client.get(f"{url}/audit")).json()
        assert snapshot["format"] == "bbx.runtime-audit.v1"
        events = [json.loads(line) for line in snapshot["commands"].splitlines()]
        assert concurrent_ids <= {event["command_id"] for event in events}
        assert len({event["command_id"] for event in events if event["status"] == "started"}) >= 10
        assert {"completed", "timed_out"} <= {event["status"] for event in events}
        assert any(event["exit_code"] == 17 for event in events if event["status"] == "completed")
        assert "agent-1" in snapshot["sudo"] and "COMMAND=" in snapshot["sudo"]
        assert "EXIT=" in snapshot["sudo"]
        assert (await command(url, "agent-3", "id")).isError

        result_data(
            await command(
                url,
                "agent-1",
                "mkdir -p /workspace/shared/node_modules; "
                "echo omit > /workspace/shared/node_modules/a",
            )
        )
        archive = await client.post(f"{url}/archive", timeout=30)
        assert archive.status_code == 200
        decompressed = zstandard.ZstdDecompressor().decompress(
            archive.content, max_output_size=20_000_000
        )
        with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:") as tar:
            names = tar.getnames()
        assert any(
            "agents/agent-1/own" in name or "agents/agent-1/child.pid" in name for name in names
        )
        assert not any("node_modules" in name for name in names)


def test_proxy_denies_unlisted_domain() -> None:
    os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"
    with (
        DockerContainer("bbx-egress-proxy:latest")
        .with_name(f"bbx-m2env-proxy-{uuid.uuid4().hex[:8]}")
        .with_env("EGRESS_ALLOWLIST", "allowed.example")
        .with_exposed_ports(8888)
    ) as container:
        proxy = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8888)}"
        with httpx.Client(proxy=proxy, timeout=10, trust_env=False) as client:
            for _ in range(50):
                try:
                    assert client.get("http://forbidden.invalid/").status_code == 403
                    break
                except httpx.TransportError:
                    time.sleep(0.2)
            else:
                pytest.fail("proxy did not become ready")
