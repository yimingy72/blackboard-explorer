"""Real PG: main priority, a one-shot helper, and original frozen late evidence."""

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bbx_blackboard.gateway_auth import issue_gateway_credentials
from bbx_blackboard.runner_control import row
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import BOARD_CONTRACT
from runner_gateway_support import invocation
from sqlalchemy import func, insert, select, update
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


async def test_twenty_native_seats_main_priority_and_unknown_retention(ctf, monkeypatch):
    ctf.objects = Objects()
    capability = "helper_foreground"
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Resolve a parent and independent work",
            "budget": {"max_cost": "1", "max_minutes": 5},
            "ctf_options": {"dispatch_mode": "automatic", "max_teammates": 20},
        },
        business_contract=BOARD_CONTRACT,
    )
    op, stamp = str(uuid4()), datetime.now(UTC)
    challenges = [str(uuid4()) for _ in range(19)]
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
            for sequence, cid in enumerate(challenges, 1):
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
            extra_ready = ready.pop(challenges[-1])
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
                "instructions": {f"member-{i}": "Worker" for i in range(1, 21)},
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
            resource = await native._get(conn, tid)
            await native.workers.reconcile(conn, tid, task, resource, owner_row, allow_new=True)
            task = await row(conn, s.tasks, tid)
            main = task["ctf_control"]["native_worker_active"]
            assert len(main) == 18

        async def commands():
            reply = await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
            assert reply.status_code == 200, reply.text
            return [
                (value, json.loads(value["payload_json"])) for value in reply.json()["commands"]
            ]

        async def prepared(command, label):
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
            )
            return {"tid": tid, "owner": owner, "binding": binding, "sdk": sdk, "journal": tail}

        worker_commands = {
            payload["spec"]["binding"]["binding_id"]: cmd
            for cmd, payload in await commands()
            if payload["kind"] == "prepare"
        }
        parent_ids = [main[f"member-{i}"]["binding_id"] for i in range(1, 4)]
        parents = [
            await prepared(worker_commands[bid], f"parent-{i}") for i, bid in enumerate(parent_ids)
        ]
        parent_headers = [
            {
                "Authorization": "Bearer "
                + issue_gateway_credentials(app.state.settings, owner_row, bid, "teammate")["mcp"][
                    "token"
                ],
            }
            for bid in parent_ids
        ]
        helpers = [control(tid) + f"/bindings/{bid}/helpers" for bid in parent_ids]
        start = asyncio.Event()
        waiting = asyncio.Barrier(3)

        async def reserve(i, label):
            body = invocation(
                parents[i],
                "delegate_helper",
                {"goal": "Inspect one observation", "expected_output": "A short answer"},
                label,
            )
            if label == "borrow":
                await waiting.wait()
                await start.wait()
            response = await http.post(
                helpers[i] + "/reserve", headers=parent_headers[i], json=body
            )
            assert response.status_code == 200, response.text
            return body, response.json()

        claims = [asyncio.create_task(reserve(i, "borrow")) for i in (0, 1)]
        await waiting.wait()
        start.set()
        (first, first_receipt), (second, second_receipt) = await asyncio.gather(*claims)
        assert first_receipt["state"] == second_receipt["state"] == "reserved"
        assert first_receipt["binding_id"] != second_receipt["binding_id"]
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            current = task["ctf_control"]
            kids = current["native_helpers_active"]
            assert (
                len(kids) == 2 and not set(kids) & set(main) and "lead" not in set(main) | set(kids)
            )
            assert set(kids) | set(main) == {f"member-{i}" for i in range(1, 21)}
            by_binding = {child["binding_id"]: child for child in kids.values()}
            old_unknown = by_binding[first_receipt["binding_id"]]
            replaceable = by_binding[second_receipt["binding_id"]]
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={**current, "native_worker_ready": {challenges[-1]: extra_ready}},
                )
            )
        _, denied = await reserve(2, "full")
        assert denied["reason"] == "no_free_seat"

        # A claimed launch ACK proves neither execution nor an unused seat.
        first_path = helpers[0] + "/" + first["invocation_id"]
        launch = await http.post(first_path + "/launch", headers=parent_headers[0], json=first)
        assert launch.status_code == 200 and launch.json()["launch_allowed"], launch.text
        second_path = helpers[1] + "/" + second["invocation_id"]
        for path in (first_path, second_path):
            response = await http.post(path + "/cancel", headers=auth(owner))
            assert response.status_code == 200, response.text
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            resource = await native._get(conn, tid)
            await native.workers.reconcile(conn, tid, task, resource, owner_row, allow_new=True)
            task = await row(conn, s.tasks, tid)
            assert len(task["ctf_control"]["native_worker_active"]) == 18
            assert len(task["ctf_control"]["native_helpers_active"]) == 2

        for close, payload in await commands():
            if payload["kind"] == "close" and payload["binding_id"] in by_binding:
                await acknowledge(
                    http,
                    tid,
                    owner,
                    close,
                    "closed",
                    binding_id=payload["binding_id"],
                    sdk_idle=True,
                )
        _, denied = await reserve(2, "main-priority")
        assert denied["reason"] == "no_free_seat"
        async with ctf.repo.engine.begin() as conn:
            task = await row(conn, s.tasks, tid)
            current = task["ctf_control"]
            assert set(current["native_helpers_active"]) == {old_unknown["seat_id"]}
            assigned = current["native_worker_active"][replaceable["seat_id"]]
            assert assigned["work_id"] == challenges[-1]
            assert assigned["binding_id"] != replaceable["binding_id"]
            assert assigned["generation"] > replaceable["generation"]
            bindings = s.runner_bindings
            assert (
                await conn.execute(
                    select(func.count())
                    .select_from(bindings)
                    .where(
                        bindings.c.task_id == tid,
                        bindings.c.owner_epoch == epoch,
                    )
                )
            ).scalar_one() == 22
            # Historical rows exercise the existing journal-manifest bound without
            # simulating 10 additional SDK lifecycles or seeding any active seat.
            template = await row(conn, bindings, tid, replaceable["binding_id"])
            for _ in range(10):
                historical = str(uuid4())
                await conn.execute(
                    insert(bindings).values(
                        **{
                            **template,
                            "id": historical,
                            "registration_id": historical,
                            "binding": {**template["binding"], "binding_id": historical},
                        }
                    )
                )
            challenge = await ctf._challenge(conn, tid, assigned["work_id"])
            await conn.execute(
                update(s.ctf_challenges)
                .where(
                    s.ctf_challenges.c.task_id == tid, s.ctf_challenges.c.id == assigned["work_id"]
                )
                .values(data={**challenge, "work_status": "completed"})
            )

        async def tick():
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                task = await row(conn, s.tasks, tid)
                resource = await native._get(conn, tid)
                await native.workers.reconcile(conn, tid, task, resource, owner_row, allow_new=True)

        await tick()
        [closing] = [
            cmd
            for cmd, payload in await commands()
            if payload["kind"] == "close" and payload["binding_id"] == assigned["binding_id"]
        ]
        await acknowledge(
            http, tid, owner, closing, "closed", binding_id=assigned["binding_id"], sdk_idle=True
        )
        _, bounded = await reserve(2, "history-bound")
        assert bounded["reason"] == "recovery_binding_limit"
        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            assert len(task["ctf_control"]["native_worker_active"]) == 18
            challenge_id = str(uuid4())
            await conn.execute(
                insert(s.ctf_challenges).values(
                    task_id=tid,
                    id=challenge_id,
                    revision=1,
                    data={
                        **challenge,
                        "id": challenge_id,
                        "revision": 1,
                        "owner_id": None,
                        "work_status": "pending",
                    },
                )
            )
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={
                        **task["ctf_control"],
                        "native_worker_ready": {challenge_id: extra_ready},
                    },
                )
            )
        await asyncio.gather(tick(), reserve(2, "main-beyond-history-bound"))
        async with ctf.repo.engine.begin() as conn:
            task = await row(conn, s.tasks, tid)
            newest = task["ctf_control"]["native_worker_active"][replaceable["seat_id"]]
            assert (
                newest["work_id"] == challenge_id and newest["generation"] > assigned["generation"]
            )
            assert len(task["ctf_control"]["native_worker_active"]) == 19
            assert len(task["ctf_control"]["native_helpers_active"]) == 1
        assert (
            sum(
                payload.get("spec", {}).get("binding", {}).get("binding_id") == newest["binding_id"]
                for _, payload in await commands()
            )
            == 1
        )
