"""Actual Lead/Worker/helper IPv4 traffic, archived seat reuse, and read-only query."""

import argparse
import asyncio
import hashlib
import json
import os
import runpy
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from uuid import uuid4

from common import ROOT, STATE, kubectl, owned_cluster

shared = runpy.run_path(str(ROOT / "deploy/kubernetes/p4g/probe.py"))
tool_results = shared["tool_results"]
PORT = 18888
MINIO = "pgsty/minio@sha256:83885c27b3b5b673049e33ddf4029afe2c134fd51ce4309e65e4f39d3b9ca282"


def target(revised, prefix="p4i"):
    owned_cluster()
    suffix = "-fixed" if revised else ""
    target_namespace = f"bbx-{prefix}-target{suffix}"
    target_name = f"bbx-{prefix}-traffic-target{suffix}"
    owner = f"{prefix}-traffic{suffix}"
    name = "traffic-target-revised.json" if revised else "traffic-target.json"
    namespace = kubectl("get", "namespace", target_namespace, "--ignore-not-found", "-o", "json")
    if not namespace.strip():
        kubectl(
            "create",
            "-f",
            "-",
            data={
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": target_namespace, "labels": {"bbx.dev/owner": owner}},
            },
        )
        namespace = kubectl("get", "namespace", target_namespace, "-o", "json")
    assert json.loads(namespace)["metadata"]["labels"]["bbx.dev/owner"] == owner
    image = json.loads((STATE / "build-source.json").read_text())["images"][
        f"bbx-{prefix}-sandbox:20261107"
    ]["reference"]
    code = (
        "import socketserver,threading\n"
        "class Echo(socketserver.BaseRequestHandler):\n"
        " def handle(self): self.request.sendall(self.request.recv(128))\n"
        "for p in (18888,18889):\n"
        " s=socketserver.ThreadingTCPServer(('0.0.0.0',p),Echo)\n"
        " threading.Thread(target=s.serve_forever,daemon=True).start()\n"
        "threading.Event().wait()\n"
    )
    try:
        existing = json.loads(
            kubectl("-n", target_namespace, "get", "pod", target_name, "-o", "json")
        )
    except RuntimeError:
        existing = None
    if existing:
        expected = json.loads((STATE / name).read_text())
        assert existing["metadata"]["uid"] == expected["pod_uid"]
        return expected
    kubectl(
        "create",
        "-f",
        "-",
        data={
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": target_name,
                "namespace": target_namespace,
                "labels": {"bbx.dev/traffic-target": "allowed", "bbx.dev/owner": owner},
            },
            "spec": {
                "automountServiceAccountToken": False,
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "echo",
                        "image": image,
                        "imagePullPolicy": "Never",
                        "command": ["/usr/bin/python3", "-c", code],
                        "readinessProbe": {"tcpSocket": {"port": PORT}},
                    }
                ],
            },
        },
    )
    kubectl(
        "-n",
        target_namespace,
        "wait",
        "--for=condition=Ready",
        f"pod/{target_name}",
        "--timeout=70s",
    )
    actual = json.loads(kubectl("-n", target_namespace, "get", "pod", target_name, "-o", "json"))
    baseline = kubectl(
        "-n",
        target_namespace,
        "exec",
        target_name,
        "--",
        "/usr/bin/python3",
        "-c",
        "import socket; s=socket.create_connection(('127.0.0.1',18889),2);"
        "s.sendall(b'baseline'); assert s.recv(8)==b'baseline'; print('reachable')",
    )
    assert baseline.strip() == "reachable"
    result = {
        "pod_uid": actual["metadata"]["uid"],
        "namespace": target_namespace,
        "ipv4": actual["status"]["podIP"],
        "forbidden_listener_baseline": True,
    }
    (STATE / name).write_text(json.dumps(result, indent=2) + "\n")
    return result


