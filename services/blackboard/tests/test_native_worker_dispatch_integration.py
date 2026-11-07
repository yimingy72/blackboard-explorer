"""Real PG Worker ownership, evidence and release with synthetic external observations."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from bbx_blackboard.auth import Principal, issue_user_token
from bbx_blackboard.challenge_board import ChallengeBoardService
from bbx_blackboard.domain import RuleViolation
from bbx_blackboard.flag_board import FlagBoardService
from bbx_blackboard.gateway_auth import (
    RuntimeBusinessContext,
    issue_gateway_credentials,
    require_gateway_identity,
)
from bbx_blackboard.store import schema as s
from bbx_blackboard.task_board import TaskBoardService
from bbx_contracts.challenge_board import (
    BOARD_CONTRACT,
    DISPATCH_TOOL,
    UPDATE_CHALLENGE_TOOL,
    business_manifest,
)
from bbx_contracts.runner import runner_json_text, runner_payload_digest
from bbx_runtime.scheduler.lock import RuntimeLock
from fastapi import HTTPException
from runner_gateway_support import invocation
from sqlalchemy import func, insert, select, update
from starlette.requests import Request
from test_board_flags_integration import Objects
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_native_runner_bootstrap_integration import config, runtime_settings
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


@pytest.mark.parametrize(
    ("revise_scope", "manual_pause"),
    [(False, False), (True, False), (False, True)],
    ids=["withdraw", "scope_revision", "manual_pause"],
)
async def test_native_worker_claim_evidence_and_original_drain(
    ctf, ctf_url, monkeypatch, revise_scope, manual_pause
):
    ctf.objects = Objects()
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Resolve two self-contained challenges",
            "budget": {"max_cost": "1", "max_minutes": 5},
            "ctf_options": {"dispatch_mode": "automatic", "max_teammates": 1},
        },
        business_contract=BOARD_CONTRACT,
    )
    operation_id, stamp = str(uuid4()), datetime.now(UTC)
    async with (
        connect(ctf) as (app, http),
        RuntimeLock(runtime_settings(ctf_url), fenced=True) as lock,
        app.state.business_mcp.run(),
    ):
        assert lock.backend_started_at is not None
        native = app.state.native_runner_bootstrap
        gateway = app.state.runner_gateway
        monkeypatch.setattr(
            gateway.native,
            "_budget",
            AsyncMock(return_value={"publication_state": "ready", "hold_reason": None}),
        )
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await native._lead(conn, tid, {"operation_id": operation_id})
            task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
            published = task["ctf_control"]["business_tools"]
            if not revise_scope and not manual_pause:
                published = {
                    **published,
                    "revision": 2,
                    "roles": {
                        **published["roles"],
                        "lead": [
                            n for n in published["roles"]["lead"] if n != UPDATE_CHALLENGE_TOOL
                        ],
                    },
                }
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    status="running",
                    active_since=stamp,
                    run_started=True,
                    ctf_control={
                        **task["ctf_control"],
                        "phase": "running",
                        "business_tools": published,
                    },
                )
            )
        assert {m["id"] for m in (await ctf.list_members(tid))["members"]} == {"lead", "member-1"}
        legacy = await http.post(f"/api/tasks/{tid}/ctf/runtime/recover", headers=SERVICE, json={})
        assert legacy.status_code == 409 and "original Runner" in legacy.text
        owner = await register_owner(http, tid)
        request = {
            **registration(owner),
            "allowed_tools": [
                DISPATCH_TOOL,
                "board_create_challenge",
                *([UPDATE_CHALLENGE_TOOL] if revise_scope else []),
            ],
        }
        registered = await http.post(mgmt(tid) + "/bindings", headers=SERVICE, json=request)
        assert registered.status_code == 200, registered.text
        lead_id = registered.json()["binding"]["binding_id"]
        async with ctf.repo.engine.connect() as conn:
            lead = (
                (
                    await conn.execute(
                        select(s.runner_bindings).where(
                            s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == lead_id
                        )
                    )
                )
                .mappings()
                .one()
            )
        epoch = owner["fence"]["epoch"]

        async def commands():
            response = await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
            assert response.status_code == 200, response.text
            return response.json()["commands"]

        async def prepared(binding, command, label):
            await acquire(http, tid, owner, binding, command)
            first = cursor(empty_cursor(), label)
            receipt = confirmation(owner, binding, empty_cursor(), first)
            receipt["sdk_session_id"] = label + "-sdk"
            await confirm(http, tid, owner, binding, receipt)
            await acknowledge(
                http,
                tid,
                owner,
                command,
                "prepared",
                binding_id=binding["binding_id"],
                sdk_session_id=receipt["sdk_session_id"],
                cursor=first,
                journal_confirmed=True,
                sdk_idle=True,
            )
            return receipt["sdk_session_id"], first

        lead_cmd = next(
            c for c in await commands() if json.loads(c["payload_json"])["kind"] == "prepare"
        )
        lead_sdk, lead_cursor = await prepared(lead["binding"], lead_cmd, "lead")
        deployment = {**config(), "runner_profile": "node-sandbox-host-v1"}
        observed = {
            "namespace_uid": str(uuid4()),
            "journal_pvc": {"uid": str(uuid4())},
            "workspace_pvc": {"uid": str(uuid4())},
            "runner_secret": {"uid": str(uuid4())},
            "runner": {
                "pod_uid": owner["fence"]["pod_uid"],
                "container_id": "runner-id",
                "host_container_id": "host-id",
                "phase": "Running",
            },
            "sandbox": {
                "id": str(uuid4()),
                "cr_uid": str(uuid4()),
                "pod_uid": str(uuid4()),
                "container_id": "sandbox-id",
                "environment_revision": deployment["environment_revision"],
            },
        }
        owner_row = None
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            owner_row = await gateway.control._current_owner(conn, tid)
            await conn.execute(
                insert(s.task_runner_resources).values(
                    task_id=tid,
                    operation_id=operation_id,
                    request_id=operation_id,
                    request_digest="fixture",
                    request={},
                    config=deployment,
                    controller_id="worker-fixture",
                    controller_generation=lock.fence,
                    controller_backend_pid=lock.backend_pid,
                    controller_backend_start=lock.backend_started_at,
                    revision=1,
                    namespace="fixture",
                    state="ready",
                    desired="active",
                    hard_deadline=stamp + timedelta(minutes=5),
                    sandbox_create_claimed=True,
                    tenant_key_hash="fixture-" + operation_id,
                    tenant_key_ciphertext="fixture",
                    observation=observed,
                    sandbox_host={
                        "ready": True,
                        "context": {"execution_context_id": "a" * 64},
                        "report_key_hash": hashlib.sha256(
                            b"synthetic-scope-report-key"
                        ).hexdigest(),
                    },
                    next_check_at=stamp,
                    created_at=stamp,
                    data={
                        "activated": True,
                        "binding_id": lead_id,
                        "owner_epoch": epoch,
                        "lead_turn_id": lead["business"]["turn_id"],
                        "worker_spec": {
                            "model": request["model"],
                            "allowed_tools": ["board_post_fact"],
                            "instructions": {"member-1": "Original Worker profile"},
                        },
                        "worker_binding_ids": [],
                    },
                )
            )
        assert owner_row

        def identity(binding, sdk, journal, role):
            credential = issue_gateway_credentials(
                app.state.settings, owner_row, binding["id"], role
            )["mcp"]
            request = Request(
                {
                    "type": "http",
                    "app": app,
                    "headers": [(b"authorization", ("Bearer " + credential["token"]).encode())],
                }
            )
            capability = require_gateway_identity(request, tid, "mcp")
            return Principal(
                "agent",
                binding["binding"]["agent_id"],
                task_id=tid,
                agent_id=binding["binding"]["agent_id"],
                mode="ctf",
                generation=binding["business"]["generation"],
                turn_id=UUID(binding["business"]["turn_id"]),
                runtime_context=RuntimeBusinessContext(capability, sdk, journal),
            )

        lead_actor = identity(lead, lead_sdk, lead_cursor, "lead")
        assert lead_actor.runtime_context is not None
        board = TaskBoardService(ctf)
        assert DISPATCH_TOOL not in business_manifest()["roles"]["lead"]
        assert DISPATCH_TOOL in await gateway.catalog(tid, lead_actor.runtime_context.identity)
        assert (
            UPDATE_CHALLENGE_TOOL in await gateway.catalog(tid, lead_actor.runtime_context.identity)
        ) is revise_scope

        async def edit(arguments, suffix):
            scope = {
                "tid": tid,
                "owner": owner,
                "binding": lead["binding"],
                "sdk": lead_sdk,
                "journal": lead_cursor,
            }
            envelope = invocation(scope, UPDATE_CHALLENGE_TOOL, arguments, suffix)
            credentials = issue_gateway_credentials(app.state.settings, owner_row, lead_id, "lead")[
                "mcp"
            ]
            headers = {"Authorization": "Bearer " + credentials["token"]}
            backend = app.state.settings.runner_mcp_backend_token.get_secret_value()
            permit = await http.post(
                control(tid) + f"/bindings/{lead_id}/tools/admit", headers=headers, json=envelope
            )
            assert permit.status_code == 200, permit.text
            response = await http.post(
                f"/api/tasks/{tid}/business-mcp/",
                headers={
                    **headers,
                    "Accept": "application/json, text/event-stream",
                    "x-bbx-sdk-session-id": lead_sdk,
                    "x-bbx-gateway-backend": backend,
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": UPDATE_CHALLENGE_TOOL,
                        "arguments": arguments,
                        "_meta": {"bbx": {**envelope, "permit_ref": envelope["invocation_id"]}},
                    },
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            return json.loads(result["content"][0]["text"]), bool(result.get("isError"))

        dispatches = []
        for n, (priority, readiness, external) in enumerate(
            (
                (3, "no_external_materials", None),
                (0, "no_external_materials", None),
                (0, "materials_pending", "external"),
            )
        ):
            challenge = await board.create_challenge(
                tid,
                lead_actor,
                {
                    "request_id": str(uuid4()),
                    "title": f"Question {n}",
                    "description": "A self-contained synthetic exercise",
                    "external_id": external,
                },
            )
            ready = {
                "request_id": str(uuid4()),
                "challenge_id": challenge["id"],
                "expected_revision": 1,
                "action": "ready",
                "readiness": readiness,
                "priority": priority,
            }
            if external:
                with pytest.raises(HTTPException, match="self-contained"):
                    await board.dispatch_challenge(
                        tid, lead_actor, {**ready, "readiness": "no_external_materials"}
                    )
            result = await board.dispatch_challenge(tid, lead_actor, ready)
            assert await board.dispatch_challenge(tid, lead_actor, ready) == result
            dispatches.append(result["challenge"])
        path = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap/actions"

        async def act(action, **proof):
            response = await http.post(
                path,
                headers=SERVICE,
                json={
                    "operation_id": operation_id,
                    "controller_id": "worker-fixture",
                    "controller_generation": lock.fence,
                    "action": action,
                    "proof": proof,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        first, concurrent = await asyncio.gather(act("activate"), act("activate"))
        bids = first["data"]["worker_binding_ids"]
        assert len(bids) == 1 and concurrent["data"]["worker_binding_ids"] == bids
        bid = bids[0]
        prepare = next(
            c
            for c in await commands()
            if json.loads(c["payload_json"]).get("spec", {}).get("binding", {}).get("binding_id")
            == bid
        )
        binding = json.loads(prepare["payload_json"])["spec"]["binding"]
        sdk, journal = await prepared(binding, prepare, "worker")
        await asyncio.gather(act("activate"), act("activate"))
        async with ctf.repo.engine.connect() as conn:
            worker = (
                (
                    await conn.execute(
                        select(s.runner_bindings).where(
                            s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == bid
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert worker["business"]["challenge_id"] == dispatches[1]["id"]
            assert (
                await conn.execute(
                    select(func.count())
                    .select_from(s.runner_commands)
                    .where(
                        s.runner_commands.c.task_id == tid,
                        s.runner_commands.c.binding_id == bid,
                        s.runner_commands.c.kind == "input",
                    )
                )
            ).scalar_one() == 1
        actor = identity(worker, sdk, journal, "teammate")
        assert actor.runtime_context is not None
        assert DISPATCH_TOOL not in await gateway.catalog(tid, actor.runtime_context.identity)
        cid = UUID(worker["business"]["challenge_id"])
        command_id, unknown_id = "a" * 64, "tool:" + "b" * 64
        original_scope = {"execution_context_id": "a" * 64}
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await conn.execute(
                insert(s.sandbox_command_records).values(
                    task_id=tid,
                    id=command_id,
                    binding_id=bid,
                    agent_id="member-1",
                    role="worker",
                    challenge_id=cid,
                    assignment_id=binding["assignment_ref"],
                    generation=binding["generation"],
                    epoch=epoch,
                    execution_id="original-exec",
                    exit_code=0,
                    state="completed",
                    created_at=stamp,
                    data={
                        "receipt": "foreground_terminal",
                        "scope_state": "active",
                        "binding": original_scope,
                    },
                )
            )
            await conn.execute(
                insert(s.runner_calls).values(
                    task_id=tid,
                    id="tool:" + command_id,
                    binding_id=bid,
                    owner_epoch=epoch,
                    kind="tool",
                    state="completed",
                    concurrency_active=False,
                    provider_key="sandbox",
                    data={"tool_name": "exec_command"},
                    created_at=stamp,
                )
            )
            await conn.execute(
                insert(s.runner_calls).values(
                    task_id=tid,
                    id=unknown_id,
                    binding_id=bid,
                    owner_epoch=epoch,
                    kind="tool",
                    state="unknown",
                    concurrency_active=True,
                    provider_key="sandbox",
                    data={
                        "sandbox_binding": original_scope,
                        "sdk_admitted": True,
                        "tool_name": "write_file",
                    },
                    created_at=stamp,
                )
            )
        facts = ChallengeBoardService(ctf)
        fact = {
            "request_id": str(uuid4()),
            "expected_revision": 3,
            "kind": "observation",
            "statement": "The original command returned zero.",
            "evidence": [
                {"type": "command_output", "summary": "Original SDK result", "call_id": command_id}
            ],
        }
        with pytest.raises(HTTPException, match="outside"):
            await facts.write(tid, UUID(dispatches[0]["id"]), actor, fact, kind="fact")
        saved = await facts.write(tid, cid, actor, fact, kind="fact")
        assert saved["object"]["assignment_ref"]["assignment_id"] == binding["assignment_ref"]
        assert saved["object"]["assignment_ref"]["epoch"] == epoch
        assert saved["object"]["provenance"] == "tool_backed"
        with pytest.raises(HTTPException, match="close proof"):
            await ctf.finish_turn(
                tid,
                "member-1",
                worker["business"]["turn_id"],
                worker["business"]["generation"],
                "completed",
            )
        if manual_pause:
            prefix = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
            user = {
                "Authorization": "Bearer " + issue_user_token(app.state.settings, "fixture-owner")
            }
            original_bootstrap = {
                "protocol": "bbx.task-runner.v1",
                "fence": owner["fence"],
                "control_url": deployment["runner_blackboard_url"]
                + f"/api/tasks/{tid}/runner-control",
                "model_proxy_url": deployment["runner_model_proxy_url"],
                "mcp_gateway_url": f"http://scripted-mcp.invalid/api/tasks/{tid}/mcp",
                "control_credential_ref": "control",
                "credential_file": "/var/run/bbx/credentials.json",
                "journal_root": "/journal",
                "mcp_metadata_timeout_ms": 15000,
            }
            original_input = {
                "id": str(uuid4()),
                "filename": "original.txt",
                "size": 12,
                "path": "/workspace/shared/inputs/original.txt",
                "uri": "inputs/original.txt",
                "sha256": "a" * 64,
            }
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                task_control = (
                    await conn.execute(select(s.tasks.c.ctf_control).where(s.tasks.c.id == tid))
                ).scalar_one()
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(
                        domain_context="Original authorized context",
                        ctf_control={
                            **task_control,
                            "completion_requirements": "Original criteria",
                        },
                        initial_attachments=[original_input],
                    )
                )
                resource = await native._get(conn, tid)
                await conn.execute(
                    update(s.task_runner_resources)
                    .where(s.task_runner_resources.c.task_id == tid)
                    .values(
                        data={
                            **resource["data"],
                            "bootstrap": original_bootstrap,
                            "credentials_issued": True,
                        }
                    )
                )
                await conn.execute(
                    insert(s.native_task_budgets).values(
                        task_id=tid,
                        accounting_source_id="fixture",
                        key_name="original-task-key",
                        key_fingerprint="original-fingerprint",
                        key_ciphertext="original-secret-reference",
                        native_resource_id="original-key-id",
                        publication_state="ready",
                        config={"threshold_usd": "1.00"},
                        status_snapshot={"used_high_water_usd": "0.10"},
                        created_at=stamp,
                    )
                )

            async def budget_row(conn, task_id):
                return (
                    (
                        await conn.execute(
                            select(s.native_task_budgets).where(
                                s.native_task_budgets.c.task_id == task_id
                            )
                        )
                    )
                    .mappings()
                    .one()
                )

            monkeypatch.setattr(gateway.native, "_budget", budget_row)
            amount = {"used": "0.05", "exceeded": False}
            budget_status = {
                "budgets": [
                    {
                        "apiKeyName": "original-task-key",
                        "name": "task-usd",
                        "limit": {"unit": "USD", "amount": "1.00"},
                        "usage": amount,
                        "window": {"durationMs": None, "expired": False},
                        "onBudgetExceeded": "Block",
                    }
                ]
            }
            readback = AsyncMock(return_value=budget_status)
            monkeypatch.setattr(gateway.native.admin, "budget", readback)
            before = await commands()
            pause_request = {"request_id": str(uuid4())}
            assert (
                await http.post(prefix + "/pause", headers=SERVICE, json=pause_request)
            ).status_code == 403
            response = await http.post(prefix + "/pause", headers=user, json=pause_request)
            assert response.status_code == 200, response.text
            paused = response.json()
            assert set(paused) == {"pause_id", "state", "blocker"} and paused["state"] == "draining"
            assert (
                await http.post(prefix + "/pause", headers=user, json=pause_request)
            ).json() == paused
            with pytest.raises(HTTPException, match="stopped"):
                await gateway.catalog(tid, actor.runtime_context.identity)
            control_only = await act("renew")
            assert control_only["control_only"] and set(control_only["credentials"]) == {"control"}
            closing = await commands()
            assert all(json.loads(c["payload_json"])["kind"] == "close" for c in closing)
            assert len(closing) == 2
            for original, current_sdk, previous in (
                (lead["binding"], lead_sdk, lead_cursor),
                (binding, sdk, journal),
            ):
                old_bid = original["binding_id"]
                inputs = [
                    p["input_id"]
                    for command in before
                    if (p := json.loads(command["payload_json"]))["kind"] == "input"
                    and p["binding_id"] == old_bid
                ]
                tail = cursor(previous, "paused-tail-" + old_bid, advance=max(1, len(inputs)))
                applied = [
                    {"input_id": iid, "entry_id": "entry-" + iid, "seq": previous["seq"] + n}
                    for n, iid in enumerate(inputs, 1)
                ]
                receipt = confirmation(owner, original, previous, tail, applied)
                receipt["sdk_session_id"] = current_sdk
                await confirm(http, tid, owner, original, receipt)
                close = next(
                    c for c in closing if json.loads(c["payload_json"])["binding_id"] == old_bid
                )
                await acknowledge(
                    http,
                    tid,
                    owner,
                    close,
                    "closed",
                    binding_id=old_bid,
                    sdk_session_id=current_sdk,
                    sdk_idle=True,
                    journal_confirmed=True,
                    cursor=tail,
                )
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                await conn.execute(
                    update(s.sandbox_command_records)
                    .where(s.sandbox_command_records.c.task_id == tid)
                    .values(
                        data={
                            "receipt": "foreground_terminal",
                            "scope_state": "sealed",
                            "stdout": "known result",
                        }
                    )
                )
            await act("activate")
            waiting = (await http.get(prefix + "/pause", headers=user)).json()
            assert (
                waiting["state"] == "draining" and waiting["blocker"] == "original_call_unresolved"
            )
            resume_request = {"request_id": str(uuid4()), "pause_id": paused["pause_id"]}
            assert (
                await http.post(prefix + "/resume", headers=user, json=resume_request)
            ).status_code == 409
            late = await http.post(
                f"/api/tasks/{tid}/sandbox-host/calls/{unknown_id.removeprefix('tool:')}/result",
                headers={"x-bbx-sandbox-report": "synthetic-scope-report-key"},
                json={"result": {"content": [{"type": "text", "text": "Original final output"}]}},
            )
            assert late.status_code == 200, late.text
            await act("activate")
            assert (await http.get(prefix + "/pause", headers=user)).json()["state"] == "paused"
            assert (
                await http.post(prefix + "/resume", headers=user, json=resume_request)
            ).status_code == 409
            amount["used"] = "0.20"
            response = await http.post(prefix + "/resume", headers=user, json=resume_request)
            assert response.status_code == 200, response.text
            assert response.json()["state"] == "resuming"
            assert (
                await http.post(prefix + "/resume", headers=user, json=resume_request)
            ).json() == response.json()
            assert all(call.args == ("original-task-key",) for call in readback.await_args_list)
            pending = await commands()
            assert len(pending) == 1
            replacement = json.loads(pending[0]["payload_json"])["spec"]
            new_lead = replacement["binding"]
            assert new_lead["binding_id"] != lead_id
            assert replacement["model_credential_ref"] == "native-model-" + new_lead["binding_id"]
            fresh = await act("renew")
            assert fresh["bootstrap"] == original_bootstrap
            assert replacement["model_credential_ref"] in fresh["credentials"]
            await prepared(new_lead, pending[0], "resumed-lead")
            resumed = await act("activate")
            assert resumed["data"]["pause_state"] == "resumed"
            new_worker = resumed["data"]["worker_binding_ids"][0]
            assert new_worker != bid
            async with ctf.repo.engine.connect() as conn:
                task = (
                    (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid)))
                    .mappings()
                    .one()
                )
                continued = task["ctf_control"]["native_worker_active"]["member-1"]
                assert continued["previous_assignment_id"] == binding["assignment_ref"]
                assert continued["assignment_ref"] != binding["assignment_ref"] and continued[
                    "work_id"
                ] == str(cid)
                assert continued["priority"] == 0 and continued["epoch"] == epoch
                budget = await budget_row(conn, tid)
                assert (
                    budget["key_fingerprint"] == "original-fingerprint"
                    and budget["native_resource_id"] == "original-key-id"
                )
                assert (
                    budget["config"]["threshold_usd"] == "1.00"
                    and budget["status_snapshot"]["used_high_water_usd"] == "0.20"
                )
                sent = (
                    (
                        await conn.execute(
                            select(s.runner_commands.c.payload_json).where(
                                s.runner_commands.c.task_id == tid,
                                s.runner_commands.c.binding_id == new_lead["binding_id"],
                                s.runner_commands.c.kind == "input",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(sent) == 1
                continued_input = json.loads(json.loads(sent[0])["content"])
                assert (
                    continued_input["task_context"]["goal"]
                    == "Resolve two self-contained challenges"
                )
                assert continued_input["previous_binding_id"] == lead_id
                assert (
                    continued_input["task_context"]["domain_context"]
                    == "Original authorized context"
                )
                assert (
                    continued_input["task_context"]["completion_requirements"]
                    == "Original criteria"
                )
                assert continued_input["task_context"]["initial_input_metadata"] == [original_input]
                assert (
                    await conn.execute(
                        select(func.count())
                        .select_from(s.ctf_messages)
                        .where(s.ctf_messages.c.task_id == tid, s.ctf_messages.c.deferred.is_(True))
                    )
                ).scalar_one() >= 1
            stop = await http.post(f"/api/tasks/{tid}/stop", headers=SERVICE)
            assert stop.status_code == 200, stop.text
            assert (await http.get(prefix + "/pause", headers=user)).json()["state"] == "stopping"
            assert (
                await http.post(
                    prefix + "/resume",
                    headers=user,
                    json={**resume_request, "request_id": str(uuid4())},
                )
            ).status_code == 409
            return
        flags = FlagBoardService(ctf, app.state.settings.agent_token_secret.get_secret_value())
        host = f"/api/tasks/{tid}/sandbox-host"
        headers = {"x-bbx-sandbox-report": "synthetic-scope-report-key"}
        candidate = None
        if revise_scope:
            assert UPDATE_CHALLENGE_TOOL not in await gateway.catalog(
                tid, actor.runtime_context.identity
            )
            pending_id = dispatches[0]["id"]
            pending_edit, failed = await edit(
                {
                    "challenge_id": pending_id,
                    "expected_revision": 2,
                    "description": "Revised queued question",
                },
                "clear-ready",
            )
            assert not failed and pending_edit["dispatch_status"] == "pending"
            assert pending_edit["challenge"]["scope_revision"] == 2
            async with ctf.repo.engine.connect() as conn:
                current = (
                    await conn.execute(select(s.tasks.c.ctf_control).where(s.tasks.c.id == tid))
                ).scalar_one()
                assert pending_id not in current["native_worker_ready"]
            renamed, failed = await edit(
                {"challenge_id": str(cid), "expected_revision": 3, "title": "Renamed"}, "title"
            )
            assert not failed and renamed["challenge"]["scope_revision"] == 1
            assert (await facts.board(tid, cid, actor))["facts"]
            candidate = await flags.candidate(
                tid,
                cid,
                actor,
                {
                    "request_id": str(uuid4()),
                    "expected_revision": 4,
                    "value": "flag{synthetic}",
                    "fact_refs": [saved["id"]],
                },
            )
            sent_candidate = await flags.candidate(
                tid,
                cid,
                actor,
                {
                    "request_id": str(uuid4()),
                    "expected_revision": 4,
                    "value": "flag{sent_fixture}",
                    "fact_refs": [saved["id"]],
                },
            )
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                submissions = []
                for flag_state, original_candidate in (
                    ("prepared", candidate),
                    ("unknown", sent_candidate),
                ):
                    sid, op = str(uuid4()), str(uuid4())
                    platform_call_id = "original-fixture-send" if flag_state == "unknown" else None
                    submission = {
                        "id": sid,
                        "operation_id": op,
                        "task_id": str(tid),
                        "challenge_id": str(cid),
                        "candidate_id": original_candidate["id"],
                        "verification_round": original_candidate["verification_round"],
                        "status": flag_state,
                        "platform_call_id": platform_call_id,
                        "progress_applied": False,
                        "created_version": 1,
                        "updated_version": 1,
                    }
                    await conn.execute(
                        insert(s.flag_submissions).values(
                            task_id=tid,
                            id=sid,
                            challenge_id=str(cid),
                            candidate_id=original_candidate["id"],
                            verification_round=original_candidate["verification_round"],
                            status=flag_state,
                            platform_call_id=platform_call_id,
                            data=submission,
                            version=1,
                        )
                    )
                    submissions.append(submission)
            args = {
                "challenge_id": str(cid),
                "expected_revision": 4,
                "description": "A new self-contained question",
                "readiness": "no_external_materials",
                "priority": 0,
            }
            refused, failed = await edit(args, "flag-unknown")
            assert failed and refused["status"] == 409
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                # Simulate the trusted external result for the original, already sent Flag.
                old_flag = submissions[1]
                await conn.execute(
                    update(s.flag_submissions)
                    .where(
                        s.flag_submissions.c.task_id == tid,
                        s.flag_submissions.c.id == old_flag["id"],
                    )
                    .values(
                        status="rejected",
                        data={**old_flag, "status": "rejected"},
                    )
                )
            changed, failed = await edit(args, "scope")
            assert not failed, changed
            assert changed["dispatch_status"] == "draining"
            assert (
                changed["challenge"]["scope_revision"] == 2
                and changed["challenge"]["owner_id"] == "member-1"
            )
            assert (await edit(args, "scope"))[0] == changed
            stale, failed = await edit({**args, "description": "Stale revision"}, "stale")
            assert failed and stale["status"] == 409
            with pytest.raises(HTTPException, match="Assignment"):
                await gateway.catalog(tid, actor.runtime_context.identity)
        else:
            await board.dispatch_challenge(
                tid,
                lead_actor,
                {
                    "request_id": str(uuid4()),
                    "challenge_id": str(cid),
                    "expected_revision": 3,
                    "action": "withdraw",
                },
            )
        await act("activate")
        close = next(
            c
            for c in await commands()
            if json.loads(c["payload_json"]).get("binding_id") == bid
            and json.loads(c["payload_json"])["kind"] == "close"
        )
        await acknowledge(
            http,
            tid,
            owner,
            close,
            "closed",
            binding_id=bid,
            sdk_session_id=sdk,
            sdk_idle=True,
            journal_confirmed=False,
        )
        assert (await act("activate"))["data"]["worker_binding_ids"] == [bid]
        await acknowledge(
            http,
            tid,
            owner,
            close,
            "closed",
            binding_id=bid,
            sdk_session_id=sdk,
            sdk_idle=True,
            journal_confirmed=True,
            cursor=journal,
        )
        if revise_scope:
            pending = await http.get(host + "/scopes", headers=headers)
            assert pending.status_code == 200 and pending.json() == [
                {"command_id": command_id, "binding": original_scope}
            ]
            claim = await http.post(host + f"/scopes/{command_id}/claim", headers=headers)
            assert claim.status_code == 200 and claim.json()["stop"] is True
            scope_id = hashlib.sha256(
                json.dumps(original_scope, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            bound = {"scope_id": scope_id, "execution_id": "original-cleanup"}
            for name, payload in (
                ("scope_bound", bound),
                (
                    "scope_observed",
                    {
                        **bound,
                        "receipt": {
                            "scope_id": scope_id,
                            "execution_context_id": original_scope["execution_context_id"],
                            "state": "confirmed",
                            "reason": "observed_empty",
                            "sealed": True,
                            "populated": 0,
                        },
                    },
                ),
            ):
                result = await http.post(
                    host + f"/scopes/{command_id}/events",
                    headers=headers,
                    json={"event": name, "payload": payload},
                )
                assert result.status_code == 200, result.text
        else:
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                await conn.execute(
                    update(s.sandbox_command_records)
                    .where(s.sandbox_command_records.c.task_id == tid)
                    .values(data={"receipt": "foreground_terminal", "scope_state": "sealed"})
                )
        assert (await act("activate"))["data"]["worker_binding_ids"] == [bid]
        if revise_scope:
            late = await http.post(
                host + f"/calls/{unknown_id.removeprefix('tool:')}/result",
                headers=headers,
                json={"result": {"content": [{"type": "text", "text": "Original saved result"}]}},
            )
            assert late.status_code == 200, late.text
        else:
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                await conn.execute(
                    update(s.runner_calls)
                    .where(s.runner_calls.c.task_id == tid, s.runner_calls.c.id == unknown_id)
                    .values(state="completed", concurrency_active=False)
                )
        replacement = (await act("activate"))["data"]["worker_binding_ids"]
        assert len(replacement) == 1 and replacement[0] != bid
        async with ctf.repo.engine.connect() as conn:
            new = (
                (
                    await conn.execute(
                        select(s.runner_bindings).where(
                            s.runner_bindings.c.task_id == tid,
                            s.runner_bindings.c.id == replacement[0],
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert new["business"]["challenge_id"] == (
                str(cid) if revise_scope else dispatches[0]["id"]
            )
            assert new["binding"]["assignment_ref"] != binding["assignment_ref"]
        if revise_scope:
            assert candidate is not None
            assert new["business"]["scope_revision"] == 2
            new_prepare = next(
                c
                for c in await commands()
                if json.loads(c["payload_json"])
                .get("spec", {})
                .get("binding", {})
                .get("binding_id")
                == new["id"]
            )
            sdk2, journal2 = await prepared(new["binding"], new_prepare, "replacement")
            await act("activate")
            new_actor = identity(new, sdk2, journal2, "teammate")
            history = Principal("user", "fixture-reviewer")
            assert saved["id"] in (await facts.board(tid, cid, history))["facts"]
            assert (await facts.board(tid, cid, new_actor))["facts"] == {}
            assert await facts.search(tid, cid, new_actor, "command", 10) == []
            with pytest.raises(HTTPException, match="not found"):
                await facts.get_object(tid, cid, new_actor, saved["id"])
            revised = await ctf.get_challenge(tid, cid)
            with pytest.raises(RuleViolation):
                await facts.write(
                    tid,
                    cid,
                    new_actor,
                    {
                        "request_id": str(uuid4()),
                        "expected_revision": revised["revision"],
                        "statement": "Cannot reference old scope",
                        "based_on": [saved["id"]],
                        "expected": "No stale references",
                        "method": "Read current board",
                    },
                    kind="intent",
                )
            with pytest.raises(HTTPException, match="unconfirmed"):
                await facts.write(
                    tid,
                    cid,
                    new_actor,
                    {**fact, "request_id": str(uuid4()), "expected_revision": revised["revision"]},
                    kind="fact",
                )
            with pytest.raises(HTTPException, match="outside"):
                await flags.candidate(
                    tid,
                    cid,
                    new_actor,
                    {
                        "request_id": str(uuid4()),
                        "expected_revision": revised["revision"],
                        "value": "flag{wrong_scope}",
                        "fact_refs": [saved["id"]],
                    },
                )
            with pytest.raises(HTTPException, match="value access denied"):
                await flags.value(tid, cid, candidate["id"], new_actor)
            assert (await flags.board(tid, cid, new_actor))["candidates"] == []
            shared_id, shared_uri = str(uuid4()), f"inputs/{tid}/shared.txt"
            ctf.objects.values[shared_uri] = b"Shared Task evidence"
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                control_data = (
                    await conn.execute(select(s.tasks.c.ctf_control).where(s.tasks.c.id == tid))
                ).scalar_one()
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(
                        initial_attachments=[
                            {"id": shared_id, "uri": shared_uri, "path": "shared.txt"}
                        ],
                        ctf_control={**control_data, "public_input_ids": [shared_id]},
                    )
                )
            shared_evidence = {"type": "file", "summary": "Shared input", "uri": shared_uri}
            shared_fact = {
                **fact,
                "request_id": str(uuid4()),
                "expected_revision": revised["revision"],
            }
            with pytest.raises(HTTPException, match="separate object and command"):
                await facts.write(
                    tid,
                    cid,
                    new_actor,
                    {**shared_fact, "evidence": [{**shared_evidence, "call_id": command_id}]},
                    kind="fact",
                )
            assert (
                await facts.write(
                    tid, cid, new_actor, {**shared_fact, "evidence": [shared_evidence]}, kind="fact"
                )
            )["id"] != saved["id"]
            assert {x["status"] for x in (await flags.board(tid, cid, history))["submissions"]} == {
                "cancelled",
                "rejected",
            }
            original_event = next(evt for evt in saved["events"] if evt["type"] == "fact.posted")
            feed = await app.state.board_service.events(tid, for_agent="member-1")
            assert (
                next(evt for evt in feed if evt["version"] == original_event["version"])["type"]
                == "ctf.cursor"
            )
        stop = await http.post(f"/api/tasks/{tid}/stop", headers=SERVICE)
        assert stop.status_code == 200, stop.text
        stopping = await act("stop")
        stopped = await act(
            "cleanup",
            pvcs_retained=True,
            runner_terminated=True,
            sandbox_terminated=True,
            observation_digest=runner_payload_digest(runner_json_text(stopping["observation"])),
        )
        assert stopped["state"] == "stopped"
        events = await http.get(f"/api/tasks/{tid}/events", headers=SERVICE)
        assert events.status_code == 200, events.text
        assert {
            "ctf.native_worker.assigned",
            "ctf.native_worker.released",
            "ctf.native_worker.stopped",
            "ctf.native_worker.cleanup_changed",
        } <= {item["type"] for item in events.json()}
        async with ctf.repo.engine.connect() as conn:
            assert (
                await conn.execute(
                    select(func.count())
                    .select_from(s.ctf_turns)
                    .where(s.ctf_turns.c.task_id == tid, s.ctf_turns.c.status == "running")
                )
            ).scalar_one() == 0
