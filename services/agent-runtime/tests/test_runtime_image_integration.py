"""Boot the runtime image without models, verify its lock and SIGTERM shutdown."""

import asyncio
from uuid import uuid4

import asyncpg
import docker
import pytest
from docker.errors import ImageNotFound
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

pytestmark = pytest.mark.integration

EMPTY_BOARD = """
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def respond(self, payload):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def do_GET(self):
        allowed = self.headers.get('Authorization') == 'Bearer image-test'
        if not allowed or self.path not in (
            '/api/tasks?limit=500&offset=0',
            '/api/tasks/deletions',
            '/api/conversations/pending',
        ):
            self.send_error(403)
            return
        self.respond(b'[]')
    def do_POST(self):
        if (self.headers.get('Authorization') != 'Bearer image-test'
                or self.path != '/api/conversations/recover'):
            self.send_error(403)
            return
        self.respond(b'{"requeued":0}')
HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
"""


async def test_runtime_image_lock_and_graceful_shutdown():
    docker_client = docker.from_env()
    try:
        docker_client.images.get("bbx-agent-runtime:latest")
    except ImageNotFound:
        pytest.skip("Build bbx-agent-runtime:latest with make image-agent-runtime")
    finally:
        docker_client.close()
    suffix = uuid4().hex[:8]
    network = Network()
    network.name = f"bbx-m3a-control-{suffix}"
    with network:
        postgres = (
            PostgresContainer(
                "pgvector/pgvector:pg16",
                driver="asyncpg",
                username="test",
                password="test",
                dbname="test",
            )
            .with_name(f"bbx-m3a-lock-pg-{suffix}")
            .with_network(network)
            .with_network_aliases("postgres")
        )
        board = (
            DockerContainer("python:3.12-slim")
            .with_name(f"bbx-m3a-empty-board-{suffix}")
            .with_network(network)
            .with_network_aliases("blackboard")
            .with_command(["python", "-u", "-c", EMPTY_BOARD])
        )
        with postgres, board:
            probe = await asyncpg.connect(
                postgres.get_connection_url().replace("postgresql+asyncpg", "postgresql")
            )
            try:

                def runtime(name):
                    container = (
                        DockerContainer("bbx-agent-runtime:latest")
                        .with_name(name)
                        .with_network(network)
                        .with_volume_mapping("/var/run/docker.sock", "/var/run/docker.sock", "rw")
                    )
                    for key, value in {
                        "POSTGRES_HOST": "postgres",
                        "POSTGRES_USER": "test",
                        "POSTGRES_PASSWORD": "test",
                        "POSTGRES_DB": "test",
                        "BLACKBOARD_URL": "http://blackboard:8000",
                        "SERVICE_TOKEN": "image-test",
                        "DEEPSEEK_API_KEY": "never-used-test-key",
                        "MINIO_ROOT_PASSWORD": "never-used-test-password",
                        "ENVD_TOKEN_SECRET": "image-test-envd-secret",
                    }.items():
                        container.with_env(key, value)
                    return container

                async def wait_for(check):
                    for _ in range(100):
                        if await check():
                            return
                        await asyncio.sleep(0.1)
                    pytest.fail("Runtime image did not reach expected state")

                async def locked():
                    return bool(
                        await probe.fetchval(
                            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted"
                        )
                    )

                with runtime(f"bbx-m3a-runtime-{suffix}") as first:
                    await wait_for(locked)
                    first_container = first.get_wrapped_container()
                    first_container.reload()
                    assert first_container.status == "running"
                    # Allow its first authenticated supervision poll to complete.
                    await asyncio.sleep(0.3)
                    first_container.reload()
                    assert first_container.status == "running", first.get_logs()
                    with runtime(f"bbx-m3a-duplicate-{suffix}") as second:
                        second_container = second.get_wrapped_container()

                        async def exited():
                            await asyncio.to_thread(second_container.reload)
                            return second_container.status == "exited"

                        await wait_for(exited)
                        assert second_container.attrs["State"]["ExitCode"] == 1
                    await asyncio.to_thread(first_container.stop, timeout=15)
                    first_container.reload()
                    assert first_container.attrs["State"]["ExitCode"] == 0, first.get_logs()
                    assert not await locked()
            finally:
                await probe.close()
