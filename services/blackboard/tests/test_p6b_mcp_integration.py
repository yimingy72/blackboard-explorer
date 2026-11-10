"""P6b real authority routes, SDK discovery and fresh native Bootstrap against PG."""

import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from bbx_blackboard import mcp_catalog
from bbx_blackboard.api import create_app
from bbx_blackboard.auth import issue_user_token
from bbx_blackboard.gateway_auth import issue_gateway_credentials
from bbx_blackboard.native_ctf_publication import NativeCtfPublication, PublicationState
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.store import schema as s
from bbx_contracts.challenge_board import DISPATCH_TOOL
from bbx_contracts.pi_models import PiModelVersion
from bbx_contracts.runner import RunnerBootstrap
from bbx_runtime.scheduler.lock import RuntimeLock
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp_gateway_support import configured_mcp_settings
from pydantic import SecretStr
from runner_gateway_support import call_record, invocation
from sqlalchemy import select
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
MCP = "/api/platform/mcp-servers"


def options():
    return settings().model_copy(
        update={
            "pi_site_id": "primary",
            "pi_model_primary_site": "primary",
            "pi_model_authority": "fixture-authority",
            "pi_model_operator": "alice",
            "pi_model_runtime_token": SecretStr("fixture-runtime-" + "x" * 32),
            "pi_model_authority_token": SecretStr("fixture-authority-" + "y" * 32),
            "runner_mcp_direct_v2": True,
            "sandbox_tenant_resolver_token": SecretStr("fixture-resolver"),
        }
    )