class TrafficProbe(shared["HelperProbe"]):
    prefix = "p4i"
    state = STATE
    launch_options = {"helper_foreground": True, "traffic_profile": "binding-pcap-v1"}

    def __init__(self, destination, revised=False):
        super().__init__(scoped=True)
        self.revised = revised
        identity = "fixed" if revised else "unique"
        self.gateway_name = f"bbx-{self.prefix}-gateway-{identity}"
        self.gateway_owner = f"{self.prefix}-{identity}"
        self.result_directory = "joint-fixed" if revised else "joint"
        self.build_source_name = "build-source-revised.json" if revised else "build-source.json"
        self.task_minutes = 15
        self.destination = destination
        images = json.loads((STATE / self.build_source_name).read_text())["images"]
        variant = "-fixed:20261108" if revised else ":20261107"
        capture = f"bbx-{self.prefix}-capture-sandbox{variant}"
        host = f"bbx-{self.prefix}-sandbox-host{variant}"
        self.resource_options = {
            "journal_writer_lock": True,
            "sandbox_host_image": images[host]["reference"],
            "traffic_sandbox_image": images[capture]["reference"],
            "traffic_target_namespace": destination["namespace"],
            "traffic_targets": [{"ipv4": destination["ipv4"], "port": PORT}],
        }
        self.change = asyncio.Event()
        self.challenge = None
        self.updated = False
        self.runner_ipv4 = None

    async def objects(self):
        from bbx_objects import ObjectStore
        from urllib3.exceptions import HTTPError

        suffix = getattr(self, "object_store_suffix", "-fixed" if self.revised else "")
        container_name = f"bbx-{self.prefix}-traffic-objects{suffix}"
        owner = f"{self.prefix}-traffic{suffix}"
        path = STATE / f"traffic-objects{suffix}-private.json"
        if path.exists():
            state = json.loads(path.read_text())
        else:
            user, password = "p4i-" + token_hex(8), token_hex(24)
            data = STATE / f"traffic-object-store{suffix}"
            data.mkdir(mode=0o700)
            with (STATE / f"traffic-minio{suffix}.log").open("w") as log:
                run = subprocess.run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        container_name,
                        "--label",
                        "bbx.dev/owner=" + owner,
                        "-e",
                        "MINIO_ROOT_USER",
                        "-e",
                        "MINIO_ROOT_PASSWORD",
                        "-p",
                        "127.0.0.1::9000",
                        "-v",
                        f"{data}:/data",
                        MINIO,
                        "server",
                        "/data",
                    ],
                    env={**os.environ, "MINIO_ROOT_USER": user, "MINIO_ROOT_PASSWORD": password},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    check=True,
                )
            state = {"id": run.stdout.strip(), "user": user, "password": password}
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(state, output)
        actual = subprocess.check_output(
            [
                "docker",
                "container",
                "inspect",
                container_name,
                "--format",
                '{{.Id}} {{index .Config.Labels "bbx.dev/owner"}}',
            ],
            text=True,
        ).strip()
        assert actual == state["id"] + " " + owner
        bind = subprocess.check_output(
            ["docker", "port", container_name, "9000/tcp"], text=True
        ).strip()
        assert bind.startswith("127.0.0.1:")
        objects = ObjectStore(bind, state["user"], state["password"], "p4i-native-evidence")
        async with asyncio.timeout(30):
            while True:
                try:
                    await objects.ensure_bucket()
                    break
                except (OSError, HTTPError):
                    await asyncio.sleep(0.2)
        return objects

    async def decoder(self, home):
        from bbx_blackboard.native import NativeWorker

        build = json.loads((STATE / "build-source.json").read_text())
        helper = (
            Path(build["candidate"]) / "services/blackboard/src/bbx_blackboard/native_worker.py"
        )
        uid, gid = home.stat().st_uid, home.stat().st_gid
        process = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--rm",
            "-i",
            "--pull",
            "never",
            "--name",
            f"bbx-{self.prefix}-decoder-" + uuid4().hex[:8],
            "--label",
            f"bbx.dev/owner={self.prefix}-traffic-decoder",
            "--network",
            "none",
            "--user",
            f"{uid}:{gid}",
            "--cap-drop",
            "ALL",
            "--read-only",
            "--memory",
            "1g",
            "--pids-limit",
            "32",
            "--security-opt",
            "no-new-privileges",
            "--volume",
            f"{home}:{home}:rw",
            "--volume",
            f"{helper}:/native_worker.py:ro",
            "--env",
            f"HOME={home}",
            "--env",
            "LC_ALL=C",
            "--env",
            "TZ=UTC",
            "--entrypoint",
            "/usr/bin/python3",
            f"bbx-{self.prefix}-ubuntu-tools:20261107",
            "/native_worker.py",
            "/usr/bin/sharkd",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024**2,
            start_new_session=True,
        )
        return await NativeWorker.attach(process)

    def command(self, label):
        address = self.destination["ipv4"]
        code = f"""
import ipaddress, os, socket, subprocess, sys, time
with socket.create_connection(({address!r},{PORT}),2) as s:
    s.sendall({label.encode()!r})
    assert s.recv(128)=={label.encode()!r}
ip = os.environ['BBX_AGENT_IP']
gateway = str(ipaddress.IPv4Address(ip)-1)
if {label!r} == 'lead':
    listen = "\\n".join((
        "import socket", "s=socket.socket()", "s.bind(('0.0.0.0',18888))",
        "s.listen()", "while True:", " c,_=s.accept();c.close()",
    ))
    subprocess.Popen([sys.executable,'-c',listen],stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    for attempt in range(20):
        try:
            with socket.create_connection((ip,18888),.1): break
        except OSError:
            time.sleep(.05)
    else: raise RuntimeError('private listener did not start')
checks = [({address!r},{PORT + 1}),(gateway,44772)]
if {label!r} != 'lead': checks.append(('198.19.0.6',18888))
if {self.runner_ipv4!r}: checks.append(({self.runner_ipv4!r},8766))
for endpoint in checks:
    try:
        with socket.create_connection(endpoint,.25): raise RuntimeError('forbidden route')
    except (TimeoutError, ConnectionRefusedError): pass
print('p4i', {label!r}, ip, os.getuid(), 'blocked')
"""
        return "exec_command", {
            "command": "python3 -c " + shlex.quote(code),
            "cwd": "/workspace",
            "timeout_seconds": 8,
        }

    async def tool(self, body, names):
        prior = tool_results(body["messages"])
        if "board_dispatch_challenge" in names:
            if "exec_command" not in prior:
                return self.command("lead")
            if "board_create_challenge" not in prior:
                return "board_create_challenge", {
                    "title": "P4i original challenge",
                    "description": "Observe the original isolated network.",
                }
            if "board_dispatch_challenge" not in prior:
                challenge = prior["board_create_challenge"]
                return "board_dispatch_challenge", {
                    "challenge_id": challenge["id"],
                    "expected_revision": challenge["revision"],
                    "action": "ready",
                    "readiness": "no_external_materials",
                    "priority": 0,
                }
            if "board_update_challenge" not in prior:
                try:
                    await asyncio.wait_for(self.change.wait(), timeout=7)
                except TimeoutError:
                    return "board_challenges", {"limit": 1}
                assert self.challenge
                self.updated = True
                return "board_update_challenge", {
                    "challenge_id": self.challenge["id"],
                    "expected_revision": self.challenge["revision"],
                    "description": "Observe the fresh Assignment's isolated network.",
                    "readiness": "no_external_materials",
                    "priority": 0,
                }
            return None
        if "delegate_helper" in names:
            if "exec_command" not in prior:
                return self.command("worker-new" if self.updated else "worker-old")
            if not self.updated and "delegate_helper" not in prior:
                return "delegate_helper", {
                    "goal": "Use the isolated IPv4 fixture as the original helper.",
                    "input_refs": [],
                    "expected_output": "One network marker.",
                }
            return None
        assert names and names <= {"exec_command", "read_file"}
        if "exec_command" not in prior:
            return self.command("helper")
        return None

    async def verify(self, tid, app, engine, wait, evidence):
        timeline = asyncio.create_task(self.trace(tid, app, engine))
        try:
            await self.verify_traffic(tid, app, engine, wait, evidence)
        except Exception as error:
            (STATE / self.result_directory / "awaiting-diagnosis.json").write_text(
                json.dumps(
                    {
                        "task_id": str(tid),
                        "stage": evidence.get("traffic_stage"),
                        "error": type(error).__name__,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                + "\n"
            )
            await asyncio.Event().wait()
        finally:
            timeline.cancel()
            await asyncio.gather(timeline, return_exceptions=True)

    async def verify_traffic(self, tid, app, engine, wait, evidence):
        import httpx
        from bbx_blackboard.auth import issue_user_token
        from bbx_blackboard.store import schema as tables
        from sqlalchemy import select

        async def snapshot():
            value = await app.state.native_runner_bootstrap.view(tid)
            async with engine.connect() as conn:
                commands = (
                    (
                        await conn.execute(
                            select(tables.sandbox_command_records).where(
                                tables.sandbox_command_records.c.task_id == tid
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            done = {}
            for item in commands:
                text = item["data"]["stdout"].strip().split()
                if item["state"] == "completed" and item["exit_code"] == 0 and text[:1] == ["p4i"]:
                    assert text[3:] == ["10000", "blocked"], text[3:]
                    done[text[1]] = item
            return value, done

        async def have(labels, *, archived=()):
            value, done = await snapshot()
            entries = list(value["data"].get("network_scopes", {}).values())
            archived_bids = {e["scope"]["binding_id"] for e in entries if e["state"] == "archived"}
            if not labels <= done.keys() or any(
                done[name]["binding_id"] not in archived_bids for name in archived
            ):
                return None
            return value, done

        evidence["traffic_stage"] = "Lead, original Worker and foreground helper"
        active, original = await wait(
            lambda: have({"lead", "worker-old", "helper"}, archived=("helper",)), 660
        )
        sandbox = active["observation"]["sandbox"]
        self.runner_ipv4 = active["observation"]["runner"]["pod_ip"]
        assert (
            kubectl(
                "-n",
                active["namespace"],
                "get",
                "pod",
                sandbox["pod_name"],
                "-o",
                "jsonpath={.metadata.uid}",
            ).strip()
            == sandbox["pod_uid"]
        )
        outer = await asyncio.to_thread(
            kubectl,
            "-n",
            active["namespace"],
            "exec",
            sandbox["pod_name"],
            "-c",
            "sandbox",
            "--",
            "/usr/bin/python3",
            "-c",
            f"""
import socket
with socket.create_connection(({self.destination["ipv4"]!r},18888),2) as s:
    s.sendall(b'outer')
    assert s.recv(5)==b'outer'
try:
    with socket.create_connection(({self.destination["ipv4"]!r},18889),.5):
        raise RuntimeError('outer policy bypass')
except TimeoutError:
    print('outer-positive-and-deny')
""",
        )
        assert outer.strip() == "outer-positive-and-deny"
        self.challenge = await app.state.ctf_service.get_challenge(
            tid, str(original["worker-old"]["challenge_id"])
        )
        self.change.set()
        evidence["traffic_stage"] = "original Worker archived before same-seat replacement"
        _, done = await wait(
            lambda: have({"lead", "worker-old", "helper", "worker-new"}, archived=("worker-old",)),
            240,
        )
        old, new = done["worker-old"], done["worker-new"]
        assert old["agent_id"] == new["agent_id"] and old["binding_id"] != new["binding_id"]
        assert old["generation"] < new["generation"]

        evidence["traffic_stage"] = "original stop and all capture sources archived"
        headers = {"Authorization": "Bearer " + issue_user_token(app.state.settings, "alice")}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
            headers=headers,
        ) as http:
            response = await http.post(f"/api/tasks/{tid}/stop")
            assert response.status_code == 200

            async def stopped():
                record, _ = await snapshot()
                return (
                    record
                    if record["state"] == "stopped" and record["data"].get("exit_confirmed")
                    else None
                )

            final = await wait(stopped, 120)
            entries = list(final["data"]["network_scopes"].values())
            assert len(entries) == 4 and all(e["state"] == "archived" for e in entries)
            for item in entries:
                assert item["seal"]["packet_count"] > 0 and item["ready"]["netns_inode"]
            for key in ("source_id", "ipv4", "namespace", "host_interface"):
                assert len({item["scope"][key] for item in entries}) == 4
            assert len({item["ready"]["netns_inode"] for item in entries}) == 4
            assert len({item["ready"]["host_ifindex"] for item in entries}) == 4
            assert {e["scope"]["role"] for e in entries} == {"lead", "worker", "helper"}
            evidence["traffic_stage"] = "original user native read-only query"
            run = entries[0]["run"]
            exported = STATE / self.result_directory / "sealed-parts"
            exported.mkdir(mode=0o700, exist_ok=True)
            sources = {e["scope"]["source_id"]: e["scope"] for e in entries}
            for item in entries:
                source, digest = item["scope"]["source_id"], item["seal"]["sha256"]
                key = f"traffic/{tid}/run-{run}/native/{source}/parts/{digest}.bin"
                content = await app.state.objects.get(key)
                assert hashlib.sha256(content).hexdigest() == digest
                assert len(content) == item["seal"]["size_bytes"]
                (exported / f"{source}.pcap").write_bytes(content)
            app.state.native_cache.worker_factory = self.decoder
            path = f"/api/tasks/{tid}/traffic/native/queries"
            query = await http.post(
                path,
                json={
                    "display_filter": f"ip.addr == {self.destination['ipv4']} && tcp.port == 18888"
                },
            )
            assert query.status_code == 202, query.status_code
            query_path = path + "/" + query.json()["id"]

            async def finished_query():
                page = await http.get(query_path)
                assert page.status_code == 200
                data = page.json()
                return data if data["status"] in {"ready", "partial", "error"} else None

            result = await wait(finished_query, 95)
            assert result["status"] in {"ready", "partial"} and result["matched_packets"] > 4
            assert {c["source_id"] for c in result["coverage"]} == {
                e["scope"]["source_id"] for e in entries
            }
            packets = await http.get(
                query_path + "/packets", params={"revision": result["revision"]}
            )
            assert packets.status_code == 200
            frames = packets.json()["records"]
            assert {f["source_id"] for f in frames} == {e["scope"]["source_id"] for e in entries}
            for source, scope in sources.items():
                attributed = [f for f in frames if f["source_id"] == source]
                assert any(scope["ipv4"] in f["columns"] for f in attributed)
                for frame in attributed:
                    assert frame["attribution"] == "exact"
                    assert all(
                        frame["scope"][key] == scope[key]
                        for key in ("binding_id", "assignment_id", "generation", "epoch", "role")
                    )
            first = frames[0]
            detail = await http.get(
                query_path + f"/sources/{first['source_id']}/frames/{first['source_number']}",
                params={"revision": result["revision"]},
            )
            assert detail.status_code == 200 and detail.json()["source_id"] == first["source_id"]
        evidence["checks"].update(
            {
                "isolated_ipv4_target_and_controls": self.destination[
                    "forbidden_listener_baseline"
                ],
                "actual_lead_worker_helper_and_same_seat_replacement": True,
                "independent_sealed_sources_archived_before_original_stop": True,
                "original_user_readonly_native_parser_and_attribution": True,
            }
        )
        evidence["traffic_sources"] = [
            {
                "source_id": e["scope"]["source_id"],
                "agent": e["scope"]["agent_id"],
                "binding": e["scope"]["binding_id"],
                "role": e["scope"]["role"],
                "ipv4": e["scope"]["ipv4"],
                "generation": e["scope"]["generation"],
                "inode": e["ready"]["netns_inode"],
                "ifindex": e["ready"]["host_ifindex"],
                "packets": e["seal"]["packet_count"],
                "sha256": e["seal"]["sha256"],
            }
            for e in entries
        ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--stop-only", action="store_true")
    parser.add_argument("--revised-startup", action="store_true")
    args = parser.parse_args()
    harness = runpy.run_path(str(ROOT / "deploy/kubernetes/p3h/probe.py"))
    name = "traffic-target-revised.json" if args.revised_startup else "traffic-target.json"
    destination = (
        json.loads((STATE / name).read_text()) if args.stop_only else target(args.revised_startup)
    )
    asyncio.run(
        harness["main"](
            args.candidate.resolve(),
            args.stop_only,
            args.stop_only,
            workers=TrafficProbe(destination, args.revised_startup),
        )
    )
