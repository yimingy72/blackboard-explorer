"""Real PG control contracts with explicit synthetic resource observations, no workloads."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.native_runner_bootstrap import identity_id
from bbx_blackboard.runner_auth import issue_runner_control_token
from bbx_blackboard.runner_control import fence
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import BOARD_CONTRACT
from bbx_contracts.pi_models import PiModelVersion
from bbx_contracts.runner import PROTOCOL, runner_json_text, runner_payload_digest
from bbx_contracts.runner_bootstrap import NativeRunnerAction
from bbx_runtime.scheduler.lock import RuntimeLock
from bbx_runtime.settings import NativeRunnerSettings
from pydantic import SecretStr
from sqlalchemy import insert, select, update
from sqlalchemy.engine import make_url
from test_board_flags_integration import Objects
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
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
    mgmt,
    poll_query,
    register_owner,
    registration,
)

pytestmark = pytest.mark.integration


def fixture_pi_model(alias="synthetic-model"):
    return (
        "fixture-authority",
        PiModelVersion.model_validate(
            {
                "id": alias,
                "model_revision": 1,
                "label": "Synthetic Native model",
                "upstream_model": "fixture",
                "base_url": "https://fixture.invalid/v1",
                "context_window": 1048576,
                "max_output_tokens": 524288,
                "input_modalities": ["text"],
                "has_secret": True,
                "tool_call": True,
                "reasoning": True,
                "usd_rates": {"input": "1", "output": "1", "cache_read": "1", "cache_write": "1"},
                "pricing_source": "Synthetic USD",
            }
        ),
    )


def config():
    return {
        "source_id": "synthetic-native-resource-control",
        "cluster_uid": str(uuid4()),
        "runner_image": "fixture-runner@sha256:" + "a" * 64,
        "sandbox_image": "fixture-sandbox@sha256:" + "b" * 64,
        "environment_revision": "synthetic-env-1",
        "runner_blackboard_url": "https://blackboard.invalid",
        "runner_model_proxy_url": "https://runtime.invalid",
        "runner_profile": "node-sandbox-host-v1",
    }


def runtime_settings(url):
    parsed = make_url(url)
    return NativeRunnerSettings.model_construct(
        postgres_host=parsed.host,
        postgres_port=parsed.port,
        postgres_user=parsed.username,
        postgres_password=SecretStr(parsed.password or ""),
        postgres_db=parsed.database,
        service_token=SecretStr("service-test"),
        runner_resource_config_file=Path("/unused-resource-config.json"),
        runner_blackboard_url="https://blackboard.invalid",
        runner_model_proxy_url="https://runtime.invalid",
        runner_gateway_url="http://native.invalid",
    )


async def fixture_owner(native, tid, actor):
    async with native.repo.engine.begin() as conn:
        record, _ = await native._checked(
            conn, tid, NativeRunnerAction.model_validate({**actor, "action": "material"}), live=True
        )
        owner = await native.control._register_owner(
            conn,
            tid,
            runner_json_text(
                {
                    "request_id": identity_id(record["operation_id"], "owner"),
                    "runner_id": identity_id(record["operation_id"], "runner"),
                    "pod_uid": record["observation"]["runner"]["pod_uid"],
                    "expected_epoch": 0,
                    "hard_deadline": record["hard_deadline"].isoformat(),
                }
            ).encode(),
        )
    return {"fence": fence(owner), "token": issue_runner_control_token(native.settings, owner)}


async def test_native_bootstrap_fence_lead_closing_and_exit_reconciliation(
    ctf, ctf_url, monkeypatch
):
    options = settings().model_copy(
        update={
            "pi_site_id": "fixture-primary",
            "pi_model_authority": "fixture-authority",
            "sandbox_tenant_resolver_token": SecretStr("synthetic-resolver-only"),
        }
    )
    ctf.objects = Objects()
    app = create_app(options, engine=ctf.repo.engine, objects=ctf.objects)
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Inspect the trusted native bootstrap control contract",
            "budget": {"max_cost": "1", "max_minutes": 5},
            "ctf_options": {"max_teammates": 1},
        },
        business_contract=BOARD_CONTRACT,
        pi_model=fixture_pi_model(),
    )
    path = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
    request = {
        "request_id": str(uuid4()),
        "model_alias": "synthetic-model",
        "exchange_rate": {
            "rate_cny_per_usd": "7",
            "source_ref": "fixture://frozen-fx",
            "observed_at": datetime.now(UTC).isoformat(),
        },
    }
    deployment = config()
    lock_settings = runtime_settings(ctf_url)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as http:
        denied = await http.post(path, json=request)
        assert denied.status_code in {401, 403}
        invalid = await http.post(
            path,
            headers=SERVICE,
            json={
                **request,
                "exchange_rate": {**request["exchange_rate"], "observed_at": "2026-11-06T12:00:00"},
            },
        )
        assert invalid.status_code == 422, invalid.text
        async with ctf.repo.engine.connect() as conn:
            assert not (
                await conn.execute(
                    select(s.task_runner_resources).where(s.task_runner_resources.c.task_id == tid)
                )
            ).first()
        response = await http.post(path, headers=SERVICE, json=request)
        assert response.status_code == 200, response.text
        queued = response.json()
        assert queued["state"] == "queued"
        assert (await http.post(path, headers=SERVICE, json=request)).json()[
            "operation_id"
        ] == queued["operation_id"]
        assert (
            await http.post(path, headers=SERVICE, json={**request, "model_alias": "changed"})
        ).status_code == 409
        async with RuntimeLock(lock_settings, fenced=True) as first:
            assert first.fence is not None and first.backend_started_at is not None
            claim_a = {
                "controller_id": "controller-a",
                "controller_fence": first.fence,
                "controller_pid": first.backend_pid,
                "controller_started_at": first.backend_started_at.isoformat(),
                "resource_config": deployment,
            }
            response = await http.post(path + "/claim", headers=SERVICE, json=claim_a)
            assert response.status_code == 200, response.text
            claimed = response.json()
            key = claimed.pop("tenant_key")
            assert key not in json.dumps((await http.get(path, headers=SERVICE)).json())
            tenant = await http.get(
                "/api/opensandbox/tenants",
                headers={
                    "x-bbx-sandbox-resolver": "synthetic-resolver-only",
                    "open-sandbox-api-key": key,
                },
            )
            assert tenant.json() == {"namespace": queued["namespace"], "ttl": 0}
            assert (
                await http.get("/api/opensandbox/tenants", headers={"open-sandbox-api-key": key})
            ).status_code == 401
            stale_session = {
                **claim_a,
                "controller_started_at": (first.backend_started_at - timedelta(days=1)).isoformat(),
            }
            assert (
                await http.post(path + "/claim", headers=SERVICE, json=stale_session)
            ).status_code == 409
            action_a = {
                "operation_id": queued["operation_id"],
                "controller_id": "controller-a",
                "controller_generation": first.fence,
            }
            dispatch = await http.post(
                path + "/actions", headers=SERVICE, json={**action_a, "action": "sandbox_create"}
            )
            assert dispatch.json()["create_allowed"] is True
            assert (
                await http.post(
                    path + "/actions",
                    headers=SERVICE,
                    json={**action_a, "action": "sandbox_create"},
                )
            ).json()["create_allowed"] is False
            observed = {
                "task_id": str(tid),
                "operation_id": queued["operation_id"],
                "namespace": queued["namespace"],
                "source_id": deployment["source_id"],
                "cluster_uid": deployment["cluster_uid"],
                "namespace_uid": str(uuid4()),
                "journal_pvc": {"name": "bbx-journal", "uid": str(uuid4())},
                "workspace_pvc": {"name": "bbx-workspace", "uid": str(uuid4())},
                "runner_secret": {"name": "bbx-runner", "uid": str(uuid4())},
                "sandbox": {
                    "id": str(uuid4()),
                    "cr_uid": str(uuid4()),
                    "pod_name": "sandbox-original",
                    "pod_uid": str(uuid4()),
                    "container_id": "containerd://synthetic-sandbox",
                    "environment_revision": deployment["environment_revision"],
                    "phase": "Running",
                },
                "runner": {
                    "pod_name": "bbx-runner",
                    "pod_uid": str(uuid4()),
                    "container_id": "containerd://synthetic-runner",
                    "host_container_id": "containerd://synthetic-host",
                    "phase": "Running",
                    "terminated": False,
                },
            }
            for _ in range(2):
                response = await http.post(
                    path + "/actions",
                    headers=SERVICE,
                    json={**action_a, "action": "observation", "observation": observed},
                )
                assert response.status_code == 200, response.text
                assert response.json()["state"] == "resources_ready"
            async with ctf.repo.engine.connect() as conn:
                turns = (
                    (await conn.execute(select(s.ctf_turns).where(s.ctf_turns.c.task_id == tid)))
                    .mappings()
                    .all()
                )
                member = (
                    (
                        await conn.execute(
                            select(s.ctf_members).where(
                                s.ctf_members.c.task_id == tid, s.ctf_members.c.id == "lead"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert len(turns) == 1 and turns[0]["generation"] == member["generation"] == 1
                assert member["current_turn_id"] == turns[0]["id"]
            unavailable = {**observed, "sandbox": {**observed["sandbox"], "observation": "unknown"}}
            assert (
                await http.post(
                    path + "/actions",
                    headers=SERVICE,
                    json={**action_a, "action": "observation", "observation": unavailable},
                )
            ).status_code == 200
            refused = await http.post(
                path + "/actions", headers=SERVICE, json={**action_a, "action": "material"}
            )
            assert refused.status_code == 409
            restored = {**observed, "sandbox": {**observed["sandbox"], "observation": None}}
            assert (
                await http.post(
                    path + "/actions",
                    headers=SERVICE,
                    json={**action_a, "action": "observation", "observation": restored},
                )
            ).status_code == 200
            # Synthetic control identity tests the finite closing filter only.
            owner = await fixture_owner(app.state.native_runner_bootstrap, tid, action_a)
            binding_request = registration(owner)
            registered = await http.post(
                mgmt(tid) + "/bindings", headers=SERVICE, json=binding_request
            )
            assert registered.status_code == 200, registered.text
            stopped = await http.post(f"/api/tasks/{tid}/stop", headers=SERVICE)
            assert stopped.status_code == 200, stopped.text
            assert (await http.get(path, headers=SERVICE)).json()["desired"] == "stopped"
            payload = runner_json_text({"kind": "shutdown"})
            enqueued = await http.post(
                mgmt(tid) + "/commands",
                headers=SERVICE,
                json={
                    "request_id": str(uuid4()),
                    "expected_epoch": owner["fence"]["epoch"],
                    "payload_json": payload,
                    "payload_digest": runner_payload_digest(payload),
                },
            )
            assert enqueued.status_code == 200, enqueued.text
            polled = await http.get(
                control(tid) + "/commands",
                headers={"authorization": "Bearer " + owner["token"]},
                params=poll_query(owner),
            )
            assert polled.status_code == 200, polled.text
            assert all(
                json.loads(command["payload_json"])["kind"] == "shutdown"
                for command in polled.json()["commands"]
            )
            assert polled.json()["lease_renewals"] == []
        # A new global fence rejects A before B claims this Task, and A cannot reclaim by retrying.
        async with RuntimeLock(lock_settings, fenced=True) as second:
            assert second.fence is not None and second.backend_started_at is not None
            assert first.fence is not None
            assert second.fence > first.fence
            denied = await http.post(
                path + "/actions", headers=SERVICE, json={**action_a, "action": "error"}
            )
            assert denied.status_code == 409
            assert (
                await http.post(path + "/claim", headers=SERVICE, json=claim_a)
            ).status_code == 409
            claim_b = {
                **claim_a,
                "controller_id": "controller-b",
                "controller_fence": second.fence,
                "controller_pid": second.backend_pid,
                "controller_started_at": second.backend_started_at.isoformat(),
            }
            assert (
                await http.post(path + "/claim", headers=SERVICE, json=claim_b)
            ).status_code == 200
            action_b = {
                **action_a,
                "controller_id": "controller-b",
                "controller_generation": second.fence,
            }
            assert (
                await http.post(
                    path + "/actions",
                    headers=SERVICE,
                    json={**action_a, "controller_generation": second.fence, "action": "error"},
                )
            ).status_code == 409
            checkpoint = (await http.get(path, headers=SERVICE)).json()
            false_cleanup = await http.post(
                path + "/actions",
                headers=SERVICE,
                json={
                    **action_b,
                    "action": "cleanup",
                    "proof": {
                        "not_started": True,
                        "pvcs_retained": True,
                        "observation_digest": runner_payload_digest(
                            runner_json_text(checkpoint["observation"])
                        ),
                    },
                },
            )
            assert false_cleanup.status_code == 200 and false_cleanup.json()["state"] == "unknown"
            native = app.state.native_runner_bootstrap
            original_finalize = native.ctf.finalize_close

            async def interrupted(*args, **kwargs):
                raise RuntimeError("Synthetic interruption after positive resource-exit commit")

            monkeypatch.setattr(native.ctf, "finalize_close", interrupted)
            proof = {
                "observation_digest": runner_payload_digest(
                    runner_json_text(checkpoint["observation"])
                ),
                "runner_terminated": True,
                "sandbox_terminated": True,
                "pvcs_retained": True,
            }
            with pytest.raises(RuntimeError):
                await native.act(
                    tid,
                    runner_json_text({**action_b, "action": "cleanup", "proof": proof}).encode(),
                )
            exited = (await http.get(path, headers=SERVICE)).json()
            assert exited["state"] == "exited" and exited["data"]["exit_confirmed"] is True
            assert (
                str(tid)
                in (await http.get("/api/native-runner/bootstraps", headers=SERVICE)).json()[
                    "task_ids"
                ]
            )
            assert (
                await http.post(
                    path + "/actions",
                    headers=SERVICE,
                    json={
                        **action_b,
                        "action": "error",
                        "error_code": "native_control_unavailable",
                    },
                )
            ).json()["state"] == "exited"
            monkeypatch.setattr(native.ctf, "finalize_close", original_finalize)
            finished = await http.post(
                path + "/actions", headers=SERVICE, json={**action_b, "action": "cleanup"}
            )
            assert finished.status_code == 200 and finished.json()["state"] == "stopped"
            assert (
                await http.get(
                    "/api/opensandbox/tenants",
                    headers={
                        "x-bbx-sandbox-resolver": "synthetic-resolver-only",
                        "open-sandbox-api-key": key,
                    },
                )
            ).status_code == 401


async def test_native_never_dispatched_cancel_and_retained_ownership(ctf, ctf_url):
    from bbx_blackboard.conversations import Conversations
    from fastapi import HTTPException
    from sqlalchemy import update

    options = settings().model_copy(
        update={
            "pi_site_id": "fixture-primary",
            "pi_model_authority": "fixture-authority",
            "sandbox_tenant_resolver_token": SecretStr("synthetic-resolver-only"),
        }
    )
    ctf.objects = Objects()
    app = create_app(options, engine=ctf.repo.engine, objects=ctf.objects)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as http:

        async def launch_task():
            tid = await ctf.create_task(
                {
                    "mode": "ctf",
                    "goal": "Cancel before native execution dispatch",
                    "budget": {"max_cost": "1", "max_minutes": 5},
                    "ctf_options": {"max_teammates": 1},
                },
                business_contract=BOARD_CONTRACT,
                pi_model=fixture_pi_model(),
            )
            path = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
            requested = await http.post(
                path,
                headers=SERVICE,
                json={
                    "request_id": str(uuid4()),
                    "model_alias": "synthetic-model",
                    "exchange_rate": {
                        "rate_cny_per_usd": "7",
                        "source_ref": "fixture://fx",
                        "observed_at": datetime.now(UTC).isoformat(),
                    },
                },
            )
            assert requested.status_code == 200, requested.text
            return tid, path, requested.json()

        async with RuntimeLock(runtime_settings(ctf_url), fenced=True) as instance:
            assert instance.backend_started_at is not None
            claim = {
                "controller_id": "never-dispatched-controller",
                "controller_fence": instance.fence,
                "controller_pid": instance.backend_pid,
                "controller_started_at": instance.backend_started_at.isoformat(),
                "resource_config": config(),
            }
            tid, path, queued = await launch_task()
            assert (
                await http.post(path + "/claim", headers=SERVICE, json=claim)
            ).status_code == 200
            assert (await http.post(f"/api/tasks/{tid}/stop", headers=SERVICE)).status_code == 200
            actor = {
                "operation_id": queued["operation_id"],
                "controller_id": claim["controller_id"],
                "controller_generation": instance.fence,
            }
            response = await http.post(
                path + "/actions",
                headers=SERVICE,
                json={
                    **actor,
                    "action": "cleanup",
                    "proof": {
                        "not_started": True,
                        "pvcs_retained": True,
                        "observation_digest": runner_payload_digest(runner_json_text({})),
                    },
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["state"] == "stopped"
            assert response.json()["data"]["exit_reason"] == "not_dispatched"
            async with ctf.repo.engine.connect() as conn:
                assert not (
                    await conn.execute(select(s.ctf_turns).where(s.ctf_turns.c.task_id == tid))
                ).first()
            await Conversations(ctf.repo.engine).request_delete(tid)
            with pytest.raises(HTTPException, match="retained volumes"):
                await Conversations(ctf.repo.engine).purge(tid, ctf.objects)
            # A never-claimed request cannot acquire a deployment after deletion begins.
            unclaimed, unclaimed_path, _ = await launch_task()
            assert (
                await http.post(f"/api/tasks/{unclaimed}/stop", headers=SERVICE)
            ).status_code == 200
            await Conversations(ctf.repo.engine).request_delete(unclaimed)
            assert (
                await http.post(unclaimed_path + "/claim", headers=SERVICE, json=claim)
            ).status_code == 409
            assert await Conversations(ctf.repo.engine).purge(unclaimed, ctf.objects) == {
                "purged": True
            }
            expired, expired_path, expiry_request = await launch_task()
            assert (
                await http.post(expired_path + "/claim", headers=SERVICE, json=claim)
            ).status_code == 200
            async with ctf.repo.engine.begin() as conn:
                await app.state.native_runner_bootstrap.repo.lock(conn, expired)
                await conn.execute(
                    update(s.task_runner_resources)
                    .where(s.task_runner_resources.c.task_id == expired)
                    .values(hard_deadline=datetime.now(UTC) - timedelta(seconds=1))
                )
            timed_out = await http.post(
                expired_path + "/actions",
                headers=SERVICE,
                json={
                    **actor,
                    "operation_id": expiry_request["operation_id"],
                    "action": "stop",
                },
            )
            assert timed_out.status_code == 200, timed_out.text
            async with ctf.repo.engine.connect() as conn:
                conclusion = (
                    await conn.execute(
                        select(s.tasks.c.ctf_conclusion).where(s.tasks.c.id == expired)
                    )
                ).scalar_one()
                assert conclusion["end_reason"] == "budget_exhausted"


async def test_native_hold_refreshes_only_control_and_never_activates_input(ctf, ctf_url):
    from sqlalchemy import func, update

    options = settings().model_copy(
        update={
            "pi_site_id": "fixture-primary",
            "pi_model_authority": "fixture-authority",
            "sandbox_tenant_resolver_token": SecretStr("synthetic-resolver-only"),
        }
    )
    ctf.objects = Objects()
    app = create_app(options, engine=ctf.repo.engine, objects=ctf.objects)
    native = app.state.native_runner_bootstrap
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Preserve control without new budget admissions",
            "budget": {"max_cost": "1", "max_minutes": 10},
            "ctf_options": {"max_teammates": 1},
        },
        business_contract=BOARD_CONTRACT,
        pi_model=fixture_pi_model(),
    )
    path = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
    deployment = config()
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
        ) as http,
        RuntimeLock(runtime_settings(ctf_url), fenced=True) as instance,
    ):
        assert instance.backend_started_at is not None
        launched = await http.post(
            path,
            headers=SERVICE,
            json={
                "request_id": str(uuid4()),
                "model_alias": "synthetic-model",
                "exchange_rate": {
                    "rate_cny_per_usd": "7",
                    "source_ref": "fixture://fx",
                    "observed_at": datetime.now(UTC).isoformat(),
                },
            },
        )
        assert launched.status_code == 200, launched.text
        queued = launched.json()
        claim = {
            "controller_id": "held-control",
            "controller_fence": instance.fence,
            "controller_pid": instance.backend_pid,
            "controller_started_at": instance.backend_started_at.isoformat(),
            "resource_config": deployment,
        }
        assert (await http.post(path + "/claim", headers=SERVICE, json=claim)).status_code == 200
        actor = {
            "operation_id": queued["operation_id"],
            "controller_id": "held-control",
            "controller_generation": instance.fence,
        }
        observed = {
            "task_id": str(tid),
            "operation_id": queued["operation_id"],
            "namespace": queued["namespace"],
            "source_id": deployment["source_id"],
            "cluster_uid": deployment["cluster_uid"],
            "namespace_uid": str(uuid4()),
            "journal_pvc": {"name": "journal", "uid": str(uuid4())},
            "workspace_pvc": {"name": "workspace", "uid": str(uuid4())},
            "runner_secret": {"name": "runner", "uid": str(uuid4())},
            "sandbox": {
                "id": str(uuid4()),
                "cr_uid": str(uuid4()),
                "pod_name": "sandbox",
                "pod_uid": str(uuid4()),
                "container_id": "containerd://sandbox",
                "environment_revision": deployment["environment_revision"],
            },
            "runner": {
                "pod_name": "runner",
                "pod_uid": str(uuid4()),
                "container_id": "containerd://runner",
                "phase": "Running",
                "terminated": False,
            },
        }
        assert (
            await http.post(
                path + "/actions",
                headers=SERVICE,
                json={**actor, "action": "observation", "observation": observed},
            )
        ).status_code == 200
        owner = await fixture_owner(native, tid, actor)
        bound = await http.post(mgmt(tid) + "/bindings", headers=SERVICE, json=registration(owner))
        assert bound.status_code == 200, bound.text
        bid = bound.json()["binding"]["binding_id"]
        # Synthetic prior publication metadata isolates control renewal from model/MCP I/O.
        bootstrap = {
            "protocol": "bbx.task-runner.v1",
            "fence": owner["fence"],
            "control_url": f"https://blackboard.invalid/api/tasks/{tid}/runner-control",
            "model_proxy_url": "https://runtime.invalid",
            "mcp_targets": {
                "business": f"https://blackboard.invalid/api/tasks/{tid}/business-mcp-v2/"
            },
            "control_credential_ref": "control",
            "credential_file": "/var/run/bbx/credentials.json",
            "journal_root": "/journal",
            "mcp_metadata_timeout_ms": 15000,
        }
        async with ctf.repo.engine.begin() as conn:
            await native.repo.lock(conn, tid)
            record = await native._get(conn, tid)
            task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
            await conn.execute(
                update(s.task_runner_resources)
                .where(s.task_runner_resources.c.task_id == tid)
                .values(sandbox_host={"ready": True, "context": {}})
            )
            await native._set(
                conn,
                tid,
                data={
                    **record["data"],
                    "owner_epoch": owner["fence"]["epoch"],
                    "binding_id": bid,
                    "bootstrap": bootstrap,
                    "credentials_issued": True,
                },
                state="runner_starting",
            )
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={
                        **task["ctf_control"],
                        "native_budget_hold": {"reason": "fixture_hold"},
                        "failure_hold": {"task_wide": True},
                    }
                )
            )
            await conn.execute(
                update(s.runner_bindings)
                .where(s.runner_bindings.c.task_id == tid, s.runner_bindings.c.id == bid)
                .values(status="closing")
            )
            before = dict(
                (
                    await conn.execute(
                        select(s.runner_owners).where(s.runner_owners.c.task_id == tid)
                    )
                )
                .mappings()
                .one()
            )

        async def forbidden_io(*args, **kwargs):
            raise AssertionError("Hold must not issue native model/MCP credentials")

        native.gateway.credentials = forbidden_io
        response = await http.post(
            path + "/actions", headers=SERVICE, json={**actor, "action": "renew"}
        )
        assert response.status_code == 200, response.text
        material = response.json()
        assert material["control_only"] is True and set(material["credentials"]) == {"control"}
        assert material["bootstrap"] == bootstrap and material["status"]["desired"] == "active"
        activation = await http.post(
            path + "/actions", headers=SERVICE, json={**actor, "action": "activate"}
        )
        assert activation.status_code == 200 and activation.json()["control_only"] is True
        async with ctf.repo.engine.connect() as conn:
            after = dict(
                (
                    await conn.execute(
                        select(s.runner_owners).where(s.runner_owners.c.task_id == tid)
                    )
                )
                .mappings()
                .one()
            )
            assert (
                after["epoch"] == before["epoch"]
                and after["hard_deadline"] == before["hard_deadline"]
            )
            assert after["expires_at"] >= before["expires_at"]
            assert (
                await conn.execute(
                    select(func.count())
                    .select_from(s.runner_commands)
                    .where(s.runner_commands.c.task_id == tid, s.runner_commands.c.kind == "input")
                )
            ).scalar_one() == 0
        polled = await http.get(
            control(tid) + "/commands",
            headers={"authorization": "Bearer " + material["credentials"]["control"]["token"]},
            params=poll_query(owner),
        )
        assert polled.status_code == 200 and not polled.json()["lease_renewals"]
        assert all(
            json.loads(c["payload_json"])["kind"] in {"close", "abort", "shutdown"}
            for c in polled.json()["commands"]
        )


@pytest.mark.parametrize("reason", ["native_resource_unknown", "native_owner_expired"])
async def test_native_recovery_freezes_the_original_and_keeps_late_evidence(
    ctf, ctf_url, monkeypatch, reason
):
    options = settings().model_copy(
        update={
            "pi_site_id": "fixture-primary",
            "pi_model_authority": "fixture-authority",
            "sandbox_tenant_resolver_token": SecretStr("synthetic-resolver-only"),
        }
    )
    ctf.objects = Objects()
    app = create_app(options, engine=ctf.repo.engine, objects=ctf.objects)
    await app.state.profile_store.ensure_bundled(
        Path(__file__).resolve().parents[3] / "profiles/default"
    )
    task_input = {
        "mode": "ctf",
        "goal": "Preserve the original journal and dispatched model attempt",
        "budget": {"max_cost": "1", "max_minutes": 5},
        "ctf_options": {"max_teammates": 1},
    }
    tid = await ctf.create_task(
        task_input, business_contract=BOARD_CONTRACT, pi_model=fixture_pi_model("fixture-model")
    )
    path = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
    user = {"Authorization": "Bearer " + issue_user_token(options, "fixture-owner")}
    lock_settings, deployment = runtime_settings(ctf_url), {**config(), "sandbox_network": {}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as http:
        request = {
            "request_id": str(uuid4()),
            "model_alias": "fixture-model",
            "exchange_rate": {
                "rate_cny_per_usd": "7",
                "source_ref": "fixture://fx",
                "observed_at": datetime.now(UTC).isoformat(),
            },
        }
        if reason == "native_resource_unknown":
            preowned = await ctf.create_task(
                task_input,
                business_contract=BOARD_CONTRACT,
                pi_model=fixture_pi_model("fixture-model"),
            )
            await register_owner(http, preowned)
            blocked_launch = await http.post(
                path.replace(str(tid), str(preowned)), headers=SERVICE, json=request
            )
            assert (
                blocked_launch.status_code == 409 and "Existing Runner owner" in blocked_launch.text
            )
        start = await http.post(path, headers=SERVICE, json=request)
        assert start.status_code == 200, start.text
        operation = start.json()
        assert (await http.get(path + "/recovery", headers=SERVICE)).status_code == 403
        assert (await http.get(path + "/recovery", headers=user)).json()["state"] == "not_frozen"

        async def claim(lock, controller_id):
            assert lock.backend_started_at is not None
            response = await http.post(
                path + "/claim",
                headers=SERVICE,
                json={
                    "controller_id": controller_id,
                    "controller_fence": lock.fence,
                    "controller_pid": lock.backend_pid,
                    "controller_started_at": lock.backend_started_at.isoformat(),
                    "resource_config": deployment,
                },
            )
            assert response.status_code == 200, response.text
            actor = {
                "operation_id": operation["operation_id"],
                "controller_id": controller_id,
                "controller_generation": lock.fence,
            }
            return actor, response.json()

        async def act(actor, action, **extra):
            return await http.post(
                path + "/actions", headers=SERVICE, json={**actor, "action": action, **extra}
            )

        async def recovery():
            response = await http.get(path + "/recovery", headers=user)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            return response.json()

        observation = {
            "namespace_uid": str(uuid4()),
            "journal_pvc": {"uid": str(uuid4())},
            "workspace_pvc": {"uid": str(uuid4())},
            "runner_secret": {"uid": str(uuid4())},
            "sandbox_host_secret": {"uid": str(uuid4())},
            "sandbox": {
                "id": str(uuid4()),
                "cr_uid": str(uuid4()),
                "pod_uid": str(uuid4()),
                "container_id": "fixture-sandbox",
                "environment_revision": deployment["environment_revision"],
            },
            "runner": {
                "pod_uid": str(uuid4()),
                "container_id": "fixture-runner",
                "host_container_id": "fixture-sandbox-host",
                "pod_ip": "10.12.0.2",
                "phase": "Running",
            },
        }
        async with RuntimeLock(lock_settings, fenced=True) as first:
            actor_a, _ = await claim(first, "first-controller")
            assert (await act(actor_a, "sandbox_create")).json()["create_allowed"]
            assert (await act(actor_a, "observation", observation=observation)).status_code == 200
            forged = {
                "request_id": identity_id(operation["operation_id"], "owner"),
                "runner_id": identity_id(operation["operation_id"], "runner"),
                "pod_uid": observation["runner"]["pod_uid"],
                "expected_epoch": 0,
                "hard_deadline": operation["hard_deadline"],
            }
            assert (
                await http.post(mgmt(tid) + "/owners", headers=SERVICE, json=forged)
            ).status_code == 409
            host = await act(
                actor_a,
                "sandbox_host_material",
                proof={
                    "context": {
                        "sandbox_id": observation["sandbox"]["id"],
                        "sandbox_pod_uid": observation["sandbox"]["pod_uid"],
                        "environment_revision": deployment["environment_revision"],
                        "execution_context_id": "f" * 64,
                        "execd_endpoint": "fixture.invalid",
                        "host_url": "http://10.12.0.2:8766",
                    }
                },
            )
            assert host.status_code == 200, host.text
            ready = await act(
                actor_a,
                "sandbox_host_ready",
                proof={"context_digest": host.json()["context_digest"]},
            )
            assert ready.status_code == 200, ready.text
            issued = await act(actor_a, "material")
            assert issued.status_code == 200, issued.text
            material = issued.json()
            owner = {
                "fence": material["bootstrap"]["fence"],
                "token": material["credentials"]["control"]["token"],
            }
            epoch = owner["fence"]["epoch"]
            polled = await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
            prepare = polled.json()["commands"][0]
            binding = json.loads(prepare["payload_json"])["spec"]["binding"]
            bid = binding["binding_id"]
            permit, _ = await acquire(http, tid, owner, binding, prepare)
            original_cursor = cursor(empty_cursor(), "confirmed-original")
            first_journal = confirmation(owner, binding, empty_cursor(), original_cursor)
            await confirm(http, tid, owner, binding, first_journal)
            await acknowledge(
                http,
                tid,
                owner,
                prepare,
                "prepared",
                binding_id=bid,
                sdk_session_id=first_journal["sdk_session_id"],
                cursor=original_cursor,
                journal_confirmed=True,
                sdk_idle=True,
            )
            assert (await act(actor_a, "activate")).json()["state"] == "ready"

        async with RuntimeLock(lock_settings, fenced=True) as second:
            actor_b, healthy = await claim(second, "second-controller")
            assert healthy["state"] == "ready" and healthy["data"]["owner_epoch"] == epoch
            assert (await act(actor_a, "renew")).status_code == 409
            renewal = await act(actor_b, "renew")
            assert (
                renewal.status_code == 200
                and renewal.json()["bootstrap"]["fence"] == owner["fence"]
            )
            old_attempt, dispatch = str(uuid4()), str(uuid4())
            existing_hold = {"task_wide": True, "source": "fixture-independent"}
            async with ctf.repo.engine.begin() as conn:
                await ctf.repo.lock(conn, tid)
                task_control = (
                    await conn.execute(select(s.tasks.c.ctf_control).where(s.tasks.c.id == tid))
                ).scalar_one()
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(
                        ctf_control={
                            **task_control,
                            "failure_hold": existing_hold,
                            "native_budget_hold": {"reason": "fixture-budget-hold"},
                        }
                    )
                )
                await conn.execute(
                    insert(s.native_task_budgets).values(
                        task_id=tid,
                        accounting_source_id="fixture-accounting",
                        key_name=str(tid),
                        key_fingerprint=tid.hex,
                        key_ciphertext="never-in-recovery-view",
                        native_resource_id=str(uuid4()),
                        publication_state="ready",
                        config={"threshold_usd": "1.0"},
                        status_snapshot={"used_high_water_usd": "0.4"},
                        hold_reason="fixture-budget-hold",
                        created_at=datetime.now(UTC),
                    )
                )
                for call, state, data in (
                    (
                        old_attempt,
                        "dispatched",
                        {"dispatch_id": dispatch, "fee_state": "awaiting_log"},
                    ),
                    (str(uuid4()), "admitted", {}),
                ):
                    await conn.execute(
                        insert(s.runner_calls).values(
                            task_id=tid,
                            id="model:" + call,
                            owner_epoch=epoch,
                            binding_id=bid,
                            kind="model",
                            state=state,
                            concurrency_active=True,
                            data=data,
                            created_at=datetime.now(UTC),
                        )
                    )
                if reason == "native_owner_expired":
                    await conn.execute(
                        update(s.runner_owners)
                        .where(s.runner_owners.c.task_id == tid)
                        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                    )
            unavailable = {
                **observation,
                "sandbox": {**observation["sandbox"], "observation": "unknown"},
            }
            frozen = await act(
                actor_b,
                "observation",
                observation=unavailable if reason == "native_resource_unknown" else observation,
            )
            assert frozen.status_code == 200 and frozen.json()["state"] == "frozen", frozen.text
            incident = await recovery()
            assert incident["reason"] == reason and incident["original_epoch"] == epoch
            assert (
                incident["controller_generation"] == second.fence
                and incident["sandbox_create_claimed"]
            )
            assert incident["resources"]["journal_pvc_uid"] == observation["journal_pvc"]["uid"]
            assert incident["bindings"][0]["confirmed_cursor"] == original_cursor
            assert incident["ledger"]["model_attempts"] == {"dispatched": 1, "not_sent": 1}
            assert incident["ledger"]["model_transport"] == {"unconfirmed": 1}
            assert "never-in-recovery-view" not in json.dumps(incident)

        async with RuntimeLock(lock_settings, fenced=True) as third:
            actor_c, inherited = await claim(third, "third-controller")
            assert inherited["state"] == "frozen" and inherited["data"]["owner_epoch"] == epoch
            assert (
                await act(actor_b, "error", error_code="native_control_unavailable")
            ).status_code == 409
            refreshed = {**observation, "sandbox": {**observation["sandbox"], "observation": None}}
            assert (await act(actor_c, "observation", observation=refreshed)).json()[
                "state"
            ] == "frozen"
            assert (await recovery())["incident_id"] == incident["incident_id"]
            assert not (await act(actor_c, "sandbox_create")).json()["create_allowed"]
            for action in ("material", "renew", "activate", "sandbox_host_material"):
                assert (await act(actor_c, action)).status_code == 409
            assert (
                await http.post(
                    mgmt(tid) + "/owners/renew", headers=SERVICE, json={"expected_epoch": epoch}
                )
            ).status_code == 409
            assert (
                await http.post(
                    control(tid) + f"/bindings/{bid}/acquire", headers=auth(owner), json=permit
                )
            ).status_code == 409
            unproven = confirmation(
                owner, binding, original_cursor, cursor(original_cursor, "late-unproven")
            )
            assert (
                await http.post(
                    control(tid) + f"/bindings/{bid}/journal/confirm",
                    headers=auth(owner),
                    json=unproven,
                )
            ).status_code == 409
            await confirm(http, tid, owner, binding, first_journal)
            observed = await http.post(
                f"/api/tasks/{tid}/ctf/runtime/runner-gateway/model/{old_attempt}/transport",
                headers=SERVICE,
                json={
                    "protocol": PROTOCOL,
                    "attempt_id": old_attempt,
                    "dispatch_id": dispatch,
                    "phase": "response_ended",
                    "http_status": 200,
                },
            )
            assert observed.status_code in {404, 409}, observed.text
            reconciled = await recovery()
            assert reconciled["ledger"]["model_transport"] == {"unconfirmed": 1}
            assert reconciled["ledger"]["model_attempts"] == {"dispatched": 1, "not_sent": 1}
            assert reconciled["bindings"][0]["confirmed_cursor"] == original_cursor
            async with ctf.repo.engine.connect() as conn:
                current = (
                    (
                        await conn.execute(
                            select(s.runner_owners).where(s.runner_owners.c.task_id == tid)
                        )
                    )
                    .mappings()
                    .one()
                )
                assert current["epoch"] == epoch and not current["current"] and current["revoked"]
                task_control = (
                    await conn.execute(select(s.tasks.c.ctf_control).where(s.tasks.c.id == tid))
                ).scalar_one()
                assert task_control["failure_hold"] == existing_hold
                budget = (
                    (
                        await conn.execute(
                            select(s.native_task_budgets).where(
                                s.native_task_budgets.c.task_id == tid
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert budget["status_snapshot"] == {"used_high_water_usd": "0.4"}
                assert budget["hold_reason"] == "fixture-budget-hold"
            assert (await http.post(f"/api/tasks/{tid}/stop", headers=SERVICE)).status_code == 200
            assert (await recovery())["state"] == "stopping"
            checkpoint = (await http.get(path, headers=SERVICE)).json()
            proof = {
                "observation_digest": runner_payload_digest(
                    runner_json_text(checkpoint["observation"])
                ),
                "pvcs_retained": True,
                "runner_terminated": True,
                "sandbox_terminated": True,
            }
            absent = await act(actor_c, "cleanup", proof={**proof, "runner_terminated": False})
            assert absent.status_code == 200 and absent.json()["state"] == "unknown"
            assert (await recovery())["state"] == "stopping"
            done = await act(actor_c, "cleanup", proof=proof)
            assert done.status_code == 200 and done.json()["state"] == "stopped", done.text
            assert (await recovery())["state"] == "stopped"
