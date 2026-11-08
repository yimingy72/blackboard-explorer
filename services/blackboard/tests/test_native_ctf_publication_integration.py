"""Public creation, immutable deployment authority, and retry behavior in real PostgreSQL."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.input_groups import request_hash
from bbx_blackboard.native_ctf_publication import PublicationState, load_publication
from bbx_blackboard.platform import ModelInput
from bbx_blackboard.profiles import ctf_profile
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import BOARD_CONTRACT
from bbx_contracts.ctf import CtfTaskCreate, load_ctf_profile
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
    app = create_app(options, engine=ctf.repo.engine, objects=Objects())
    app.state.native_ctf_publication = PublicationState()
    platform = app.state.platform_store
    model = await platform.save(
        "models",
        "synthetic-published",
        ModelInput.model_validate(
            {
                "label": "Synthetic CTF model",
                "provider": "openai_chat",
                "model": "synthetic",
                "base_url": "https://native.invalid/v1",
                "reasoning_effort": "none",
                "supports_vision": False,
                "context_window": 16384,
                "max_output_tokens": 128,
                "price": {
                    "billing_mode": "fixed",
                    "currency": "CNY",
                    "cache_hit_per_m": "1",
                    "cache_miss_per_m": "1",
                    "output_per_m": "1",
                    "off_peak": False,
                },
                "api_key": "not-a-real-key",
            }
        ),
        "test",
    )
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
        "model": {
            "id": model["name"],
            "version": model["version"],
            "gateway_alias": "fixture-native",
        },
        "exchange_rate": {
            "rate_cny_per_usd": "7",
            "source_ref": "fixture://fx",
            "observed_at": datetime.now(UTC).isoformat(),
        },
        "runtime_resource_config": resource,
    }
    receipt = tmp_path / "acceptance.json"
    accepted = {
        "schema_version": 1,
        "base_source": "reconstructed",
        "base_tool_image": "fixture-reconstructed@sha256:" + "e" * 64,
        "recipe_sha256": "f" * 64,
        "traffic_sandbox_image": resource["traffic_sandbox_image"],
        "catalog_revision": options.native_gateway_catalog_revision,
        "model": publication["model"],
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
                    **publication["model"],
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
        "budget": {"max_cost": "2", "max_minutes": 5},
        "model_id": model["name"],
        "model_version": model["version"],
        "ctf_options": {"max_teammates": 2},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:

        async def group():
            response = await http.post("/api/task-input-groups", headers=user)
            assert response.status_code == 200, response.text
            return response.json()["id"]

        legacy_body = {**create, "input_group_id": await group()}
        old_hash = request_hash(
            CtfTaskCreate.model_validate(legacy_body).model_dump(exclude={"native_environment"})
        )
        response = await http.post("/api/tasks", headers=user, json=legacy_body)
        assert response.status_code == 200, response.text
        legacy = response.json()["id"]
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
            "model_id",
            "model_version",
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
                    "budget": {"max_cost": "0.00000000001", "max_minutes": 5},
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
