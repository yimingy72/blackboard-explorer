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
from bbx_contracts.runner import PROTOCOL, ToolResultReceipt
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


@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
async def test_native_helper_borrow_once_and_preserve_frozen_parent_scope(
    ctf, monkeypatch, background
):
    ctf.objects = Objects()
    capability = "helper_background" if background else "helper_foreground"
    delegate = "delegate_helper_background" if background else "delegate_helper"
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
                not {"helper_foreground", "helper_background", "helper_child"}
                & json.loads(old_lead["payload_json"])["spec"].keys()
            )
            spec = {
                "model": registration(owner)["model"],
                "allowed_tools": ["exec_command", "read_file"],
                capability: True,
                "instructions": {"member-1": "Worker", "member-2": "Worker"},
            }
            await conn.execute(
                insert(s.task_runner_resources).values(
                    task_id=tid,
                    operation_id=op,
                    request_id=op,
                    request_digest="helper-fixture",
                    request={capability: True},
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
                delegate,
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
        assert "helper_background" not in spec
        accepted = None
        if background:
            assert spec["helper_child"]["mode"] == "background"
            assert (
                launch["deadline_ms"]
                < launch["child_deadline_ms"]
                == spec["helper_child"]["deadline_ms"]
            )
            cmd = launch["command"]
            denied_lease = await http.post(
                control(tid) + f"/bindings/{reserved['binding_id']}/acquire",
                headers=auth(owner),
                json={
                    "protocol": PROTOCOL,
                    "request_id": cmd["request_id"],
                    "payload_digest": cmd["payload_digest"],
                    "fence": cmd["fence"],
                    "binding": spec["binding"],
                },
            )
            assert denied_lease.status_code == 409 and "not accepted" in denied_lease.text
            acceptance = {
                "child_id": reserved["child_id"],
                "binding_id": reserved["binding_id"],
                "payload_digest": cmd["payload_digest"],
            }
            accepted_http = await http.post(
                path + "/accepted", headers=auth(owner), json=acceptance
            )
            assert accepted_http.status_code == 200, accepted_http.text
            accepted = accepted_http.json()
            assert accepted["state"] == "accepted" and accepted["background_state"] == "scheduled"
            assert (
                await http.post(path + "/accepted", headers=auth(owner), json=acceptance)
            ).json() == accepted
            async with ctf.repo.engine.connect() as conn:
                assert not await gateway._count(
                    conn,
                    s.runner_calls.c.task_id == tid,
                    s.runner_calls.c.binding_id == parent_bid,
                    gateway._unresolved(),
                )
        else:
            assert "mode" not in spec["helper_child"]
        assert set(spec["allowed_tools"]) == {"exec_command", "read_file"}
        assert "Current description" in spec["instructions"] and not spec.get("helper_foreground")
        assert spec["model"] == json.loads(parent_command["payload_json"])["spec"]["model"]
        child = await prepared(launch["command"], "child", helper=True)
        child_mcp = launch["credentials"][spec["mcp_credential_ref"]]["token"]
        child_headers = {"Authorization": "Bearer " + child_mcp}
        assert (
            launch["credentials"][spec["mcp_credential_ref"]]["expires_at"]
            <= (launch["child_deadline_ms"] if background else launch["deadline_ms"]) // 1000
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
        if background:
            assert accepted is not None
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                task = await row(conn, s.tasks, tid)
                children = task["ctf_control"]["native_helpers_active"]
                assert await native.helpers.unresolved_background(conn, tid, children)
                resource = await native._get(conn, tid)
                assert resource
                savepoint = await conn.begin_nested()
                await native.pause_control._start(
                    conn, tid, task, resource, owner_row, str(uuid4())
                )
                held = await row(conn, s.tasks, tid)
                paused = await native._get(conn, tid)
                assert paused
                assert (
                    await native.pause_control._blocker(
                        conn, tid, held, paused["data"]["pause_snapshot"]
                    )
                    == "original_background_helper_unresolved"
                )
                await savepoint.rollback()
            target = {"child_run_id": reserved["child_id"]}

            async def parent_tool(name, suffix):
                action = "cancel" if name == "cancel_helper" else "status"
                value = invocation(parent, name, target, suffix)
                reply = await http.post(base + "/" + action, headers=headers, json=value)
                assert reply.status_code == 200, reply.text
                saved = ToolResultReceipt.model_validate(reply.json())
                assert saved.invocation_id == value["invocation_id"]
                return saved

            running = await parent_tool("background_helper_status", "running")
            assert (
                json.loads(json.loads(running.result_json)["content"][0]["text"])[
                    "background_state"
                ]
                == "running"
            )
            cancel = await parent_tool("cancel_helper", "cancel")
            assert (
                json.loads(json.loads(cancel.result_json)["content"][0]["text"])["state"]
                == "cancellation_requested"
            )
            assert await parent_tool("cancel_helper", "cancel") == cancel
            close = next(
                cmd
                for cmd, payload in await commands()
                if payload.get("kind") == "close"
                and payload["binding_id"] == reserved["binding_id"]
            )
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
            term = await http.post(path + "/terminal", headers=auth(owner), json=terminal)
            assert term.status_code == 200, term.text
            await acknowledge(
                http,
                tid,
                owner,
                close,
                "closed",
                binding_id=reserved["binding_id"],
                sdk_session_id=child["sdk"],
                cursor=child["journal"],
                journal_confirmed=True,
                sdk_idle=True,
            )
            result = {
                "result_json": json.dumps(
                    {
                        "content": [{"type": "text", "text": "Original child cancelled"}],
                        "isError": True,
                    }
                )
            }
            final = await http.post(path + "/result", headers=auth(owner), json=result)
            assert final.status_code == 200, final.text
            finished = final.json()
            assert (
                finished["state"] == "accepted"
                and finished["result_json"] == accepted["result_json"]
            )
            assert finished["background_state"] == "finished" and finished["delivery"] == "queued"
            assert "Original child cancelled" in finished["outcome_json"]
            assert (
                await http.post(path + "/result", headers=auth(owner), json=result)
            ).json() == finished
            [notice] = [
                payload
                for _, payload in await commands()
                if payload.get("kind") == "input"
                and "Original child cancelled" in payload["content"]
            ]
            assert notice["binding_id"] == parent_bid and notice["mode"] == "follow_up"
            next_cursor = cursor(parent["journal"], "background-result")
            await confirm(
                http,
                tid,
                owner,
                parent["binding"],
                {
                    **confirmation(
                        owner,
                        parent["binding"],
                        parent["journal"],
                        next_cursor,
                        [
                            {
                                "input_id": notice["input_id"],
                                "entry_id": next_cursor["leaf_id"],
                                "seq": next_cursor["seq"],
                            }
                        ],
                    ),
                    "sdk_session_id": parent["sdk"],
                },
            )
            assert (await http.get(path, headers=auth(owner))).json()["delivery"] == "applied"
            async with ctf.repo.engine.connect() as conn:
                task = await row(conn, s.tasks, tid)
                assert not task["ctf_control"]["native_helpers_active"]
                assert (
                    task["ctf_control"]["native_worker_active"]["member-1"]["binding_id"]
                    == parent_bid
                )
            parent["journal"] = next_cursor
            late_body, late_child = await reserve("late-while-frozen")
            late_path = base + "/" + late_body["invocation_id"]
            late_grant = await http.post(late_path + "/launch", headers=headers, json=late_body)
            assert late_grant.status_code == 200, late_grant.text
            late_command = late_grant.json()["command"]
            late_accept = await http.post(
                late_path + "/accepted",
                headers=auth(owner),
                json={
                    "child_id": late_child["child_id"],
                    "binding_id": late_child["binding_id"],
                    "payload_digest": late_command["payload_digest"],
                },
            )
            assert late_accept.status_code == 200, late_accept.text
            late_sdk = await prepared(late_command, "late-child", helper=True)
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                task = await row(conn, s.tasks, tid)
                resource = await native._get(conn, tid)
                assert resource
                data = await native_recovery.capture(
                    native.repo, conn, tid, resource, task, "native_resource_unknown", stamp
                )
                await native._set(conn, tid, data=data, state="frozen")
                frozen_children = data["recovery"]["helpers"]
            terminal.update(
                child_id=late_child["child_id"],
                binding_id=late_child["binding_id"],
                sdk_session_id=late_sdk["sdk"],
                cursor=late_sdk["journal"],
            )
            assert (
                await http.post(late_path + "/terminal", headers=auth(owner), json=terminal)
            ).status_code == 200
            late_result = await http.post(late_path + "/result", headers=auth(owner), json=result)
            assert late_result.status_code == 200 and late_result.json()["outcome_json"] is None
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                task = await row(conn, s.tasks, tid)
                assert await native.helpers.unresolved_background(conn, tid, frozen_children)
                assert task["ctf_control"]["native_helpers_active"] == frozen_children
                incident = {
                    **data["recovery"],
                    "exit_proof": {
                        "runner_terminated": True,
                        "sandbox_terminated": True,
                        "pvcs_retained": True,
                    },
                }
                await native.helpers.reconcile_frozen(conn, tid, task, incident)
                assert not await native.helpers.unresolved_background(conn, tid, frozen_children)
                current = await row(conn, s.tasks, tid)
                assert current["ctf_control"]["native_helpers_active"] == frozen_children
            late_status = (await http.get(late_path, headers=auth(owner))).json()
            assert late_status["delivery"] == "historical"
            assert late_status["result_json"] == late_accept.json()["result_json"]
            return
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
