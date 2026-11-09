"""Exercise both Lead network choices through one public publication and actual traffic."""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import runpy
import shlex
import ssl
import subprocess
import sys
from uuid import uuid4

from build_images import VPN
from common import HERE, NETWORK, SOURCE, STATE, kubectl, owned_cluster
from peer import DIRECT_PORT, PROBE, SOURCE_HOST, WORKER, prepare

public = runpy.run_path(str(SOURCE / "deploy/kubernetes/p4j/probe_public.py"))
sys.path.append(str(HERE))
base = runpy.run_path(str(SOURCE / "deploy/kubernetes/p3f/probe.py"))
tool_results = public["tool_results"]
PROFILE = "openvpn-task-ipv4-tcp-v1"


def kubernetes_boundaries():
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    services = json.loads(kubectl("get", "servicecidrs", "-o", "json"))["items"]
    return sorted(
        {
            str(ipaddress.IPv4Network(value))
            for value in (
                *(cidr for item in nodes for cidr in item["spec"]["podCIDRs"]),
                *(
                    addr["address"]
                    for item in nodes
                    for addr in item["status"]["addresses"]
                    if addr["type"] == "InternalIP" and ":" not in addr["address"]
                ),
                *(cidr for item in services for cidr in item["spec"]["cidrs"] if ":" not in cidr),
            )
        }
    )


