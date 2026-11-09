"""Public creation, immutable deployment authority, and retry behavior in real PostgreSQL."""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_agent_token, issue_user_token
from bbx_blackboard.input_groups import request_hash
from bbx_blackboard.native_ctf_publication import PublicationState, load_publication
from bbx_blackboard.native_inputs import require_inputs_confirmed
from bbx_blackboard.profiles import ctf_profile
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import BOARD_CONTRACT
from bbx_contracts.ctf import CtfTaskCreate, load_ctf_profile
from bbx_contracts.gateway_models import GatewayModelSnapshot, GatewayUsdRates
from bbx_contracts.runner_inputs import INPUT_PROFILE
from bbx_runtime.scheduler.lock import RuntimeLock
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import select, update
from test_board_flags_integration import Objects
from test_ctf_api import settings
from test_ctf_integration import ctf as ctf
from test_ctf_integration import ctf_url as ctf_url
from test_native_runner_bootstrap_integration import config, runtime_settings
from test_runner_control_integration import SERVICE

pytestmark = pytest.mark.integration


class UploadObjects(Objects):
    async def put(self, uri, data, **kwargs):
        await super().put(uri, data.read() if hasattr(data, "read") else data, **kwargs)


async def test_public_native_creation_freezes_and_replays_original_task(
    ctf, ctf_url, tmp_path, monkeypatch
):
    path = tmp_path / "native-ctf.json"
    options = settings().model_copy(
        update={
            "native_ctf_publication_file": path,
            "sandbox_tenant_resolver_token": SecretStr("synthetic-resolver-only"),
            "native_gateway_accounting_source_id": "fixture-accounting",
            "native_gateway_admin_url": "https://native.invalid",
            "native_gateway_data_url": "https://native.invalid",
            "native_gateway_catalog_revision": "fixture-catalog",
            "native_mcp_gateway_url": "https://mcp.invalid",
            "native_mcp_backend_url": "https://blackboard.invalid",
            "native_mcp_jwks_file": "/private/public-jwks.json",
            "native_mcp_backend_token_file": "/private/backend-token",
        }
    )
    app = create_app(options, engine=ctf.repo.engine, objects=UploadObjects())
    app.state.native_ctf_publication = PublicationState()
    model = GatewayModelSnapshot(
        alias="fixture-native",
        label="Synthetic CTF model",
        catalog_revision="a" * 64,
        accounting_source_id="fixture-accounting",
        route_revision=1,
        route_digest="b" * 64,
        model_catalog_digest="c" * 64,
        context_window=16384,
        max_output_tokens=128,
        input_modalities=["text"],
        tool_call=True,
        usd_rates=GatewayUsdRates.model_validate(
            {"input": "1", "output": "1", "cache_read": "1", "cache_write": "1"}
        ),
    )

    async def require_active(alias, revision=None):
        assert alias == model.alias and revision in (None, model.catalog_revision)
        return model

    monkeypatch.setattr(app.state.gateway_models, "require_active", require_active)
    profile = await app.state.profile_store.create(
        "ctf", load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf"), "test"
    )
    resource = {
        **config(),
        "runner_profile": "node-sandbox-host-v1",
        "sandbox_host_image": "fixture-host@sha256:" + "c" * 64,
        "traffic_sandbox_image": "fixture-capture@sha256:" + "d" * 64,
        "sandbox_network": {
            "namespace": "worker-a",
            "network_identity": "worker-a",
            "ipv4": "198.19.1.2",
        },
        "traffic_target_namespace": "fixture-target",
        "traffic_targets": [{"ipv4": "10.12.0.5", "port": 18888}],
        "kubeconfig": "/private/owned-kubeconfig",
        "context": "fixture-context",
        "storage_class": "fixture-storage",
        "system_namespace": "fixture-system",
        "sandbox_server_url": "https://sandbox.invalid",
    }
    publication = {
        "id": "fixture-environment",
        "revision": 1,
        "label": "Fixture CTF",
        "release": "test_only",
        "ctf_profile_version": profile["version"],
        "budget_currency": "USD",
        "runtime_resource_config": resource,
    }
    receipt = tmp_path / "acceptance.json"
    accepted = {
        "schema_version": 2,
        "base_source": "reconstructed",
        "base_tool_image": "fixture-reconstructed@sha256:" + "e" * 64,
        "recipe_sha256": "f" * 64,
        "traffic_sandbox_image": resource["traffic_sandbox_image"],
        "public_create_and_archive": True,
    }
    receipt.write_text(json.dumps(accepted))
    verified = {
        **publication,
        "release": "verified",
        "acceptance": {
            "file": receipt.name,
            "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        },
    }
    path.write_text(json.dumps(verified))
    assert load_publication(options).publication
    path.write_text(
        json.dumps(
            {
                **verified,
                "model": {
                    "id": "historical-model",
                    "version": 1,
                    "gateway_alias": "different-in-the-same-catalog",
                },
            }
        )
    )
    assert load_publication(options).publication is None
    path.write_text(json.dumps(publication))
    assert load_publication(options).capabilities().model_dump() == {
        "mode": "native-ctf",
        "native_ctf": None,
    }
    configured = load_publication(options, allow_test_only=True)
    assert configured.publication
    user = {"Authorization": f"Bearer {issue_user_token(options, 'alice')}"}
    other = {"Authorization": f"Bearer {issue_user_token(options, 'bob')}"}
    create = {
        "mode": "ctf",
        "goal": "Verify a published native setup",
        "budget": {"currency": "USD", "max_cost": "2", "max_minutes": 5},
        "gateway_model": {"alias": model.alias, "catalog_revision": model.catalog_revision},
        "ctf_options": {"max_teammates": 2},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:

        async def group() -> str:
            response = await http.post("/api/task-input-groups", headers=user)
            assert response.status_code == 200, response.text
            return str(response.json()["id"])

        legacy_body = {key: value for key, value in create.items() if key != "gateway_model"}
        legacy_body["budget"] = {"max_cost": "2", "max_minutes": 5}
        legacy_body["input_group_id"] = await group()
        parsed = CtfTaskCreate.model_validate(legacy_body)
        old_hash = request_hash(parsed.model_dump(exclude={"native_environment"}))
        response = await http.post("/api/tasks", headers=user, json=legacy_body)
        assert response.status_code == 409, response.text
        legacy_id = UUID(legacy_body["input_group_id"])
        legacy = str(
            await app.state.ctf_service.create_task(
                parsed.model_dump(
                    exclude={
                        "input_group_id",
                        "input_file_ids",
                        "public_input_ids",
                        "model_id",
                        "model_version",
                        "profile_version",
                        "reasoning_effort",
                    }
                ),
                profile_version=profile["version"],
                input_group_id=legacy_id,
                input_owner="user:alice",
                create_request_hash=old_hash,
            )
        )
        async with ctf.repo.engine.connect() as conn:
            stored = (
                (
                    await conn.execute(
                        select(s.task_input_groups).where(s.task_input_groups.c.id == legacy)
                    )
                )
                .mappings()
                .one()
            )
            assert stored["create_request_hash"] == old_hash

        app.state.native_ctf_publication = configured
        caps = await http.get("/api/task-creation-capabilities", headers=user)
        assert caps.status_code == 200, caps.text
        environment = caps.json()["native_ctf"]
        assert set(environment) == {
            "id",
            "revision",
            "publication_stamp",
            "label",
        }
        reference = {key: environment[key] for key in ("id", "revision", "publication_stamp")}
        assert (await http.post("/api/tasks", headers=user, json=legacy_body)).json()[
            "id"
        ] == legacy
        assert (await http.post(f"/api/tasks/{legacy}/start", headers=user)).status_code == 409
        assert (await http.post("/api/tasks", headers=user, json=create)).status_code == 409
        native = {**create, "native_environment": reference, "input_group_id": await group()}
        assert (await http.post("/api/tasks", headers=other, json=native)).status_code == 403
        assert (
            await http.post(
                "/api/tasks",
                headers=user,
                json={
                    **native,
                    "egress_allowlist": ["unpublished.invalid"],
                },
            )
        ).status_code == 422
        assert (
            await http.post(
                "/api/tasks",
                headers=user,
                json={
                    **native,
                    "budget": {"currency": "USD", "max_cost": "0.00000000001", "max_minutes": 5},
                },
            )
        ).status_code == 422

        with monkeypatch.context() as patch:
            patch.setattr(
                app.state.native_runner_bootstrap,
                "queue_locked",
                AsyncMock(side_effect=HTTPException(503, "Fixture before native queue")),
            )
            failure = await http.post("/api/tasks", headers=user, json=native)
            assert failure.status_code == 503, failure.text
        async with ctf.repo.engine.connect() as conn:
            assert (
                await conn.execute(
                    select(s.tasks.c.id).where(s.tasks.c.id == native["input_group_id"])
                )
            ).first() is None
            assert (
                await conn.execute(
                    select(s.task_input_groups.c.bound_task_id).where(
                        s.task_input_groups.c.id == native["input_group_id"]
                    )
                )
            ).scalar_one() is None
        # This catches omitted attachment IDs, not just request-side explicit lists.
        nonempty = await group()
        async with ctf.repo.engine.begin() as conn:
            await conn.execute(
                update(s.task_input_groups)
                .where(s.task_input_groups.c.id == nonempty)
                .values(files=[{"id": str(uuid4())}])
            )
        assert (
            await http.post(
                "/api/tasks",
                headers=user,
                json={
                    **native,
                    "input_group_id": nonempty,
                },
            )
        ).status_code in {409, 422}
        first, repeated = await asyncio.gather(
            http.post("/api/tasks", headers=user, json=native),
            http.post("/api/tasks", headers=user, json=native),
        )
        assert first.status_code == repeated.status_code == 200, (first.text, repeated.text)
        assert first.json() == repeated.json()
        tid = first.json()["id"]
        async with ctf.repo.engine.connect() as conn:
            task = (await conn.execute(select(s.tasks).where(s.tasks.c.id == tid))).mappings().one()
            operation = (
                (
                    await conn.execute(
                        select(s.task_runner_resources).where(
                            s.task_runner_resources.c.task_id == tid
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert task["ctf_control"]["business_contract"] == BOARD_CONTRACT
            assert task["ctf_options"]["dispatch_mode"] == "automatic"
            assert task["status"] == "provisioning"
            assert operation["state"] == "queued" and not operation["sandbox_create_claimed"]
            assert operation["request"]["model_alias"] == "fixture-native"
            assert operation["request"]["publication"] == configured.publication.frozen()
        # Runtime material revalidates this immutable profile, including its legacy tool catalog.
        frozen_profile = await app.state.profile_store.get(
            task["agent_profile"], task["agent_profile_version"]
        )
        loaded_profile = ctf_profile(frozen_profile)
        assert loaded_profile.options.dispatch_mode == "manual"
        assert "execute_command" in loaded_profile.worker_tools["lead"].builtin
        traffic = await http.get(f"/api/tasks/{tid}/traffic", headers=user)
        assert traffic.status_code == 200, traffic.text
        assert traffic.json()["native_bound"] and traffic.json()["native_sources"] == []

        # Changed private config invalidates new selections, not original retries.
        path.write_text(
            json.dumps(
                {
                    **publication,
                    "runtime_resource_config": {
                        **resource,
                        "traffic_targets": [{"ipv4": "10.12.0.6", "port": 18888}],
                    },
                }
            )
        )
        app.state.native_ctf_publication = load_publication(options, allow_test_only=True)
        assert app.state.native_ctf_publication.stamp != configured.stamp
        assert (await http.post("/api/tasks", headers=user, json=native)).json()["id"] == tid
        previous_stamp = app.state.native_ctf_publication.stamp
        rotated = options.model_copy(
            update={
                "agent_token_secret": SecretStr("rotated-fixture-signing-key-minimum-thirty-two")
            }
        )
        app.state.settings = rotated
        app.state.native_ctf_publication = load_publication(rotated, allow_test_only=True)
        user = {"Authorization": f"Bearer {issue_user_token(rotated, 'alice')}"}
        assert app.state.native_ctf_publication.stamp != previous_stamp
        assert (await http.post("/api/tasks", headers=user, json=native)).json()["id"] == tid
        assert (
            await http.post(
                "/api/tasks",
                headers=user,
                json={
                    **native,
                    "input_group_id": await group(),
                },
            )
        ).status_code == 409

        endpoint = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap/claim"
        async with RuntimeLock(runtime_settings(ctf_url), fenced=True) as runtime:
            assert runtime.backend_started_at is not None
            claim = {
                "controller_id": str(uuid4()),
                "controller_fence": runtime.fence,
                "controller_pid": runtime.backend_pid,
                "controller_started_at": runtime.backend_started_at.isoformat(),
            }
            response = await http.post(
                endpoint,
                headers=SERVICE,
                json={
                    **claim,
                    "resource_config": {**resource, "environment_revision": "drift"},
                },
            )
            assert response.status_code == 409, response.text
            response = await http.post(
                endpoint,
                headers=SERVICE,
                json={
                    **claim,
                    "resource_config": resource,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["traffic_profile"] == "binding-pcap-v1"

        # Enable the additive P4k capability under the same original publication contract.
        path.write_text(json.dumps({**publication, "inputs_profile": INPUT_PROFILE}))
        app.state.native_ctf_publication = load_publication(rotated, allow_test_only=True)
        shared = (await http.get("/api/task-creation-capabilities", headers=user)).json()[
            "native_ctf"
        ]
        assert shared["shared_inputs"] == {
            "profile": INPUT_PROFILE,
            "max_file_bytes": 16 * 1024 * 1024,
            "max_total_bytes": 24 * 1024 * 1024,
            "max_files": 20,
        }
        source_group = await group()
        uploads = f"/api/task-input-groups/{source_group}/files"
        payload = b"\x00\xffinitial attachment\x00"
        attached = []
        for filename, data in (("材料.bin", payload), ("empty", b"")):
            response = await http.post(
                uploads, headers=user, params={"filename": filename}, content=data
            )
            assert response.status_code == 200, response.text
            attached.append(response.json())
        oversized = await http.post(
            uploads,
            headers=user,
            params={"filename": "too-large.bin"},
            content=b"x" * (shared["shared_inputs"]["max_file_bytes"] + 1),
        )
        assert oversized.status_code == 413
        selected = [item["id"] for item in attached]
        body = {
            **create,
            "input_group_id": source_group,
            "input_file_ids": selected,
            "native_environment": {
                key: shared[key] for key in ("id", "revision", "publication_stamp")
            },
        }
        assert (await http.post("/api/tasks", headers=user, json=body)).status_code == 422
        assert (
            await http.post(
                "/api/tasks", headers=user, json={**body, "public_input_ids": selected[:1]}
            )
        ).status_code == 422
        body["public_input_ids"] = selected
        response = await http.post("/api/tasks", headers=user, json=body)
        assert response.status_code == 200, response.text
        attached_tid = response.json()["id"]
        replay = await http.post("/api/tasks", headers=user, json=body)
        assert replay.json()["id"] == attached_tid
        assert (await http.get(f"/api/tasks/{attached_tid}", headers=user)).json()[
            "initial_attachment_state"
        ] == "pending"
        # Users retain the original object; a Task-agent token cannot use the group API.
        downloaded = await http.get(uploads + "/" + selected[0], headers=user)
        assert downloaded.status_code == 200 and downloaded.content == payload
        agent_token = issue_agent_token(
            rotated, UUID(attached_tid), "lead", generation=1, turn_id=uuid4()
        )
        agent = {"Authorization": f"Bearer {agent_token}"}
        assert (await http.get(uploads + "/" + selected[0], headers=agent)).status_code == 403

        bootstrap_url = f"/api/tasks/{attached_tid}/ctf/runtime/runner-bootstrap"
        host_url = f"/api/tasks/{attached_tid}/sandbox-host/inputs"
        uid = str(uuid4())
        observation = {
            "namespace_uid": str(uuid4()),
            "workspace_pvc": {"uid": uid, "name": "bbx-workspace"},
            "journal_pvc": {"uid": str(uuid4()), "name": "bbx-journal"},
            "runner_secret": {"uid": str(uuid4()), "name": "runner"},
            "sandbox_host_secret": {"uid": str(uuid4()), "name": "bbx-sandbox-host"},
            "runner": {
                "pod_uid": str(uuid4()),
                "container_id": "containerd://" + "a" * 64,
                "host_container_id": "containerd://" + "b" * 64,
                "phase": "Running",
                "pod_ip": "10.12.0.1",
            },
            "sandbox": {
                "id": str(uuid4()),
                "cr_uid": str(uuid4()),
                "pod_uid": str(uuid4()),
                "container_id": "containerd://" + "c" * 64,
                "phase": "Running",
                "environment_revision": resource["environment_revision"],
            },
        }
        async with RuntimeLock(runtime_settings(ctf_url), fenced=True) as runtime:
            assert runtime.backend_started_at is not None
            claim = {
                "controller_id": str(uuid4()),
                "controller_fence": runtime.fence,
                "controller_pid": runtime.backend_pid,
                "controller_started_at": runtime.backend_started_at.isoformat(),
            }
            response = await http.post(
                bootstrap_url + "/claim",
                headers=SERVICE,
                json={**claim, "resource_config": resource},
            )
            assert response.status_code == 200, response.text
            operation = response.json()
            fence = {
                "operation_id": operation["operation_id"],
                "controller_id": claim["controller_id"],
                "controller_generation": runtime.fence,
            }

            async def action(name, **params):
                return await http.post(
                    bootstrap_url + "/actions",
                    headers=SERVICE,
                    json={**fence, "action": name, **params},
                )

            response = await action("observation", observation=observation)
            assert response.status_code == 200, response.text
            context = {
                "sandbox_id": observation["sandbox"]["id"],
                "sandbox_pod_uid": observation["sandbox"]["pod_uid"],
                "environment_revision": resource["environment_revision"],
                "execution_context_id": "a" * 64,
                "execd_endpoint": "sandbox.invalid",
                "host_url": "http://10.12.0.1:8766",
            }
            material = await action("sandbox_host_material", proof={"context": context})
            assert material.status_code == 200, material.text
            assert material.json()["inputs_profile"] == INPUT_PROFILE
            ready_proof = {"context_digest": material.json()["context_digest"]}
            for name, params in (
                ("material", {}),
                ("activate", {}),
                ("sandbox_host_ready", {"proof": ready_proof}),
            ):
                response = await action(name, **params)
                assert response.status_code == 409, (name, response.text)
            async with ctf.repo.engine.connect() as conn:
                assert (
                    await conn.execute(
                        select(s.runner_owners.c.epoch).where(
                            s.runner_owners.c.task_id == attached_tid
                        )
                    )
                ).first() is None
                assert (
                    await conn.execute(
                        select(s.native_task_budgets.c.task_id).where(
                            s.native_task_budgets.c.task_id == attached_tid
                        )
                    )
                ).first() is None
            assert (await http.get(host_url, headers=user)).status_code == 401
            host = {"x-bbx-sandbox-report": material.json()["report_key"]}
            assert (
                await http.post(host_url + "/claim", headers=host, content=b"x" * 4097)
            ).status_code == 413
            response = await http.get(host_url, headers=host)
            assert response.status_code == 200, response.text
            plan = response.json()
            assert plan["state"] == "pending" and len(plan["files"]) == 2
            assert all(
                set(file) == {"id", "filename", "path", "size", "sha256"} for file in plan["files"]
            )
            scope = plan["scope"]
            assert scope["workspace_pvc_uid"] == uid
            wrong = {**scope, "workspace_pvc_uid": str(uuid4())}
            assert (await http.get(host_url + "/" + selected[0], headers=host)).status_code == 409
            assert (
                await http.post(host_url + "/claim", headers=host, json={"scope": wrong})
            ).status_code == 409
            first = await http.post(host_url + "/claim", headers=host, json={"scope": scope})
            repeat = await http.post(host_url + "/claim", headers=host, json={"scope": scope})
            assert first.json() == {"launch_allowed": True, "state": "claimed"}
            assert repeat.json() == {"launch_allowed": False, "state": "claimed"}
            assert (await http.get(host_url + "/" + str(uuid4()), headers=host)).status_code == 404
            for file, data in zip(selected, (payload, b""), strict=True):
                response = await http.get(host_url + "/" + file, headers=host)
                assert response.status_code == 200 and response.content == data, response.text
            assert (
                await http.post(
                    host_url + "/confirm",
                    headers=host,
                    json={"scope": wrong, "result": "materialized"},
                )
            ).status_code == 409
            wrong_pod = {**scope, "sandbox_pod_uid": str(uuid4())}
            assert (
                await http.post(
                    host_url + "/confirm",
                    headers=host,
                    json={"scope": wrong_pod, "result": "materialized"},
                )
            ).status_code == 409
            proof = {"scope": scope, "result": "materialized"}
            for _ in range(2):
                assert (
                    await http.post(host_url + "/confirm", headers=host, json=proof)
                ).json() == {"state": "confirmed"}
            assert (await http.get(host_url + "/" + selected[0], headers=host)).status_code == 409
            response = await action("sandbox_host_ready", proof=ready_proof)
            assert response.status_code == 200, response.text
            async with ctf.repo.engine.connect() as conn:
                record = await app.state.native_runner_bootstrap._get(conn, attached_tid)
            assert record is not None
            require_inputs_confirmed(record)
            # The original proof follows the retained PVC, not replacement Pod or mutable files.
            replacement = {
                **record,
                "data": {**record["data"], "resource_generation": 2},
                "observation": {**record["observation"], "sandbox": {"pod_uid": str(uuid4())}},
            }
            with pytest.raises(HTTPException, match="receipt identity changed"):
                require_inputs_confirmed(replacement)
            replacement["data"]["recovery_history"] = [
                {
                    "operation_id": operation["operation_id"],
                    "resource_generation": 1,
                    "execution_context_id": scope["execution_context_id"],
                    "original_observation": record["observation"],
                    "resources": {
                        key: scope[key]
                        for key in (
                            "workspace_pvc_uid",
                            "sandbox_id",
                            "sandbox_pod_uid",
                            "sandbox_container_id",
                        )
                    },
                    "exit_proof": {
                        "runner_terminated": True,
                        "sandbox_terminated": True,
                        "pvcs_retained": True,
                    },
                    "replacement_proof": {"writer_released": True, "workspace_pvc_uid": uid},
                }
            ]
            require_inputs_confirmed(replacement)
            missing = {
                **replacement,
                "data": {
                    key: value
                    for key, value in replacement["data"].items()
                    if key != "inputs_receipt"
                },
            }
            with pytest.raises(HTTPException, match="not confirmed"):
                require_inputs_confirmed(missing)
            replacement["observation"]["workspace_pvc"] = {"uid": str(uuid4())}
            with pytest.raises(HTTPException, match="not confirmed"):
                require_inputs_confirmed(replacement)

        from bbx_contracts.runner_vpn import VPN_DYNAMIC_PROFILE
        from test_native_vpn_integration import task_openvpn_file

        app.state.settings = options
        user = {"Authorization": f"Bearer {issue_user_token(options, 'alice')}"}
        other = {"Authorization": f"Bearer {issue_user_token(options, 'bob')}"}
        path.write_text(
            json.dumps(
                {
                    **publication,
                    "vpn_profile": VPN_DYNAMIC_PROFILE,
                    "runtime_resource_config": {
                        **resource,
                        "traffic_targets": [],
                        "vpn": {
                            "profile": VPN_DYNAMIC_PROFILE,
                            "id": "isolated-task-vpn",
                            "label": "Task network",
                            "tun_resource": "devic.es/tun",
                            "egress_image": "fixture-egress@sha256:" + "a" * 64,
                            "sandbox_image": "fixture-vpn@sha256:" + "e" * 64,
                            "peer_cidrs": ["10.14.0.0/24"],
                            "default_cidrs": ["10.12.0.0/24"],
                            "target_cidrs": ["10.31.0.0/24"],
                            "deny_cidrs": ["10.15.0.0/24"],
                        },
                    },
                }
            )
        )
        app.state.native_ctf_publication = load_publication(options, allow_test_only=True)
        capabilities = (await http.get("/api/task-creation-capabilities", headers=user)).json()
        environment = capabilities["native_ctf"]
        assert environment["vpn"]["profile"] == VPN_DYNAMIC_PROFILE
        reference = {key: environment[key] for key in ("id", "revision", "publication_stamp")}
        private_group = await group()
        source_url = f"/api/task-input-groups/{private_group}/private-vpn-source"
        raw = task_openvpn_file()
        assert (await http.put(source_url, headers=other, content=raw)).status_code == 403

        async def broken_parser(_):
            from anyio import BrokenWorkerProcess

            raise BrokenWorkerProcess("private-worker-startup-detail")

        with monkeypatch.context() as failing:
            failing.setattr(app.state.native_runner_bootstrap.vpn, "parse_source", broken_parser)
            unavailable = await http.put(source_url, headers=user, content=raw)
            assert unavailable.status_code == 503
            assert "private-worker-startup-detail" not in unavailable.text
        upload = await http.put(source_url, headers=user, content=raw)
        assert upload.status_code == 200, upload.text
        source_id = upload.json()["source_id"]
        replay_upload = await http.put(source_url, headers=user, content=raw)
        assert replay_upload.json() == {"source_id": source_id}
        listed = (await http.get(f"/api/task-input-groups/{private_group}", headers=user)).json()
        assert listed["files"] == [] and source_id not in json.dumps(listed)
        private_create = {
            **create,
            "input_group_id": private_group,
            "native_environment": reference,
            "native_vpn_source_id": source_id,
        }
        wrong_group = await http.post(
            "/api/tasks", headers=user, json={**private_create, "input_group_id": await group()}
        )
        assert wrong_group.status_code == 422
        created = await http.post("/api/tasks", headers=user, json=private_create)
        assert created.status_code == 200, created.text
        tid = UUID(created.json()["id"])
        state = (await http.get(f"/api/tasks/{tid}/ctf/state", headers=user)).json()
        assert state["task"]["native_network"] == {"mode": "unselected", "state": "unselected"}
        assert state["task"]["initial_attachments"] == [] and source_id not in json.dumps(state)
        async with ctf.repo.engine.connect() as conn:
            asset = (
                (
                    await conn.execute(
                        select(s.native_vpn_assets).where(
                            s.native_vpn_assets.c.id == UUID(source_id)
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert asset["task_id"] == tid and "PRIVATE KEY" not in asset["ciphertext"]
            bootstrap = app.state.native_runner_bootstrap
            assert bootstrap.vpn.asset_cipher.decrypt(asset["ciphertext"].encode()) == raw
            instructions = (
                (
                    await conn.execute(
                        select(s.ctf_messages.c.body).where(
                            s.ctf_messages.c.task_id == tid, s.ctf_messages.c.recipient_id == "lead"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(instructions) == 1
            assert all(
                expected in instructions[0]
                for expected in (source_id, "network_use_default", create["goal"])
            )
            assert all("PRIVATE KEY" not in text for text in instructions)
