"""Real PostgreSQL admission and saved command records; SDK identities are synthetic here."""

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.store import schema as s
from bbx_contracts.sandbox_tools import SANDBOX_TOOLS
from runner_gateway_support import cap, invocation, prepare, worker_scope
from sqlalchemy import insert, select, update
from test_board_flags_integration import Objects
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_runner_control_integration import (
    SERVICE,
    acquire,
    confirm,
    confirmation,
    connect,
    control,
    cursor,
    empty_cursor,
    mgmt,
    register_owner,
)

pytestmark = pytest.mark.integration


async def test_original_sandbox_dispatch_and_late_records(ctf):
    ctf.objects = Objects()
    async with connect(ctf) as (app, http):
        lead = await prepare(http, ctf, tools=list(SANDBOX_TOOLS), native=False)
        await acquire(http, lead["tid"], lead["owner"], lead["binding"], lead["command"])
        previous = empty_cursor()
        first = cursor(previous, "lead-journal")
        confirmed = confirmation(lead["owner"], lead["binding"], previous, first)
        await confirm(http, lead["tid"], lead["owner"], lead["binding"], confirmed)
        lead.update(sdk=confirmed["sdk_session_id"], journal=first)
        worker = await worker_scope(http, ctf, lead, tools=list(SANDBOX_TOOLS))
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
                request={},
                config={
                    "runner_profile": "node-sandbox-host-v1",
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
                data={"owner_epoch": owner["epoch"]},
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
        assert material["inbound_key"] != material["report_key"]
        report = {"x-bbx-sandbox-report": material["report_key"]}
        agent = {**cap(lead, "mcp"), **report, "x-bbx-sdk-session-id": lead["sdk"]}
        path = f"/api/tasks/{tid}/sandbox-host"
        reads = f"/api/tasks/{tid}/sandbox/commands"
        user = {"Authorization": "Bearer " + issue_user_token(app.state.settings, "test")}
        tool = control(tid) + f"/bindings/{lead['binding']['binding_id']}/tools/admit"
        arguments = {"command": "printf hello", "timeout_seconds": 300}
        original = invocation(lead, "exec_command", arguments)
        iid = original["invocation_id"]
        request = {"invocation": original, "name": "exec_command", "arguments": arguments}
        admitted = await http.post(tool, headers=cap(lead, "mcp"), json=original)
        assert admitted.status_code == 200, admitted.text
        assert (
            await http.post(
                path + "/claim",
                headers={**agent, "x-bbx-sandbox-report": material["inbound_key"]},
                json=request,
            )
        ).status_code == 401
        claim = await http.post(path + "/claim", headers=agent, json=request)
        assert claim.status_code == 200, claim.text
        assert claim.json()["arguments"]["timeout_seconds"] == 45
        assert claim.json()["binding"]["role"] == "lead"

        async def event(event, **payload):
            return await http.post(
                f"{path}/calls/{iid}/events",
                headers=report,
                json={"event": event, "payload": payload},
            )

        assert (await event("execution_bound", execution_id="actual-original")).status_code == 403
        assert (await http.post(path + "/admit", headers=agent, json=request)).json()["admitted"]
        assert (await http.post(path + "/admit", headers=agent, json=request)).status_code == 409
        assert (await event("execution_bound", execution_id="actual-original")).status_code == 200
        assert (await event("output", execution_id="different")).status_code == 409
        output = await event(
            "output",
            execution_id="actual-original",
            stream="stdout",
            offset=0,
            size=5,
            data=base64.b64encode(b"hello").decode(),
        )
        assert output.status_code == 200, output.text
        assert (await event("unknown", reason="fixture_eof")).status_code == 200
        again = await http.post(tool, headers=cap(lead, "mcp"), json=original)
        assert again.json()["disposition"] == "unknown"
        assert (await http.post(path + "/claim", headers=agent, json=request)).status_code == 409
        status_args = {"command_id": iid}
        status_call = invocation(lead, "execution_status", status_args, "status")
        status_id = status_call["invocation_id"]
        status_request = {
            "invocation": status_call,
            "name": "execution_status",
            "arguments": status_args,
        }
        assert (
            await http.post(tool, headers=cap(lead, "mcp"), json=status_call)
        ).status_code == 200
        status_claim = await http.post(path + "/claim", headers=agent, json=status_request)
        assert status_claim.json()["arguments"] == {"execution_id": "actual-original"}
        assert (await http.post(path + "/admit", headers=agent, json=status_request)).json()[
            "admitted"
        ]
        observed_status = await http.post(
            f"{path}/calls/{status_id}/events",
            headers=report,
            json={
                "event": "status",
                "payload": {"execution_id": "actual-original", "running": False, "exit_code": 0},
            },
        )
        assert observed_status.status_code == 200, observed_status.text
        status_result = {"content": [{"type": "text", "text": "Official exit 0"}], "isError": False}
        assert (
            await http.post(
                f"{path}/calls/{status_id}/result", headers=report, json={"result": status_result}
            )
        ).status_code == 200
        assert (await http.post(tool, headers=cap(lead, "mcp"), json=original)).json()[
            "disposition"
        ] == "unknown"
        worker_tool = control(tid) + f"/bindings/{worker['binding']['binding_id']}/tools/admit"
        foreign = invocation(worker, "execution_status", {"command_id": iid})
        assert (
            await http.post(worker_tool, headers=cap(worker, "mcp"), json=foreign)
        ).status_code == 403
        assert (await http.get(reads, headers=cap(worker, "mcp"))).json()["items"] == []
        assert (await http.get(reads + "/" + iid, headers=cap(worker, "mcp"))).status_code == 404
        async with ctf.repo.engine.begin() as conn:
            await gateway.repo.lock(conn, tid)
            task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={**task["ctf_control"], "native_budget_hold": {"reason": "fixture"}}
                )
            )
        held = await http.get(path + "/scopes", headers=report)
        assert held.status_code == 200 and held.json() == []
        async with ctf.repo.engine.begin() as conn:
            await gateway.repo.lock(conn, tid)
            await conn.execute(
                update(s.tasks).where(s.tasks.c.id == tid).values(ctf_control=task["ctf_control"])
            )
        attach = mgmt(tid) + f"/bindings/{lead['binding']['binding_id']}/attach"
        assert (
            await http.post(attach, headers=SERVICE, json={"expected_epoch": owner["epoch"]})
        ).status_code == 200
        successor = await register_owner(http, tid, owner["epoch"])
        denied = await http.post(
            attach, headers=SERVICE, json={"expected_epoch": successor["fence"]["epoch"]}
        )
        assert denied.status_code == 409 and "another owner epoch" in denied.text
        assert (await http.post(f"/api/tasks/{tid}/stop", headers=SERVICE)).status_code == 200
        result = {
            "structuredContent": {"result": {"command_id": iid, "execution_id": "actual-original"}},
            "content": [{"type": "text", "text": "original terminal result"}],
            "isError": False,
        }
        result_path = f"{path}/calls/{iid}/result"
        assert (
            await http.post(result_path, headers=report, json={"result": result})
        ).status_code == 409
        terminal = await event(
            "returned",
            execution_id="actual-original",
            background=False,
            evidence="foreground_terminal",
            exit_code=0,
            observed_output_bytes={"stdout": 70000, "stderr": 0},
            output_truncated={"stdout": True},
        )
        assert terminal.status_code == 200, terminal.text
        assert (await event("execution_bound", execution_id="actual-original")).status_code == 200
        saved = await http.post(result_path, headers=report, json={"result": result})
        assert saved.status_code == 200 and saved.json()["result"] == result
        repeated = await http.post(result_path, headers=report, json={"result": {"changed": True}})
        assert repeated.json()["result"] == result
        page = await http.get(reads, headers=user, params={"agent_id": "lead", "limit": 1})
        assert page.status_code == 200 and page.json()["items"][0]["id"] == iid
        detail = await http.get(reads + "/" + iid, headers=user)
        assert detail.status_code == 200, detail.text
        assert detail.json()["command"] == arguments["command"]
        assert detail.json()["execution_id"] == "actual-original"
        assert detail.json()["state"] == "completed" and detail.json()["exit_code"] == 0
        assert detail.json()["stdout"] == "hello" and detail.json()["stdout_bytes"] == 70000
        assert detail.json()["output_truncated"] is True
