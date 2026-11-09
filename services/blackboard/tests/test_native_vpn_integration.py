"""Real PG Task locks and original Runner MCP receipts for one shared VPN slot."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import runner_gateway_support as support
from bbx_blackboard.gateway_auth import require_gateway_identity
from bbx_blackboard.native_vpn import (
    default_network,
    require_vpn_resume,
    vpn_exit_data,
    vpn_selected,
    vpn_solver_deadline,
)
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import business_manifest
from bbx_contracts.runner_vpn import (
    NETWORK_USE_DEFAULT,
    VPN_CONFIGURE,
    VPN_DISCONNECT,
    VPN_DYNAMIC_PROFILE,
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


def fixture_owner(monkeypatch):
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


async def task_fixture_with_profile(app, http, ctf, runtime, profile):
    dynamic = profile["profile"] == VPN_DYNAMIC_PROFILE
    tools = [*VPN_TOOLS, "exec_command"]
    if dynamic:
        tools.append(NETWORK_USE_DEFAULT)
    lead = await support.prepare(http, ctf, tools=tools)
    gateway = app.state.runner_gateway
    bootstrap = app.state.native_runner_bootstrap
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
        "traffic_targets": [] if dynamic else [{"ipv4": "10.31.0.5", "port": 8000}],
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
            "publication": {"vpn_profile": profile["profile"]},
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
        "data": {
            "owner_epoch": 1,
            "vpn_profile": profile["profile"],
            "vpn": {"state": "idle"},
            **({"native_network": "unselected"} if dynamic else {}),
        },
        "sandbox_host": {},
        "next_check_at": stamp,
        "created_at": stamp,
    }
    async with ctf.repo.engine.begin() as conn:
        await ctf.repo.lock(conn, tid)
        task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **task["ctf_control"],
                    "business_tools": business_manifest(vpn=True, task_network=dynamic),
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


async def saved_tool(app, http, lead, name, args, suffix):
    gateway = app.state.runner_gateway
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


async def test_original_vpn_authority_and_deployment_slot(ctf, ctf_url, monkeypatch):
    ctf.objects = Objects()

    fixture_owner(monkeypatch)
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
        bootstrap = app.state.native_runner_bootstrap

        async def task_fixture():
            return await task_fixture_with_profile(app, http, ctf, runtime, profile)

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
            return await saved_tool(app, http, lead, name, args, suffix)

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
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, second["tid"])
            second_record = await bootstrap._get(conn, second["tid"])
            second_released = await vpn_exit_data(
                conn,
                second_record,
                {
                    **exit_proof,
                    "egress_container_id": second_record["observation"]["runner"][
                        "egress_container_id"
                    ],
                },
            )
            await bootstrap._set(conn, second["tid"], data=second_released)


def task_openvpn_file():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "isolated-config-client")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
        .sign(key, hashes.SHA256())
    ).public_bytes(serialization.Encoding.PEM)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return (
        b"client\ndev tun\nproto udp\nremote 10.14.0.5 1194\n"
        b"<ca>\n"
        + certificate
        + b"</ca>\n<cert>\n"
        + certificate
        + b"</cert>\n<key>\n"
        + pem
        + b"</key>\n<tls-crypt>\n# generated by the fixture\n"
        + b"-----BEGIN OpenVPN Static key V1-----\n"
        + (b"3" * 32 + b"\n") * 16
        + b"-----END OpenVPN Static key V1-----\n</tls-crypt>\n"
    )


async def test_task_chooses_once_and_both_paths_own_distinct_policy(ctf, ctf_url, monkeypatch):
    import hashlib

    from bbx_blackboard.input_groups import InputGroups

    ctf.objects = Objects()
    fixture_owner(monkeypatch)
    profile = {
        "profile": VPN_DYNAMIC_PROFILE,
        "id": "fixture-task-network",
        "label": "Task-selected network",
        "egress_image": "fixture-egress@sha256:" + "f" * 64,
        "sandbox_image": "fixture-vpn-sandbox@sha256:" + "e" * 64,
        "tun_resource": "devic.es/tun",
        "peer_cidrs": ["10.14.0.0/24"],
        "default_cidrs": ["10.12.0.0/24"],
        "target_cidrs": ["10.31.0.0/24"],
        "deny_cidrs": ["10.15.0.0/24", "10.31.0.128/25"],
    }
    async with (
        connect(ctf) as (app, http),
        RuntimeLock(runtime_settings(ctf_url), fenced=True) as runtime,
    ):
        bootstrap = app.state.native_runner_bootstrap
        first, first_key, first_host, _ = await task_fixture_with_profile(
            app, http, ctf, runtime, profile
        )
        second, second_key, second_host, _ = await task_fixture_with_profile(
            app, http, ctf, runtime, profile
        )

        async def read(tid):
            async with ctf.repo.engine.connect() as conn:
                return await bootstrap._get(conn, tid)

        async def action(tid, name, proof):
            record = await read(tid)
            return await http.post(
                f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap/actions",
                headers=SERVICE,
                json={
                    "action": name,
                    "operation_id": record["operation_id"],
                    "controller_id": "fixture-controller",
                    "controller_generation": runtime.fence,
                    "proof": proof,
                },
            )

        tid = first["tid"]
        denied = await saved_tool(app, http, first, "exec_command", {"command": "never"}, "pre")
        assert denied["isError"]
        second_tid = second["tid"]
        group = await InputGroups(ctf.repo.engine, ctf.objects).create("fixture")
        raw = task_openvpn_file()
        private_id = uuid4()
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                insert(s.native_vpn_assets).values(
                    id=private_id,
                    group_id=group["id"],
                    task_id=second_tid,
                    digest=hashlib.sha256(raw).hexdigest(),
                    ciphertext=bootstrap.vpn.asset_cipher.encrypt(raw).decode(),
                )
            )
        args = {"source_id": str(private_id), "preflight": {"ipv4": "10.31.0.5", "port": 18888}}

        wrong_task = await saved_tool(app, http, first, VPN_CONFIGURE, args, "wrong-task-source")
        assert wrong_task["structuredContent"]["result"] == {
            "error": "Private VPN source is not available for this Task"
        }
        assert (await read(tid))["data"]["native_network"] == "unselected"
        default = await saved_tool(app, http, first, NETWORK_USE_DEFAULT, {}, "default")
        assert default["structuredContent"]["result"]["state"] == "preparing"
        assert await saved_tool(app, http, first, NETWORK_USE_DEFAULT, {}, "default") == default
        selected = await read(tid)
        assert not vpn_selected(selected)
        assert selected["data"]["vpn"]["state"] == "idle"
        with pytest.raises(HTTPException, match="not ready"):
            vpn_solver_deadline(selected)
        fields = {
            "network_digest": selected["data"]["network_digest"],
            "policy_name": "bbx-binding-direct",
            "policy_uid": str(uuid4()),
        }
        assert (
            await action(tid, "network_default_ready", {**fields, "network_digest": "1" * 64})
        ).status_code == 409
        ready = await action(tid, "network_default_ready", fields)
        assert ready.status_code == 200, ready.text
        selected = await read(tid)
        assert default_network(selected) and vpn_solver_deadline(selected) is None
        assert bootstrap.vpn.status_projection(selected).model_dump() == {
            "mode": "default",
            "state": "ready",
        }
        first_edge = f"/api/tasks/{tid}/runner-vpn"
        first_desired = (await http.get(first_edge + "/desired", headers=first_key)).json()
        assert first_desired["state"] == "idle" and first_desired["vpn_operation_id"] is None
        host_desired = (
            await http.get(f"/api/tasks/{tid}/sandbox-host/vpn", headers=first_host)
        ).json()
        assert host_desired["default_network"] == default_network(selected)
        assert not selected["data"]["vpn"].get("credential_reserved")
        require_vpn_resume(selected, str(uuid4()))
        stop_proof = {
            "runner_terminated": True,
            "sandbox_terminated": True,
            "pvcs_retained": True,
            "egress_terminated": True,
            "egress_container_id": selected["observation"]["runner"]["egress_container_id"],
        }
        async with ctf.repo.engine.begin() as conn:
            with pytest.raises(HTTPException, match="exit is not confirmed"):
                await vpn_exit_data(conn, selected, {**stop_proof, "egress_terminated": False})
            assert (await vpn_exit_data(conn, selected, stop_proof))["vpn"]["egress_exit_confirmed"]

        opposite = await saved_tool(app, http, first, VPN_CONFIGURE, args, "cannot-switch")
        assert opposite["isError"]
        bad_probe = await saved_tool(
            app,
            http,
            second,
            VPN_CONFIGURE,
            {**args, "preflight": {"ipv4": "10.31.0.150", "port": 18888}},
            "denied-probe",
        )
        assert bad_probe["isError"]
        configured = await saved_tool(app, http, second, VPN_CONFIGURE, args, "source")
        assert not configured["isError"], configured
        accepted = configured["structuredContent"]["result"]
        assert accepted["state"] == "source_pending" and accepted["credential_reserved"]
        assert (await saved_tool(app, http, second, NETWORK_USE_DEFAULT, {}, "cannot-default"))[
            "isError"
        ]
        acquired = await action(
            second_tid, "vpn_prepare_source", {"vpn_operation_id": accepted["operation_id"]}
        )
        assert acquired.status_code == 200, acquired.text
        await asyncio.gather(*bootstrap.vpn.source_jobs)
        record = await read(second_tid)
        vpn = record["data"]["vpn"]
        assert vpn["state"] == "awaiting_policy"
        network = vpn["network"]
        assert network["peer"]["ipv4"] == "10.14.0.5"
        assert network["route_policy"]["allow_cidrs"] == ["10.31.0.0/24"]
        assert "10.31.0.128/25" in network["route_policy"]["deny_cidrs"]
        assert "targets" not in network and "PRIVATE KEY" not in json.dumps(record["data"])
        edge = f"/api/tasks/{second_tid}/runner-vpn"
        desired = (await http.get(edge + "/desired", headers=second_key)).json()
        operation = {
            "scope": desired["scope"],
            "vpn_operation_id": desired["vpn_operation_id"],
            "network_digest": network["network_digest"],
        }
        assert not (await http.post(edge + "/claim", headers=second_key, json=operation)).json()[
            "launch_allowed"
        ]
        policy = await action(
            second_tid,
            "vpn_peer_ready",
            {
                "vpn_operation_id": accepted["operation_id"],
                "network_digest": network["network_digest"],
                "policy_name": "bbx-vpn-peer",
                "policy_uid": str(uuid4()),
            },
        )
        assert policy.status_code == 200, policy.text
        assert (
            await http.post(edge + "/claim", headers=second_host, json=operation)
        ).status_code == 401
        claim = (await http.post(edge + "/claim", headers=second_key, json=operation)).json()
        assert claim["launch_allowed"] and "PRIVATE KEY" in claim["material"]["key"]
        duplicate = (await http.post(edge + "/claim", headers=second_key, json=operation)).json()
        assert duplicate == {"launch_allowed": False, "state": "starting"}
