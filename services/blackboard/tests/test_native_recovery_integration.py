"""Real PostgreSQL recovery with scripted Kubernetes and direct Pi evidence."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.runner_control import row
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import BOARD_CONTRACT
from bbx_contracts.mcp_catalog import FrozenNativeMcpTool, McpToolDetail, tool_schema_digest
from bbx_contracts.pi_models import PiModelVersion
from bbx_contracts.runner import runner_json_text, runner_payload_digest
from bbx_contracts.runner_bootstrap import NativeRunnerLaunch
from bbx_runtime.scheduler.lock import RuntimeLock
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import insert, update
from test_board_flags_integration import Objects
from test_ctf_api import settings
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
    control,
    cursor,
    empty_cursor,
    poll_query,
)

pytestmark = pytest.mark.integration


async def test_native_recovery_replaces_one_generation_after_explicit_user_intent(
    ctf, ctf_url, monkeypatch
):
    options = settings().model_copy(
        update={
            "sandbox_tenant_resolver_token": SecretStr("synthetic-resolver"),
            "pi_site_id": "fixture-primary",
            "pi_model_authority": "fixture-authority",
            "pi_model_primary_site": "fixture-primary",
        }
    )
    ctf.objects = Objects()
    app = create_app(options, engine=ctf.repo.engine, objects=ctf.objects)
    await app.state.profile_store.ensure_bundled(
        Path(__file__).resolve().parents[3] / "profiles/default"
    )
    native = app.state.native_runner_bootstrap
    gateway = native.gateway
    model = PiModelVersion.model_validate(
        {
            "id": "fixture-model",
            "model_revision": 1,
            "label": "Synthetic",
            "upstream_model": "fixture",
            "base_url": "https://fixture.invalid/v1",
            "context_window": 1048576,
            "max_output_tokens": 524288,
            "input_modalities": ["text"],
            "reasoning": True,
            "has_secret": True,
            "usd_rates": {"input": "1", "output": "1", "cache_read": "1", "cache_write": "1"},
            "pricing_source": "Synthetic USD",
        }
    )
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Retain the original task and re-enter with fresh execution",
            "budget": {
                "max_cost": "7",
                "max_minutes": 5,
                "currency": "USD",
            },
            "ctf_options": {"dispatch_mode": "automatic", "max_teammates": 1},
        },
        business_contract=BOARD_CONTRACT,
        pi_model=(options.pi_model_authority, model),
    )
    path = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
    user = {"Authorization": "Bearer " + issue_user_token(options, "fixture-owner")}
    deployment = {
        **config(),
        "runner_profile": "node-sandbox-host-v1",
        "journal_writer_lock": True,
        "sandbox_network": {},
    }
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
        ) as http,
        RuntimeLock(runtime_settings(ctf_url), fenced=True) as lock,
    ):
        assert lock.backend_started_at is not None
        request = {
            "request_id": str(uuid4()),
            "model_alias": "fixture-model",
            "budget_currency": "USD",
        }
        external_alias = "mcp_fixture_read_status"
        schema = {"type": "object"}
        grants = {
            "lead": {
                external_alias: FrozenNativeMcpTool(
                    authority=options.pi_model_authority,
                    name="fixture-mcp",
                    version=1,
                    tool=McpToolDetail(
                        name="read_status",
                        input_schema=schema,
                        schema_digest=tool_schema_digest(schema, None),
                    ),
                ).model_dump(mode="json")
            }
        }
        async with ctf.repo.engine.begin() as conn:
            await native.repo.lock(conn, tid)
            await native.queue_locked(
                conn,
                tid,
                NativeRunnerLaunch.model_validate(request),
                "a" * 64,
                native_grants=grants,
            )
        original = (await http.get(path, headers=SERVICE)).json()
        claimed = await http.post(
            path + "/claim",
            headers=SERVICE,
            json={
                "controller_id": "recovery-controller",
                "controller_fence": lock.fence,
                "controller_pid": lock.backend_pid,
                "controller_started_at": lock.backend_started_at.isoformat(),
                "resource_config": deployment,
            },
        )
        assert claimed.status_code == 200, claimed.text
        generation = 1

        async def action(name, *, expected=200, **values):
            reply = await http.post(
                path + "/actions",
                headers=SERVICE,
                json={
                    "operation_id": original["operation_id"],
                    "controller_id": "recovery-controller",
                    "controller_generation": lock.fence,
                    "resource_generation": generation,
                    "action": name,
                    **values,
                },
            )
            assert reply.status_code == expected, reply.text
            return reply.json()

        async def status():
            reply = await http.get(path + "/recovery", headers=user)
            assert reply.status_code == 200, reply.text
            return reply.json()

        pvc = {key: {"uid": str(uuid4())} for key in ("journal_pvc", "workspace_pvc")}
        namespace_uid = str(uuid4())

        async def material() -> tuple[dict[str, Any], dict[str, Any]]:
            assert (await action("sandbox_create"))["create_allowed"]
            assert not (await action("sandbox_create"))["create_allowed"]
            observation = {
                **pvc,
                "namespace_uid": namespace_uid,
                "resource_generation": generation,
                "journal_subdir": f"epoch-{generation}",
                "journal_lock_id": "31:1234",
                "journal_storage": {
                    "node": "fixture-node",
                    "node_uid": "fixture-node-uid",
                    "journal_pv_uid": "fixture-journal-pv",
                    "workspace_pv_uid": "fixture-workspace-pv",
                },
                "runner_secret": {"uid": str(uuid4())},
                "sandbox_host_secret": {"uid": str(uuid4())},
                "runner": {
                    "pod_uid": str(uuid4()),
                    "container_id": f"runner-{generation}",
                    "host_container_id": f"host-{generation}",
                    "phase": "Running",
                    "journal_writer_lock": True,
                    "pod_ip": f"10.0.0.{generation}",
                },
                "sandbox": {
                    "id": str(uuid4()),
                    "cr_uid": str(uuid4()),
                    "pod_uid": str(uuid4()),
                    "container_id": f"sandbox-{generation}",
                    "environment_revision": deployment["environment_revision"],
                },
            }
            observed = await action("observation", observation=observation)
            assert observed["state"] == "resources_ready"
            host = {
                "sandbox_id": observation["sandbox"]["id"],
                "sandbox_pod_uid": observation["sandbox"]["pod_uid"],
                "environment_revision": deployment["environment_revision"],
                "execution_context_id": str(generation) * 64,
                "execd_endpoint": "http://execd.invalid",
                "host_url": f"http://10.0.0.{generation}:8766",
            }
            host_material = await action("sandbox_host_material", proof={"context": host})
            async with ctf.repo.engine.connect() as conn:
                record = await native._get(conn, tid)
                context = record["sandbox_host"]["context"]
            await action(
                "sandbox_host_ready",
                proof={"context_digest": gateway.sandbox.context_digest(context)},
            )
            value = await action("material")
            assert host_material["mcp_transport"] == "direct-v2"
            assert set(value["bootstrap"]["mcp_targets"]) == {"business", "sandbox"}
            assert "mcp_gateway_url" not in value["bootstrap"]
            return {
                "fence": value["bootstrap"]["fence"],
                "token": value["credentials"]["control"]["token"],
                "sandbox_report_key": host_material["report_key"],
            }, observed["observation"]

        async def commands(owner):
            reply = await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
            assert reply.status_code == 200, reply.text
            return [(item, json.loads(item["payload_json"])) for item in reply.json()["commands"]]

        async def prepare(owner):
            [(command, payload)] = await commands(owner)
            binding = payload["spec"]["binding"]
            assert payload["spec"]["mcp_tool_routes"][external_alias] == {
                "target": "business",
                "wire_name": external_alias,
            }
            await acquire(http, tid, owner, binding, command)
            first = cursor(empty_cursor(), f"sdk{generation}")
            sdk = f"fresh-sdk-{generation}"
            await confirm(
                http,
                tid,
                owner,
                binding,
                {
                    **confirmation(owner, binding, empty_cursor(), first),
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
                cursor=first,
                journal_confirmed=True,
                sdk_idle=True,
            )
            return binding, sdk, first

        old, observed = await material()
        old_binding, old_sdk, first = await prepare(old)
        assert (await action("activate"))["state"] == "ready"
        [old_input] = [payload for _, payload in await commands(old) if payload["kind"] == "input"]
        tail = cursor(first, "old_input")
        await confirm(
            http,
            tid,
            old,
            old_binding,
            {
                **confirmation(
                    old,
                    old_binding,
                    first,
                    tail,
                    [
                        {
                            "input_id": old_input["input_id"],
                            "entry_id": tail["leaf_id"],
                            "seq": tail["seq"],
                        }
                    ],
                ),
                "sdk_session_id": old_sdk,
            },
        )
        await action("error", error_code="native_resource_unknown", proof={"revoke_owner": True})
        incident = await status()
        intent = {
            "request_id": str(uuid4()),
            "incident_id": incident["incident_id"],
            "instruction": "Continue without replaying the old input",
        }
        proof = {"incident_id": incident["incident_id"]}
        await action("recovery_prepare", expected=409, proof=proof)
        assert (
            await http.post(path + "/recovery", headers=SERVICE, json=intent)
        ).status_code == 403
        for _ in range(2):
            accepted = await http.post(path + "/recovery", headers=user, json=intent)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["request_id"] == intent["request_id"]
        assert (await status())["state"] == "requested"
        exit_proof = {
            **proof,
            "runner_terminated": True,
            "sandbox_terminated": True,
            "pvcs_retained": True,
            "observation_digest": runner_payload_digest(runner_json_text(observed)),
        }
        await action(
            "recovery_exit", expected=409, proof={**exit_proof, "runner_terminated": False}
        )
        await action("recovery_exit", proof=exit_proof)

        async with ctf.repo.engine.begin() as conn:
            await ctf.repo.lock(conn, tid)
            await conn.execute(
                insert(s.sandbox_command_records).values(
                    task_id=tid,
                    id="original-completed-command",
                    binding_id=old_binding["binding_id"],
                    agent_id="lead",
                    role="lead",
                    assignment_id="old-lead",
                    generation=1,
                    epoch=old["fence"]["epoch"],
                    execution_id="old-execution",
                    state="completed",
                    exit_code=0,
                    created_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    data={
                        "scope_state": "active",
                        "binding": {
                            "epoch": old["fence"]["epoch"],
                            "sandbox_id": observed["sandbox"]["id"],
                            "pod_uid": observed["sandbox"]["pod_uid"],
                            "execution_context_id": "1" * 64,
                        },
                    },
                )
            )
            await conn.execute(
                insert(s.runner_calls).values(
                    task_id=tid,
                    id="tool:unknown",
                    owner_epoch=old["fence"]["epoch"],
                    binding_id=old_binding["binding_id"],
                    kind="tool",
                    state="unknown",
                    concurrency_active=False,
                    data={},
                    created_at=datetime.now(UTC),
                )
            )
        assert (await action("recovery_prepare", proof=proof))[
            "blocker"
        ] == "original_call_unresolved"
        await gateway.save_business_result(tid, "unknown", {"result": "original confirmed result"})
        table = s.sandbox_command_records
        async with ctf.repo.engine.begin() as conn:
            command = await row(conn, table, tid, "original-completed-command")
            original_scope = command["data"]
            await conn.execute(
                update(table)
                .where(
                    table.c.task_id == tid,
                    table.c.id == command["id"],
                )
                .values(
                    data={
                        **original_scope,
                        "binding": {**original_scope["binding"], "pod_uid": "wrong-pod"},
                    }
                )
            )
        assert (await action("recovery_prepare", proof=proof))[
            "blocker"
        ] == "original_command_unresolved"
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(table)
                .where(
                    table.c.task_id == tid,
                    table.c.id == command["id"],
                )
                .values(data=original_scope)
            )
        async with ctf.repo.engine.connect() as conn:
            old_row = await row(conn, s.runner_bindings, tid, old_binding["binding_id"])
        plan = await action("recovery_prepare", proof=proof)
        entries = plan["manifest"]["bindings"]
        assert entries[0]["cursor"] == tail and entries[0]["sdk_session_id"] == old_sdk
        proof.update(
            manifest_digest=plan["manifest_digest"],
            writer_released=True,
            journal_pvc_uid=pvc["journal_pvc"]["uid"],
            workspace_pvc_uid=pvc["workspace_pvc"]["uid"],
            verifier_pod_uid=str(uuid4()),
            work_copy={
                "directory": "epoch-2",
                "bindings": [
                    {key: item[key] for key in ("binding_id", "sdk_session_id", "cursor")}
                    for item in entries
                ],
            },
        )
        await action("recovery_replace", expected=409, proof=proof)
        verifier = {
            key: proof[key] for key in ("incident_id", "manifest_digest", "verifier_pod_uid")
        }
        admit = {key: proof[key] for key in ("incident_id", "manifest_digest")}
        assert (await action("recovery_verifier_admit", proof=admit))["create_allowed"]
        assert not (await action("recovery_verifier_admit", proof=admit))["create_allowed"]
        await action("recovery_verifier_observe", proof={**verifier, "verifier_terminated": False})
        await action("recovery_replace", expected=409, proof=proof)
        terminal = {
            **verifier,
            "verifier_init_container_id": "copy-init",
            "verifier_container_id": "copy",
            "verifier_terminated": True,
            "work_copy": proof["work_copy"],
        }
        await action(
            "recovery_verifier_observe",
            expected=409,
            proof={**terminal, "verifier_pod_uid": "different"},
        )
        await action("recovery_verifier_observe", proof=terminal)
        await action("recovery_replace", expected=409, proof={**proof, "writer_released": False})
        replacement = await action("recovery_replace", proof=proof)
        assert replacement["hard_deadline"] == original["hard_deadline"]
        assert replacement["data"]["sandbox_create_op_id"] != original["operation_id"]
        assert replacement["data"]["resource_generation"] == 2
        assert (
            not replacement["sandbox_create_claimed"] and (await status())["state"] == "rebuilding"
        )
        assert replacement["data"]["mcp_transport"] == "direct-v2"
        assert replacement["data"]["native_mcp"] == grants
        await action("sandbox_create", expected=409)
        generation = 2
        fresh, current_observation = await material()
        assert current_observation["runner"]["pod_uid"] != observed["runner"]["pod_uid"]
        assert current_observation["sandbox"]["id"] != observed["sandbox"]["id"]
        assert fresh["fence"]["epoch"] == old["fence"]["epoch"] + 1
        assert not await gateway.sandbox.pending_scopes(tid, fresh["sandbox_report_key"])
        with pytest.raises(HTTPException, match="exited epoch"):
            await gateway.sandbox.claim_scope(
                tid, "original-completed-command", fresh["sandbox_report_key"]
            )
        denied = await http.get(
            control(tid) + "/commands", headers=auth(old), params=poll_query(old)
        )
        assert denied.status_code in {403, 409}
        new_binding, new_sdk, new_first = await prepare(fresh)
        assert new_binding["binding_id"] != old_binding["binding_id"] and new_sdk != old_sdk
        assert (await action("activate"))["state"] == "ready"
        [new_input] = [
            payload for _, payload in await commands(fresh) if payload["kind"] == "input"
        ]
        assert new_input["input_id"] != old_input["input_id"]
        assert json.loads(new_input["content"])["instruction"] == intent["instruction"]
        final = await status()
        assert final["state"] == "resumed" and final["replacement_epoch"] == fresh["fence"]["epoch"]
        async with ctf.repo.engine.connect() as conn:
            assert await row(conn, s.runner_bindings, tid, old_binding["binding_id"]) == old_row
            command = await row(conn, s.sandbox_command_records, tid, "original-completed-command")
            assert command["data"]["scope_state"] == "active" and command["state"] == "completed"
            assert not hasattr(gateway, "routes") and not hasattr(gateway, "native")
        new_tail = cursor(new_first, "new_input")
        await confirm(
            http,
            tid,
            fresh,
            new_binding,
            {
                **confirmation(
                    fresh,
                    new_binding,
                    new_first,
                    new_tail,
                    [
                        {
                            "input_id": new_input["input_id"],
                            "entry_id": new_tail["leaf_id"],
                            "seq": new_tail["seq"],
                        }
                    ],
                ),
                "sdk_session_id": new_sdk,
            },
        )
        await action("error", error_code="native_resource_unknown", proof={"revoke_owner": True})
        second = await status()
        assert second["state"] == "frozen" and second["incident_id"] != incident["incident_id"]
        requested = await http.post(
            path + "/recovery",
            headers=user,
            json={
                "request_id": str(uuid4()),
                "incident_id": second["incident_id"],
            },
        )
        assert requested.status_code == 200, requested.text
        second_identity = {"incident_id": second["incident_id"]}
        second_exit = {
            **second_identity,
            "runner_terminated": True,
            "sandbox_terminated": True,
            "pvcs_retained": True,
            "observation_digest": runner_payload_digest(runner_json_text(current_observation)),
        }
        await action("recovery_exit", proof=second_exit)
        plan = await action("recovery_prepare", proof=second_identity)
        second_admit = {**second_identity, "manifest_digest": plan["manifest_digest"]}
        assert (await action("recovery_verifier_admit", proof=second_admit))["create_allowed"]
        await action("stop")
        await action("recovery_prepare", expected=409, proof=second_identity)
        await action("recovery_verifier_admit", expected=409, proof=second_admit)
        assert (await action("cleanup", proof=second_exit))["state"] == "unknown"
        assert (await status())["state"] == "stopping"
        await action(
            "recovery_verifier_observe",
            proof={
                **second_admit,
                "verifier_pod_uid": str(uuid4()),
                "verifier_init_container_id": "second-init",
                "verifier_container_id": "second-verify",
                "verifier_terminated": True,
            },
        )
        assert (await action("cleanup", proof=second_exit))["state"] == "stopped"