async def test_discover_publish_and_disable_preserve_exact_frozen_material(
    ctf, monkeypatch, caplog
):
    server = Server("synthetic-catalog")
    manager = StreamableHTTPSessionManager(server, stateless=True, json_response=True)
    methods = []
    secret = "synthetic-mcp-secret"
    remote_ref = False
    malformed = False

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name="check",
                description="Synthetic check",
                inputSchema={
                    "type": "object",
                    "properties": {"value": {"type": "string", "description": "Value to check"}},
                    "required": ["value"],
                    **({"$ref": "https://another.invalid/schema"} if remote_ref else {}),
                },
                outputSchema={"type": "object"},
                annotations=types.ToolAnnotations(readOnlyHint=True),
            ),
            types.Tool(name="ungranted", inputSchema={"type": "object"}),
        ]

    async def dns(_url):
        return "1.1.1.1"

    async def wire(_transport, request):
        assert request.url.host == "1.1.1.1"
        assert request.headers["host"] == "mcp.example.test"
        assert request.extensions["sni_hostname"] == "mcp.example.test"
        assert request.headers["authorization"] == f"Bearer {secret}"
        if request.method == "POST":
            envelope = json.loads(request.content)
            methods.append(envelope["method"])
            if malformed and envelope["method"] == "initialize":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": envelope["id"], "result": {"unexpected": secret}},
                )
        return await httpx.ASGITransport(app=manager.handle_request).handle_async_request(request)

    monkeypatch.setattr(mcp_catalog, "public_address", dns)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", wire)
    conf = options()
    app = create_app(conf, engine=ctf.repo.engine, objects=Objects())
    await ProfileStore(ctf.repo.engine).ensure_bundled(
        Path(__file__).resolve().parents[3] / "profiles/default"
    )
    user = {"Authorization": "Bearer " + issue_user_token(conf, "alice")}
    sender = {"X-BBX-Pi-Runtime": conf.pi_model_runtime_token.get_secret_value()}
    body = {"label": "Synthetic MCP", "url": "https://mcp.example.test/mcp", "secret": secret}
    async with (
        manager.run(),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        assert (await http.post(MCP, headers=SERVICE, json=body)).status_code == 403
        saved = await http.post(MCP, headers=user, json=body)
        assert saved.status_code == 200, saved.text
        name = saved.json()["name"]
        versions = f"{MCP}/{name}/versions"
        material = f"/api/runtime/mcp-servers/{name}/versions"
        assert secret not in saved.text
        assert (await http.get(versions + "/1/credentials", headers=SERVICE)).status_code == 403
        assert (await http.get(material + "/1/material", headers=sender)).status_code == 403
        assert (await http.post(versions + "/1/discover", headers=SERVICE)).status_code == 403
        discovery = await http.post(versions + "/1/discover", headers=user)
        assert discovery.status_code == 200, discovery.text
        found = discovery.json()
        assert found["status"] == "ready", found
        assert found["tools"][0]["input_schema"]["required"] == ["value"]
        assert found["tools"][0]["annotations"]["readOnlyHint"] is True
        assert methods == ["initialize", "notifications/initialized", "tools/list"]
        assert (await http.get(versions + "/1/tools", headers=SERVICE)).status_code == 403
        published = await http.post(
            versions + "/1/publish",
            headers=user,
            json={
                "discovery_digest": found["discovery_digest"],
                "tools": ["check"],
                "roles": ["explore"],
            },
        )
        assert published.status_code == 200, published.text
        assert published.json()["version"] == 2
        assert (await http.get(versions + "/2/credentials", headers=SERVICE)).status_code == 403
        assert (await http.get(material + "/2/material", headers=SERVICE)).status_code == 401
        frozen = (await http.get(material + "/2/material", headers=sender)).json()
        assert frozen["secret"] == secret
        assert [tool["name"] for tool in frozen["published"]["tools"]] == ["check"]
        assert (await http.get(versions + "/2/tools", headers=SERVICE)).json()["published"]
        draft = await http.post(
            f"{MCP}/{name}", headers=user, json={"label": "Draft", "url": body["url"]}
        )
        assert draft.status_code == 200, draft.text
        listed = (await http.get(MCP, headers=user)).json()
        assert [(row["version"], row["published_version"]) for row in listed] == [(3, 2)]
        assert [
            (row["version"], row["published_version"])
            for row in (await http.get(MCP, headers=SERVICE)).json()
        ] == [(2, 2)]
        workers = (await http.get("/api/settings/workers", headers=user)).json()
        profile = workers["profile"]
        selection = {
            "expected_revision": workers["revision"],
            "prompt": profile["prompt_templates"]["explore"],
            "tools": {
                **profile["worker_tools"].get("explore", {"builtin": ["post_fact", "release"]}),
                "mcp_servers": [
                    {
                        "name": name,
                        "version": 2,
                        "catalog_version": "p6b",
                        "allowed_tools": ["check"],
                    }
                ],
            },
        }
        accepted = await http.put("/api/settings/workers/explore", headers=user, json=selection)
        assert accepted.status_code == 200, accepted.text
        selection["expected_revision"] = accepted.json()["revision"]
        selection["tools"]["mcp_servers"][0]["version"] = 3
        refused = await http.put("/api/settings/workers/explore", headers=user, json=selection)
        assert refused.status_code == 422, refused.text
        remote_ref = True
        bad = await http.post(versions + "/3/discover", headers=user)
        assert bad.json()["status"] == "error" and secret not in bad.text
        malformed = True
        bad = await http.post(versions + "/3/discover", headers=user)
        assert bad.json()["status"] == "error" and secret not in bad.text
        assert secret not in caplog.text
        disabled = await http.post(
            f"{MCP}/{name}",
            headers=user,
            json={"label": "Disabled", "url": body["url"], "enabled": False},
        )
        assert disabled.status_code == 200, disabled.text
        assert (await http.get(MCP, headers=SERVICE)).json() == []
        assert (await http.get(MCP, headers=user)).json()[0]["published_version"] is None
        assert (await http.get(material + "/2/material", headers=sender)).json() == frozen
        selection["tools"]["mcp_servers"][0]["version"] = 2
        selection["tools"]["mcp_servers"][0]["allowed_tools"].append("ungranted")
        refused = await http.put("/api/settings/workers/explore", headers=user, json=selection)
        assert refused.status_code == 422 and "停用" in refused.text


async def test_fresh_direct_bootstrap_and_sdk_without_agentgateway(ctf, ctf_url, monkeypatch):
    conf = options()
    ctf.objects = Objects()
    app = create_app(conf, engine=ctf.repo.engine, objects=ctf.objects)
    await ProfileStore(ctf.repo.engine).ensure_bundled(
        Path(__file__).resolve().parents[3] / "profiles/default"
    )
    native = app.state.native_runner_bootstrap
    forbidden = []

    def old_gateway(*_args, **_kwargs):
        forbidden.append(True)
        raise AssertionError("direct startup touched the old Gateway")

    for name in ("configuration", "prepare", "reconcile"):
        monkeypatch.setattr(native.gateway.routes, name, old_gateway)
    for name in ("_configured", "prepare", "publish"):
        monkeypatch.setattr(native.gateway.native, name, old_gateway)
    model_id = "fixture-" + uuid4().hex
    model = PiModelVersion.model_validate(
        {
            "id": model_id,
            "model_revision": 1,
            "label": "Synthetic Pi",
            "upstream_model": "fixture",
            "base_url": "https://fixture.test/v1",
            "context_window": 1000000,
            "max_output_tokens": 400000,
            "reasoning": True,
            "input_modalities": ["text"],
            "has_secret": True,
            "usd_rates": {"input": "1", "output": "1", "cache_read": "1", "cache_write": "1"},
            "pricing_source": "Fixture USD",
        }
    )
    deployment = {
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
    app.state.native_ctf_publication = PublicationState(
        exclusive=True,
        stamp="a" * 64,
        publication=NativeCtfPublication(
            id="fixture-environment",
            revision=1,
            label="Synthetic Native",
            release="test_only",
            ctf_profile_version=1,
            runtime_resource_config=deployment,
        ),
    )
    monkeypatch.setattr(app.state.pi_registry, "selected", AsyncMock(return_value=model))

    external = Server("synthetic-native-external")
    external_manager = StreamableHTTPSessionManager(external, stateless=True, json_response=True)
    remote_calls = []
    lost_result = False
    remote_secret = "synthetic-native-external-key"
    remote_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    @external.list_tools()
    async def list_external():
        return [
            types.Tool(name=name, description=f"Read {name}", inputSchema=remote_schema)
            for name in ("board_announcements", "worker_status")
        ]

    @external.call_tool()
    async def call_external(name, arguments):
        remote_calls.append((name, arguments))
        return [types.TextContent(type="text", text="Synthetic external result")]

    async def dns(_url):
        return "1.1.1.1"

    async def outbound(_transport, request):
        assert request.url.host == "1.1.1.1"
        assert request.headers["Host"] == "mcp.native.test"
        assert request.headers["Authorization"] == f"Bearer {remote_secret}"
        assert not any(key.startswith("x-bbx-") for key in request.headers)
        result = await httpx.ASGITransport(
            app=external_manager.handle_request
        ).handle_async_request(request)
        if (
            lost_result
            and request.method == "POST"
            and json.loads(request.content)["method"] == "tools/call"
        ):
            await result.aclose()
            raise httpx.ReadError("Synthetic lost tool result")
        return result

    monkeypatch.setattr(mcp_catalog, "public_address", dns)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", outbound)
    user = {"Authorization": "Bearer " + issue_user_token(conf, "alice")}
    async with (
        app.state.direct_business_mcp.run(),
        external_manager.run(),
        RuntimeLock(runtime_settings(ctf_url), fenced=True) as lock,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        caps = (await http.get("/api/task-creation-capabilities", headers=user)).json()
        environment = caps["native_ctf"]
        assert environment["external_mcp"] is True, caps
        saved = await http.post(
            MCP,
            headers=user,
            json={
                "label": "Native MCP",
                "url": "https://mcp.native.test/mcp",
                "secret": remote_secret,
            },
        )
        assert saved.status_code == 200, saved.text
        server_name = saved.json()["name"]
        versions = f"{MCP}/{server_name}/versions"
        discovered = await http.post(versions + "/1/discover", headers=user)
        published = await http.post(
            versions + "/1/publish",
            headers=user,
            json={
                "discovery_digest": discovered.json()["discovery_digest"],
                "tools": ["board_announcements", "worker_status"],
                "roles": ["lead", "teammate"],
            },
        )
        assert published.status_code == 200, published.text
        group = await http.post("/api/task-input-groups", headers=user)
        assert group.status_code == 200, group.text
        lead_alias = mcp_catalog.native_tool_alias(server_name, "board_announcements")
        worker_alias = mcp_catalog.native_tool_alias(server_name, "worker_status")
        body = {
            "mode": "ctf",
            "goal": "Verify fresh direct Runner and external MCP role grants",
            "budget": {"max_cost": "1", "currency": "USD", "max_minutes": 5},
            "ctf_options": {"max_teammates": 1},
            "pi_model": {"model_id": model_id, "model_revision": 1},
            "native_environment": {
                key: environment[key] for key in ("id", "revision", "publication_stamp")
            },
            "input_group_id": group.json()["id"],
            "native_mcp": {
                "lead": [
                    {"name": server_name, "version": 2, "allowed_tools": ["board_announcements"]}
                ],
                "teammate": [
                    {"name": server_name, "version": 2, "allowed_tools": ["worker_status"]}
                ],
            },
        }
        created = await http.post("/api/tasks", headers=user, json=body)
        assert created.status_code == 200, created.text
        tid = UUID(created.json()["id"])
        path = f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap"
        queued = (await http.get(path, headers=SERVICE)).json()
        assert set(queued["data"]["native_mcp"]) == {"lead", "teammate"}
        assert remote_secret not in json.dumps(queued)
        assert lock.backend_started_at is not None
        response = await http.post(
            path + "/claim",
            headers=SERVICE,
            json={
                "controller_id": "direct-controller",
                "controller_fence": lock.fence,
                "controller_pid": lock.backend_pid,
                "controller_started_at": lock.backend_started_at.isoformat(),
                "resource_config": deployment,
            },
        )
        assert response.status_code == 200, response.text
        actor = {
            "operation_id": queued["operation_id"],
            "controller_id": "direct-controller",
            "controller_generation": lock.fence,
        }
        actions = path + "/actions"
        assert (
            await http.post(actions, headers=SERVICE, json={**actor, "action": "sandbox_create"})
        ).status_code == 200
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
            "sandbox_host_secret": {"name": "bbx-sandbox-host", "uid": str(uuid4())},
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
                "host_container_id": "containerd://synthetic-sandbox-host",
                "pod_ip": "10.12.0.1",
                "phase": "Running",
                "terminated": False,
            },
        }
        response = await http.post(
            actions,
            headers=SERVICE,
            json={**actor, "action": "observation", "observation": observed},
        )
        assert response.status_code == 200, response.text
        host = await http.post(
            actions,
            headers=SERVICE,
            json={
                **actor,
                "action": "sandbox_host_material",
                "proof": {
                    "context": {
                        "sandbox_id": observed["sandbox"]["id"],
                        "sandbox_pod_uid": observed["sandbox"]["pod_uid"],
                        "environment_revision": deployment["environment_revision"],
                        "execution_context_id": "b" * 64,
                        "execd_endpoint": "sandbox.invalid",
                        "host_url": "http://10.12.0.1:8766",
                    }
                },
            },
        )
        assert host.status_code == 200, host.text
        assert host.json()["mcp_transport"] == "direct-v2"
        ready = await http.post(
            actions,
            headers=SERVICE,
            json={
                **actor,
                "action": "sandbox_host_ready",
                "proof": {"context_digest": host.json()["context_digest"]},
            },
        )
        assert ready.status_code == 200, ready.text
        response = await http.post(actions, headers=SERVICE, json={**actor, "action": "material"})
        assert response.status_code == 200, response.text
        material = response.json()
        bootstrap = RunnerBootstrap.model_validate(material["bootstrap"])
        assert bootstrap.mcp_gateway_url is None and set(bootstrap.mcp_targets or {}) == {
            "business",
            "sandbox",
        }
        with pytest.raises(ValueError):
            RunnerBootstrap.model_validate({**material["bootstrap"], "mcp_gateway_url": ""})
        async with ctf.repo.engine.connect() as conn:
            assert not await conn.scalar(
                select(s.native_task_mcp_routes.c.task_id).where(
                    s.native_task_mcp_routes.c.task_id == tid
                )
            )
        credential = next(
            item for item in material["credentials"].values() if item["audience"] == "business-mcp"
        )
        wire = f"/api/tasks/{tid}/business-mcp-v2/"
        sdk_headers = {
            "Authorization": "Bearer " + credential["token"],
            "X-BBX-SDK-Session-ID": str(uuid4()),
        }
        sandbox = next(
            item for item in material["credentials"].values() if item["audience"] == "sandbox-mcp"
        )
        assert (
            host.json()["report_key"] not in response.text
            and host.json()["inbound_key"] not in response.text
        )
        sandbox_headers = {
            "Authorization": "Bearer " + sandbox["token"],
            "X-BBX-Sandbox-Report": host.json()["report_key"],
        }
        authorize = f"/api/tasks/{tid}/sandbox-host/authorize"
        sdk = {"sdk_session_id": sdk_headers["X-BBX-SDK-Session-ID"]}
        authorization = await http.post(
            authorize, headers=sandbox_headers, json={**sdk, "method": "initialize"}
        )
        assert authorization.status_code == 200, authorization.text
        denied = await http.post(
            authorize, headers=sandbox_headers, json={**sdk, "method": "tools/call"}
        )
        assert denied.status_code == 403
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), headers=sdk_headers
            ) as transport,
            streamable_http_client("http://test" + wire, http_client=transport) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {"board_announcements", lead_alias} <= names
            assert "exec_command" not in names
            assert worker_alias not in names
            owner = {
                "fence": material["bootstrap"]["fence"],
                "token": material["credentials"]["control"]["token"],
            }
            batch = await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
            assert batch.status_code == 200, batch.text
            command = batch.json()["commands"][0]
            binding = json.loads(command["payload_json"])["spec"]["binding"]
            await acquire(http, tid, owner, binding, command)
            initial = empty_cursor()
            journal = cursor(initial)
            proof = confirmation(owner, binding, initial, journal)
            sdk_id = sdk_headers["X-BBX-SDK-Session-ID"]
            proof["sdk_session_id"] = sdk_id
            await confirm(http, tid, owner, binding, proof)
            await acknowledge(
                http,
                tid,
                owner,
                command,
                "prepared",
                binding_id=binding["binding_id"],
                sdk_session_id=sdk_id,
                cursor=journal,
                journal_confirmed=True,
                sdk_idle=True,
            )
            activation = await http.post(
                actions, headers=SERVICE, json={**actor, "action": "activate"}
            )
            assert activation.status_code == 200, activation.text

            async def run_tool(
                active_session, bound, session_id, confirmed, headers, name, args, suffix
            ):
                intent = invocation(
                    {
                        "tid": tid,
                        "binding": bound,
                        "sdk": session_id,
                        "journal": confirmed,
                        "owner": owner,
                    },
                    name,
                    args,
                    suffix,
                )
                url = f"{control(tid)}/bindings/{bound['binding_id']}/tools/admit"
                allowed = await http.post(url, headers=headers, json=intent)
                assert allowed.status_code == 200, allowed.text
                assert allowed.json()["disposition"] == "admitted"
                evidence = {"bbx": {**intent, "permit_ref": intent["invocation_id"]}}
                result = await active_session.call_tool(name, args, meta=evidence)
                return result, evidence, intent

            def lead_call(name, args, suffix):
                return run_tool(session, binding, sdk_id, journal, sdk_headers, name, args, suffix)

            denied_intent = invocation(
                {"tid": tid, "binding": binding, "sdk": sdk_id, "journal": journal, "owner": owner},
                worker_alias,
                {"value": "forbidden"},
                "wrong-role",
            )
            denied = await http.post(
                f"{control(tid)}/bindings/{binding['binding_id']}/tools/admit",
                headers=sdk_headers,
                json=denied_intent,
            )
            assert denied.status_code == 403
            built_in, _, _ = await lead_call("board_announcements", {}, "built-in")
            assert not built_in.isError and remote_calls == []
            remote_schema["properties"]["value"]["type"] = "integer"
            drifted, _, drift_intent = await lead_call(
                lead_alias, {"value": "must not send"}, "schema-drift"
            )
            assert drifted.isError and not remote_calls
            refused = await call_record(ctf, tid, "tool:" + drift_intent["invocation_id"])
            assert refused["state"] == "completed"
            remote_schema["properties"]["value"]["type"] = "string"
            first, evidence, lead_intent = await lead_call(lead_alias, {"value": "lead"}, "lead")
            second = await session.call_tool(lead_alias, {"value": "lead"}, meta=evidence)
            assert not first.isError and second == first
            receipt = await call_record(ctf, tid, "tool:" + lead_intent["invocation_id"])
            assert receipt["state"] == "completed" and receipt["provider_key"] == "external-mcp"
            assert remote_calls == [("board_announcements", {"value": "lead"})]

            challenge, _, _ = await lead_call(
                "board_create_challenge",
                {
                    "title": "Synthetic role grant",
                    "description": "Self-contained synthetic exercise",
                },
                "challenge",
            )
            assert challenge.structuredContent
            cid = challenge.structuredContent["result"]["id"]
            ready, _, _ = await lead_call(
                DISPATCH_TOOL,
                {
                    "challenge_id": cid,
                    "expected_revision": 1,
                    "action": "ready",
                    "readiness": "no_external_materials",
                },
                "ready",
            )
            assert not ready.isError
            activated = await http.post(
                actions, headers=SERVICE, json={**actor, "action": "activate"}
            )
            assert activated.status_code == 200, activated.text
            [worker_bid] = activated.json()["data"]["worker_binding_ids"]
            worker_material = await http.post(
                actions,
                headers=SERVICE,
                json={**actor, "action": "material"},
            )
            assert worker_material.status_code == 200, worker_material.text
            worker_credential = next(
                item
                for item in worker_material.json()["credentials"].values()
                if item.get("audience") == "business-mcp" and item.get("binding_id") == worker_bid
            )
            worker_sdk = str(uuid4())
            worker_headers = {
                "Authorization": "Bearer " + worker_credential["token"],
                "X-BBX-SDK-Session-ID": worker_sdk,
            }
            batch = await http.get(
                control(tid) + "/commands", headers=auth(owner), params=poll_query(owner)
            )
            worker_command = next(
                item
                for item in batch.json()["commands"]
                if json.loads(item["payload_json"])
                .get("spec", {})
                .get("binding", {})
                .get("binding_id")
                == worker_bid
            )
            worker_spec = json.loads(worker_command["payload_json"])["spec"]
            assert worker_spec["mcp_tool_routes"][worker_alias] == {
                "target": "business",
                "wire_name": worker_alias,
            }
            assert lead_alias not in worker_spec["allowed_tools"]
            worker_binding = worker_spec["binding"]
            await acquire(http, tid, owner, worker_binding, worker_command)
            worker_journal = cursor(empty_cursor(), "worker")
            await confirm(
                http,
                tid,
                owner,
                worker_binding,
                {
                    **confirmation(owner, worker_binding, empty_cursor(), worker_journal),
                    "sdk_session_id": worker_sdk,
                },
            )
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), headers=worker_headers
                ) as worker_http,
                streamable_http_client("http://test" + wire, http_client=worker_http) as (
                    wr,
                    ww,
                    _,
                ),
                ClientSession(wr, ww) as worker_session,
            ):
                await worker_session.initialize()
                worker_names = {tool.name for tool in (await worker_session.list_tools()).tools}
                assert worker_alias in worker_names and lead_alias not in worker_names
                result, _, _ = await run_tool(
                    worker_session,
                    worker_binding,
                    worker_sdk,
                    worker_journal,
                    worker_headers,
                    worker_alias,
                    {"value": "worker"},
                    "worker",
                )
                assert not result.isError
                lost_result = True
                outcome, _, unknown_intent = await run_tool(
                    worker_session,
                    worker_binding,
                    worker_sdk,
                    worker_journal,
                    worker_headers,
                    worker_alias,
                    {"value": "lost"},
                    "lost",
                )
                assert outcome.isError
            unknown = await call_record(ctf, tid, "tool:" + unknown_intent["invocation_id"])
            assert unknown["state"] == "unknown"
            retry = invocation(
                {"tid": tid, "binding": binding, "sdk": sdk_id, "journal": journal, "owner": owner},
                lead_alias,
                {"value": "new call"},
                "different-role",
            )
            blocked = await http.post(
                f"{control(tid)}/bindings/{binding['binding_id']}/tools/admit",
                headers=sdk_headers,
                json=retry,
            )
            assert blocked.status_code == 409
            assert remote_calls == [
                ("board_announcements", {"value": "lead"}),
                ("worker_status", {"value": "worker"}),
                ("worker_status", {"value": "lost"}),
            ]
        blocked = await http.post(
            wire, headers={**sdk_headers, "Origin": "https://browser.invalid"}
        )
        assert blocked.status_code == 403
        swapped = await http.post(
            wire, headers={**sdk_headers, "Authorization": sandbox_headers["Authorization"]}
        )
        assert swapped.status_code in {401, 403}
        old_settings = configured_mcp_settings(conf)
        async with ctf.repo.engine.connect() as conn:
            owner_row = await native.control._current_owner(conn, tid)
        old_mcp = issue_gateway_credentials(old_settings, owner_row, binding["binding_id"], "lead")[
            "mcp"
        ]
        app.state.settings = old_settings
        old_headers = {
            **sdk_headers,
            "Authorization": "Bearer " + old_mcp["token"],
            "X-BBX-Gateway-Backend": old_settings.runner_mcp_backend_token.get_secret_value(),
        }
        legacy_denied = await http.post(f"/api/tasks/{tid}/business-mcp/", headers=old_headers)
        direct_denied = await http.post(wire, headers=old_headers)
        assert legacy_denied.status_code == 403 and direct_denied.status_code == 401
        assert forbidden == []
