"""Isolated live packets and two-Agent memory/latency checks on the existing 4 GiB profile."""

import asyncio
import json
import os
import shlex
import time
import uuid
from pathlib import Path

import httpx
import pytest
from testcontainers.core.container import DockerContainer

from services.envd.tests.test_traffic_integration import TOKEN, command

pytestmark = pytest.mark.integration

MEMORY = """
import json
from pathlib import Path
rss={}
for status in Path('/proc').glob('[0-9]*/status'):
    try:
        fields=dict(line.split(':',1) for line in status.read_text().splitlines() if ':' in line)
        amount=int(fields.get('VmRSS','0 kB').split()[0])*1024
        name=fields.get('Name','').strip()
        rss[name]=rss.get(name,0)+amount
    except (OSError,ValueError):pass
print(json.dumps({'rss_bytes':sum(rss.values()),'rss_by_process':rss,
                  'cgroup_memory':int(Path('/sys/fs/cgroup/memory.current').read_text())}))
"""


def traffic_script(marker: str, count: int, interval: float, hold: float = 0) -> str:
    script = (
        "import socket,time; "
        "receiver=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
        "receiver.bind(('127.0.0.1',0)); receiver.settimeout(1); "
        "sender=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
        f"payload={marker.encode()!r}; "
        f'exec("for index in range({count}):\\n '
        "sender.sendto(payload,receiver.getsockname()); receiver.recv(4096); "
        f'time.sleep({interval})"); '
        f"time.sleep({hold}); sender.close(); receiver.close()"
    )
    return "python3 -c " + shlex.quote(script)