class NetworkProbe(public["PublicCTFProbe"]):
    prefix = "p4m"
    publication_id = "p4m-isolated-network-choice"
    strict_control_tls = True
    sandbox_never_restart = True

    def __init__(self, mode, peer, inventory, source_ip, source_port, tls, requests):
        super().__init__(peer)
        self.mode = "default" if mode == "both" else mode
        self.both = mode == "both"
        self.gateway_name = "bbx-p4m-gateway-" + mode
        self.gateway_owner = "p4m-" + mode
        self.result_directory = "joint-" + mode
        self.object_store_suffix = "-" + mode
        self.publication_label = "Isolated P4m network choice fixture"
        network = json.loads(subprocess.check_output(["docker", "network", "inspect", NETWORK]))[0]
        bridge = next(
            item["Subnet"] for item in network["IPAM"]["Config"] if ":" not in item["Subnet"]
        )
        self.source_origin = f"https://{SOURCE_HOST}:{source_port}"
        self.source_url = self.source_origin + "/client.ovpn"
        self.source_ip, self.source_tls, self.requests = source_ip, tls, requests
        self.configure = asyncio.Event()
        self.choice_sent = False
        self.resumed = False
        self.after_resume_sent = False
        self.disconnect = False
        self.disconnect_sent = False
        self.lifecycle_only = False
        self.stop_task_id = None
        image = inventory["images"][VPN]["reference"]
        self.resource_options.update(
            traffic_targets=[],
            vpn={
                "profile": PROFILE,
                "id": self.publication_id,
                "label": self.publication_label,
                "sandbox_image": image,
                "egress_image": image,
                "tun_resource": "devic.es/tun",
                "source_origins": [self.source_origin],
                "source_cidrs": [str(ipaddress.IPv4Network(source_ip + "/24", strict=False))],
                "peer_cidrs": [bridge],
                "target_cidrs": ["10.0.0.0/8"],
                "default_cidrs": [bridge],
                "deny_cidrs": kubernetes_boundaries(),
            },
        )

    def publication_options(self):
        return {"vpn_profile": PROFILE}

    async def group_inputs(self, http, origin, group_id, capability):
        del http, origin, group_id, capability
        goal = (
            f"Use the configuration from {self.source_url} and the private TCP preflight. "
            "Read the challenge container_addr from the synthetic provider and solve it."
            if self.mode == "vpn"
            else "Use the default network; no VPN is needed. Read the synthetic TCP marker."
        )
        return {"goal": goal}

    async def public_create(self, app, *args):
        vpn = app.state.native_runner_bootstrap.vpn
        resolve = vpn.resolve_source

        async def fixture_resolve(host, port):
            if host == SOURCE_HOST:
                return {self.source_ip}
            return await resolve(host, port)

        vpn.resolve_source = fixture_resolve
        vpn.source_tls = self.source_tls
        return await super().public_create(app, *args)

    def command(self, label):
        target = (self.destination["peer_ipv4"], DIRECT_PORT)
        if self.mode == "vpn":
            code = (
                "import http.client,json\n"
                f"conn=http.client.HTTPConnection({PROBE['ipv4']!r},{PROBE['port']},timeout=3)\n"
                "conn.request('GET','/challenge')\n"
                "answer=conn.getresponse(); assert answer.status==200\n"
                "host,port=json.loads(answer.read())['container_addr'].split(':')\n"
                "target=(host,int(port)); conn.close()\n"
                f"assert target==({WORKER['ipv4']!r},{WORKER['port']})\n"
            )
        else:
            code = f"target={target!r}\n"
        marker = ("p4m-" + label).encode()
        code += (
            "import os,socket\n"
            f"with socket.create_connection(target,3) as s:\n"
            f" s.sendall({marker!r}); assert s.makefile('rb').read({len(marker)})=={marker!r}\n"
        )
        if self.mode == "vpn":
            code += (
                "try:\n"
                f" with socket.create_connection({target!r},.4): pass\n"
                "except OSError: pass\n"
                "else: raise RuntimeError('unapproved VPN underlay access')\n"
            )
        code += f"print({marker.decode()!r},os.environ['BBX_AGENT_IP'],os.getuid(),target)"
        return "exec_command", {
            "command": "python3 -c " + shlex.quote(code),
            "cwd": "/workspace",
            "timeout_seconds": 10,
        }

    async def tool(self, body, names):
        prior = tool_results(body["messages"])
        if "network_use_default" in names or "board_dispatch_challenge" in names:
            if not self.choice_sent:
                await asyncio.wait_for(self.configure.wait(), 30)
                self.choice_sent = True
                return (
                    ("vpn_configure", {"https_url": self.source_url, "preflight": PROBE})
                    if self.mode == "vpn"
                    else ("network_use_default", {})
                )
            status = prior.get("vpn_status") or {}
            status = status.get("result", status)
            if self.disconnect and not self.disconnect_sent and status.get("operation_id"):
                self.disconnect_sent = True
                return "vpn_disconnect", {"operation_id": status["operation_id"]}
            if status.get("state") != "ready":
                await asyncio.sleep(0.3)
                return "vpn_status", {}
            if self.resumed and not self.after_resume_sent:
                self.after_resume_sent = True
                return self.command("resumed")
            if self.mode == "default":
                if "exec_command" not in prior:
                    return self.command("default")
                await asyncio.sleep(0.5)
                return "vpn_status", {}
            if "board_create_challenge" not in prior:
                return "board_create_challenge", {
                    "title": "P4m synthetic challenge",
                    "description": "Get container_addr from the task's synthetic provider.",
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
            if self.lifecycle_only and not self.resumed:
                return None
            await asyncio.sleep(0.5)
            return "vpn_status", {}
        assert not ({"vpn_configure", "network_use_default", "vpn_disconnect"} & names)
        if "exec_command" not in prior:
            return self.command("worker")
        return None

    async def verify_traffic(self, tid, app, engine, wait, evidence):
        await self.verify_choice(tid, app, engine, wait, evidence)
        if not self.both:
            return
        import httpx

        first = await app.state.native_runner_bootstrap.view(tid)
        self.mode = "vpn"
        self.configure = asyncio.Event()
        self.choice_sent = False
        directory = STATE / self.result_directory
        original = json.loads((directory / "public-create-intent.json").read_text())
        async with httpx.AsyncClient(timeout=20, trust_env=False, headers=self.user) as client:
            group = await client.post(self.origin + "/api/task-input-groups")
            group.raise_for_status()
            capabilities = await client.get(self.origin + "/api/task-creation-capabilities")
            capabilities.raise_for_status()
            capability = capabilities.json()["native_ctf"]
            assert all(
                original["native_environment"][k] == capability[k] for k in ("id", "revision")
            )
            body = {
                **{
                    key: original[key]
                    for key in ("mode", "budget", "agent_profile", "model_id", "model_version")
                },
                "goal": (
                    f"Use the configuration from {self.source_url} and the private TCP preflight. "
                    "Read container_addr from the synthetic provider and solve it."
                ),
                "input_group_id": group.json()["id"],
                "native_environment": {
                    **original["native_environment"],
                    "publication_stamp": capability["publication_stamp"],
                },
            }
            created = await client.post(self.origin + "/api/tasks", json=body)
            created.raise_for_status()
            second = created.json()["id"]
        (directory / "vpn-create-receipt.json").write_text(
            json.dumps(
                {
                    "task_id": second,
                    "default_task_id": str(tid),
                    "publication_id": capability["id"],
                    "publication_revision": capability["revision"],
                },
                indent=2,
            )
            + "\n"
        )
        assert second != str(tid) and first["state"] == "stopped"
        from uuid import UUID

        await self.verify_choice(UUID(second), app, engine, wait, evidence)
        evidence["checks"]["two_modes_one_running_publication"] = True

    async def verify_choice(self, tid, app, engine, wait, evidence):
        import httpx
        from bbx_blackboard.store import schema as tables
        from sqlalchemy import select

        async def read():
            return await app.state.native_runner_bootstrap.view(tid)

        async def staged():
            value = await read()
            return value if value["data"].get("activated") else None

        evidence["traffic_stage"] = "pre-choice control online; no direct rule and idle VPN"
        initial = await wait(staged, 210)
        namespace = initial["namespace"]

        def policy(name):
            raw = kubectl("-n", namespace, "get", "cnp", name, "--ignore-not-found", "-o", "json")
            return json.loads(raw) if raw.strip() else None

        async def idle():
            return await asyncio.to_thread(
                kubectl,
                "-n",
                namespace,
                "exec",
                "bbx-runner",
                "-c",
                "vpn-egress",
                "--",
                "/usr/bin/python3",
                "-I",
                "-S",
                "-c",
                "import pathlib,socket; p=pathlib.Path('/var/run/bbx-vpn/state'); "
                "files=('claimed.json','tls.key','openvpn.sock'); "
                "assert not any((p/n).exists() for n in files); "
                "assert not any(n in ('bbxwg','bbxvpn') for _,n in socket.if_nameindex()); "
                "print('idle')",
            )

        assert initial["data"]["native_network"] == "unselected"
        assert not policy("bbx-binding-direct") and not policy("bbx-vpn-peer")
        assert (await idle()).strip() == "idle"
        self.configure.set()

        async def command(label):
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        select(tables.sandbox_command_records).where(
                            tables.sandbox_command_records.c.task_id == tid
                        )
                    )
                ).mappings()
                return next(
                    (
                        row
                        for row in rows
                        if row["state"] == "completed"
                        and f"p4m-{label}" in row["data"].get("stdout", "")
                    ),
                    None,
                )

        label = "default" if self.mode == "default" else "worker"
        evidence["traffic_stage"] = "real captured " + label + " command"
        first = await wait(lambda: command(label), 160)
        assert first["exit_code"] == 0 and "10000" in first["data"]["stdout"]
        active = await read()
        data = active["data"]
        vpn = data["vpn"]
        rows = [(label, first)]
        prefix = self.origin + f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
        async with httpx.AsyncClient(timeout=20, trust_env=False, headers=self.user) as client:
            if self.mode == "default":
                assert data["native_network"] == "default" and policy("bbx-binding-direct")
                assert not policy("bbx-vpn-peer") and (await idle()).strip() == "idle"
                assert not any(
                    vpn.get(key)
                    for key in ("credential_reserved", "tunnel", "issued", "operation_id")
                )
                assert not self.requests
                evidence["checks"]["captured_default_without_vpn_claim_or_data_plane"] = True
            else:
                assert first["agent_id"] != "lead" and data["native_network"] == "vpn"
                assert not policy("bbx-binding-direct")
                applied = policy("bbx-vpn-peer")
                assert applied["spec"]["egress"] == [
                    {
                        "toCIDR": [self.destination["peer_ipv4"] + "/32"],
                        "toPorts": [{"ports": [{"port": "1194", "protocol": "UDP"}]}],
                    }
                ]
                assert self.requests == ["/client.ovpn"]
                assert vpn["preflight"]["passed"] is True and vpn["tunnel"]["tun_ipv4"]
                assert vpn["network"]["preflight"] == PROBE and "targets" not in vpn["network"]
                evidence["checks"]["remote_tls_dynamic_tun_and_api_returned_worker_target"] = True
                evidence["traffic_stage"] = "same tunnel across planned pause and resume"
                if self.lifecycle_only:

                    async def input_receipts_applied():
                        async with engine.connect() as conn:
                            commands = tables.runner_commands
                            rows = (
                                await conn.execute(
                                    select(commands.c.status, commands.c.native_proof).where(
                                        commands.c.task_id == tid, commands.c.kind == "input"
                                    )
                                )
                            ).all()
                        return bool(rows) and all(
                            status == "applied" and isinstance(proof, dict)
                            for status, proof in rows
                        )

                    await wait(input_receipts_applied, 30)
                    evidence["checks"]["original_inputs_applied_before_pause"] = True
                pause = await client.post(prefix + "/pause", json={"request_id": str(uuid4())})
                assert pause.status_code == 200
                pause_id = pause.json()["pause_id"]

                async def paused():
                    value = (await client.get(prefix + "/pause")).json()
                    return value if value["state"] == "paused" else None

                await wait(paused, 110)
                self.resumed = True
                resume = await client.post(
                    prefix + "/resume",
                    json={
                        "request_id": str(uuid4()),
                        "pause_id": pause_id,
                        "instruction": "Send a new marker using the same shared task network.",
                    },
                )
                assert resume.status_code == 200
                second = await wait(lambda: command("resumed"), 160)
                resumed = (await read())["data"]["vpn"]
                assert (
                    resumed["operation_id"] == vpn["operation_id"]
                    and resumed["tunnel"] == vpn["tunnel"]
                )
                assert first["binding_id"] != second["binding_id"]
                rows.append(("resumed", second))
                evidence["checks"]["pause_resume_same_tunnel_new_binding"] = True
                self.disconnect = True

                async def disconnected():
                    current = await read()
                    state = current["data"]["vpn"]
                    return state if state["state"] == "disconnected" else None

                closed = await wait(disconnected, 45)
                assert (
                    closed["operation_id"] == vpn["operation_id"] and closed["credential_reserved"]
                )
                assert not closed.get("applied")
                evidence["checks"]["lead_disconnected_original_vpn_before_stop"] = True
            stop = await client.post(self.origin + f"/api/tasks/{tid}/stop")
            assert stop.status_code == 200

            async def stopped():
                value = await read()
                return (
                    value
                    if value["state"] == "stopped" and value["data"].get("exit_confirmed")
                    else None
                )

            final = await wait(stopped, 120)
        original = initial["observation"]
        observed = final["observation"]
        for key in ("container_id", "host_container_id", "egress_container_id"):
            assert observed["runner"][key] == original["runner"][key]
        assert observed["sandbox"]["container_id"] == original["sandbox"]["container_id"]
        assert final["data"]["vpn"]["egress_exit_confirmed"]
        for label, row in rows:
            (entry,) = [
                entry
                for entry in final["data"]["network_scopes"].values()
                if entry["scope"]["binding_id"] == row["binding_id"]
            ]
            assert entry["state"] == "archived" and entry["seal"]["packet_count"] > 0
            scope, seal = entry["scope"], entry["seal"]
            digest = seal["sha256"]
            part = await app.state.objects.get(
                f"traffic/{tid}/run-{entry['run']}/native/{scope['source_id']}/parts/{digest}.bin"
            )
            assert hashlib.sha256(part).hexdigest() == digest and ("p4m-" + label).encode() in part
            (STATE / self.result_directory / f"{label}-sealed.pcap").write_bytes(part)
            evidence["capture_" + label] = {
                "source_id": scope["source_id"],
                "binding_id": row["binding_id"],
                "sha256": digest,
                "packets": seal["packet_count"],
            }
        evidence["checks"]["original_four_cids_and_pcap_archives"] = True


