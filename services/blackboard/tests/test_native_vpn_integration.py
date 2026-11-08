"""Real PG Task locks and original Runner MCP receipts for one shared VPN slot."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import runner_gateway_support as support
from bbx_blackboard.gateway_auth import require_gateway_identity
from bbx_blackboard.native_vpn import require_vpn_resume, vpn_exit_data
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import business_manifest
from bbx_contracts.runner_vpn import (
    VPN_CONFIGURE,
    VPN_DISCONNECT,
    VPN_PROFILE,
    VPN_STATUS,
    VPN_TOOLS,
)
from bbx_runtime.scheduler.lock import RuntimeLock
from fastapi import HTTPException, Request
from sqlalchemy import insert, select, update
from test_board_flags_integration import Objects
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_native_runner_bootstrap_integration import runtime_settings
from test_runner_control_integration import SERVICE, connect, control, mgmt

pytestmark = pytest.mark.integration


async def test_original_vpn_authority_and_deployment_slot(ctf, ctf_url, monkeypatch):
    ctf.objects = Objects()

    async def uuid_owner(http, tid):
        reply = await http.post(
            mgmt(tid) + "/owners",
            headers=SERVICE,
            json={
                "request_id": str(uuid4()),
                "runner_id": "vpn-fixture",
                "pod_uid": str(uuid4()),
                "expected_epoch": 0,
            },
        )
        assert reply.status_code == 200, reply.text
        return reply.json()

    monkeypatch.setattr(support, "register_owner", uuid_owner)
    cursor = support.cursor
    monkeypatch.setattr(support, "cursor", lambda previous: cursor(previous, uuid4().hex))
    profile = {
        "profile": VPN_PROFILE,
        "id": "fixture-vpn",
        "label": "Local VPN",
        "egress_image": "fixture-egress@sha256:" + "f" * 64,
        "sandbox_image": "fixture-vpn-sandbox@sha256:" + "e" * 64,
        "credential": {
            "namespace": "fixture-system",
            "name": "fixture-vpn",
            "uid": str(uuid4()),
            "cert_sha256": "d" * 64,
        },
        "peer": {"ipv4": "10.14.0.5", "port": 1194, "server_name": "fixture"},
        "tun_ipv4": "10.20.0.2",
        "tun_resource": "devic.es/tun",
    }
    async with (
        connect(ctf) as (app, http),
        RuntimeLock(runtime_settings(ctf_url), fenced=True) as runtime,
    ):
        gateway = app.state.runner_gateway
        bootstrap = app.state.native_runner_bootstrap

        async def task_fixture():
            lead = await support.prepare(http, ctf, tools=[*VPN_TOOLS, "exec_command"])
            worker = await support.worker_scope(http, ctf, lead, tools=["read_file"])
            tid = lead["tid"]
            stamp = datetime.now(UTC)
            operation = str(uuid4())
            runner = {
                "pod_uid": lead["owner"]["fence"]["pod_uid"],
                "pod_ip": "10.15.0.2",
                "phase": "Running",
                "container_id": "containerd://" + "1" * 64,
                "host_container_id": "containerd://" + "2" * 64,
                "egress_container_id": "containerd://" + "3" * 64,
            }
            sandbox = {
                "id": str(uuid4()),
                "cr_uid": str(uuid4()),
                "pod_uid": str(uuid4()),
                "pod_ip": "10.15.0.3",
                "container_id": "containerd://" + "4" * 64,
                "environment_revision": "vpn-fixture",
            }
            context = {
                "sandbox_id": sandbox["id"],
                "sandbox_pod_uid": sandbox["pod_uid"],
                "environment_revision": sandbox["environment_revision"],
                "execution_context_id": "a" * 64,
                "execd_endpoint": "sandbox.invalid",
                "host_url": "http://10.15.0.2:8766",
            }
            config = {
                "vpn": profile,
                "runner_profile": "node-sandbox-host-v1",
                "runner_blackboard_url": "https://blackboard.invalid",
                "environment_revision": sandbox["environment_revision"],
                "traffic_targets": [{"ipv4": "10.31.0.5", "port": 8000}],
                "sandbox_network": {
                    "namespace": "worker-a",
                    "network_identity": "worker-a",
                    "ipv4": "198.19.1.2",
                },
            }
            resource = {
                "task_id": tid,
                "operation_id": operation,
                "request_id": operation,
                "request_digest": "fixture",
                "namespace": "fixture-" + str(tid),
                "request": {
                    "traffic_profile": "binding-pcap-v1",
                    "publication": {"vpn_profile": VPN_PROFILE},
                },
                "config": config,
                "controller_generation": runtime.fence,
                "controller_backend_pid": runtime.backend_pid,
                "controller_backend_start": runtime.backend_started_at,
                "controller_id": "fixture-controller",
                "revision": 1,
                "state": "ready",
                "desired": "active",
                "hard_deadline": stamp + timedelta(minutes=5),
                "sandbox_create_claimed": True,
                "tenant_key_hash": "fixture-" + operation,
                "tenant_key_ciphertext": "fixture",
                "observation": {
                    "runner": runner,
                    "sandbox": sandbox,
                    "namespace_uid": str(uuid4()),
                    **{
                        name: {"uid": str(uuid4())}
                        for name in (
                            "journal_pvc",
                            "workspace_pvc",
                            "runner_secret",
                            "vpn_egress_secret",
                        )
                    },
                },
                "data": {"owner_epoch": 1, "vpn_profile": VPN_PROFILE, "vpn": {"state": "idle"}},
                "sandbox_host": {},
                "next_check_at": stamp,
                "created_at": stamp,
            }
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                task = (
                    (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid)))
                    .mappings()
                    .one()
                )
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(
                        ctf_control={
                            **task["ctf_control"],
                            "business_tools": business_manifest(vpn=True),
                        },
                    )
                )
                await conn.execute(insert(s.task_runner_resources).values(**resource))
                host = await gateway.sandbox.host_material(conn, tid, resource, context)
                record = await bootstrap._get(conn, tid)
                await conn.execute(
                    update(s.task_runner_resources)
                    .where(s.task_runner_resources.c.task_id == tid)
                    .values(
                        sandbox_host={**record["sandbox_host"], "ready": True},
                    )
                )
            reply = await http.post(
                f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap/actions",
                headers=SERVICE,
                json={
                    "action": "vpn_material",
                    "operation_id": operation,
                    "controller_id": "fixture-controller",
                    "controller_generation": runtime.fence,
                },
            )
            assert reply.status_code == 200, reply.text
            material = reply.json()
            return (
                lead,
                {"x-bbx-vpn-egress": material["report_key"]},
                {"x-bbx-sandbox-report": host["report_key"]},
                worker,
            )

        first, first_key, first_host, worker = await task_fixture()
        second, second_key, second_host, _ = await task_fixture()
        worker_config = support.invocation(
            worker, VPN_CONFIGURE, {"profile_id": profile["id"]}, "worker-denied"
        )
        denied_worker = await http.post(
            control(worker["tid"]) + f"/bindings/{worker['binding']['binding_id']}/tools/admit",
            headers=support.cap(worker, "mcp"),
            json=worker_config,
        )
        assert denied_worker.status_code == 403, denied_worker.text

        async def tool(lead, name, args, suffix):
            tid = lead["tid"]
            headers = support.cap(lead, "mcp")
            raw = support.invocation(lead, name, args, suffix)
            route = control(tid) + f"/bindings/{lead['binding']['binding_id']}/tools/admit"
            admitted = await http.post(route, headers=headers, json=raw)
            assert admitted.status_code == 200, admitted.text
            receipt = admitted.json()
            if receipt["disposition"] != "completed":
                identity = require_gateway_identity(
                    Request({"type": "http", "app": app, "headers": []}),
                    tid,
                    "mcp",
                    authorization=headers["Authorization"],
                )
                from bbx_contracts.runner import ToolInvocation

                dispatch, receipt = await gateway.claim_business_tool(
                    tid,
                    identity,
                    ToolInvocation.model_validate(raw),
                    name,
                    args,
                )
                assert dispatch is None
            assert receipt["disposition"] == "completed"
            return json.loads(receipt["result_json"])

        denied = await tool(first, "exec_command", {"command": "echo never dispatched"}, "down")
        assert denied["isError"]
        args = {"profile_id": profile["id"]}
        claims = await asyncio.gather(
            tool(first, VPN_CONFIGURE, args, "configure"),
            tool(second, VPN_CONFIGURE, args, "configure"),
        )
        assert sum(not result["isError"] for result in claims) == 1
        if claims[0]["isError"]:
            first, second = second, first
            first_key, first_host = second_key, second_host
        tid = first["tid"]
        edge, host = f"/api/tasks/{tid}/runner-vpn", f"/api/tasks/{tid}/sandbox-host/vpn"
        desired = (await http.get(edge + "/desired", headers=first_key)).json()
        scope = {key: desired[key] for key in ("scope", "vpn_operation_id")}
        assert desired["state"] == "requested" and not desired["can_probe"]
        assert (await http.get(edge + "/desired", headers=first_host)).status_code == 401

        async def post(url, body, headers=first_key):
            response = await http.post(url, headers=headers, json=body)
            assert response.status_code == 200, response.text
            return response.json()

        assert (await post(edge + "/claim", scope))["launch_allowed"]
        assert not (await post(edge + "/claim", scope))["launch_allowed"]
        left = {"public_key": "A" * 43 + "=", "ifindex": 4}
        right = {"public_key": "B" * 43 + "=", "ifindex": 6}
        tunnel = {
            "pid": 10,
            "start_ticks": 20,
            "tun_ifindex": 7,
            "tun_ipv4": profile["tun_ipv4"],
            "link": left,
        }
        await post(edge + "/observe", {**scope, "phase": "link", "link": left})
        await post(host + "/link", {**scope, "link": right}, first_host)

        async def apply(mode):
            await post(edge + "/observe", {**scope, "phase": "tunnel", "tunnel": tunnel})
            issued = await post(edge + "/lease", scope)
            assert issued["mode"] == mode
            if mode == "probe":
                assert not (await http.get(host, headers=first_host)).json()["can_probe"]
                excessive = await http.post(
                    edge + "/observe",
                    headers=first_key,
                    json={
                        **scope,
                        "phase": "tunnel",
                        "tunnel": tunnel,
                        "lease_revision": issued["grant_revision"],
                        "applied_until_ms": issued["issued_at_ms"] + 11000,
                    },
                )
                assert excessive.status_code == 409
            await post(
                edge + "/observe",
                {
                    **scope,
                    "phase": "tunnel",
                    "tunnel": tunnel,
                    "lease_revision": issued["grant_revision"],
                    "applied_until_ms": issued["issued_at_ms"] + 10000,
                },
            )

        await apply("probe")
        assert (await http.get(host, headers=first_host)).json()["can_probe"]
        await post(
            host + "/preflight",
            {
                **scope,
                "link": right,
                "tunnel": tunnel,
                "ipv4": "10.31.0.5",
                "port": 8000,
                "passed": True,
            },
            first_host,
        )
        await apply("solver")
        status = (await tool(first, VPN_STATUS, {}, "status-ready"))["structuredContent"]["result"]
        assert status["state"] == "ready" and status["solver_exec"]

        pause_id = str(uuid4())

        async def phase(value):
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                task = (
                    (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid)))
                    .mappings()
                    .one()
                )
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(
                        ctf_control={
                            **task["ctf_control"],
                            "native_pause": {"pause_id": pause_id, "state": value},
                        },
                    )
                )

        await phase("paused")
        held = (await http.get(edge + "/desired", headers=first_key)).json()
        assert held["state"] == "held" and held["pause_id"] == pause_id
        assert (await http.post(edge + "/lease", headers=first_key, json=scope)).status_code == 409
        async with ctf.repo.engine.connect() as conn:
            record = await bootstrap._get(conn, tid)
            with pytest.raises(HTTPException, match="pause is not confirmed"):
                require_vpn_resume(record, pause_id)
        await post(
            edge + "/observe",
            {
                **scope,
                "phase": "held",
                "pause_id": pause_id,
                "forwarding_closed": True,
                "tunnel": tunnel,
            },
        )
        async with ctf.repo.engine.connect() as conn:
            require_vpn_resume(await bootstrap._get(conn, tid), pause_id)
        await phase("resuming")
        assert (await http.get(edge + "/desired", headers=first_key)).json()[
            "resume_pause_id"
        ] == pause_id
        await apply("solver")
        await phase("resumed")
        # An observed loss is terminal for this generation and retains the deployment slot.
        await post(edge + "/observe", {**scope, "phase": "unknown"})
        assert (await http.post(edge + "/lease", headers=first_key, json=scope)).status_code == 409
        assert (await tool(second, VPN_CONFIGURE, args, "still-occupied"))["isError"]
        done = await tool(
            first, VPN_DISCONNECT, {"operation_id": scope["vpn_operation_id"]}, "disconnect"
        )
        assert done["structuredContent"]["result"]["credential_reserved"]
        await post(edge + "/observe", {**scope, "phase": "closed", "forwarding_closed": True})
        assert (await tool(second, VPN_CONFIGURE, args, "after-disconnect"))["isError"]
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            record = await bootstrap._get(conn, tid)
            exit_proof = {
                "runner_terminated": True,
                "sandbox_terminated": True,
                "pvcs_retained": True,
                "egress_terminated": True,
            }
            with pytest.raises(HTTPException, match="egress container exit"):
                await vpn_exit_data(conn, record, {**exit_proof, "egress_container_id": "wrong"})
            released = await vpn_exit_data(
                conn,
                record,
                {
                    **exit_proof,
                    "egress_container_id": scope["scope"]["egress_container_id"],
                },
            )
            await bootstrap._set(conn, tid, data=released)
        assert not (await tool(second, VPN_CONFIGURE, args, "after-original-exit"))["isError"]