async def test_packet_visible_before_command_and_raw_seal_with_two_agent_rss():
    os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"
    task_id = str(uuid.uuid4())
    report = {"profile": {"memory": "4g", "cpus": 2}, "samples": []}
    container = (
        DockerContainer(os.environ.get("BBX_TRAFFIC_TEST_IMAGE", "bbx-traffic-envd:latest"))
        .with_name("bbx-traffic-live-" + uuid.uuid4().hex[:8])
        .with_env("ENVD_TOKEN", TOKEN)
        .with_env("ENVD_TASK_ID", task_id)
        .with_env("ENVD_TRAFFIC_ENABLED", "true")
        .with_exposed_ports(8080)
        .with_kwargs(
            cap_add=["NET_ADMIN", "SYS_ADMIN"],
            sysctls={"net.ipv4.ip_forward": "1", "net.ipv6.conf.all.forwarding": "1"},
            devices=["/dev/net/tun:/dev/net/tun:rwm"],
            mem_limit="4g",
            nano_cpus=2_000_000_000,
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
        container.with_env(key, "")
    with container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        async with httpx.AsyncClient(
            base_url=url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30, trust_env=False
        ) as client:
            for _ in range(100):
                try:
                    if (await client.get("/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
            for agent in ("agent-1", "agent-2"):
                response = await client.post("/users", json={"agent_id": agent})
                assert response.status_code == 200, response.text
            started = (
                await client.post("/traffic/start", json={"task_id": task_id, "run_number": 1})
            ).json()
            assert started["https"] == "enabled", started

            async def memory(label):
                code, output = await asyncio.to_thread(container.exec, ["python3", "-c", MEMORY])
                assert code == 0, output
                assert isinstance(output, bytes)
                sample = json.loads(output)
                sample["phase"] = label
                capture_status = (await client.get("/traffic/status")).json()
                sample["preview_dropped"] = capture_status["preview_dropped"]
                sample["record_count"] = capture_status["record_count"]
                report["samples"].append(sample)
                assert sample["cgroup_memory"] < 4 * 1024**3

            await memory("ready")
            begin = time.monotonic()
            held = asyncio.create_task(
                command(url, "agent-1", traffic_script("isolated-live-packet", 1, 0, 3))
            )
            packet = None
            listing = {}
            for _ in range(60):
                listing = (
                    await client.get(
                        "/traffic/records", params={"agent_id": "agent-1", "limit": 1000}
                    )
                ).json()
                packet = next(
                    (
                        item
                        for item in listing["items"]
                        if item.get("record_kind") == "packet" and item["protocol"] == "udp"
                    ),
                    None,
                )
                if packet is not None:
                    break
                await asyncio.sleep(0.03)
            assert packet is not None, listing
            report["isolated_packet_latency_ms"] = round((time.monotonic() - begin) * 1000, 2)
            assert not held.done()
            detail = (await client.get(f"/traffic/records/{packet['id']}")).json()
            assert detail["packet"]["direction"] == "loopback"
            assert detail["packet"]["interface"] == "lo"
            assert b"isolated-live-packet" in bytes.fromhex(detail["packet_detail"]["raw_hex"])
            assert {"frame", "ip", "udp"} <= {
                node["name"] for node in detail["packet_detail"]["layers"]
            }
            for _ in range(30):
                chunks = (await client.get("/traffic/chunks")).json()["items"]
                if any(item["first_seq"] <= packet["seq"] <= item["last_seq"] for item in chunks):
                    break
                await asyncio.sleep(0.03)
            else:
                pytest.fail("Packet metadata was not independently sealed within one second")
            files = (await client.get("/traffic/artifacts")).json()["items"]
            assert not any(item["agent_id"] == "agent-1" for item in files)
            native = (await client.get("/traffic/native")).json()
            source = next(item for item in native["sources"] if item["agent_id"] == "agent-1")
            assert source["packet_count"] >= packet["packet"]["number"]
            assert not source["complete"] and source["parts"]
            assert not held.done()
            assert (await held)["exit_code"] == 0
            assert report["isolated_packet_latency_ms"] < 1800

            normal = await asyncio.gather(
                *[
                    command(url, agent, traffic_script("normal-" + agent, 100, 0.01))
                    for agent in ("agent-1", "agent-2")
                ]
            )
            assert all(item["exit_code"] == 0 for item in normal)
            await asyncio.sleep(0.5)
            await memory("normal_200_packets")
            assert report["samples"][-1]["preview_dropped"] == 0
            for round_number in (1, 2):
                before = (await client.get("/traffic/status")).json()["record_count"]
                begin = time.monotonic()
                sending = asyncio.gather(
                    *[
                        command(
                            url, agent, traffic_script(f"burst-{round_number}-" + agent, 1500, 0)
                        )
                        for agent in ("agent-1", "agent-2")
                    ]
                )
                while not sending.done():
                    status = (await client.get("/traffic/status")).json()
                    if status["record_count"] > before:
                        report[f"burst_{round_number}_first_preview_ms"] = round(
                            (time.monotonic() - begin) * 1000, 2
                        )
                        break
                    await asyncio.sleep(0.02)
                command_started = time.monotonic()
                ping = await command(url, "agent-1", "true")
                report[f"burst_{round_number}_command_latency_ms"] = round(
                    (time.monotonic() - command_started) * 1000, 2
                )
                assert ping["exit_code"] == 0
                results = await sending
                assert all(item["exit_code"] == 0 for item in results)
                await asyncio.sleep(1)
                await memory(f"burst_{round_number}_3000_packets")
            await asyncio.sleep(10)
            await memory("after_pcap_rotation")
            stopped = (await client.post("/traffic/stop", json={})).json()
            report["capture"] = {
                key: stopped.get(key)
                for key in (
                    "status",
                    "record_count",
                    "preview_dropped",
                    "dropped_packets",
                    "captured_bytes",
                    "pending_chunks",
                    "last_packet_at",
                    "error",
                )
            }
            assert stopped["record_count"] > 200
            assert stopped["preview_dropped"] >= 0 and stopped["dropped_packets"] >= 0
            directory = Path(".data/traffic")
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "live-packet-metrics.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report, separators=(",", ":")))
