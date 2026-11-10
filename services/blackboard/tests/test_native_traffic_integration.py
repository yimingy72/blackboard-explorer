"""Original tool admission and immutable per-binding archives against PostgreSQL."""

import hashlib
import struct
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.native_traffic import PROFILE, close_archive_for_purge, traffic_purge_guard
from bbx_blackboard.store import schema as s
from bbx_contracts.sandbox_tools import SANDBOX_TOOLS
from fastapi import HTTPException
from runner_gateway_support import cap, invocation, prepare
from sqlalchemy import insert, update
from test_board_flags_integration import Objects
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_runner_control_integration import (
    acquire,
    confirm,
    confirmation,
    connect,
    control,
    cursor,
    empty_cursor,
)

pytestmark = pytest.mark.integration


async def test_original_network_admit_seal_and_durable_upload(ctf):
    ctf.objects = Objects()
    async with connect(ctf) as (app, http):
        lead = await prepare(http, ctf, tools=list(SANDBOX_TOOLS), native=False)
        await acquire(http, lead["tid"], lead["owner"], lead["binding"], lead["command"])
        previous = empty_cursor()
        first = cursor(previous, "lead-journal")
        confirmed = confirmation(lead["owner"], lead["binding"], previous, first)
        await confirm(http, lead["tid"], lead["owner"], lead["binding"], confirmed)
        lead.update(sdk=confirmed["sdk_session_id"], journal=first)
        tid = lead["tid"]
        gateway = app.state.runner_gateway
        stamp = datetime.now(UTC)
        owner = lead["owner"]["fence"]
        operation = str(uuid4())
        context = {
            "sandbox_id": str(uuid4()),
            "sandbox_pod_uid": str(uuid4()),
            "environment_revision": "synthetic-original",
            "execution_context_id": "a" * 64,
            "execd_endpoint": "sandbox.internal:44772",
            "host_url": "http://10.1.0.2:8766",
        }
        # Projection fixture only; the separate Kubernetes seam obtains these identities natively.
        async with ctf.repo.engine.begin() as conn:
            await gateway.repo.lock(conn, tid)
            resource = dict(
                task_id=tid,
                operation_id=operation,
                request_id=operation,
                request_digest="fixture",
                request={"traffic_profile": PROFILE},
                config={
                    "runner_profile": "node-sandbox-host-v1",
                    "traffic_targets": [{"ipv4": "10.12.0.5", "port": 8000}],
                    "environment_revision": context["environment_revision"],
                    "runner_blackboard_url": "https://blackboard.invalid",
                    "sandbox_network": {
                        "namespace": "worker-a",
                        "network_identity": "worker-a",
                        "ipv4": "198.19.1.2",
                    },
                },
                controller_generation=1,
                revision=1,
                namespace="fixture-" + str(tid),
                state="active",
                desired="active",
                hard_deadline=stamp + timedelta(minutes=5),
                sandbox_create_claimed=True,
                tenant_key_hash="fixture-" + operation,
                tenant_key_ciphertext="fixture",
                observation={
                    "sandbox": {"id": context["sandbox_id"], "pod_uid": context["sandbox_pod_uid"]},
                    "runner": {"pod_uid": owner["pod_uid"], "pod_ip": "10.1.0.2"},
                },
                data={
                    "owner_epoch": owner["epoch"],
                    "traffic_profile": PROFILE,
                    "mcp_transport": "direct-v2",
                },
                sandbox_host={},
                next_check_at=stamp,
                created_at=stamp,
            )
            await conn.execute(insert(s.task_runner_resources).values(**resource))
            material = await gateway.sandbox.host_material(conn, tid, resource, context)
            row = await gateway.sandbox.resource(conn, tid)
            await conn.execute(
                update(s.task_runner_resources)
                .where(s.task_runner_resources.c.task_id == tid)
                .values(sandbox_host={**row["sandbox_host"], "ready": True})
            )

        report = {"x-bbx-sandbox-report": material["report_key"]}
        agent = {**cap(lead, "sandbox"), **report, "x-bbx-sdk-session-id": lead["sdk"]}
        path = f"/api/tasks/{tid}/sandbox-host"
        tool = control(tid) + f"/bindings/{lead['binding']['binding_id']}/tools/admit"
        arguments = {"command": "printf hello"}
        original = invocation(lead, "exec_command", arguments)
        body = {"invocation": original, "name": "exec_command", "arguments": arguments}
        response = await http.post(tool, headers=agent, json=original)
        assert response.status_code == 200, response.text
        response = await http.post(path + "/claim", headers=agent, json=body)
        assert response.status_code == 200, response.text
        network = response.json()["network"]
        scope = network["scope"]
        assert network["prepare_allowed"] is True
        assert scope["binding_id"] == lead["binding"]["binding_id"]
        assert scope["role"] == "lead"
        assert scope["targets"] == [{"ipv4": "10.12.0.5", "port": 8000}]
        assert (await http.post(path + "/admit", headers=agent, json=body)).status_code == 409
        base = path + "/network/" + scope["source_id"]
        identity = {
            "netns_inode": "123",
            "host_ifindex": 42,
            "capture_pid": 43,
            "capture_start_ticks": "44",
        }
        response = await http.post(base + "/observe", headers=report, json=identity)
        assert response.status_code == 200, response.text
        response = await http.post(base + "/permit", headers=report)
        assert response.status_code == 200, response.text
        body["network_grant_revision"] = response.json()["grant_revision"]
        response = await http.post(path + "/admit", headers=agent, json=body)
        assert response.status_code == 200, response.text
        assert (await http.post(path + "/admit", headers=agent, json=body)).status_code == 409

        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.task_runner_resources)
                .where(s.task_runner_resources.c.task_id == tid)
                .values(desired="stopped")
            )
        assert (await http.post(base + "/permit", headers=report)).status_code == 409
        response = await http.get(path + "/network", headers=report)
        assert response.status_code == 200, response.text
        assert response.json()["scopes"][0]["can_seal"] is True

        pcap = (
            struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 512, 1)
            + struct.pack("<IIII", 1, 0, 60, 60)
            + bytes(60)
        )
        seal = {
            **identity,
            "size_bytes": len(pcap),
            "packet_count": 1,
            "sha256": hashlib.sha256(pcap).hexdigest(),
            "complete": True,
            "dropped_packets": 0,
            "truncated_packets": 0,
            "discarded_tail_bytes": 0,
        }
        response = await http.post(base + "/seal", headers=report, json=seal)
        assert response.status_code == 200, response.text
        assert (
            await http.post(base + "/bytes", headers=report, content=pcap[:-1])
        ).status_code == 409
        response = await http.post(base + "/bytes", headers=report, content=pcap)
        assert response.status_code == 200, response.text
        assert response.json() == {"archive_confirmed": True}
        assert sum(key.endswith(".bin") for key in ctf.objects.values) == 1
        assert sum(key.endswith(".json") for key in ctf.objects.values) == 1

        user = {"Authorization": "Bearer " + issue_user_token(app.state.settings, "test")}
        traffic = f"/api/tasks/{tid}/traffic"
        page = await http.get(traffic, headers=user)
        assert page.status_code == 200, page.text
        assert page.json()["native_sources"][0]["scope"]["binding_id"] == scope["binding_id"]
        download = traffic + "/files/" + scope["source_id"]
        assert (await http.get(download)).status_code == 401
        archived = await http.get(download, headers=user)
        assert archived.status_code == 200 and archived.content == pcap, archived.text

        # Equal bytes do not prove an interrupted writer has no PUT still in flight.
        async with ctf.repo.engine.begin() as conn:
            resource = await gateway.sandbox.resource(conn, tid)
            data = resource["data"]
            scopes = data["network_scopes"]
            entry = scopes[scope["source_id"]]
            entry["state"] = "sealed"
            entry["writes"]["part"] = "claimed"
            await conn.execute(
                update(s.task_runner_resources)
                .where(s.task_runner_resources.c.task_id == tid)
                .values(
                    state="stopped",
                    data={**data, "exit_confirmed": True, "retirement": {"state": "retired"}},
                )
            )
            await conn.execute(update(s.tasks).where(s.tasks.c.id == tid).values(deleting=True))
        assert (await http.post(base + "/bytes", headers=report, content=pcap)).status_code == 409
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            assert await close_archive_for_purge(conn, await gateway.sandbox.resource(conn, tid))
        with pytest.raises(HTTPException, match="Original capture object write is not confirmed"):
            async with traffic_purge_guard(ctf.repo, tid, protect=True):
                pytest.fail("Unconfirmed writer permitted a destructive purge")
