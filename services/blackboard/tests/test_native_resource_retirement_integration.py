"""Retirement authorization and immutable UID ledger against real PostgreSQL."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import BOARD_CONTRACT
from bbx_contracts.runner import runner_json_text, runner_payload_digest
from bbx_contracts.runner_bootstrap import retirement_manifest_digest
from bbx_runtime.scheduler.lock import RuntimeLock
from pydantic import SecretStr
from sqlalchemy import select
from test_board_flags_integration import Objects
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_native_runner_bootstrap_integration import config, fixture_pi_model, runtime_settings
from test_runner_control_integration import SERVICE

pytestmark = pytest.mark.integration


async def test_explicit_delete_retirement_manifest_and_purge(ctf, ctf_url):
    options = settings().model_copy(
        update={
            "pi_site_id": "fixture-primary",
            "pi_model_primary_site": "fixture-primary",
            "pi_model_authority": "fixture-authority",
            "sandbox_tenant_resolver_token": SecretStr("fixture-resolver"),
        }
    )
    objects = Objects()
    app = create_app(options, engine=ctf.repo.engine, objects=objects)
    user = {"Authorization": "Bearer " + issue_user_token(options, "fixture-user")}
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Retire explicitly deleted retained resources",
            "budget": {"max_cost": "1", "max_minutes": 5},
            "ctf_options": {"max_teammates": 1},
        },
        business_contract=BOARD_CONTRACT,
        pi_model=fixture_pi_model("fixture-model"),
    )
    path = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
    deployment = {**config(), "storage_class": "fixture-delete-class"}
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
        ) as http,
        RuntimeLock(runtime_settings(ctf_url), fenced=True) as controller,
    ):
        requested = await http.post(
            path,
            headers=SERVICE,
            json={
                "request_id": str(uuid4()),
                "model_alias": "fixture-model",
                "exchange_rate": {
                    "rate_cny_per_usd": "7",
                    "source_ref": "fixture://fx",
                    "observed_at": datetime.now(UTC).isoformat(),
                },
            },
        )
        assert requested.status_code == 200, requested.text
        operation = requested.json()
        assert controller.backend_started_at is not None
        claim = {
            "controller_id": "retirement-fixture",
            "controller_fence": controller.fence,
            "controller_pid": controller.backend_pid,
            "controller_started_at": controller.backend_started_at.isoformat(),
            "resource_config": deployment,
            "retirement_single_apiserver": {"pod_uid": str(uuid4()), "node_uid": str(uuid4())},
        }
        assert (await http.post(path + "/claim", headers=SERVICE, json=claim)).status_code == 200
        actor = {
            "operation_id": operation["operation_id"],
            "controller_id": claim["controller_id"],
            "controller_generation": controller.fence,
        }

        async def act(action, **fields):
            return await http.post(
                path + "/actions", headers=SERVICE, json={**actor, "action": action, **fields}
            )

        observation = {
            "task_id": str(tid),
            "operation_id": operation["operation_id"],
            "source_id": deployment["source_id"],
            "cluster_uid": deployment["cluster_uid"],
            "namespace": operation["namespace"],
            "namespace_uid": str(uuid4()),
            "journal_pvc": {"name": "bbx-journal", "uid": str(uuid4())},
            "workspace_pvc": {"name": "bbx-workspace", "uid": str(uuid4())},
        }
        assert (await act("observation", observation=observation)).status_code == 200
        assert (await http.post(f"/api/tasks/{tid}/stop", headers=SERVICE)).status_code == 200
        saved = (await http.get(path, headers=SERVICE)).json()["observation"]
        stopped = await act(
            "cleanup",
            proof={
                "not_started": True,
                "pvcs_retained": True,
                "observation_digest": runner_payload_digest(runner_json_text(saved)),
            },
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] == "stopped"
        assert (await act("retire_prepare")).status_code == 409
        deleted = await http.delete(f"/api/tasks/{tid}", headers=user)
        assert deleted.status_code == 202, deleted.text
        first_intent = (await http.get(path, headers=SERVICE)).json()["retirement_id"]
        assert first_intent
        await http.delete(f"/api/tasks/{tid}", headers=user)
        assert (await http.get(path, headers=SERVICE)).json()["retirement_id"] == first_intent
        assert (await http.post(f"/api/tasks/{tid}/purge", headers=SERVICE)).status_code == 409
        claimed = await http.post(path + "/claim", headers=SERVICE, json=claim)
        assert claimed.status_code == 200, claimed.text
        assert (await act("retire_prepare")).status_code == 200
        namespace = operation["namespace"]
        manifest = {
            "task_id": str(tid),
            "operation_id": operation["operation_id"],
            "retirement_id": first_intent,
            "source_id": deployment["source_id"],
            "cluster_uid": deployment["cluster_uid"],
            "namespace": {"name": namespace, "uid": observation["namespace_uid"]},
            "authority": claim["retirement_single_apiserver"],
            "resources": [
                {
                    "path": f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{ref['name']}",
                    "uid": ref["uid"],
                    "kind": "persistentvolumeclaims",
                    "name": ref["name"],
                }
                for key in ("journal_pvc", "workspace_pvc")
                if (ref := observation[key])
            ],
            "volumes": [
                {
                    "name": "pv-" + ref["name"],
                    "uid": str(uuid4()),
                    "claim_uid": ref["uid"],
                    "claim_name": ref["name"],
                    "claim_namespace": namespace,
                    "storage_class": deployment["storage_class"],
                    "reclaim_policy": "Delete",
                }
                for key in ("journal_pvc", "workspace_pvc")
                if (ref := observation[key])
            ],
            "policy": {"path": "/apis/admissionregistration.k8s.io/v1/policies/frozen"},
            "binding": {"path": "/apis/admissionregistration.k8s.io/v1/bindings/frozen"},
            "fence": {"policy": {"fixture": True}, "binding": {"fixture": True}},
            "topology": claim["retirement_single_apiserver"],
        }
        incomplete = {**manifest, "resources": [], "volumes": [], "retired": True}
        assert (await act("retire_plan", proof={"manifest": incomplete})).status_code == 409
        unbound = deepcopy(manifest)
        unbound["volumes"][0].update(name="", uid="", unbound=True)
        assert (await act("retire_plan", proof={"manifest": unbound})).status_code == 422
        assert (await act("retire_plan", proof={"manifest": manifest})).status_code == 200
        changed = deepcopy(manifest)
        changed["volumes"][0]["uid"] = str(uuid4())
        assert (await act("retire_plan", proof={"manifest": changed})).status_code == 409
        digest = retirement_manifest_digest(manifest)
        assert (await act("retire_admit", proof={"manifest_digest": digest})).json()["admitted"]
        assert (
            await act("retire_complete", proof={"manifest_digest": digest, "retired": True})
        ).status_code == 409
        for key in ("resources", "volumes"):
            for item in manifest[key]:
                item["absent"] = True
        for key in ("policy", "binding"):
            manifest[key].update(uid=str(uuid4()), absent=True)
        manifest.update(
            namespace_delete_started=True, namespace_absent=True, fence_absent=True, retired=True
        )
        assert (await act("retire_plan", proof={"manifest": manifest})).status_code == 200
        digest = retirement_manifest_digest(manifest)
        completed = await act("retire_complete", proof={"manifest_digest": digest, "retired": True})
        assert completed.status_code == 200, completed.text
        assert completed.json()["data"]["retirement"]["state"] == "retired"
        purged = await http.post(f"/api/tasks/{tid}/purge", headers=SERVICE)
        assert purged.status_code == 200, purged.text
        async with ctf.repo.engine.connect() as conn:
            assert not (
                await conn.execute(
                    select(s.task_runner_resources).where(s.task_runner_resources.c.task_id == tid)
                )
            ).first()
