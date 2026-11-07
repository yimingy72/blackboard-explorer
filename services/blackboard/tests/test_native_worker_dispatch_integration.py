"""Real PG Worker ownership, evidence and release with synthetic external observations."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from bbx_blackboard.auth import Principal
from bbx_blackboard.challenge_board import ChallengeBoardService
from bbx_blackboard.gateway_auth import (
    RuntimeBusinessContext,
    issue_gateway_credentials,
    require_gateway_identity,
)
from bbx_blackboard.store import schema as s
from bbx_blackboard.task_board import TaskBoardService
from bbx_contracts.challenge_board import BOARD_CONTRACT, DISPATCH_TOOL, business_manifest
from bbx_contracts.runner import runner_json_text, runner_payload_digest
from bbx_runtime.scheduler.lock import RuntimeLock
from fastapi import HTTPException
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


async def test_native_worker_claim_evidence_and_original_drain(ctf, ctf_url, monkeypatch):
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
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    status="running",
                    active_since=stamp,
                    run_started=True,
                    ctf_control={**task["ctf_control"], "phase": "running"},
                )
            )
        assert {m["id"] for m in (await ctf.list_members(tid))["members"]} == {"lead", "member-1"}
        legacy = await http.post(f"/api/tasks/{tid}/ctf/runtime/recover", headers=SERVICE, json={})
        assert legacy.status_code == 409 and "original Runner" in legacy.text
        owner = await register_owner(http, tid)
        request = {
            **registration(owner),
            "allowed_tools": [DISPATCH_TOOL, "board_create_challenge"],
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
                    sandbox_host={"ready": True},
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
                    data={"receipt": "foreground_terminal", "scope_state": "active"},
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
                    data={},
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
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await conn.execute(
                update(s.sandbox_command_records)
                .where(s.sandbox_command_records.c.task_id == tid)
                .values(data={"receipt": "foreground_terminal", "scope_state": "sealed"})
            )
        assert (await act("activate"))["data"]["worker_binding_ids"] == [bid]
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
            assert new["business"]["challenge_id"] == dispatches[0]["id"]
            assert new["binding"]["assignment_ref"] != binding["assignment_ref"]
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
