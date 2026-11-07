"""Real retained PVCs and Task DELETE control; no Pi/model/Sandbox execution is dispatched."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from common import CONFIG, STATE, owned_cluster


async def prepare_retained(adapter, operation, evidence):
    namespace, labels = adapter._scope(operation)
    ns = await adapter._ensure(adapter._document("Namespace", namespace, labels), labels)
    observation = {key: operation[key] for key in ["task_id", "operation_id", "namespace"]}
    observation.update(
        source_id=adapter.config["source_id"],
        cluster_uid=adapter.config["cluster_uid"],
        namespace_uid=ns["metadata"]["uid"],
    )
    for short, size in [("journal", "128Mi"), ("workspace", "64Mi")]:
        claim = await adapter._ensure(
            adapter._document(
                "PersistentVolumeClaim",
                "bbx-" + short,
                labels,
                namespace,
                spec={
                    "storageClassName": adapter.config["storage_class"],
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": size}},
                },
            ),
            labels,
        )
        observation[short + "_pvc"] = {
            "name": claim["metadata"]["name"],
            "uid": claim["metadata"]["uid"],
        }
    binder = adapter._document(
        "Pod",
        "bbx-retention-binder",
        labels,
        namespace,
        spec={
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {"runAsUser": 10000, "runAsGroup": 10000, "fsGroup": 10000},
            "containers": [
                {
                    "name": "binder",
                    "image": adapter.config["sandbox_image"],
                    "command": [
                        "/bin/sh",
                        "-c",
                        "printf retained > /journal/p3g-proof; "
                        "printf retained > /workspace/p3g-proof",
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [
                        {"name": n, "mountPath": "/" + n} for n in ["journal", "workspace"]
                    ],
                }
            ],
            "volumes": [
                {"name": n, "persistentVolumeClaim": {"claimName": "bbx-" + n}}
                for n in ["journal", "workspace"]
            ],
        },
    )
    pod = await adapter._ensure(binder, labels)
    uid = pod["metadata"]["uid"]
    until = time.monotonic() + 120
    while True:
        pod = await adapter._get("pod", "bbx-retention-binder", namespace)
        if not pod or pod["metadata"]["uid"] != uid:
            raise RuntimeError("Original binder identity lost")
        projection = adapter._pod_projection(pod)
        if projection["terminated"]:
            assert projection.get("exit_code") == 0
            break
        if time.monotonic() >= until:
            raise RuntimeError("Binder did not finish")
        await asyncio.sleep(0.2)
    evidence["binder"] = projection
    await adapter._kubectl(
        "delete",
        "--raw",
        f"/api/v1/namespaces/{namespace}/pods/bbx-retention-binder",
        "-f",
        "-",
        body={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}},
    )
    until = time.monotonic() + 30
    while await adapter._get("pod", "bbx-retention-binder", namespace) is not None:
        if time.monotonic() >= until:
            raise RuntimeError("Binder delete unresolved")
        await asyncio.sleep(0.2)
    # The binder is a fixture, not a platform execution. Remove only its UID-linked metadata.
    seen = set()
    for path, value in await adapter._retirement_inventory(namespace):
        meta = value["metadata"]
        related = value.get("regarding", value.get("involvedObject", {}))
        owned = (value["kind"] == "Event" and related.get("uid") == uid) or any(
            o.get("uid") == uid for o in meta.get("ownerReferences", [])
        )
        if owned and meta["uid"] not in seen:
            seen.add(meta["uid"])
            await adapter._kubectl(
                "delete",
                "--raw",
                path,
                "-f",
                "-",
                body={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": meta["uid"]},
                },
            )
    evidence["retained_pvc_uids"] = [
        observation[k]["uid"] for k in ["journal_pvc", "workspace_pvc"]
    ]
    evidence["fixture_scope"] = (
        "retained PVC binder only; no Pi, official Sandbox, Agent or model was launched"
    )
    return observation


async def main(candidate, resume=False, verify_retired=False):
    # Imports are resolved only from the clean reviewed candidate and its test helpers.
    for directory in reversed(
        [
            "packages/contracts/src",
            "packages/objects/src",
            "services/blackboard/src",
            "services/agent-runtime/src",
            "services/blackboard/tests",
        ]
    ):
        sys.path.insert(0, str(candidate / directory))
    import httpx
    from alembic import command
    from alembic.config import Config
    from bbx_blackboard.api import create_app
    from bbx_blackboard.auth import issue_user_token
    from bbx_blackboard.ctf import CtfService
    from bbx_blackboard.store import schema as tables
    from bbx_contracts.challenge_board import BOARD_CONTRACT
    from bbx_contracts.runner import runner_json_text, runner_payload_digest
    from bbx_contracts.runner_bootstrap import retirement_manifest_digest
    from bbx_runtime.clients import BlackboardClient
    from bbx_runtime.native_runner import NativeRunnerController
    from bbx_runtime.runner_resources import RunnerResources
    from bbx_runtime.scheduler.lock import RuntimeLock
    from pydantic import SecretStr
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine
    from test_board_flags_integration import Objects
    from test_ctf_api import settings
    from test_native_runner_bootstrap_integration import runtime_settings
    from testcontainers.community.postgres import PostgresContainer

    assert Path(inspect.getfile(RunnerResources)).resolve().is_relative_to(candidate)
    owned_cluster()
    identity = json.loads((STATE / "cluster-identity.json").read_text())
    built = json.loads((STATE / "built-images.json").read_text())
    cfg = {
        "source_id": "bbx-p3g-retained-" + uuid4().hex,
        "kubeconfig": str(CONFIG),
        "context": "kind-bbx-p3f-k8s",
        "cluster_uid": identity["cluster_uid"],
        "runner_image": "unused@sha256:" + "a" * 64,
        "sandbox_image": "bbx-p3g-sandbox@" + built["images"]["bbx-p3g-sandbox:20261107"],
        "environment_revision": built["environment_revision"],
        "sandbox_server_url": "http://127.0.0.1:1",
        "storage_class": "standard",
        "system_namespace": "bbx-p3g-system",
    }
    cfg["retirement_single_apiserver"] = identity["retirement_single_apiserver"]
    state = STATE / "joint"
    state.mkdir(mode=0o700, exist_ok=True)
    private_path = state / "private-state.json"
    private = json.loads(private_path.read_text()) if resume or verify_retired else {}
    if resume or verify_retired:
        cfg = {
            **private["resource_config"],
            "retirement_single_apiserver": cfg["retirement_single_apiserver"],
        }
    elif private_path.exists():
        raise RuntimeError("An original attempt exists; reconcile with --resume")
    adapter = RunnerResources(cfg)
    evidence = {
        "started_at": datetime.now(UTC).isoformat(),
        "candidate": str(candidate),
        "resource_sha256": hashlib.sha256(
            Path(inspect.getfile(RunnerResources)).read_bytes()
        ).hexdigest(),
        "cluster": identity,
        "checks": {},
    }
    if verify_retired:
        import docker

        original = json.loads((state / "evidence.json").read_text())
        manifest = json.loads((state / "manifest.json").read_text())
        proof = original["proof"]
        assert proof["retired"] is True and manifest["retired"] is True
        assert proof["manifest_digest"] == retirement_manifest_digest(manifest)
        await adapter._cluster()
        assert await adapter._get("namespace", manifest["namespace"]["name"]) is None
        assert await adapter._retirement_inventory(manifest["namespace"]["name"]) == []
        for volume in manifest["volumes"]:
            assert await adapter._get("pv", volume["name"]) is None
        for key, kind in [
            ("policy", "validatingadmissionpolicies"),
            ("binding", "validatingadmissionpolicybindings"),
        ]:
            assert await adapter._get(kind, manifest["fence"][key]["metadata"]["name"]) is None
        engine = create_async_engine(private["postgres_url"])
        try:
            async with engine.connect() as conn:
                task_row = await conn.execute(
                    select(tables.tasks.c.id).where(tables.tasks.c.id == UUID(private["task_id"]))
                )
                resource_row = await conn.execute(
                    select(tables.task_runner_resources.c.task_id).where(
                        tables.task_runner_resources.c.task_id == UUID(private["task_id"])
                    )
                )
                assert task_row.first() is None and resource_row.first() is None
        finally:
            await engine.dispose()
        container = docker.from_env().containers.get(private["postgres_container_id"])
        assert container.name.startswith("bbx-p3g-retire-pg-")
        container.remove(force=True, v=True)
        verification = {
            "verified": True,
            "task_id": private["task_id"],
            "product_proof": proof,
            "manifest_digest": retirement_manifest_digest(manifest),
            "actual_controller_resource_sha256": original["resource_sha256"],
            "prior_fixture_completed": original["completed"],
            "reason": (
                "Controller retired and purged; fixture expected not_registered, "
                "Task guard returned 404. Read-only original resource and PG checks pass."
            ),
            "owned_postgres_container_removed": private["postgres_container_id"],
            "verified_at": datetime.now(UTC).isoformat(),
        }
        (state / "final-verification.json").write_text(json.dumps(verification, indent=2))
        print(json.dumps(verification, indent=2))
        return
    if resume:
        import docker

        original = json.loads((state / "evidence.json").read_text())
        evidence = {**original, "resumed_at": datetime.now(UTC).isoformat(), "cleanup_only": True}
        container = docker.from_env().containers.get(private["postgres_container_id"])
        if not container.name.startswith("bbx-p3g-retire-pg-"):
            raise RuntimeError("Foreign PG recovery refused")
        url = private["postgres_url"]
        container_id = container.id
    else:
        container = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg").with_name(
            "bbx-p3g-retire-pg-" + uuid4().hex[:10]
        )
        container.start()
        url = container.get_connection_url()
        container_id = container.get_wrapped_container().id
    engine = create_async_engine(url)
    if not resume:
        old = os.environ.get("BBX_DATABASE_URL")
        os.environ["BBX_DATABASE_URL"] = url
        try:
            migration = Config(str(candidate / "services/blackboard/alembic.ini"))
            migration.set_main_option(
                "script_location", str(candidate / "services/blackboard/migrations")
            )
            await asyncio.to_thread(command.upgrade, migration, "head")
        except BaseException:
            await engine.dispose()
            container.stop()
            raise
        finally:
            if old is None:
                os.environ.pop("BBX_DATABASE_URL", None)
            else:
                os.environ["BBX_DATABASE_URL"] = old
    completed = False
    try:
        opts = settings().model_copy(
            update={
                "sandbox_tenant_resolver_token": SecretStr("p3g-only-synthetic-resolver"),
                "native_gateway_accounting_source_id": "p3g-no-model-db",
                "native_gateway_admin_url": "https://native.invalid",
                "native_gateway_data_url": "https://native.invalid",
                "native_gateway_catalog_revision": "p3g-unused",
                "native_mcp_gateway_url": "https://mcp.invalid",
                "native_mcp_backend_url": "https://blackboard.invalid",
                "native_mcp_jwks_file": "/unused",
                "native_mcp_backend_token_file": "/unused",
            }
        )
        app = create_app(opts, engine=engine, objects=Objects())
        ctf = CtfService(engine)
        if resume:
            tid = UUID(private["task_id"])
        else:
            tid = await ctf.create_task(
                {
                    "mode": "ctf",
                    "goal": "Retained resources only",
                    "budget": {"max_cost": "1", "max_minutes": 5},
                    "ctf_options": {"max_teammates": 1},
                },
                business_contract=BOARD_CONTRACT,
            )
        evidence["task_id"] = str(tid)
        headers = {"Authorization": "Bearer service-test"}
        path = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
            ) as http,
            RuntimeLock(runtime_settings(url), fenced=True) as lock,
        ):

            async def post(endpoint, payload):
                response = await http.post(endpoint, headers=headers, json=payload)
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Control refused {endpoint}: {response.status_code} {response.text[:400]}"
                    )
                return response.json()

            if resume:
                queued = (await http.get(path, headers=headers)).json()
            else:
                queued = await post(
                    path,
                    {
                        "request_id": str(uuid4()),
                        "model_alias": "unused-no-model",
                        "exchange_rate": {
                            "rate_cny_per_usd": "7",
                            "source_ref": "fixture://p3g",
                            "observed_at": datetime.now(UTC).isoformat(),
                        },
                    },
                )
            control_settings = runtime_settings(url)
            service = BlackboardClient("http://test", "service-test", http_client=http)
            controller = NativeRunnerController(control_settings, service, adapter, lock)
            basecfg = controller.config
            claim = {
                "controller_id": controller.controller_id,
                "controller_fence": lock.fence,
                "controller_pid": lock.backend_pid,
                "controller_started_at": lock.backend_started_at.isoformat(),
                "resource_config": basecfg,
                "retirement_single_apiserver": cfg["retirement_single_apiserver"],
            }
            private = {
                "postgres_url": url,
                "postgres_container_id": container_id,
                "task_id": str(tid),
                "resource_config": basecfg,
            }
            private_path.write_text(json.dumps(private, indent=2))
            private_path.chmod(0o600)
            operation = await post(path + "/claim", claim)
            operation.update(
                task_id=str(tid), operation_id=queued["operation_id"], namespace=queued["namespace"]
            )
            actionbase = {
                "operation_id": queued["operation_id"],
                "controller_id": claim["controller_id"],
                "controller_generation": lock.fence,
            }

            async def action(name, **kwargs):
                return await post(path + "/actions", {**actionbase, "action": name, **kwargs})

            if resume:
                observation = operation["observation"]
            else:
                observed = await prepare_retained(adapter, operation, evidence)
                observation = (await action("observation", observation=observed))["observation"]
            await http.post(f"/api/tasks/{tid}/stop", headers=headers)
            await action(
                "cleanup",
                proof={
                    "not_started": True,
                    "pvcs_retained": True,
                    "observation_digest": runner_payload_digest(runner_json_text(observation)),
                },
            )
            evidence["checks"]["ordinary_stop_retains_original_pvcs"] = all(
                [
                    await adapter._get("pvc", observation[k]["name"], queued["namespace"])
                    for k in ["journal_pvc", "workspace_pvc"]
                ]
            )
            user = {"Authorization": "Bearer " + issue_user_token(opts, "alice")}
            if not operation.get("data", {}).get("retirement"):
                response = await http.delete(f"/api/tasks/{tid}", headers=user)
                assert response.status_code in {200, 202}, response.text
            evidence["checks"]["user_delete_intent_persisted"] = True
            original_retire = adapter.retire

            async def recorded_retire(operation, **callbacks):
                checkpoint = callbacks["checkpoint"]

                async def recorded_checkpoint(manifest):
                    await checkpoint(manifest)
                    (state / "manifest.json").write_text(json.dumps(manifest, indent=2))

                callbacks["checkpoint"] = recorded_checkpoint
                proof = await original_retire(operation, **callbacks)
                evidence["proof"] = proof
                return proof

            adapter.retire = recorded_retire
            work = asyncio.create_task(controller.run())
            try:
                until = time.monotonic() + 150
                while True:
                    if work.done():
                        await work
                        raise RuntimeError("Controller exited before retirement")
                    status = await http.get(path, headers=headers)
                    if evidence.get("proof", {}).get("retired") is True and (
                        status.status_code == 404
                        or (
                            status.status_code == 200
                            and status.json().get("state") == "not_registered"
                        )
                    ):
                        break
                    if time.monotonic() >= until:
                        raise RuntimeError("Automatic Task retirement remains unresolved")
                    await asyncio.sleep(0.2)
                evidence["checks"]["real_controller_task_delete_retired_and_purged"] = (
                    evidence.get("proof", {}).get("retired") is True
                )
                completed = evidence["checks"]["real_controller_task_delete_retired_and_purged"]
            finally:
                await controller.close()
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
    finally:
        await engine.dispose()
        evidence["completed"] = completed
        (state / "evidence.json").write_text(json.dumps(evidence, indent=2))
        if completed:
            if resume:
                container.remove(force=True, v=True)
            else:
                container.stop()
        else:
            (state / "authority-retained.json").write_text(
                json.dumps(
                    {
                        "postgres_container_id": container_id,
                        "reason": "Retirement unresolved; preserve original PG authority",
                    },
                    indent=2,
                )
            )
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--verify-retired", action="store_true")
    options = parser.parse_args()
    asyncio.run(
        main(
            options.candidate.resolve(),
            resume=options.resume,
            verify_retired=options.verify_retired,
        )
    )
