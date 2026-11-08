"""One public CTF creation through the explicit test-only Ubuntu publication."""

import argparse
import asyncio
import hashlib
import json
import runpy
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from common import NODE, ROOT, STATE, kubectl, owned_cluster

shared = runpy.run_path(str(ROOT / "deploy/kubernetes/p4i/probe.py"))
tool_results = shared["tool_results"]
PORT = shared["PORT"]


def install_kali_capture(receipt_path):
    from image_release import DIGEST, GIB, local_docker, pinned_image

    owned_cluster()
    receipt = json.loads(receipt_path.read_text())
    if (
        receipt.get("result") != "local_overlay_verified"
        or receipt.get("base_source") != "reconstructed"
        or receipt.get("publish_ready") is not False
    ):
        raise ValueError("Expected the private rebuilt Kali overlay receipt")
    derived = receipt["derived"]
    reference, tag = derived["reference"], derived["tag"]
    digest = reference.rsplit("@", 1)[1]
    if not DIGEST.fullmatch(digest) or not tag.startswith("bbx-p4j-kali-traffic:"):
        raise ValueError("Unexpected reconstructed Sandbox image")
    if pinned_image(local_docker(None), reference) != derived["platform_id"]:
        raise ValueError("The locally verified Sandbox image changed")
    path = STATE / "build-source-kali.json"
    if path.exists():
        existing = json.loads(path.read_text())
        if existing["kali_capture"]["reference"] != reference:
            raise ValueError("The original fixture image binding changed")
        return receipt
    available = int(
        subprocess.check_output(
            ["docker", "exec", NODE, "df", "-B1", "--output=avail", "/var/lib/containerd"],
            text=True,
        ).splitlines()[-1]
    )
    if available < 50 * GIB:
        raise RuntimeError("The owned node lacks 50 GiB for the original CTF tool layer")
    runpy.run_path(str(ROOT / "deploy/kubernetes/p3f/load_images.py"))["load"](tag, digest)
    inventory = json.loads((STATE / "build-source.json").read_text())
    inventory["environment_revision"] = receipt["environment_revision"]
    inventory["kali_capture"] = {
        "reference": reference,
        "base_tool_image": receipt["base"]["reference"],
        "recipe_sha256": receipt["recipe_sha256"],
        "node_available_before_import": available,
    }
    path.write_text(json.dumps(inventory, indent=2) + "\n")
    return receipt


