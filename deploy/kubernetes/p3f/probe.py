"""One clean-candidate PG/native/Pi/Kubernetes composition; every model response is synthetic."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from common import STATE, SYSTEM, kubectl, owned_cluster
from install_server import install

NATIVE_IMAGE = (
    "cr.agentgateway.dev/agentgateway@sha256:"
    "60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b"
)


def private_json(path, value):
    path.write_text(json.dumps(value, indent=2))
    path.chmod(0o600)


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


@asynccontextmanager
async def serving(app, port, *, cert=None, key=None):
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            lifespan="off",
            access_log=False,
            log_level="critical",
            ssl_certfile=str(cert) if cert else None,
            ssl_keyfile=str(key) if key else None,
            timeout_graceful_shutdown=2,
        )
    )
    work = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if work.done():
                await work
                raise RuntimeError("Owned HTTP service did not start")
            await asyncio.sleep(0.02)
        yield
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(work, 5)
        except TimeoutError:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)


@asynccontextmanager
async def forward(service, port):
    owned_cluster()
    process = await asyncio.create_subprocess_exec(
        "kubectl",
        "--kubeconfig",
        str(STATE / "kubeconfig"),
        "--context",
        "kind-bbx-p3f-k8s",
        "-n",
        SYSTEM,
        "port-forward",
        "service/" + service,
        f"{port}:80",
        "--address",
        "127.0.0.1",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        end = asyncio.get_running_loop().time() + 20
        while True:
            if process.returncode is not None:
                raise RuntimeError("Owned port-forward stopped")
            try:
                _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                if asyncio.get_running_loop().time() > end:
                    raise
                await asyncio.sleep(0.1)
        yield
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()


def certificates(directory, control_ip):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "P3f synthetic CA")])
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=12))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "host.docker.internal")]))
        .issuer_name(name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=12))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("host.docker.internal"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.IPAddress(ipaddress.ip_address(control_ip)),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    (directory / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (directory / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (directory / "leaf-key.pem").write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    mcp_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (directory / "mcp-key.pem").write_bytes(
        mcp_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    for path in directory.iterdir():
        if path.is_file():
            path.chmod(0o600)


def local_docker():
    import docker

    context = subprocess.check_output(["docker", "context", "show"], text=True).strip()
    endpoint = json.loads(
        subprocess.check_output(
            [
                "docker",
                "context",
                "inspect",
                context,
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            text=True,
        )
    )
    if not endpoint.startswith("unix://"):
        raise RuntimeError("Only the local Unix Docker endpoint is authorized")
    return docker.DockerClient(base_url=endpoint)


async def run(candidate, resume):
    os.environ["PATH"] = str(STATE / "bin") + os.pathsep + os.environ.get("PATH", "")
    # Imports below must resolve to the selected clean product snapshot, never original WIP.
    for directory in reversed(
        [
            "packages/contracts/src",
            "packages/objects/src",
            "services/blackboard/src",
            "services/agent-runtime/src",
        ]
    ):
        sys.path.insert(0, str(candidate / directory))
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
    from bbx_runtime.clients import BlackboardClient
    from bbx_runtime.native_runner import NativeRunnerController
    from bbx_runtime.runner_proxy import RunnerModelProxy
    from bbx_runtime.runner_resources import RunnerResources
    from bbx_runtime.scheduler.lock import RuntimeLock
    from bbx_runtime.settings import NativeRunnerSettings
    from pydantic import SecretStr
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    for value in [create_app, NativeRunnerController, RunnerResources]:
        if not Path(inspect.getfile(value)).resolve().is_relative_to(candidate):
            raise RuntimeError("Product import escaped the clean candidate")
    owned_cluster()
    directory = STATE / "joint"
    state_path = directory / "private-state.json"
    if resume:
        state = json.loads(state_path.read_text())
    else:
        if state_path.exists():
            raise RuntimeError("Original attempt exists; use --resume to reconcile it")
        directory.mkdir(mode=0o700, exist_ok=True)
        state = {
            "run_id": uuid4().hex,
            "source_id": "bbx-p3f-native-" + uuid4().hex,
            "ports": {
                key: free_port()
                for key in ["bb_http", "bb_tls", "model_tls", "provider", "sandbox", "ingress"]
            },
            "pg_password": secrets.token_urlsafe(24),
            "service_token": secrets.token_urlsafe(32),
            "agent_secret": secrets.token_urlsafe(48),
            "backend_token": secrets.token_urlsafe(32),
            "resolver_token": secrets.token_urlsafe(32),
            "provider_key": "synthetic-" + secrets.token_urlsafe(24),
            "launch_request_id": str(uuid4()),
        }
        state["control_ip"] = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                "bbx-p3f-host-address",
                "--network",
                "none",
                "--add-host",
                "host.docker.internal:host-gateway",
                "python:3.12-slim",
                "python",
                "-c",
                "import socket; print(socket.gethostbyname('host.docker.internal'))",
            ],
            text=True,
        ).strip()
        ipaddress.IPv4Address(state["control_ip"])
        certificates(directory, state["control_ip"])
        private_json(state_path, state)
    os.umask(0o077)
    native_directory = directory / "native-data"
    native_directory.mkdir(mode=0o700, exist_ok=True)
    for leaf in ["leaf.pem", "leaf-key.pem"]:
        (native_directory / leaf).write_bytes((directory / leaf).read_bytes())
        (native_directory / leaf).chmod(0o600)
    temporary = directory / "tmp"
    temporary.mkdir(exist_ok=True)
    tempfile.tempdir = str(temporary)
    docker = local_docker()
    labels = {"bbx.dev/owner": "p3f", "bbx.dev/run-id": state["run_id"]}
    ports = state["ports"]
    control_ip = state["control_ip"]
    evidence = {
        "candidate_root": str(candidate),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "synthetic_only": True,
        "checks": {},
        "product_sources": {
            inspect.getfile(value): hashlib.sha256(
                Path(inspect.getfile(value)).read_bytes()
            ).hexdigest()
            for value in [create_app, NativeRunnerController, RunnerResources]
        },
    }
    pg = None
    native = None
    engine = None
    controller = None
    controller_work = None
    proxy = None
    closed = False
    stage = "prepare"
    counts = {"model_sends": 0, "provider_key_only": True, "canonical_tool_seen": False}

    def own_container(identifier):
        value = docker.containers.get(identifier)
        if any(value.labels.get(k) != v for k, v in labels.items()):
            raise RuntimeError("Foreign continuation container refused")
        return value

    try:
        if state.get("pg_id"):
            pg = own_container(state["pg_id"])
        else:
            pg = docker.containers.run(
                "pgvector/pgvector:pg16",
                name="bbx-p3f-pg-" + state["run_id"][:8],
                environment={
                    "POSTGRES_USER": "p3f",
                    "POSTGRES_PASSWORD": state["pg_password"],
                    "POSTGRES_DB": "p3f",
                },
                ports={"5432/tcp": ("127.0.0.1", 0)},
                labels=labels,
                detach=True,
                mem_limit="768m",
            )
            state["pg_id"] = pg.id
            pg.reload()
            state["pg_port"] = int(pg.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"])
            private_json(state_path, state)
        pg_url = f"postgresql+asyncpg://p3f:{state['pg_password']}@127.0.0.1:{state['pg_port']}/p3f"
        engine = create_async_engine(pg_url, pool_pre_ping=True)

        async def pg_ready():
            from sqlalchemy import text

            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        end = asyncio.get_running_loop().time() + 30
        while True:
            try:
                await pg_ready()
                break
            except Exception:
                if asyncio.get_running_loop().time() > end:
                    raise
                await asyncio.sleep(0.2)
        if not state.get("migrated"):

            def migrate():
                old = os.environ.get("BBX_DATABASE_URL")
                os.environ["BBX_DATABASE_URL"] = pg_url
                try:
                    cfg = Config(str(candidate / "services/blackboard/alembic.ini"))
                    cfg.set_main_option(
                        "script_location", str(candidate / "services/blackboard/migrations")
                    )
                    cfg.set_main_option(
                        "prepend_sys_path", str(candidate / "services/blackboard/src")
                    )
                    command.upgrade(cfg, "head")
                finally:
                    if old is None:
                        os.environ.pop("BBX_DATABASE_URL", None)
                    else:
                        os.environ["BBX_DATABASE_URL"] = old

            await asyncio.to_thread(migrate)
            state["migrated"] = True
            private_json(state_path, state)

        async def provider(request: Request):
            value = await request.json()
            counts["model_sends"] += 1
            counts["provider_key_only"] &= (
                request.headers.get("authorization") == "Bearer " + state["provider_key"]
            )
            names = [tool["function"]["name"] for tool in value.get("tools", [])]
            tool = bool(names) and not any(
                message.get("role") == "tool" for message in value["messages"]
            )
            if tool:
                if "board_announcements" not in names:
                    raise RuntimeError("Expected canonical published business tool missing")
                counts["canonical_tool_seen"] = True

            async def events():
                delta = (
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "p3f-native-tool",
                                "type": "function",
                                "function": {"name": "board_announcements", "arguments": "{}"},
                            }
                        ],
                    }
                    if tool
                    else {"role": "assistant", "content": "Synthetic native bootstrap complete."}
                )
                chunks = [
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
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 100,
                            "total_tokens": 200,
                        },
                    },
                ]
                for chunk in chunks:
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "id": "synthetic",
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
            await stack.enter_async_context(serving(provider_app, ports["provider"]))
            base_config = {
                "config": {
                    "adminAddr": "0.0.0.0:15000",
                    "storage": {"mode": "hybrid"},
                    "database": {"url": "sqlite:///probe/native.db?mode=rwc"},
                    "modelCatalog": [{"file": "/probe/catalog.json"}],
                },
                "frontendPolicies": {
                    "accessLog": {
                        "database": {
                            "llm": "metadata",
                            "add": {
                                name: f'request.headers["{header}"]'
                                for name, header in TRUSTED_ATTRIBUTE_HEADERS.items()
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
                                "baseUrl": f"http://{control_ip}:{ports['provider']}/v1",
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
            if not state.get("native_id"):
                private_json(native_directory / "native.json", base_config)
                private_json(
                    native_directory / "catalog.json",
                    {
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
                    },
                )
                native = docker.containers.run(
                    NATIVE_IMAGE,
                    name="bbx-p3f-native-" + state["run_id"][:8],
                    command=["-f", "/probe/native.json"],
                    ports={
                        "3000/tcp": ("0.0.0.0", 0),
                        "4000/tcp": ("127.0.0.1", 0),
                        "15000/tcp": ("127.0.0.1", 0),
                    },
                    volumes={str(native_directory.resolve()): {"bind": "/probe", "mode": "rw"}},
                    extra_hosts={"host.docker.internal": "host-gateway"},
                    labels=labels,
                    detach=True,
                    mem_limit="512m",
                )
                native.reload()
                state["native_id"] = native.id
                state["native_ports"] = {
                    key: int(native.attrs["NetworkSettings"]["Ports"][key + "/tcp"][0]["HostPort"])
                    for key in ["3000", "4000", "15000"]
                }
                private_json(state_path, state)
            else:
                native = own_container(state["native_id"])
            np = state["native_ports"]
            bb_origin = f"http://127.0.0.1:{ports['bb_http']}"
            bb_tls = f"https://{control_ip}:{ports['bb_tls']}"
            mcp_tls = f"https://{control_ip}:{np['3000']}"
            settings = Settings(
                postgres_user="p3f",
                postgres_password=SecretStr(state["pg_password"]),
                postgres_db="p3f",
                postgres_host="127.0.0.1",
                postgres_port=state["pg_port"],
                minio_root_password=SecretStr("synthetic-unused"),
                minio_endpoint="http://127.0.0.1:1",
                service_token=SecretStr(state["service_token"]),
                agent_token_secret=SecretStr(state["agent_secret"]),
                admin_users=SecretStr(""),
                profiles_dir=candidate / "profiles/default",
                native_gateway_accounting_source_id=state["source_id"],
                native_gateway_admin_url=f"http://127.0.0.1:{np['15000']}",
                native_gateway_data_url=f"http://127.0.0.1:{np['4000']}",
                native_gateway_catalog_revision="p3f-synthetic-catalog-v1",
                runner_mcp_signing_key=SecretStr((directory / "mcp-key.pem").read_text()),
                runner_mcp_backend_token=SecretStr(state["backend_token"]),
                native_mcp_gateway_url=mcp_tls,
                native_mcp_backend_url=f"http://{control_ip}:{ports['bb_http']}",
                native_mcp_jwks_file="/probe/jwks.json",
                native_mcp_backend_token_file="/probe/backend-token",
                sandbox_tenant_resolver_token=SecretStr(state["resolver_token"]),
            )
            private_json(native_directory / "jwks.json", mcp_public_jwks(settings))
            (native_directory / "backend-token").write_text(state["backend_token"])
            (directory / "resolver-token").write_text(state["resolver_token"])
            app = create_app(settings, engine=engine)
            await stack.enter_async_context(app.router.lifespan_context(app))
            await stack.enter_async_context(serving(app, ports["bb_http"]))
            await stack.enter_async_context(
                serving(
                    app,
                    ports["bb_tls"],
                    cert=directory / "leaf.pem",
                    key=directory / "leaf-key.pem",
                )
            )
            async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
                deadline = asyncio.get_running_loop().time() + 15
                while True:
                    try:
                        if (
                            await http.get(f"http://127.0.0.1:{np['15000']}/api/runtime")
                        ).is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    if asyncio.get_running_loop().time() > deadline:
                        raise RuntimeError("Owned native gateway unavailable")
                    await asyncio.sleep(0.1)
                stage = "install official control chain"
                await asyncio.to_thread(
                    install,
                    bb_tls + "/api/opensandbox/tenants",
                    directory / "resolver-token",
                    directory / "ca.pem",
                    f"127.0.0.1:{ports['ingress']}",
                )
                await stack.enter_async_context(forward("bbx-p3f-server", ports["sandbox"]))
                await stack.enter_async_context(forward("bbx-p3f-ingress", ports["ingress"]))
                identities = json.loads((STATE / "deployment-identities.json").read_text())
                host_ips = [control_ip]
                resource_config = {
                    key: identities[key]
                    for key in [
                        "source_id",
                        "kubeconfig",
                        "context",
                        "cluster_uid",
                        "storage_class",
                        "environment_revision",
                    ]
                }
                resource_config.update(
                    runner_image=identities["images"]["bbx-p3f-runner:20261106"]["reference"],
                    sandbox_image=identities["images"]["bbx-p3f-sandbox:20261106"]["reference"],
                    sandbox_server_url=f"http://127.0.0.1:{ports['sandbox']}",
                    ca_file=str(directory / "ca.pem"),
                    system_namespace=SYSTEM,
                    runner_control_egress=[
                        {
                            "cidrs": [ip + "/32" for ip in host_ips],
                            "ports": [ports["bb_tls"], ports["model_tls"], np["3000"]],
                        }
                    ],
                )
                private_json(directory / "resource-config.json", resource_config)
                runtime_settings = NativeRunnerSettings(
                    blackboard_url=bb_origin,
                    service_token=SecretStr(state["service_token"]),
                    postgres_host="127.0.0.1",
                    postgres_port=state["pg_port"],
                    postgres_user="p3f",
                    postgres_password=SecretStr(state["pg_password"]),
                    postgres_db="p3f",
                    runner_resource_config_file=directory / "resource-config.json",
                    runner_blackboard_url=bb_tls,
                    runner_model_proxy_url=f"https://{control_ip}:{ports['model_tls']}",
                    runner_gateway_url=f"http://127.0.0.1:{np['4000']}",
                    runner_proxy_port=ports["model_tls"],
                )
                proxy = RunnerModelProxy(
                    runtime_settings, BlackboardClient(bb_origin, state["service_token"])
                )
                await stack.enter_async_context(
                    serving(
                        proxy.app,
                        ports["model_tls"],
                        cert=directory / "leaf.pem",
                        key=directory / "leaf-key.pem",
                    )
                )
                service = await stack.enter_async_context(
                    BlackboardClient(bb_origin, state["service_token"])
                )
                if not state.get("old_fence"):
                    async with RuntimeLock(runtime_settings, fenced=True) as old:
                        assert old.backend_started_at is not None
                        state["old_fence"] = {
                            "controller_fence": old.fence,
                            "controller_pid": old.backend_pid,
                            "controller_started_at": old.backend_started_at.isoformat(),
                        }
                    private_json(state_path, state)
                instance = await stack.enter_async_context(
                    RuntimeLock(runtime_settings, fenced=True)
                )
                resources = RunnerResources(resource_config)
                controller = NativeRunnerController(runtime_settings, service, resources, instance)
                if not state.get("task_id"):
                    stored = await PlatformStore(engine, state["agent_secret"]).save(
                        "models",
                        "p3f-fake",
                        ModelInput.model_validate(
                            {
                                "label": "P3f synthetic model",
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
                        "p3f",
                    )
                    template = load_ctf_profile(candidate / "profiles/ctf").model_dump(mode="json")
                    template["model"] = stored["config"]
                    template["role_run_options"] = {}
                    profile = await ProfileStore(engine).create(
                        "p3f-profile", CtfAgentProfile.model_validate(template), "p3f"
                    )
                    tid = await app.state.ctf_service.create_task(
                        {
                            "mode": "ctf",
                            "goal": "Read board_announcements once. Synthetic proof only.",
                            "budget": {"max_cost": "1", "max_minutes": 10},
                            "ctf_options": {"max_teammates": 1},
                            "agent_profile": "p3f-profile",
                        },
                        profile_version=profile["version"],
                        business_contract=BOARD_CONTRACT,
                    )
                    state["task_id"] = str(tid)
                    private_json(state_path, state)
                    response = await http.post(
                        bb_origin + f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap",
                        headers={"Authorization": "Bearer " + state["service_token"]},
                        json={
                            "request_id": state["launch_request_id"],
                            "model_alias": "bounded-fake",
                            "exchange_rate": {
                                "rate_cny_per_usd": "7",
                                "source_ref": "fixture://p3f",
                                "observed_at": datetime.now(UTC).isoformat(),
                            },
                        },
                    )
                    if response.status_code != 200:
                        raise RuntimeError(
                            "Trusted native launch refused (" + str(response.status_code) + ")"
                        )
                tid = UUID(state["task_id"])
                evidence["task_id"] = str(tid)
                controller_work = asyncio.create_task(controller.run())

                async def record():
                    return await app.state.native_runner_bootstrap.view(tid)

                async def wait_for(read, label, seconds=210):
                    end = asyncio.get_running_loop().time() + seconds
                    while True:
                        if controller_work.done():
                            await controller_work
                            raise RuntimeError("Native controller stopped")
                        value = await read()
                        if value:
                            return value
                        if asyncio.get_running_loop().time() > end:
                            raise RuntimeError("Timeout: " + label)
                        await asyncio.sleep(0.25)

                if not resume:
                    stage = "real Pod/Pi/ledger readiness"

                    async def ready():
                        value = await record()
                        async with engine.connect() as conn:
                            calls = (
                                await conn.execute(
                                    select(
                                        tables.runner_calls.c.kind, tables.runner_calls.c.state
                                    ).where(tables.runner_calls.c.task_id == tid)
                                )
                            ).all()
                            bindings = (
                                await conn.execute(
                                    select(
                                        tables.runner_bindings.c.sdk_session_id,
                                        tables.runner_bindings.c.cursor,
                                    ).where(tables.runner_bindings.c.task_id == tid)
                                )
                            ).all()
                        if (
                            value["state"] == "ready"
                            and any(c.kind == "tool" and c.state == "completed" for c in calls)
                            and any(
                                b.sdk_session_id and b.cursor and b.cursor["seq"] > 0
                                for b in bindings
                            )
                        ):
                            return value
                        return None

                    active = await wait_for(ready, "actual SDK journal and durable MCP result")
                    evidence["observation"] = active["observation"]
                    evidence["checks"]["real_sdk_journal_and_saved_tool"] = True
                    uid = active["observation"]["runner_secret"]["uid"]
                    ns = active["namespace"]
                    secret0 = json.loads(
                        kubectl("-n", ns, "get", "secret", "bbx-runner", "-o", "json")
                    )
                    async with engine.connect() as conn:
                        owner0 = (
                            await conn.execute(
                                select(
                                    tables.runner_owners.c.epoch, tables.runner_owners.c.expires_at
                                ).where(
                                    tables.runner_owners.c.task_id == tid,
                                    tables.runner_owners.c.current.is_(True),
                                )
                            )
                        ).one()
                    stage = "owner and projected credentials beyond sixty seconds"
                    until = asyncio.get_running_loop().time() + 65
                    while asyncio.get_running_loop().time() < until:
                        if controller_work.done():
                            await controller_work
                        await asyncio.sleep(0.5)
                    after = await record()
                    secret1 = json.loads(
                        kubectl("-n", ns, "get", "secret", "bbx-runner", "-o", "json")
                    )
                    async with engine.connect() as conn:
                        owner1 = (
                            await conn.execute(
                                select(
                                    tables.runner_owners.c.epoch, tables.runner_owners.c.expires_at
                                ).where(
                                    tables.runner_owners.c.task_id == tid,
                                    tables.runner_owners.c.current.is_(True),
                                )
                            )
                        ).one()
                    assert (
                        after["state"] == "ready"
                        and after["observation"]["runner"]["pod_uid"]
                        == active["observation"]["runner"]["pod_uid"]
                    )
                    assert (
                        secret1["metadata"]["uid"] == uid
                        and secret1["data"]["credentials.json"]
                        != secret0["data"]["credentials.json"]
                    )
                    assert (
                        owner1.epoch == owner0.epoch
                        and owner1.expires_at > owner0.expires_at
                        and owner1.expires_at > datetime.now(UTC)
                    )
                    evidence["checks"]["same_pod_owner_renewed_over_60s"] = True
                    evidence["checks"]["scoped_secret_rotated"] = True
                    stale = await http.post(
                        bb_origin + f"/api/tasks/{tid}/ctf/runtime/runner-bootstrap/claim",
                        headers={"Authorization": "Bearer " + state["service_token"]},
                        json={
                            "controller_id": "stale-probe",
                            **state["old_fence"],
                            "resource_config": controller.config,
                        },
                    )
                    assert stale.status_code in {403, 409}
                    evidence["checks"]["old_global_controller_refused"] = True
                else:
                    active = await record()
                    ns = active["namespace"]
                    evidence["checks"]["resumed_cleanup_only"] = True
                stage = "public stop and original native exits"
                stopped = await http.post(
                    bb_origin + f"/api/tasks/{tid}/stop",
                    headers={"Authorization": "Bearer " + issue_user_token(settings, "p3f")},
                )
                assert stopped.status_code == 200

                async def ended():
                    value = await record()
                    return (
                        value
                        if value["state"] == "stopped" and value["data"].get("exit_confirmed")
                        else None
                    )

                final = await wait_for(
                    ended, "positive original Pod exits and domain finalization", 120
                )
                for key in ["journal_pvc", "workspace_pvc"]:
                    if key not in final["observation"]:
                        if resume:
                            continue
                        raise RuntimeError("Expected retained Task PVC identity missing")
                    claim = json.loads(
                        kubectl(
                            "-n", ns, "get", "pvc", final["observation"][key]["name"], "-o", "json"
                        )
                    )
                    assert claim["metadata"]["uid"] == active["observation"][key][
                        "uid"
                    ] and not claim["metadata"].get("ownerReferences")
                evidence["checks"]["public_stop_positive_exits"] = True
                evidence["checks"]["both_original_pvcs_retained"] = True
                evidence["final_observation"] = final["observation"]
                evidence["exit_reason"] = final["data"]["exit_reason"]
                evidence["provider"] = counts
                if not resume:
                    assert counts["canonical_tool_seen"] and counts["provider_key_only"]
                evidence["pass"] = not resume
                evidence["cleanup_only"] = resume
                closed = True
                await controller.close()
                controller_work.cancel()
                await asyncio.gather(controller_work, return_exceptions=True)
                await proxy.close()
                await proxy.service.close()
    except BaseException as error:
        evidence.update(
            pass_=False,
            stage=stage,
            error_type=type(error).__name__,
            cleanup_pending=bool(state.get("task_id")),
        )
        raise
    finally:
        if controller is not None:
            await controller.close()
        if controller_work is not None:
            controller_work.cancel()
            await asyncio.gather(controller_work, return_exceptions=True)
        if proxy is not None:
            await proxy.close()
            await proxy.service.close()
        if engine is not None:
            await engine.dispose()
        evidence["container_cleanup_confirmed"] = closed or not state.get("task_id")
        if evidence["container_cleanup_confirmed"]:
            for container in [native, pg]:
                if container is not None:
                    current = own_container(container.id)
                    current.stop(timeout=10)
                    current.remove(v=True)
            state["containers_removed"] = True
            private_json(state_path, state)
        private_json(directory / ("cleanup-evidence.json" if resume else "evidence.json"), evidence)
        docker.close()
    print(
        json.dumps(
            {
                "pass": evidence.get("pass", False),
                "task_id": state.get("task_id"),
                "checks": evidence["checks"],
                "evidence": str(directory / "evidence.json"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.candidate_root.resolve(), args.resume))
