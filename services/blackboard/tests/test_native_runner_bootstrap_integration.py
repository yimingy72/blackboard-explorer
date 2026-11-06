"""Real PG control contracts with explicit synthetic resource observations, no workloads."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import BOARD_CONTRACT
from bbx_contracts.runner import runner_json_text, runner_payload_digest
from bbx_runtime.scheduler.lock import RuntimeLock
from bbx_runtime.settings import NativeRunnerSettings
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import make_url
from test_board_flags_integration import Objects
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_runner_control_integration import SERVICE, control, mgmt, poll_query, registration

pytestmark = pytest.mark.integration


def config():
    return {
        "source_id": "synthetic-native-resource-control",
        "cluster_uid": str(uuid4()),
        "runner_image": "fixture-runner@sha256:" + "a" * 64,
        "sandbox_image": "fixture-sandbox@sha256:" + "b" * 64,
        "environment_revision": "synthetic-env-1",
        "runner_blackboard_url": "https://blackboard.invalid",
        "runner_model_proxy_url": "https://runtime.invalid",
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


async def test_native_bootstrap_fence_lead_closing_and_exit_reconciliation(
    ctf, ctf_url, monkeypatch
):
    options = settings().model_copy(
        update={
            "sandbox_tenant_resolver_token": SecretStr("synthetic-resolver-only"),
            "native_gateway_accounting_source_id": "synthetic-accounting-db",
            "native_gateway_admin_url": "https://native.invalid",
            "native_gateway_data_url": "https://native.invalid",
            "native_gateway_catalog_revision": "synthetic-native-catalog",
            "native_mcp_gateway_url": "https://mcp.invalid",
            "native_mcp_backend_url": "https://blackboard.invalid",
            "native_mcp_jwks_file": "/private/public-jwks.json",
            "native_mcp_backend_token_file": "/private/backend-token",
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
            # Synthetic standalone control identity tests the finite closing filter only.
            owner = (
                await http.post(
                    mgmt(tid) + "/owners",
                    headers=SERVICE,
                    json={
                        "request_id": str(uuid4()),
                        "runner_id": "synthetic-runner",
                        "pod_uid": observed["runner"]["pod_uid"],
                        "expected_epoch": 0,
                        "hard_deadline": queued["hard_deadline"],
                    },
                )
            ).json()
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
    await app.state.runner_gateway.native.admin.close()


async def test_native_never_dispatched_cancel_and_retained_ownership(ctf, ctf_url):
    from bbx_blackboard.conversations import Conversations
    from fastapi import HTTPException
    from sqlalchemy import update

    options = settings().model_copy(
        update={
            "sandbox_tenant_resolver_token": SecretStr("synthetic-resolver-only"),
            "native_gateway_accounting_source_id": "synthetic-accounting-db",
            "native_gateway_admin_url": "https://native.invalid",
            "native_gateway_data_url": "https://native.invalid",
            "native_gateway_catalog_revision": "synthetic-native-catalog",
            "native_mcp_gateway_url": "https://mcp.invalid",
            "native_mcp_backend_url": "https://blackboard.invalid",
            "native_mcp_jwks_file": "/private/public-jwks.json",
            "native_mcp_backend_token_file": "/private/backend-token",
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
    await app.state.runner_gateway.native.admin.close()


async def test_native_hold_refreshes_only_control_and_never_activates_input(ctf, ctf_url):
    from sqlalchemy import func, update

    options = settings().model_copy(
        update={
            "sandbox_tenant_resolver_token": SecretStr("synthetic-resolver-only"),
            "native_gateway_accounting_source_id": "synthetic-accounting-db",
            "native_gateway_admin_url": "https://native.invalid",
            "native_gateway_data_url": "https://native.invalid",
            "native_gateway_catalog_revision": "synthetic-native-catalog",
            "native_mcp_gateway_url": "https://mcp.invalid",
            "native_mcp_backend_url": "https://blackboard.invalid",
            "native_mcp_jwks_file": "/private/public-jwks.json",
            "native_mcp_backend_token_file": "/private/backend-token",
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
        owner_response = await http.post(
            mgmt(tid) + "/owners",
            headers=SERVICE,
            json={
                "request_id": str(uuid4()),
                "runner_id": "held-runner",
                "pod_uid": observed["runner"]["pod_uid"],
                "expected_epoch": 0,
                "hard_deadline": queued["hard_deadline"],
            },
        )
        owner = owner_response.json()
        bound = await http.post(mgmt(tid) + "/bindings", headers=SERVICE, json=registration(owner))
        assert bound.status_code == 200, bound.text
        bid = bound.json()["binding"]["binding_id"]
        # Synthetic prior publication metadata isolates control renewal from model/MCP I/O.
        bootstrap = {
            "protocol": "bbx.task-runner.v1",
            "fence": owner["fence"],
            "control_url": f"https://blackboard.invalid/api/tasks/{tid}/runner-control",
            "model_proxy_url": "https://runtime.invalid",
            "mcp_gateway_url": f"https://mcp.invalid/api/tasks/{tid}/mcp",
            "control_credential_ref": "control",
            "credential_file": "/var/run/bbx/credentials.json",
            "journal_root": "/journal",
            "mcp_metadata_timeout_ms": 15000,
        }
        async with ctf.repo.engine.begin() as conn:
            await native.repo.lock(conn, tid)
            record = await native._get(conn, tid)
            task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
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
        native.gateway.native.prepare = forbidden_io
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
    await app.state.runner_gateway.native.admin.close()
