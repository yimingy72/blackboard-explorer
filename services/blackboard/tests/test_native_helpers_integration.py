"""Real PG: main priority, a one-shot helper, and original frozen late evidence."""

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bbx_blackboard import native_recovery
from bbx_blackboard.gateway_auth import issue_gateway_credentials
from bbx_blackboard.runner_control import row
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import BOARD_CONTRACT
from runner_gateway_support import invocation
from sqlalchemy import insert, update
from test_board_flags_integration import Objects
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_native_runner_bootstrap_integration import config
from test_runner_control_integration import (
    SERVICE,
    acknowledge,
    acquire,
    auth,
    confirm,
    confirmation,
    connect,
    control,
    cursor,
    empty_cursor,
    mgmt,
    poll_query,
    register_owner,
    registration,
)

pytestmark = pytest.mark.integration


async def test_native_helper_borrow_once_and_preserve_frozen_parent_scope(ctf, monkeypatch):
    ctf.objects = Objects()
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Resolve a parent and independent work",
            "budget": {"max_cost": "1", "max_minutes": 5},
            "ctf_options": {"dispatch_mode": "automatic", "max_teammates": 2},
        },
        business_contract=BOARD_CONTRACT,
    )
    op, stamp = str(uuid4()), datetime.now(UTC)
    parent_cid, other_cid = str(uuid4()), str(uuid4())
    async with connect(ctf) as (app, http):
        native, gateway = app.state.native_runner_bootstrap, app.state.runner_gateway
        monkeypatch.setattr(
            gateway.native,
            "_budget",
            AsyncMock(return_value={"publication_state": "ready", "hold_reason": None}),
        )
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await native._lead(conn, tid, {"operation_id": op})
            task = await row(conn, s.tasks, tid)
            control_state = task["ctf_control"]
            ready = {}
            for sequence, cid in enumerate((parent_cid, other_cid), 1):
                challenge = {
                    "id": cid,
                    "title": "Synthetic scoped question",
                    "description": "Current description",
                    "requirements": "Current requirements",
                    "revision": 1,
                    "scope_revision": 1,
                    "work_status": "pending",
                    "owner_id": None,
                    "tombstone": False,
                }
                await conn.execute(
                    insert(s.ctf_challenges).values(
                        task_id=tid,
                        id=cid,
                        revision=1,
                        data=challenge,
                    )
                )
                ready[cid] = {
                    "priority": sequence,
                    "sequence": sequence,
                    "scope_revision": 1,
                    "readiness": "no_external_materials",
                }
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    status="running",
                    active_since=stamp,
                    run_started=True,
                    ctf_control={**control_state, "phase": "running", "native_worker_ready": ready},
                )
            )
        owner = await register_owner(http, tid)
        lead = await http.post(mgmt(tid) + "/bindings", headers=SERVICE, json=registration(owner))
        assert lead.status_code == 200, lead.text
        lead_bid = lead.json()["binding"]["binding_id"]
        epoch = owner["fence"]["epoch"]
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            owner_row = await gateway.control._current_owner(conn, tid)
            old_lead = await row(conn, s.runner_bindings, tid, lead_bid)
            assert (
                not {"helper_foreground", "helper_child"}
                & json.loads(old_lead["payload_json"])["spec"].keys()
            )
            spec = {
                "model": registration(owner)["model"],
                "allowed_tools": ["exec_command", "read_file"],
                "helper_foreground": True,
                "instructions": {"member-1": "Worker", "member-2": "Worker"},
            }
            await conn.execute(
                insert(s.task_runner_resources).values(
                    task_id=tid,
                    operation_id=op,
                    request_id=op,
                    request_digest="helper-fixture",
                    request={"helper_foreground": True},
                    config={**config(), "journal_writer_lock": True},
                    controller_generation=1,
                    revision=1,
                    namespace="fixture",
                    state="ready",
                    desired="active",
                    hard_deadline=owner_row["hard_deadline"],
                    sandbox_create_claimed=True,
                    tenant_key_hash="fixture-" + op,
                    tenant_key_ciphertext="fixture",
                    next_check_at=stamp,
                    observation={
                        "runner": {
                            "pod_uid": owner_row["pod_uid"],
                            "container_id": "original-runner",
                            "host_container_id": "original-host",
                            "journal_writer_lock": True,
                        },
                        "sandbox": {
                            "id": "original-sandbox",
                            "pod_uid": "original-sandbox-pod",
                            "container_id": "original-sandbox-container",
                        },
                    },
                    sandbox_host={
                        "ready": True,
                        "context": {
                            "execution_context_id": "a" * 64,
                            "runner_pod_uid": owner_row["pod_uid"],
                            "sandbox_id": "original-sandbox",
                            "sandbox_pod_uid": "original-sandbox-pod",
                        },
                    },
                    data={
                        "activated": True,
                        "credentials_issued": True,
                        "binding_id": lead_bid,
                        "owner_epoch": epoch,
                        "worker_spec": spec,
                        "worker_binding_ids": [],
                    },
                    created_at=stamp,
                )
            )
            task = await row(conn, s.tasks, tid)
            member = await row(conn, s.ctf_members, tid, "member-1")
            await native.workers._assign(
                conn,
                tid,
                task,
                owner_row,
                spec,
                member,
                await ctf._challenge(conn, tid, parent_cid),
            )
            task = await row(conn, s.tasks, tid)
            parent_bid = task["ctf_control"]["native_worker_active"]["member-1"]["binding_id"]

        async def commands():
            reply = await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
            assert reply.status_code == 200, reply.text
            return [
                (value, json.loads(value["payload_json"])) for value in reply.json()["commands"]
            ]

        async def prepared(command, label, *, helper=False):
            binding = json.loads(command["payload_json"])["spec"]["binding"]
            await acquire(http, tid, owner, binding, command)
            tail = cursor(empty_cursor(), label)
            sdk = label + "-sdk"
            await confirm(
                http,
                tid,
                owner,
                binding,
                {
                    **confirmation(owner, binding, empty_cursor(), tail),
                    "sdk_session_id": sdk,
                },
            )
            await acknowledge(
                http,
                tid,
                owner,
                command,
                "prepared",
                binding_id=binding["binding_id"],
                sdk_session_id=sdk,
                cursor=tail,
                journal_confirmed=True,
                sdk_idle=True,
                **({"helper_run_id": "plugin-run"} if helper else {}),
            )
            return {"tid": tid, "owner": owner, "binding": binding, "sdk": sdk, "journal": tail}

        parent_command = next(
            cmd
            for cmd, payload in await commands()
            if payload.get("spec", {}).get("binding", {}).get("binding_id") == parent_bid
        )
        parent = await prepared(parent_command, "parent")
        parent_mcp = issue_gateway_credentials(
            app.state.settings, owner_row, parent_bid, "teammate"
        )["mcp"]
        headers = {"Authorization": "Bearer " + parent_mcp["token"]}
        base = control(tid) + f"/bindings/{parent_bid}/helpers"

        async def reserve(suffix):
            body = invocation(
                parent,
                "delegate_helper",
                {
                    "goal": "Check one local observation",
                    "expected_output": "A short answer",
                },
                suffix,
            )
            reply = await http.post(base + "/reserve", headers=headers, json=body)
            assert reply.status_code == 200, reply.text
            return body, reply.json()

        _, denied = await reserve("main-priority")
        assert denied["state"] == "unavailable" and denied["reason"] == "no_free_seat"
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            other = task["ctf_control"]["native_worker_active"]["member-2"]
            assert other["work_id"] == other_cid
            other_binding = await row(conn, s.runner_bindings, tid, other["binding_id"])
            assert other_binding["permit_revision"] == 0
            challenge = await ctf._challenge(conn, tid, other_cid)
            await conn.execute(
                update(s.ctf_challenges)
                .where(
                    s.ctf_challenges.c.task_id == tid,
                    s.ctf_challenges.c.id == other_cid,
                )
                .values(data={**challenge, "work_status": "completed"})
            )
            resource = await native._get(conn, tid)
            await native.workers.reconcile(conn, tid, task, resource, owner_row, allow_new=True)
        close = next(
            cmd
            for cmd, payload in await commands()
            if payload.get("kind") == "close" and payload["binding_id"] == other["binding_id"]
        )
        await acknowledge(
            http, tid, owner, close, "closed", binding_id=other["binding_id"], sdk_idle=True
        )
        first, second = await asyncio.gather(reserve("borrow"), reserve("borrow"))
        body, reserved = first
        assert reserved == second[1] and reserved["state"] == "reserved"
        assert all(
            payload.get("spec", {}).get("binding", {}).get("binding_id") != reserved["binding_id"]
            for _, payload in await commands()
        )
        path = base + "/" + body["invocation_id"]
        claims = await asyncio.gather(
            *(http.post(path + "/launch", headers=headers, json=body) for _ in range(2))
        )
        assert all(reply.status_code == 200 for reply in claims), [reply.text for reply in claims]
        allowed = [reply.json() for reply in claims if reply.json()["launch_allowed"]]
        assert len(allowed) == 1 and all(
            "credentials" not in reply.json()
            for reply in claims
            if not reply.json()["launch_allowed"]
        )
        launch = allowed[0]
        spec = json.loads(launch["command"]["payload_json"])["spec"]
        assert spec["helper_child"]["parent_binding_id"] == parent_bid
        assert set(spec["allowed_tools"]) == {"exec_command", "read_file"}
        assert "Current description" in spec["instructions"] and not spec.get("helper_foreground")
        assert spec["model"] == json.loads(parent_command["payload_json"])["spec"]["model"]
        child = await prepared(launch["command"], "child", helper=True)
        child_mcp = launch["credentials"][spec["mcp_credential_ref"]]["token"]
        child_headers = {"Authorization": "Bearer " + child_mcp}
        assert (
            launch["credentials"][spec["mcp_credential_ref"]]["expires_at"]
            <= launch["deadline_ms"] // 1000
        )
        child_admit = control(tid) + f"/bindings/{reserved['binding_id']}/tools/admit"
        for name, args in (
            ("exec_command", {"command": "printf ignored", "background": True}),
            ("board_challenges", {}),
        ):
            denied_tool = await http.post(
                child_admit,
                headers=child_headers,
                json=invocation(child, name, args, "denied"),
            )
            assert denied_tool.status_code == 403, denied_tool.text
        recursive = invocation(
            child, "delegate_helper", {"goal": "Nested", "expected_output": "No"}, "recursive"
        )
        assert (
            await http.post(
                control(tid) + f"/bindings/{reserved['binding_id']}/helpers/reserve",
                headers=child_headers,
                json=recursive,
            )
        ).status_code == 403
        assert "credentials" not in (await http.get(path, headers=auth(owner))).json()
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            record = await native._get(conn, tid)
            assert record is not None
            savepoint = await conn.begin_nested()
            await native.pause_control._start(conn, tid, task, record, owner_row, str(uuid4()))
            paused = await native._get(conn, tid)
            assert (
                paused is not None
                and reserved["binding_id"] in paused["data"]["pause_snapshot"]["binding_ids"]
            )
            await savepoint.rollback()
            data = await native_recovery.capture(
                native.repo, conn, tid, record, task, "native_resource_unknown", stamp
            )
            await native._set(conn, tid, data=data, state="frozen")
            original_children = data["recovery"]["helpers"]
        terminal = {
            "child_id": reserved["child_id"],
            "binding_id": reserved["binding_id"],
            "plugin_run_id": "plugin-run",
            "sdk_session_id": child["sdk"],
            "cursor": child["journal"],
            "sdk_idle": True,
            "disposed": True,
            "journal_confirmed": True,
        }
        assert (
            await http.post(path + "/terminal", headers=auth(owner), json=terminal)
        ).status_code == 200
        result = {
            "result_json": json.dumps(
                {"content": [{"type": "text", "text": "Original scoped result"}]}
            )
        }
        late = await http.post(path + "/result", headers=auth(owner), json=result)
        assert late.status_code == 200 and late.json()["state"] == "launch_claimed"
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            assert task["ctf_control"]["native_helpers_active"] == original_children
            assert (await ctf._challenge(conn, tid, parent_cid))["owner_id"] == "member-1"
            assert (await row(conn, s.runner_calls, tid, "tool:" + body["invocation_id"]))[
                "state"
            ] == "dispatched"
            incident = {
                **data["recovery"],
                "exit_proof": {
                    "runner_terminated": True,
                    "sandbox_terminated": True,
                    "pvcs_retained": True,
                },
                "resources": {
                    **data["recovery"]["resources"],
                    "sandbox_id": "original-sandbox",
                    "sandbox_pod_uid": "original-sandbox-pod",
                },
                "execution_context_id": "a" * 64,
            }
            command_table = s.sandbox_command_records
            command_binding = {
                "epoch": epoch,
                "sandbox_id": "original-sandbox",
                "pod_uid": "different-sandbox-pod",
                "execution_context_id": "a" * 64,
            }
            await conn.execute(
                insert(command_table).values(
                    task_id=tid,
                    id="child-command",
                    binding_id=reserved["binding_id"],
                    agent_id="member-2",
                    role="helper",
                    assignment_id=reserved["child_id"],
                    generation=child["binding"]["generation"],
                    epoch=epoch,
                    execution_id="real-id",
                    state="completed",
                    exit_code=0,
                    created_at=stamp,
                    finished_at=stamp,
                    data={"binding": command_binding, "scope_state": "active"},
                )
            )
            await native.helpers.reconcile_frozen(conn, tid, task, incident)
            assert (await row(conn, s.runner_calls, tid, "tool:" + body["invocation_id"]))[
                "state"
            ] == "dispatched"
            await conn.execute(
                update(command_table)
                .where(
                    command_table.c.task_id == tid,
                    command_table.c.id == "child-command",
                )
                .values(
                    data={
                        "binding": {**command_binding, "pod_uid": "original-sandbox-pod"},
                        "scope_state": "active",
                    }
                )
            )
            await native.helpers.reconcile_frozen(conn, tid, task, incident)
            call = await row(conn, s.runner_calls, tid, "tool:" + body["invocation_id"])
            assert (
                call["state"] == "completed"
                and "Original scoped result" in native.helpers._status(call)["result_json"]
            )
            assert (await row(conn, s.tasks, tid))["ctf_control"][
                "native_helpers_active"
            ] == original_children