class PublicCTFProbe(shared["TrafficProbe"]):
    prefix = "p4j"
    publication_id = "p4j-isolated-ubuntu"

    def __init__(self, destination, capture=None, corrected=False):
        super().__init__(destination)
        self.capture = capture
        self.publication_label = "Isolated Ubuntu CTF test fixture"
        if capture:
            self.result_directory = "joint-kali"
            self.build_source_name = "build-source-kali.json"
            self.publication_id = "p4j-isolated-rebuilt-kali"
            self.publication_label = "Isolated reconstructed Kali CTF fixture"
            self.resource_options["traffic_sandbox_image"] = capture["derived"]["reference"]
        if corrected:
            # Separate PG/object store, Gateway selector and source after the original stop.
            self.result_directory += "-corrected"
            self.gateway_name = "bbx-p4j-gateway-corrected"
            self.gateway_owner = "p4j-corrected"
            self.revised = True

    def publication_options(self):
        return {}

    async def group_inputs(self, http, origin, group_id, capability):
        return {}

    async def public_create(self, app, cfg, settings, resources, http, origin, model):
        from bbx_blackboard.auth import issue_user_token
        from bbx_blackboard.native_ctf_publication import load_publication

        profile = await app.state.profile_store.get("ctf")
        publication = {
            "id": self.publication_id,
            "revision": 1,
            "label": self.publication_label,
            "release": "test_only",
            "model": {
                "id": "p3h-fake",
                "version": model["version"],
                "gateway_alias": "bounded-fake",
            },
            "ctf_profile_version": profile["version"],
            "exchange_rate": {
                "rate_cny_per_usd": "7",
                "source_ref": "fixture://p4j",
                "observed_at": datetime.now(UTC).isoformat(),
            },
            "runtime_resource_config": {
                **{k: v for k, v in resources.items() if k != "retirement_single_apiserver"},
                "runner_blackboard_url": settings.runner_blackboard_url.rstrip("/"),
                "runner_model_proxy_url": settings.runner_model_proxy_url.rstrip("/"),
            },
            **self.publication_options(),
        }
        path = STATE / self.result_directory / "test-only-publication.json"
        path.write_text(json.dumps(publication, indent=2) + "\n")
        app.state.native_ctf_publication = load_publication(
            cfg.model_copy(update={"native_ctf_publication_file": path}), allow_test_only=True
        )
        self.origin = origin
        self.user = {"Authorization": "Bearer " + issue_user_token(cfg, "alice")}
        capability = await http.get(origin + "/api/task-creation-capabilities", headers=self.user)
        assert capability.status_code == 200, capability.status_code
        view = capability.json()
        assert view["mode"] == "native-ctf"
        expected = {"id": self.publication_id, "revision": 1}
        assert all(view["native_ctf"][key] == value for key, value in expected.items())
        assert view["native_ctf"]["model_id"] == "p3h-fake"
        assert view["native_ctf"]["model_version"] == model["version"]
        group = await http.post(origin + "/api/task-input-groups", headers=self.user)
        assert group.status_code in {200, 201}, group.status_code
        group_id = group.json()["id"]
        body = {
            "mode": "ctf",
            "goal": "Send one isolated P4j test marker.",
            "budget": {"max_cost": "7", "max_minutes": 15},
            "agent_profile": "ctf",
            "model_id": "p3h-fake",
            "model_version": model["version"],
            "input_group_id": group_id,
            "native_environment": {
                **expected,
                "publication_stamp": view["native_ctf"]["publication_stamp"],
            },
            **await self.group_inputs(http, origin, group_id, view["native_ctf"]),
        }
        intent = STATE / self.result_directory / "public-create-intent.json"
        intent.write_text(
            json.dumps(
                {"method": "POST", "path": "/api/tasks", **body, "native_environment": expected},
                indent=2,
            )
            + "\n"
        )
        try:
            response = await http.post(origin + "/api/tasks", json=body, headers=self.user)
        except Exception as error:
            (intent.parent / "awaiting-public-create-readback.json").write_text(
                json.dumps({"input_group_id": group_id, "error": type(error).__name__}) + "\n"
            )
            await asyncio.Event().wait()
            raise
        assert response.status_code in {200, 201}, response.status_code
        task_id = UUID(response.json()["id"])
        (intent.parent / "public-create-receipt.json").write_text(
            json.dumps(
                {
                    "task_id": str(task_id),
                    "status": response.status_code,
                    "release": "test_only",
                    "capability": expected,
                },
                indent=2,
            )
            + "\n"
        )
        return task_id

    async def tool(self, body, names):
        assert "exec_command" in names and "bash" not in names
        if "exec_command" in tool_results(body["messages"]):
            return None
        address = self.destination["ipv4"]
        code = (
            "import os,socket\n"
            f"with socket.create_connection(({address!r},{PORT}),2) as s:\n"
            " s.sendall(b'p4j-public-create')\n"
            " assert s.makefile('rb').read(17)==b'p4j-public-create'\n"
            "print('p4j-public-create',os.environ['BBX_AGENT_IP'],os.getuid())"
        )
        if self.capture:
            code += (
                "\nimport pwn,z3,Crypto,angr,subprocess"
                "\nassert angr.Project('/bin/true',auto_load_libs=False).arch.name=='AMD64'"
                "\nassert subprocess.run(['/usr/bin/gdb','--version'],capture_output=True,"
                "timeout=10).returncode==0"
                "\nprint('p4j-ctf-tools')"
            )
        return "exec_command", {
            "command": "python3 -c " + shlex.quote(code),
            "cwd": "/workspace",
            "timeout_seconds": 30 if self.capture else 5,
        }

    async def verify_traffic(self, tid, app, engine, wait, evidence):
        import httpx
        from bbx_blackboard.store import schema as tables
        from sqlalchemy import select

        async def executed():
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
            if not rows or rows[0]["state"] != "completed":
                return None
            assert len(rows) == 1 and rows[0]["exit_code"] == 0
            return rows[0]

        evidence["traffic_stage"] = "public native task, first Lead prepare and one actual command"
        command = await wait(executed, 660)
        record = await app.state.native_runner_bootstrap.view(tid)
        operation_id = record["operation_id"]
        assert record["data"]["traffic_profile"] == "binding-pcap-v1"
        assert command["role"] == "lead"
        expected = ["p4j-public-create 198.19.0.6 10000"]
        if self.capture:
            expected.append("p4j-ctf-tools")
            original = record["observation"]["sandbox"]
            assert original["environment_revision"] == self.capture["environment_revision"]
            pod = json.loads(
                kubectl("-n", record["namespace"], "get", "pod", original["pod_name"], "-o", "json")
            )
            assert pod["metadata"]["uid"] == original["pod_uid"]
            (spec,) = [c for c in pod["spec"]["containers"] if c["name"] == "sandbox"]
            (actual,) = [c for c in pod["status"]["containerStatuses"] if c["name"] == "sandbox"]
            expected_image = self.capture["derived"]
            assert spec["image"] == expected_image["reference"]
            assert actual["containerID"] == original["container_id"]
            assert actual["imageID"].rsplit("@", 1)[-1].rsplit("://", 1)[-1] in {
                expected_image["reference"].rsplit("@", 1)[1],
                expected_image["platform_id"],
                expected_image["config_digest"],
            }
            evidence["capture_pod_image"] = {
                "pod_uid": original["pod_uid"],
                "spec_image": spec["image"],
                "observed_image_id": actual["imageID"],
                "container_id": actual["containerID"],
            }
        assert command["data"]["stdout"].splitlines() == expected
        evidence["traffic_stage"] = "same original ordinary user stop and archived capture"

        async def stopped():
            latest = await app.state.native_runner_bootstrap.view(tid)
            return (
                latest
                if latest["state"] == "stopped" and latest["data"].get("exit_confirmed")
                else None
            )

        async with httpx.AsyncClient(timeout=15, trust_env=False, headers=self.user) as http:
            stop = await http.post(self.origin + f"/api/tasks/{tid}/stop")
            assert stop.status_code == 200
            final = await wait(stopped, 120)
            assert final["operation_id"] == operation_id
            (source,) = final["data"]["network_scopes"].values()
            assert source["state"] == "archived"
            seal, scope = source["seal"], source["scope"]
            assert scope["binding_id"] == command["binding_id"] and scope["ipv4"] == "198.19.0.6"
            assert seal["packet_count"] > 0 and seal["complete"] and seal["dropped_packets"] == 0
            digest, sid = seal["sha256"], scope["source_id"]
            path = f"traffic/{tid}/run-{source['run']}/native/{sid}"
            part = await app.state.objects.get(path + f"/parts/{digest}.bin")
            index = await app.state.objects.get(path + f"/indexes/{source['ref']['revision']}.json")
            assert b"p4j-public-create" in part and hashlib.sha256(part).hexdigest() == digest
            assert hashlib.sha256(index).hexdigest() == source["ref"]["revision"]
            directory = STATE / self.result_directory
            (directory / "sealed.pcap").write_bytes(part)
            (directory / "sealed-source-index.json").write_bytes(index)

            evidence["traffic_stage"] = "same Task authorized Linux packet and frame query"
            app.state.native_cache.worker_factory = self.decoder
            query_url = self.origin + f"/api/tasks/{tid}/traffic/native/queries"
            query = await http.post(
                query_url,
                json={
                    "source_id": sid,
                    "display_filter": (
                        f"ip.addr == {self.destination['ipv4']} && tcp.port == {PORT}"
                    ),
                },
            )
            assert query.status_code == 202, query.status_code
            query_url += "/" + query.json()["id"]

            async def decoded():
                response = await http.get(query_url)
                assert response.status_code == 200
                result = response.json()
                return result if result["status"] in {"ready", "partial", "error"} else None

            result = await wait(decoded, 95)
            assert result["status"] == "ready" and result["matched_packets"] > 1
            params = {"revision": result["revision"]}
            response = await http.get(query_url + "/packets", params=params)
            assert response.status_code == 200
            packets = response.json()["records"]
            assert all(
                p["source_id"] == sid
                and p["scope"]["binding_id"] == scope["binding_id"]
                and p["attribution"] == "exact"
                for p in packets
            )
            frame = await http.get(
                query_url + f"/sources/{sid}/frames/{packets[0]['source_number']}", params=params
            )
            assert frame.status_code == 200 and frame.json()["scope"]["role"] == "lead"

        evidence["publication"] = {
            "release": "test_only",
            "capability": self.publication_id,
            "operation_id": operation_id,
            "binding_id": scope["binding_id"],
            "command_id": command["id"],
            "source_id": sid,
            "pcap_sha256": digest,
            "packets": seal["packet_count"],
        }
        if self.capture:
            evidence["publication"]["capture_image"] = self.capture["derived"]["reference"]
            evidence["publication"]["base_tool_image"] = self.capture["base"]["reference"]
            evidence["checks"]["actual_reconstructed_kali_tools_in_low_identity_sandbox"] = True
        evidence["checks"].update(
            {
                "public_create_used_exact_test_only_capability": True,
                "original_native_lead_real_nonroot_tcp": True,
                "original_pcap_and_index_read_back_from_minio": True,
                "authorized_linux_packet_and_frame": True,
            }
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--stop-only", action="store_true")
    parser.add_argument("--capture-receipt", type=Path)
    parser.add_argument("--corrected-profile", action="store_true")
    args = parser.parse_args()
    capture = install_kali_capture(args.capture_receipt) if args.capture_receipt else None
    harness = runpy.run_path(str(ROOT / "deploy/kubernetes/p3h/probe.py"))
    target_receipt = (
        "traffic-target-revised.json" if args.corrected_profile else "traffic-target.json"
    )
    destination = (
        json.loads((STATE / target_receipt).read_text())
        if args.stop_only
        else shared["target"](args.corrected_profile, "p4j")
    )
    asyncio.run(
        harness["main"](
            args.candidate.resolve(),
            args.stop_only,
            args.stop_only,
            workers=PublicCTFProbe(destination, capture, args.corrected_profile),
        )
    )