async def main(mode, source_ip, stop_only, resume_setup=False, attempt=None):
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    owned_cluster()
    inventory = json.loads((STATE / "build-source.json").read_text())
    peer = prepare(inventory)
    source_ip = str(ipaddress.IPv4Address(source_ip))
    assert not ipaddress.IPv4Address(source_ip).is_loopback
    directory = STATE / "private-https"
    directory.mkdir(mode=0o700, exist_ok=True)
    if not (directory / "leaf.pem").exists():
        # The certificate does not name the actual pinned LAN address.
        base["certificates"](directory, "192.0.2.123", dns_names=(SOURCE_HOST,), strict=True)
    context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
    origin_state = directory / "origin.json"
    if origin_state.exists():
        original = json.loads(origin_state.read_text())
        if original["ip"] != source_ip:
            raise RuntimeError("The original HTTPS source changed")
        port = original["port"]
    else:
        port = base["free_port"]()
        origin_state.write_text(json.dumps({"ip": source_ip, "port": port}) + "\n")
    requests = []

    async def source(request):
        requests.append(request.url.path)
        return Response((STATE / "private-peer/client.ovpn").read_bytes(), media_type="text/plain")

    async with base["serving"](
        Starlette(routes=[Route("/client.ovpn", source)]),
        port,
        cert=directory / "leaf.pem",
        key=directory / "leaf-key.pem",
    ):
        try:
            _, bad = await asyncio.open_connection(
                source_ip, port, ssl=context, server_hostname="wrong.p4m.test"
            )
        except ssl.SSLCertVerificationError:
            pass
        else:
            bad.close()
            raise RuntimeError("TLS accepted the wrong original hostname")
        worker = NetworkProbe(mode, peer, inventory, source_ip, port, context, requests)
        if attempt is not None:
            if attempt in {"lifecycle", "lifecycle-final"}:
                if mode != "vpn":
                    raise RuntimeError("The independent lifecycle case is VPN-only")
                baseline = (
                    STATE / "joint-vpn-lifecycle/original-user-stop-proof.json"
                    if attempt == "lifecycle-final"
                    else STATE / "joint-both-fixed/vpn-original-user-stop-proof.json"
                )
                worker.lifecycle_only = True
            else:
                baseline = STATE / worker.result_directory / "original-user-stop-proof.json"
            if not json.loads(baseline.read_text()).get("stop_confirmed"):
                raise RuntimeError("The original Task has no positive stop proof")
            worker.result_directory += "-" + attempt
            worker.gateway_name += "-" + attempt
            worker.gateway_owner += "-" + attempt
            worker.object_store_suffix += "-" + attempt
        if resume_setup and json.loads(
            (STATE / worker.result_directory / "private-state.json").read_text()
        ).get("task_id"):
            raise RuntimeError("The original setup already created a Task")
        original = STATE / worker.result_directory / "vpn-create-receipt.json"
        if stop_only and original.exists():
            worker.stop_task_id = json.loads(original.read_text())["task_id"]
        harness = runpy.run_path(str(SOURCE / "deploy/kubernetes/p3h/probe.py"))
        await harness["main"](SOURCE, stop_only or resume_setup, stop_only, workers=worker)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("both", "default", "vpn"), default="both")
    parser.add_argument("--source-ip", required=True)
    parser.add_argument("--stop-only", action="store_true")
    parser.add_argument("--resume-setup", action="store_true")
    parser.add_argument("--attempt", choices=("fixed", "lifecycle", "lifecycle-final"))
    parser.add_argument("--parser-only", action="store_true")
    args = parser.parse_args()
    if args.parser_only:
        from anyio import CapacityLimiter
        from bbx_blackboard.native_vpn_sources import parse_private_configuration

        async def verify_process():
            raw = (STATE / "private-peer/client.ovpn").read_bytes()
            remote, _, _ = await parse_private_configuration(raw, CapacityLimiter(1))
            receipt = json.loads((STATE / "peer-receipt.json").read_text())
            assert remote == (receipt["peer_ipv4"], 1194)
            print("Original synthetic configuration parsed in the isolated worker process.")

        asyncio.run(verify_process())
    else:
        asyncio.run(
            main(args.mode, args.source_ip, args.stop_only, args.resume_setup, args.attempt)
        )
