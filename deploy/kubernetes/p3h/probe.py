"""One real Pi → native AGW → Task Sandbox host → official execd command/file receipt."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import inspect
import json
import os
import runpy
import secrets
import subprocess
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from common import CONFIG, HERE, KUBE_STATE, STATE, SYSTEM, kubectl, owned_cluster

NATIVE_IMAGE = (
    "cr.agentgateway.dev/agentgateway@sha256:"
    "60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b"
)
NATIVE_TAG = "cr.agentgateway.dev/agentgateway:bbx-p3h-20261107"
SERVICE = "bbx-p3h-gateway"
DNS = f"{SERVICE}.{SYSTEM}.svc.cluster.local"


@asynccontextmanager
async def forward(service, local, remote, *, namespace=SYSTEM, kind="service", diagnostics=None):
    owned_cluster()
    process = await asyncio.create_subprocess_exec(
        "kubectl",
        "--kubeconfig",
        str(CONFIG),
        "-n",
        namespace,
        "port-forward",
        kind + "/" + service,
        f"{local}:{remote}",
        "--address",
        "127.0.0.1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    drains = []

    async def drain(stream, classify=False):
        while line := await stream.readline():
            if classify and diagnostics is not None and len(diagnostics) < 32:
                category = next(
                    (
                        label
                        for word, label in (
                            (b"lost connection", "lost_connection"),
                            (b"error forwarding port", "forward_error"),
                            (b"EOF", "eof"),
                            (b"connection refused", "connection_refused"),
                        )
                        if word in line
                    ),
                    None,
                )
                if category:
                    diagnostics.append(
                        {"service": service, "remote_port": remote, "category": category}
                    )

    try:
        assert process.stderr is not None
        drains.append(asyncio.create_task(drain(process.stderr, True)))
        async with asyncio.timeout(30):
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise RuntimeError("Owned service forward failed")
                if f"127.0.0.1:{local} ->".encode() in line:
                    break
        drains.append(asyncio.create_task(drain(process.stdout)))
        yield
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()
        await asyncio.gather(*drains, return_exceptions=True)


def native_gateway(document, files):
    labels = {"bbx.dev/owner": "p3h", "bbx.dev/component": "native-mcp-gateway"}

    def apply(kind, name, *, api="v1", **value):
        raw = kubectl("get", kind, name, "-n", SYSTEM, "--ignore-not-found", "-o", "json")
        if raw.strip():
            existing = json.loads(raw)
            if any(existing["metadata"].get("labels", {}).get(k) != v for k, v in labels.items()):
                raise RuntimeError("Foreign native gateway object refused")
            return existing["metadata"]["uid"]
        doc = {
            "apiVersion": api,
            "kind": kind,
            "metadata": {"name": name, "namespace": SYSTEM, "labels": labels},
            **value,
        }
        return json.loads(kubectl("create", "-f", "-", "-o", "json", data=doc))["metadata"]["uid"]

    contents = {**files, "native.json": json.dumps(document).encode()}
    secret = apply(
        "Secret",
        SERVICE,
        type="Opaque",
        data={k: base64.b64encode(v).decode() for k, v in contents.items()},
    )
    pod = apply(
        "Pod",
        SERVICE,
        spec={
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "gateway",
                    "image": NATIVE_IMAGE,
                    "imagePullPolicy": "Never",
                    "args": ["-f", "/probe/native.json"],
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "96Mi"},
                        "limits": {"cpu": "500m", "memory": "512Mi"},
                    },
                    "volumeMounts": [
                        {"name": "config", "mountPath": "/probe", "readOnly": True},
                        {"name": "state", "mountPath": "/state"},
                    ],
                }
            ],
            "volumes": [
                {"name": "config", "secret": {"secretName": SERVICE}},
                {"name": "state", "emptyDir": {}},
            ],
        },
    )
    service = apply(
        "Service",
        SERVICE,
        spec={
            "selector": labels,
            "ports": [
                {"name": n, "port": p, "targetPort": p}
                for n, p in [("mcp", 3000), ("models", 4000), ("admin", 15000)]
            ],
        },
    )
    return {"secret_uid": secret, "pod_uid": pod, "service_uid": service}


async def main(candidate, resume, stop_only=False, fresh=False, retire_only=False):
    for directory in reversed(
        [
            "packages/contracts/src",
            "packages/objects/src",
            "services/blackboard/src",
            "services/blackboard/tests",
            "services/agent-runtime/src",
            "services/sandbox-execution/src",
        ]
    ):
        sys.path.insert(0, str(candidate / directory))
    sys.path.append(str(HERE))
    shared = runpy.run_path(str(HERE / "probe.py"), run_name="p3h_shared_fixture")
    serve = shared["serving"]
    free_port = shared["free_port"]
    private_json = shared["private_json"]
    import docker
    import httpx
    from alembic import command
    from alembic.config import Config
    from bbx_blackboard.api import create_app
    from bbx_blackboard.auth import issue_user_token
    from bbx_blackboard.gateway_auth import mcp_public_jwks
    from bbx_blackboard.platform import ModelInput, PlatformStore
    from bbx_blackboard.profiles import ProfileStore
    from bbx_blackboard.settings import Settings
    from bbx_blackboard.store import schema as tables
    from bbx_contracts.challenge_board import BOARD_CONTRACT
    from bbx_contracts.ctf import CtfAgentProfile, load_ctf_profile
    from bbx_contracts.native_gateway import TRUSTED_ATTRIBUTE_HEADERS
    from bbx_contracts.runner import runner_json_text, runner_payload_digest
    from bbx_contracts.runner_bootstrap import retirement_manifest_digest
    from bbx_runtime.clients import BlackboardClient
    from bbx_runtime.native_runner import NativeRunnerController
    from bbx_runtime.runner_proxy import RunnerModelProxy
    from bbx_runtime.runner_resources import ResourceUnknown, RunnerResources
    from bbx_runtime.scheduler.lock import RuntimeLock
    from bbx_runtime.settings import NativeRunnerSettings
    from pydantic import SecretStr
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route
    from test_board_flags_integration import Objects

    for obj in [create_app, NativeRunnerController, RunnerResources]:
        if not Path(inspect.getfile(obj)).resolve().is_relative_to(candidate):
            raise RuntimeError("Import escaped the reviewed candidate")
    owned_cluster()
    cluster = json.loads((KUBE_STATE / "cluster-identity.json").read_text())
    images = json.loads((STATE / "build-source.json").read_text())
    directory = STATE / "joint"
    directory.mkdir(mode=0o700, exist_ok=True)
    state_path = directory / "private-state.json"
    evidence = {
        "candidate": str(candidate),
        "source_sha256": {
            x.__name__: hashlib.sha256(Path(inspect.getfile(x)).read_bytes()).hexdigest()
            for x in [create_app, NativeRunnerController, RunnerResources]
        },
        "checks": {},
        "started_at": datetime.now(UTC).isoformat(),
    }
    d = docker.from_env()
    if resume:
        state = json.loads(state_path.read_text())
    else:
        if state_path.exists():
            raise RuntimeError("Original attempt exists; reconcile with --resume")
        state = {
            "id": uuid4().hex,
            "source_id": "bbx-p3h-" + uuid4().hex,
            "ports": {
                p: free_port()
                for p in ["bb", "tls", "provider", "proxy", "server", "ingress", "admin", "llm"]
            },
            **{
                k: secrets.token_urlsafe(32)
                for k in [
                    "pg_password",
                    "service_token",
                    "agent_secret",
                    "backend_key",
                    "resolver_key",
                    "provider_key",
                ]
            },
        }
        state["host_ip"] = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                "bbx-p3h-host-address",
                "--network",
                "none",
                "--add-host",
                "host.docker.internal:host-gateway",
                "python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30",
                "python",
                "-c",
                'import socket;print(socket.gethostbyname("host.docker.internal"))',
            ],
            text=True,
        ).strip()
        shared["certificates"](directory, state["host_ip"], dns_names=(DNS,))
        private_json(state_path, state)
    ports = state["ports"]
    ip = state["host_ip"]
    stage = "postgres"
    if state.get("pg_id"):
        pg = d.containers.get(state["pg_id"])
        assert pg.labels.get("bbx.dev/owner") == state["id"]
    else:
        pg = d.containers.run(
            "pgvector/pgvector:pg16",
            name="bbx-p3h-pg-" + state["id"][:8],
            environment={
                "POSTGRES_USER": "p3h",
                "POSTGRES_PASSWORD": state["pg_password"],
                "POSTGRES_DB": "p3h",
            },
            ports={"5432/tcp": ("127.0.0.1", 0)},
            labels={"bbx.dev/owner": state["id"]},
            detach=True,
            mem_limit="768m",
        )
        pg.reload()
        state["pg_id"] = pg.id
        state["pg_port"] = int(pg.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"])
        private_json(state_path, state)
    url = f"postgresql+asyncpg://p3h:{state['pg_password']}@127.0.0.1:{state['pg_port']}/p3h"
    engine = create_async_engine(url, pool_pre_ping=True)
    completed = False
    evidence["forward_diagnostics"] = []
    evidence["accounting_diagnostics"] = []
    try:
        async with asyncio.timeout(30):
            while True:
                try:
                    async with engine.connect() as conn:
                        await conn.execute(text("SELECT 1"))
                    break
                except Exception:
                    await asyncio.sleep(0.2)
        if not state.get("migrated"):

            def migrate():
                old = os.environ.get("BBX_DATABASE_URL")
                os.environ["BBX_DATABASE_URL"] = url
                try:
                    config = Config(str(candidate / "services/blackboard/alembic.ini"))
                    config.set_main_option(
                        "script_location", str(candidate / "services/blackboard/migrations")
                    )
                    command.upgrade(config, "head")
                finally:
                    if old is None:
                        os.environ.pop("BBX_DATABASE_URL", None)
                    else:
                        os.environ["BBX_DATABASE_URL"] = old

            await asyncio.to_thread(migrate)
            state["migrated"] = True
            private_json(state_path, state)
        origin = f"http://127.0.0.1:{ports['bb']}"
        tls = f"https://{ip}:{ports['tls']}"
        model_proxy = f"https://{ip}:{ports['proxy']}"
        llm = f"http://127.0.0.1:{ports['llm']}"
        ca = directory / "ca.pem"
        leaf = directory / "leaf.pem"
        key = directory / "leaf-key.pem"
        cfg = Settings(
            _env_file=None,
            postgres_host="127.0.0.1",
            postgres_port=state["pg_port"],
            postgres_user="p3h",
            postgres_password=SecretStr(state["pg_password"]),
            postgres_db="p3h",
            minio_root_password=SecretStr("synthetic-unused"),
            minio_endpoint="http://127.0.0.1:1",
            service_token=SecretStr(state["service_token"]),
            agent_token_secret=SecretStr(state["agent_secret"]),
            admin_users=SecretStr("alice:synthetic"),
            profiles_dir=candidate / "profiles/default",
            native_gateway_accounting_source_id=state["source_id"],
            native_gateway_admin_url=f"http://127.0.0.1:{ports['admin']}",
            native_gateway_data_url=llm,
            native_gateway_catalog_revision="p3h-synthetic-catalog-v1",
            runner_mcp_signing_key=SecretStr((directory / "mcp-key.pem").read_text()),
            runner_mcp_backend_token=SecretStr(state["backend_key"]),
            native_mcp_gateway_url=f"https://{DNS}:3000",
            native_mcp_backend_url=f"http://{ip}:{ports['bb']}",
            native_mcp_jwks_file="/probe/jwks.json",
            native_mcp_backend_token_file="/probe/backend-token",
            sandbox_tenant_resolver_token=SecretStr(state["resolver_key"]),
        )
        app = create_app(cfg, engine=engine, objects=Objects())
        tool_plan = [
            ("write_file", {"path": "/workspace/p3h-proof.txt", "content": "p3h-file\n"}),
            ("read_file", {"path": "/workspace/p3h-proof.txt", "limit": 1024}),
            (
                "exec_command",
                {
                    "command": 'printf "p3h-command uid="; id -u; cat /workspace/p3h-proof.txt',
                    "cwd": "/workspace",
                    "timeout_seconds": 15,
                },
            ),
        ]
        sends = []

        async def provider(request):
            if request.headers.get("authorization") != "Bearer " + state["provider_key"]:
                raise RuntimeError("Synthetic provider key isolation failed")
            body = await request.json()
            names = {v["function"]["name"] for v in body.get("tools", [])}
            index = sum(m.get("role") == "tool" for m in body["messages"])
            tool = tool_plan[index] if index < len(tool_plan) else None
            if tool and tool[0] not in names:
                raise RuntimeError("Canonical Sandbox tool is missing")
            sends.append(tool[0] if tool else "answer")

            async def events():
                delta = (
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"p3h-tool-{index}",
                                "type": "function",
                                "function": {"name": tool[0], "arguments": json.dumps(tool[1])},
                            }
                        ],
                    }
                    if tool
                    else {"role": "assistant", "content": "Synthetic Sandbox receipt complete."}
                )
                for chunk in [
                    {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls" if tool else "stop",
                            }
                        ]
                    },
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45},
                    },
                ]:
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "id": "synthetic-p3h",
                                "model": "bounded-fake",
                                "object": "chat.completion.chunk",
                                "created": 1,
                                **chunk,
                            }
                        )
                        + "\n\n"
                    )
                yield "data: [DONE]\n\n"

            return StreamingResponse(events(), media_type="text/event-stream")

        provider_app = Starlette(routes=[Route("/v1/chat/completions", provider, methods=["POST"])])
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(serve(provider_app, ports["provider"]))
            await stack.enter_async_context(app.router.lifespan_context(app))
            await stack.enter_async_context(serve(app, ports["bb"]))
            await stack.enter_async_context(serve(app, ports["tls"], cert=leaf, key=key))
            stage = "official server and ingress"
            resolver = directory / "resolver-token"
            resolver.write_text(state["resolver_key"])
            resolver.chmod(0o600)
            await asyncio.to_thread(
                shared["install"],
                tls + "/api/opensandbox/tenants",
                resolver,
                ca,
                f"127.0.0.1:{ports['ingress']}",
                input_state=KUBE_STATE,
                owner="p3h",
                server_image="bbx-p3h-server-g7:20261107",
                environment_revision=images["environment_revision"],
            )
            await stack.enter_async_context(forward("bbx-p3f-server", ports["server"], 80))
            await stack.enter_async_context(forward("bbx-p3f-ingress", ports["ingress"], 80))
            stage = "native gateway in cluster"
            load = runpy.run_path(str(HERE / "load_images.py"), run_name="p3h_native_image")["load"]
            subprocess.run(
                ["docker", "tag", NATIVE_IMAGE, NATIVE_TAG], check=True, capture_output=True
            )
            await asyncio.to_thread(load, NATIVE_TAG, NATIVE_IMAGE.split("@", 1)[1])
            gateway = {
                "config": {
                    "adminAddr": "0.0.0.0:15000",
                    "storage": {"mode": "hybrid"},
                    "database": {"url": "sqlite:///state/native.db?mode=rwc"},
                    "modelCatalog": [{"file": "/probe/catalog.json"}],
                },
                "frontendPolicies": {
                    "accessLog": {
                        "database": {
                            "llm": "metadata",
                            "add": {
                                n: f'request.headers["{h}"]'
                                for n, h in TRUSTED_ATTRIBUTE_HEADERS.items()
                            },
                        }
                    }
                },
                "llm": {
                    "port": 4000,
                    "policies": {"apiKey": {"mode": "strict", "keys": []}},
                    "models": [
                        {
                            "name": "bounded-fake",
                            "provider": "openAI",
                            "params": {
                                "apiKey": state["provider_key"],
                                "baseUrl": f"http://{ip}:{ports['provider']}/v1",
                            },
                        }
                    ],
                },
                "gateways": {
                    "business-mcp": {
                        "port": 3000,
                        "tls": {"cert": "/probe/leaf.pem", "key": "/probe/leaf-key.pem"},
                    }
                },
            }
            catalog = {
                "providers": {
                    "openai": {
                        "models": {
                            "bounded-fake": {
                                "limits": {"context": 16384, "output": 128},
                                "modalities": {"input": ["text"], "output": ["text"]},
                                "capabilities": {"reasoning": False, "toolCall": True},
                                "rates": {"input": "10.0", "output": "20.0"},
                            }
                        }
                    }
                }
            }
            evidence["native_gateway"] = await asyncio.to_thread(
                native_gateway,
                gateway,
                {
                    "catalog.json": json.dumps(catalog).encode(),
                    "jwks.json": json.dumps(mcp_public_jwks(cfg)).encode(),
                    "backend-token": state["backend_key"].encode(),
                    "leaf.pem": leaf.read_bytes(),
                    "leaf-key.pem": key.read_bytes(),
                },
            )
            kubectl(
                "-n", SYSTEM, "wait", "--for=condition=Ready", "pod/" + SERVICE, "--timeout=90s"
            )
            await stack.enter_async_context(
                forward(SERVICE, ports["admin"], 15000, diagnostics=evidence["forward_diagnostics"])
            )
            await stack.enter_async_context(
                forward(SERVICE, ports["llm"], 4000, diagnostics=evidence["forward_diagnostics"])
            )
            native = app.state.runner_gateway.native
            await native.admin.close()
            native.admin.http = await stack.enter_async_context(
                httpx.AsyncClient(
                    trust_env=False,
                    timeout=5,
                    transport=httpx.AsyncHTTPTransport(
                        retries=0, limits=httpx.Limits(max_keepalive_connections=0)
                    ),
                )
            )
            native.admin.owned = False
            for endpoint in ("/api/runtime", "/api/budgets/status", "/api/runtime"):
                snapshot = await native.admin.request("GET", endpoint)
                if not isinstance(snapshot, dict):
                    raise RuntimeError("Native management health shape changed")
            original_consume = native.consume

            async def observed_consume(task_id):
                try:
                    return await original_consume(task_id)
                except Exception as error:
                    chain = []
                    current = error
                    while current is not None and len(chain) < 6:
                        chain.append(type(current).__name__)
                        current = current.__cause__
                    if len(evidence["accounting_diagnostics"]) < 12:
                        item = {"at": datetime.now(UTC).isoformat(), "classes": chain}
                        if isinstance(error, ValueError) and str(error) in {
                            "Native budget status has an unsupported shape",
                            "Native budget status is missing or ambiguous",
                            "Native budget status does not match the frozen contract",
                        }:
                            item["reason"] = str(error)
                        evidence["accounting_diagnostics"].append(item)
                        private_json(
                            directory / "accounting-diagnostics.json",
                            evidence["accounting_diagnostics"],
                        )
                    raise

            native.consume = observed_consume
            resources_cfg = {
                "source_id": state["source_id"],
                "cluster_uid": cluster["cluster_uid"],
                "kubeconfig": str(CONFIG),
                "context": "kind-bbx-p3f-k8s",
                "storage_class": "standard",
                "system_namespace": SYSTEM,
                "ca_file": str(ca),
                "environment_revision": images["environment_revision"],
                "runner_image": images["images"]["bbx-p3h-runner:20261107"]["reference"],
                "sandbox_image": images["images"]["bbx-p3g-sandbox:20261107"]["reference"],
                "sandbox_host_image": images["images"]["bbx-p3h-sandbox-host:20261107"][
                    "reference"
                ],
                "runner_profile": "node-sandbox-host-v1",
                "sandbox_network": {
                    "namespace": "worker-a",
                    "network_identity": "worker-a",
                    "ipv4": "198.19.1.2",
                },
                "sandbox_host_execd_origin": f"bbx-p3f-ingress.{SYSTEM}.svc.cluster.local:80",
                "sandbox_server_url": f"http://127.0.0.1:{ports['server']}",
                "runner_control_egress": [
                    {"cidrs": [ip + "/32"], "ports": [ports["tls"], ports["proxy"]]}
                ],
            }
            if retire_only:
                resources_cfg["retirement_single_apiserver"] = cluster[
                    "retirement_single_apiserver"
                ]
            private_json(directory / "resources.json", resources_cfg)
            settings = NativeRunnerSettings(
                _env_file=None,
                blackboard_url=origin,
                service_token=SecretStr(state["service_token"]),
                postgres_host="127.0.0.1",
                postgres_port=state["pg_port"],
                postgres_user="p3h",
                postgres_password=SecretStr(state["pg_password"]),
                postgres_db="p3h",
                runner_resource_config_file=directory / "resources.json",
                runner_blackboard_url=tls,
                runner_model_proxy_url=model_proxy,
                runner_gateway_url=llm,
                runner_proxy_port=ports["proxy"],
            )
            service = await stack.enter_async_context(
                BlackboardClient(origin, state["service_token"])
            )
            await stack.enter_async_context(
                serve(RunnerModelProxy(settings, service).app, ports["proxy"], cert=leaf, key=key)
            )
            lock = await stack.enter_async_context(RuntimeLock(settings, fenced=True))
            resources = RunnerResources(resources_cfg)

            def diagnostic(name, method):
                async def observed(*args, **kwargs):
                    try:
                        result = await method(*args, **kwargs)
                        if name == "stop":
                            evidence["last_stop_proof"] = result
                        return result
                    except ResourceUnknown as error:
                        evidence["last_resource_failure"] = {"step": name, "reason": str(error)}
                        raise

                return observed

            for step in (
                "ensure_task_resources",
                "sandbox_host_context",
                "publish_sandbox_host",
                "wait_sandbox_host_ready",
                "stop",
            ):
                setattr(resources, step, diagnostic(step, getattr(resources, step)))
            controller = NativeRunnerController(settings, service, resources, lock)
            http = await stack.enter_async_context(httpx.AsyncClient(trust_env=False, timeout=10))
            headers = {"Authorization": "Bearer " + state["service_token"]}
            if fresh:
                if not state.get("task_id") or state.get("fresh_attempted"):
                    raise RuntimeError("The one new Task requires a positively stopped original")
                original = await app.state.native_runner_bootstrap.view(UUID(state["task_id"]))
                if original["state"] != "stopped" or not original["data"].get("exit_confirmed"):
                    raise RuntimeError("The original Task has no positive exit proof")
                state["previous_task_id"] = state.pop("task_id")
                state["fresh_attempted"] = True
                private_json(state_path, state)
            if not state.get("task_id"):
                stage = "original Task launch"
                stored = await PlatformStore(engine, state["agent_secret"]).save(
                    "models",
                    "p3h-fake",
                    ModelInput.model_validate(
                        {
                            "label": "Synthetic P3h",
                            "provider": "openai_chat",
                            "model": "bounded-fake",
                            "base_url": f"http://127.0.0.1:{ports['provider']}/v1",
                            "reasoning_effort": "none",
                            "supports_vision": False,
                            "context_window": 16384,
                            "max_output_tokens": 64,
                            "price": {
                                "billing_mode": "fixed",
                                "currency": "CNY",
                                "cache_hit_per_m": "1",
                                "cache_miss_per_m": "1",
                                "output_per_m": "1",
                                "off_peak": False,
                            },
                            "api_key": state["provider_key"],
                            "credential_source": "stored",
                        }
                    ),
                    "p3h",
                )
                template = load_ctf_profile(candidate / "profiles/ctf").model_dump(mode="json")
                template["model"] = stored["config"]
                template["role_run_options"] = {}
                profile_name = "p3h-profile-" + uuid4().hex[:8]
                profile = await ProfileStore(engine).create(
                    profile_name, CtfAgentProfile.model_validate(template), "p3h"
                )
                tid = await app.state.ctf_service.create_task(
                    {
                        "mode": "ctf",
                        "goal": "Run the local Sandbox receipt only",
                        "budget": {"max_cost": "1", "max_minutes": 8},
                        "ctf_options": {"max_teammates": 1},
                        "agent_profile": profile_name,
                    },
                    profile_version=profile["version"],
                    business_contract=BOARD_CONTRACT,
                )
                state["task_id"] = str(tid)
                private_json(state_path, state)
                response = await http.post(
                    origin + f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap",
                    headers=headers,
                    json={
                        "request_id": str(uuid4()),
                        "model_alias": "bounded-fake",
                        "exchange_rate": {
                            "rate_cny_per_usd": "7",
                            "source_ref": "fixture://p3h",
                            "observed_at": datetime.now(UTC).isoformat(),
                        },
                    },
                )
                if response.status_code != 200:
                    raise RuntimeError("Trusted H launch refused: " + str(response.status_code))
            tid = UUID(state["task_id"])
            evidence["task_id"] = str(tid)
            if retire_only:
                stage = "original H Task DELETE and retirement"
                success = json.loads((directory / "fresh-evidence.json").read_text())
                original = await app.state.native_runner_bootstrap.view(tid)
                if (
                    success["task_id"] != str(tid)
                    or not success["completed"]
                    or original["state"] != "stopped"
                    or not original["data"].get("exit_confirmed")
                ):
                    raise RuntimeError("Only the positively stopped original H Task may retire")
                user = {"Authorization": "Bearer " + issue_user_token(cfg, "alice")}
                retirement = original["data"].get("retirement", {})
                if retirement.get("state") == "retired":
                    previous = json.loads((directory / "retirement-evidence.json").read_text())
                    proof = previous["retirement_proof"]
                    assert previous["task_id"] == str(tid) and proof["retired"] is True
                    assert proof["manifest_digest"] == retirement_manifest_digest(
                        retirement["manifest"]
                    )
                    assert await resources._get("namespace", original["namespace"]) is None
                    evidence["retirement_proof"] = proof
                    evidence["purge_recovery_only"] = True
                if not retirement:
                    page = await http.get(
                        origin + f"/api/tasks/{tid}/sandbox/commands", headers=user
                    )
                    assert (
                        page.status_code == 200 and success["command"]["execution_id"] in page.text
                    )
                    private_json(
                        directory / "before-retire-command-record.json",
                        {
                            "task_id": str(tid),
                            "user_http_status": page.status_code,
                            "command": success["command"],
                            "scope": success["scope"],
                        },
                    )
                else:
                    prior_user = json.loads(
                        (directory / "before-retire-command-record.json").read_text()
                    )
                    assert (
                        prior_user["task_id"] == str(tid) and prior_user["user_http_status"] == 200
                    )
                original_retire = resources.retire

                async def recorded_retire(operation, **callbacks):
                    checkpoint = callbacks["checkpoint"]

                    async def record_checkpoint(manifest):
                        await checkpoint(manifest)
                        private_json(directory / "retirement-manifest.json", manifest)

                    callbacks["checkpoint"] = record_checkpoint
                    try:
                        proof = await original_retire(operation, **callbacks)
                    except ResourceUnknown as error:
                        evidence["last_resource_failure"] = {"step": "retire", "reason": str(error)}
                        raise
                    evidence["retirement_proof"] = proof
                    return proof

                resources.retire = recorded_retire
                if not original.get("data", {}).get("retirement"):
                    deleted = await http.delete(origin + f"/api/tasks/{tid}", headers=user)
                    assert deleted.status_code in {200, 202}
                work = asyncio.create_task(controller.run())
                try:
                    async with asyncio.timeout(150):
                        while True:
                            if work.done():
                                await work
                                raise RuntimeError("Original H retirement controller exited")
                            if evidence.get("retirement_proof", {}).get("retired") is True:
                                async with engine.connect() as conn:
                                    if (
                                        await conn.execute(
                                            select(tables.tasks.c.id).where(
                                                tables.tasks.c.id == tid
                                            )
                                        )
                                    ).first() is None:
                                        break
                            await asyncio.sleep(0.2)
                    manifest = json.loads((directory / "retirement-manifest.json").read_text())
                    host_uid = success["observed"]["sandbox_host_secret"]["uid"]
                    assert any(
                        ref["kind"] == "Secret"
                        and ref["name"] == "bbx-sandbox-host"
                        and ref["uid"] == host_uid
                        and ref.get("absent") is True
                        for ref in manifest["resources"]
                    )
                    async with engine.connect() as conn:
                        for column in (
                            tables.tasks.c.id,
                            tables.task_runner_resources.c.task_id,
                            tables.sandbox_command_records.c.task_id,
                        ):
                            assert (
                                await conn.execute(select(column).where(column == tid))
                            ).first() is None
                        assert (
                            await conn.execute(
                                select(tables.task_runner_resources.c.state).where(
                                    tables.task_runner_resources.c.task_id
                                    == UUID(state["previous_task_id"])
                                )
                            )
                        ).scalar_one() == "stopped"
                    assert await resources._get("namespace", original["namespace"]) is None
                    assert await resources._retirement_inventory(original["namespace"]) == []
                    evidence["checks"] = {
                        "original_host_secret_uid_retired": True,
                        "original_namespace_pvc_pv_retired": True,
                        "original_task_capability_and_command_rows_purged": True,
                        "other_stopped_task_authority_preserved": True,
                    }
                    completed = True
                finally:
                    await controller.close()
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                return
            work = asyncio.create_task(controller.run())

            async def wait(read, seconds):
                async with asyncio.timeout(seconds):
                    while True:
                        if work.done():
                            await work
                            raise RuntimeError("Native controller exited")
                        value = await read()
                        if value:
                            return value
                        await asyncio.sleep(0.25)

            try:
                stage = "Pi command/file saved records"

                async def ready():
                    bootstrap = await app.state.native_runner_bootstrap.view(tid)
                    evidence["last_bootstrap"] = {
                        k: bootstrap.get(k) for k in ["state", "desired", "last_error"]
                    }
                    async with engine.connect() as conn:
                        cmd = (
                            (
                                await conn.execute(
                                    select(tables.sandbox_command_records).where(
                                        tables.sandbox_command_records.c.task_id == tid
                                    )
                                )
                            )
                            .mappings()
                            .first()
                        )
                        calls = (
                            (
                                await conn.execute(
                                    select(tables.runner_calls).where(
                                        tables.runner_calls.c.task_id == tid,
                                        tables.runner_calls.c.kind == "tool",
                                        tables.runner_calls.c.state == "completed",
                                        tables.runner_calls.c.provider_key == "sandbox",
                                    )
                                )
                            )
                            .mappings()
                            .all()
                        )
                    if (
                        cmd
                        and cmd["state"] == "completed"
                        and cmd["execution_id"]
                        and len(calls) >= 3
                    ):
                        saved = {
                            call["data"]["tool_name"]: json.loads(
                                app.state.runner_gateway._permit(call)["result_json"]
                            )
                            for call in calls
                        }
                        if {"read_file", "write_file", "exec_command"} <= set(
                            saved
                        ) and "answer" in sends:
                            assert all(not item.get("isError") for item in saved.values())
                            assert (
                                saved["read_file"]["structuredContent"]["result"]["content"]
                                == "p3h-file\n"
                            )
                            return bootstrap, cmd
                    return None

                cmd = None
                if not stop_only:
                    bootstrap, cmd = await wait(ready, 240)
                    output = cmd["data"]["stdout"]
                    assert "p3h-command uid=10000" in output and "p3h-file" in output
                    assert cmd["exit_code"] == 0 and sends[:3] == [
                        "write_file",
                        "read_file",
                        "exec_command",
                    ]
                    evidence["checks"]["actual_pi_saved_command_and_two_file_tools"] = True
                    evidence["command"] = {
                        k: str(cmd[k])
                        for k in [
                            "id",
                            "execution_id",
                            "agent_id",
                            "assignment_id",
                            "state",
                            "exit_code",
                        ]
                    }
                    evidence["command"]["stdout"] = output
                    evidence["observed"] = {
                        k: bootstrap["observation"][k]
                        for k in [
                            "namespace_uid",
                            "runner",
                            "sandbox",
                            "journal_pvc",
                            "workspace_pvc",
                            "sandbox_host_secret",
                        ]
                    }
                    stage = "original Assignment close and sealed empty scope"
                    host_port = free_port()
                    async with forward(
                        "bbx-runner",
                        host_port,
                        8766,
                        namespace=bootstrap["namespace"],
                        kind="pod",
                        diagnostics=evidence["forward_diagnostics"],
                    ):
                        denied = await http.get(f"http://127.0.0.1:{host_port}/ready")
                        assert denied.status_code == 401
                    evidence["checks"]["sandbox_host_missing_inbound_key_denied"] = True
                    payload = runner_json_text({"kind": "close", "binding_id": cmd["binding_id"]})
                    close = await http.post(
                        origin + f"/api/tasks/{tid}/ctf/runtime/runner-management/commands",
                        headers=headers,
                        json={
                            "request_id": str(uuid4()),
                            "expected_epoch": bootstrap["data"]["owner_epoch"],
                            "payload_json": payload,
                            "payload_digest": runner_payload_digest(payload),
                        },
                    )
                    assert close.status_code == 200

                    async def sealed():
                        async with engine.connect() as conn:
                            record = (
                                await conn.execute(
                                    select(tables.sandbox_command_records.c.data).where(
                                        tables.sandbox_command_records.c.task_id == tid,
                                        tables.sandbox_command_records.c.id == cmd["id"],
                                    )
                                )
                            ).scalar_one()
                        return record if record.get("scope_state") == "sealed" else None

                    scope = await wait(sealed, 60)
                    assert (
                        scope["scope_execution_id"]
                        and scope["scope_execution_id"] != cmd["execution_id"]
                    )
                    evidence["checks"]["original_assignment_sealed_and_empty"] = True
                    evidence["scope"] = {
                        k: scope[k] for k in ("scope_state", "scope_id", "scope_execution_id")
                    }
                stage = "original two-container and Sandbox exit"
                stop = await http.post(origin + f"/api/tasks/{tid}/stop", headers=headers)
                assert stop.status_code == 200

                async def stopped():
                    value = await app.state.native_runner_bootstrap.view(tid)
                    evidence["last_bootstrap"] = {
                        k: value.get(k) for k in ["state", "desired", "last_error"]
                    }
                    return (
                        value
                        if value["state"] == "stopped" and value["data"].get("exit_confirmed")
                        else None
                    )

                value = await wait(stopped, 120)
                assert value["data"]["exit_reason"] == "terminated"
                evidence["checks"]["original_dual_runner_and_sandbox_exit"] = True
                evidence["checks"]["ordinary_stop_preserved_pvcs"] = all(
                    [
                        await resources._get(
                            "pvc", value["observation"][k]["name"], value["namespace"]
                        )
                        is not None
                        for k in ["journal_pvc", "workspace_pvc"]
                    ]
                )
                evidence["observed"] = {
                    k: value["observation"][k]
                    for k in (
                        "namespace_uid",
                        "runner",
                        "sandbox",
                        "journal_pvc",
                        "workspace_pvc",
                        "sandbox_host_secret",
                    )
                }
                if cmd is not None:
                    user = {"Authorization": "Bearer " + issue_user_token(cfg, "alice")}
                    page = await http.get(
                        origin + f"/api/tasks/{tid}/sandbox/commands", headers=user
                    )
                    evidence["checks"]["user_scoped_command_records"] = (
                        page.status_code == 200 and cmd["execution_id"] in page.text
                    )
                    completed = all(evidence["checks"].values())
            finally:
                await controller.close()
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
    finally:
        await engine.dispose()
        evidence["stage"] = stage
        evidence["synthetic_model_sends"] = sends if "sends" in locals() else []
        evidence["completed"] = completed
        evidence["stop_only"] = stop_only
        evidence["task_id"] = state.get("task_id")
        name = (
            "retirement-evidence.json"
            if retire_only
            else (
                "fresh-evidence.json"
                if fresh
                else ("resume-evidence.json" if resume else "evidence.json")
            )
        )
        private_json(directory / name, evidence)
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-only", action="store_true")
    parser.add_argument("--fresh-after-stop", action="store_true")
    parser.add_argument("--retire-only", action="store_true")
    options = parser.parse_args()
    if options.stop_only and not options.resume:
        parser.error("--stop-only requires the original --resume authority")
    if options.fresh_after_stop and (not options.resume or options.stop_only):
        parser.error("--fresh-after-stop requires --resume and a positive original stop")
    if options.retire_only and (
        not options.resume or options.stop_only or options.fresh_after_stop
    ):
        parser.error("--retire-only requires the positively stopped original --resume authority")
    asyncio.run(
        main(
            options.candidate.resolve(),
            options.resume,
            options.stop_only,
            options.fresh_after_stop,
            options.retire_only,
        )
    )
