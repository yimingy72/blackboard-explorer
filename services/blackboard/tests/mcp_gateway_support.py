"""Private synthetic RSA/backend credentials and one shared native MCP process."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from functools import cache
from uuid import uuid4

import httpx
from bbx_blackboard.gateway_auth import mcp_public_jwks
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from native_gateway_support import IMAGE
from pydantic import SecretStr
from testcontainers.core.container import DockerContainer


@cache
def signing_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return SecretStr(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
    )


def configured_mcp_settings(settings):
    return settings.model_copy(
        update={
            "runner_mcp_signing_key": signing_key(),
            "runner_mcp_backend_token": SecretStr("synthetic-backend-" + uuid4().hex),
        }
    )


class ScriptedMCPRoutes:
    """Isolate pre-existing ledger tests from the separately verified native publisher."""

    async def prepare(self, conn, tid):
        pass

    async def reconcile(self, tid):
        return 1

    async def require_ready(self, conn, tid, verified_revision):
        assert verified_revision == 1
        return f"http://scripted-mcp.invalid/api/tasks/{tid}/mcp"

    async def retire(self, tid):
        pass


@asynccontextmanager
async def native_business_gateway(directory, app, base, tids, *, dynamic=False, on_ready=None):
    from bbx_blackboard.native_mcp import business_mcp_route

    directory.mkdir()
    (directory / "jwks.json").write_text(json.dumps(mcp_public_jwks(app.state.settings)))
    (directory / "backend-token").write_text(
        app.state.settings.runner_mcp_backend_token.get_secret_value()
    )
    config = {
        "config": {"adminAddr": "0.0.0.0:15000"},
        "gateways": {"business-mcp": {"port": 3000}},
        "routes": [
            business_mcp_route(
                tid,
                base.replace("127.0.0.1", "host.docker.internal"),
                "http://localhost:3000",
                "/probe/jwks.json",
                "/probe/backend-token",
            )
            for tid in tids
        ],
    }
    if dynamic:
        config["config"].update(
            {
                "storage": {"mode": "hybrid"},
                "database": {"url": "sqlite:///probe/routes.db?mode=rwc"},
            }
        )
    (directory / "config.json").write_text(json.dumps(config))
    container = (
        DockerContainer(IMAGE)
        .with_name("bbx-p3d-business-mcp-" + uuid4().hex[:8])
        .with_exposed_ports(3000, 15000)
        .with_kwargs(extra_hosts={"host.docker.internal": "host-gateway"})
        .with_volume_mapping(str(directory), "/probe", "rw" if dynamic else "ro")
        .with_command("-f /probe/config.json")
    )
    try:
        await asyncio.to_thread(container.start)
        url = f"http://127.0.0.1:{container.get_exposed_port(3000)}"
        admin = f"http://127.0.0.1:{container.get_exposed_port(15000)}"
        async with httpx.AsyncClient(trust_env=False) as http:
            async with asyncio.timeout(15):
                while True:
                    try:
                        if (await http.get(admin + "/api/runtime")).is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
        if on_ready is not None:
            await on_ready(url, admin)
        try:
            yield url
        except BaseException:
            stdout, stderr = container.get_logs()
            logs = re.sub(
                r"mcp\.session\.id=\S+",
                "mcp.session.id=<redacted>",
                (stdout + stderr).decode(errors="replace"),
            )
            print(
                logs.replace(
                    app.state.settings.runner_mcp_backend_token.get_secret_value(), "<redacted>"
                )
            )
            raise
    finally:
        await asyncio.to_thread(container.stop)
        (directory / "backend-token").unlink(missing_ok=True)
