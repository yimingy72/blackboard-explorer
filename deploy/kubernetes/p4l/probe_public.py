"""One public Task through real OpenVPN, a planned hold and original peer loss."""

import argparse
import asyncio
import hashlib
import json
import runpy
import shlex
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from build_images import VPN
from common import ROOT, STATE, kubectl, owned_cluster
from peer import CLIENT_TUN, PORT, SERVER, UDP, install

public = runpy.run_path(str(ROOT / "deploy/kubernetes/p4j/probe_public.py"))
tool_results = public["tool_results"]
PROFILE = "openvpn-ipv4-tcp-v1"
PROFILE_ID = "p4l-local-openvpn"


class VpnProbe(public["PublicCTFProbe"]):
    prefix = "p4l"
    publication_id = "p4l-isolated-openvpn"
    sandbox_never_restart = True
    strict_control_tls = True

    def __init__(self, peer, inventory, *, variant=None):
        super().__init__(peer)
        if variant:
            self.result_directory = "joint-" + variant
            self.gateway_name = "bbx-p4l-gateway-" + variant
            self.gateway_owner = "p4l-" + variant
            self.revised = variant == "strict-tls"
        if variant in {"egress-tmpdir", "aead"}:
            self.object_store_suffix = "-" + variant
        self.publication_label = "Isolated local OpenVPN CTF fixture"
        image = inventory["images"][VPN]["reference"]
        self.resource_options["vpn"] = {
            "profile": PROFILE,
            "id": PROFILE_ID,
            "label": "Local OpenVPN peer",
            "sandbox_image": image,
            "egress_image": image,
            "credential": peer["credential"],
            "peer": {"ipv4": peer["peer_ipv4"], "port": UDP, "server_name": SERVER},
            "peer_namespace": peer["namespace"],
            "tun_ipv4": CLIENT_TUN,
            "tun_resource": "devic.es/tun",
        }
        self.round = "initial"
        self.operation = None
        self.sent = set()
        self.control_baseline = asyncio.Event()

    def publication_options(self):
        return {"vpn_profile": PROFILE}

    async def tool(self, body, names):
        assert {"vpn_configure", "vpn_status", "vpn_disconnect", "exec_command"} <= names
        prior = tool_results(body["messages"])
        value = prior.get("vpn_status")
        status = value.get("result", value) if isinstance(value, dict) else {}
        if not self.operation:
            if "vpn_configure" not in self.sent:
                await asyncio.wait_for(self.control_baseline.wait(), 30)
                self.sent.add("vpn_configure")
                return "vpn_configure", {"profile_id": PROFILE_ID}
            configured = prior.get("vpn_configure")
            if isinstance(configured, dict):
                self.operation = configured.get("result", configured).get("operation_id")
        if self.round == "loss" and status.get("state") in {"failed", "unknown"}:
            assert self.operation
            if "disconnect" not in self.sent:
                self.sent.add("disconnect")
                return "vpn_disconnect", {"operation_id": self.operation}
        if self.round in {"initial", "resumed"} and status.get("state") == "ready":
            if self.round not in self.sent:
                self.sent.add(self.round)
                return self.command(self.round)
            if "exec_command" in prior and self.round == "initial":
                return None
        if status.get("state") == "disconnected":
            return None
        await asyncio.sleep(1.5)
        return "vpn_status", {}

    def command(self, label):
        address = self.destination["ipv4"]
        marker = ("p4l-" + label).encode()
        code = (
            "import os,socket,time\n"
            f"with socket.create_connection(({address!r},{PORT}),2) as s:\n"
            f" s.sendall({marker!r}); stream=s.makefile('rb')\n"
            f" assert stream.read({len(marker)})=={marker!r}\n"
            " try:\n"
            f"  socket.create_connection(({address!r},{PORT + 1}),.5).close()\n"
            " except OSError: pass\n"
            " else: raise RuntimeError('unapproved tunnel port opened')\n"
        )
        if label == "resumed":
            code += (
                " until=time.monotonic()+22\n"
                " while time.monotonic()<until:\n"
                "  time.sleep(.3)\n"
                "  try:\n"
                "   s.sendall(b'x')\n"
                "   if stream.read(1)!=b'x': break\n"
                "  except OSError: break\n"
                " else: raise RuntimeError('original socket remained usable after peer loss')\n"
            )
        outcome = "blocked peer-loss-blocked" if label == "resumed" else "blocked"
        code += f"print({marker.decode()!r},os.environ['BBX_AGENT_IP'],os.getuid(),{outcome!r})"
        return "exec_command", {
            "command": "python3 -c " + shlex.quote(code),
            "cwd": "/workspace",
            "timeout_seconds": 30 if label == "resumed" else 8,
        }

    async def trace(self, tid, app, engine):
        from bbx_blackboard.native_vpn import vpn_state

        runner_trace = asyncio.create_task(super().trace(tid, app, engine))
        timeline = STATE / self.result_directory / "vpn-timeline.jsonl"
        previous = None
        try:
            while True:
                value = await app.state.native_runner_bootstrap.view(tid)
                vpn = vpn_state(value)
                visible = {
                    "resource_state": value["state"],
                    "resource_error": value.get("last_error"),
                    "vpn_state": vpn.get("state"),
                    "vpn_operation_id": vpn.get("operation_id"),
                    "held": bool(vpn.get("hold")),
                    "exact_tunnel": bool(vpn.get("tunnel")),
                    "sandbox_link": bool(vpn.get("sandbox_link")),
                    "preflight": vpn.get("preflight", {}).get("passed"),
                    "applied_mode": vpn.get("applied", {}).get("mode"),
                }
                if visible != previous:
                    with timeline.open("a") as output:
                        output.write(
                            json.dumps({"at": datetime.now(UTC).isoformat(), **visible}) + "\n"
                        )
                    previous = visible
                await asyncio.sleep(1)
        finally:
            runner_trace.cancel()
            await asyncio.gather(runner_trace, return_exceptions=True)

    async def verify_traffic(self, tid, app, engine, wait, evidence):
        import httpx
        from bbx_blackboard.native_vpn import vpn_state
        from bbx_blackboard.store import schema as tables
        from sqlalchemy import select

        async def read():
            return await app.state.native_runner_bootstrap.view(tid)

        async def command(label):
            async with engine.connect() as conn:
                rows = (
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
            prefix = "p4l-" + label
            for row in rows:
                line = row["data"].get("stdout", "").split()
                if row["state"] == "completed" and line[:1] == [prefix]:
                    assert row["role"] == "lead" and row["exit_code"] == 0
                    extra = ["peer-loss-blocked"] if label == "resumed" else []
                    assert line[2:] == ["10000", "blocked", *extra]
                    return row
            return None

        async def stage(name, timeout=90):
            async def current():
                value = await read()
                return value if vpn_state(value).get("state") == name else None

            return await wait(current, timeout)

        async def original():
            value = await read()
            runner = value["observation"].get("runner", {})
            return value if runner.get("egress_container_id") else None

        async def control_routes(value):
            runner = value["observation"]["runner"]
            args = ("-n", value["namespace"], "exec", runner["pod_name"], "-c", "vpn-egress", "--")
            return {
                "default": await asyncio.to_thread(
                    kubectl, *args, "ip", "-4", "route", "show", "default"
                ),
                "peer": await asyncio.to_thread(
                    kubectl, *args, "ip", "-4", "route", "get", self.destination["peer_ipv4"]
                ),
                "dns_sha256": (
                    await asyncio.to_thread(kubectl, *args, "sha256sum", "/etc/resolv.conf")
                ).split()[0],
            }

        before = await control_routes(await wait(original, 520))
        self.control_baseline.set()
        evidence["traffic_stage"] = "public Task, original OpenVPN/WireGuard and first solver TCP"
        initial = await wait(lambda: command("initial"), 660)
        active = await stage("ready")
        vpn = vpn_state(active)
        tunnel = vpn["tunnel"]
        evidence["vpn_initial"] = {
            "operation_id": vpn["operation_id"],
            "tunnel": tunnel,
            "credential_reserved": vpn["credential_reserved"],
            "target": self.destination["ipv4"],
        }
        assert vpn["preflight"]["passed"] is True
        runner = active["observation"]["runner"]
        transfer = await asyncio.to_thread(
            kubectl,
            "-n",
            active["namespace"],
            "exec",
            runner["pod_name"],
            "-c",
            "vpn-egress",
            "--",
            "/usr/bin/wg",
            "show",
            "bbxwg",
            "transfer",
        )
        _, received, sent = transfer.split()
        assert int(received) > 0 and int(sent) > 0
        evidence["wireguard_bytes"] = {"received": int(received), "sent": int(sent)}
        after = await control_routes(active)
        assert before == after and before["default"].strip()
        evidence["control_routes"] = {
            "before": before,
            "after": after,
            "peer_push_redirect_and_dns": True,
        }
        prefix = self.origin + f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
        async with httpx.AsyncClient(timeout=20, trust_env=False, headers=self.user) as http:
            evidence["traffic_stage"] = "normal pause seals original source; same VPN child resumes"
            pause = await http.post(prefix + "/pause", json={"request_id": str(uuid4())})
            assert pause.status_code == 200, pause.status_code
            pause_id = pause.json()["pause_id"]

            async def paused():
                response = await http.get(prefix + "/pause")
                assert response.status_code == 200
                payload = response.json()
                return payload if payload["state"] == "paused" else None

            await wait(paused, 110)
            held = vpn_state(await read())
            assert held["tunnel"] == tunnel and held["hold"]["pause_id"] == pause_id
            self.round = "resumed"
            resume = await http.post(
                prefix + "/resume",
                json={
                    "request_id": str(uuid4()),
                    "pause_id": pause_id,
                    "instruction": "Send the second synthetic VPN marker.",
                },
            )
            assert resume.status_code == 200, resume.status_code
            peer = self.destination
            assert (
                kubectl(
                    "-n",
                    peer["namespace"],
                    "get",
                    "pod",
                    peer["name"],
                    "-o",
                    "jsonpath={.metadata.uid}",
                )
                == peer["pod_uid"]
            )

            async def established():
                line = await asyncio.to_thread(
                    kubectl,
                    "-n",
                    peer["namespace"],
                    "exec",
                    peer["name"],
                    "--",
                    "/usr/bin/ss",
                    "-Hnt",
                    "state",
                    "established",
                    "sport",
                    "=",
                    f":{PORT}",
                )
                return line.strip() if CLIENT_TUN + ":" in line else None

            evidence["established_solver_socket"] = await wait(established, 190)
            assert vpn_state(await stage("ready"))["tunnel"] == tunnel
            evidence["traffic_stage"] = "peer loss on existing solver TCP; Lead control disconnects"
            self.round = "loss"
            await asyncio.to_thread(
                kubectl,
                "-n",
                peer["namespace"],
                "exec",
                peer["name"],
                "--",
                "/usr/bin/python3",
                "-c",
                "import os,signal; (pid,)=open('/proc/1/task/1/children').read().split(); "
                "assert open('/proc/'+pid+'/comm').read().strip()=='openvpn'; "
                "os.kill(int(pid),signal.SIGTERM)",
            )

            async def target_still_live():
                try:
                    output = await asyncio.to_thread(
                        kubectl,
                        "-n",
                        peer["namespace"],
                        "exec",
                        peer["name"],
                        "--",
                        "/usr/bin/python3",
                        "-I",
                        "-S",
                        "-c",
                        "import socket; "
                        "assert all(n!='p4lpeer' for _,n in socket.if_nameindex()); "
                        "s=socket.create_connection(('10.70.0.1',18888),1); "
                        "s.sendall(b'still-live'); "
                        "assert s.makefile('rb').read(10)==b'still-live'; "
                        "print('target alive after original OpenVPN TUN exit')",
                    )
                except RuntimeError:
                    return None
                return output.strip()

            evidence["target_after_peer_loss"] = await wait(target_still_live, 12)
            second = await wait(lambda: command("resumed"), 45)
            assert second["binding_id"] != initial["binding_id"]
            disconnected = await stage("disconnected", 55)
            assert "disconnect" in self.sent and vpn_state(disconnected)["credential_reserved"]
            evidence["traffic_stage"] = (
                "original ordinary stop, retained PVCs and archived captures"
            )
            stop = await http.post(self.origin + f"/api/tasks/{tid}/stop")
            assert stop.status_code == 200

            async def stopped():
                value = await read()
                return (
                    value
                    if value["state"] == "stopped" and value["data"].get("exit_confirmed")
                    else None
                )

            final = await wait(stopped, 120)
            assert final["operation_id"] == active["operation_id"]
            assert (
                vpn_state(final)["egress_exit_confirmed"]
                and not vpn_state(final)["credential_reserved"]
            )
            assert (
                final["observation"]["runner"]["egress_container_id"]
                == active["observation"]["runner"]["egress_container_id"]
            )
            for key in ("journal_pvc", "workspace_pvc"):
                claim = final["observation"][key]
                actual = kubectl(
                    "-n",
                    final["namespace"],
                    "get",
                    "pvc",
                    claim["name"],
                    "-o",
                    "jsonpath={.metadata.uid}",
                )
                assert actual == claim["uid"]

            directory = STATE / self.result_directory
            entries = list(final["data"]["network_scopes"].values())
            for label, row in [("initial", initial), ("resumed", second)]:
                (entry,) = [
                    entry for entry in entries if entry["scope"]["binding_id"] == row["binding_id"]
                ]
                assert entry["state"] == "archived"
                scope, seal = entry["scope"], entry["seal"]
                assert scope["vpn_operation_id"] == vpn["operation_id"] and seal["packet_count"] > 0
                source_id = scope["source_id"]
                digest = seal["sha256"]
                base = f"traffic/{tid}/run-{entry['run']}/native/{source_id}"
                part = await app.state.objects.get(base + f"/parts/{digest}.bin")
                assert (
                    hashlib.sha256(part).hexdigest() == digest and ("p4l-" + label).encode() in part
                )
                (directory / f"{label}-sealed.pcap").write_bytes(part)
                evidence[f"capture_{label}"] = {
                    "source_id": source_id,
                    "binding_id": row["binding_id"],
                    "pcap_sha256": digest,
                    "packet_count": seal["packet_count"],
                }
        evidence["checks"].update(
            {
                "actual_public_task_real_openvpn_and_inner_wireguard": True,
                "two_nonroot_solver_paths_and_forbidden_port_block": True,
                "peer_redirect_dns_cannot_change_control_routes": True,
                "confirmed_pause_same_vpn_child_new_binding_source": True,
                "peer_loss_then_original_lead_disconnect_kept_credential_until_exit": True,
                "original_three_runner_cids_sandbox_exit_and_pvcs_retained": True,
                "two_original_source_archives_pre_encryption": True,
            }
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--stop-only", action="store_true")
    variants = parser.add_mutually_exclusive_group()
    variants.add_argument("--after-strict-control-fix", action="store_true")
    variants.add_argument("--after-egress-tmpdir-fix", action="store_true")
    variants.add_argument("--after-data-negotiation-fix", action="store_true")
    args = parser.parse_args()
    owned_cluster()
    if args.after_strict_control_fix:
        original = json.loads((STATE / "joint/original-user-stop-proof.json").read_text())
        if (
            original.get("stop_confirmed") is not True
            or original.get("credentials_released_after_egress_exit") is not True
            or original.get("never_claimed_external_vpn") is not True
        ):
            raise RuntimeError("The original fixture must have an exact positive stop")
    if args.after_egress_tmpdir_fix:
        second = json.loads((STATE / "joint-strict-tls/original-user-stop-proof.json").read_text())
        if (
            second.get("stop_confirmed") is not True
            or second.get("credentials_released_after_egress_exit") is not True
            or second.get("original_tunnel_not_observed") is not True
        ):
            raise RuntimeError("The original OpenVPN startup attempt must have a positive stop")
    if args.after_data_negotiation_fix:
        third = json.loads(
            (STATE / "joint-egress-tmpdir/original-user-stop-proof.json").read_text()
        )
        if (
            third.get("stop_confirmed") is not True
            or third.get("credentials_released_after_egress_exit") is not True
            or third.get("original_canary_failed") is not True
        ):
            raise RuntimeError("The original data-channel attempt must have a positive stop")
    variant = next(
        (
            name
            for enabled, name in (
                (args.after_data_negotiation_fix, "aead"),
                (args.after_egress_tmpdir_fix, "egress-tmpdir"),
                (args.after_strict_control_fix, "strict-tls"),
            )
            if enabled
        ),
        None,
    )
    inventory = json.loads((STATE / "build-source.json").read_text())
    receipt = STATE / "peer-receipt.json"
    peer = json.loads(receipt.read_text()) if receipt.exists() else install(inventory)
    harness = runpy.run_path(str(ROOT / "deploy/kubernetes/p3h/probe.py"))
    asyncio.run(
        harness["main"](
            args.candidate.resolve(),
            args.stop_only,
            args.stop_only,
            workers=VpnProbe(peer, inventory, variant=variant),
        )
    )
