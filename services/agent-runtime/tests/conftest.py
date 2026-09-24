"""Isolated PostgreSQL and MinIO shared by runtime integration scenarios."""

import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def runtime_infrastructure():
    suffix = uuid4().hex[:8]
    postgres = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg").with_name(
        f"bbx-runtime-postgres-{suffix}"
    )
    minio = (
        DockerContainer("pgsty/minio:RELEASE.2026-04-17T00-00-00Z")
        .with_name(f"bbx-runtime-minio-{suffix}")
        .with_exposed_ports(9000)
        .with_env("MINIO_ROOT_USER", "bbxm2buser")
        .with_env("MINIO_ROOT_PASSWORD", "bbxm2b-test-password")
        .with_command("server /data --console-address :9001")
    )
    with postgres, minio:
        database_url = postgres.get_connection_url()
        endpoint = f"{minio.get_container_host_ip()}:{minio.get_exposed_port(9000)}"
        with httpx.Client(trust_env=False) as client:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if client.get(f"http://{endpoint}/minio/health/live").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("Test MinIO did not start")
                time.sleep(0.2)
        old_url = os.environ.get("BBX_DATABASE_URL")
        os.environ["BBX_DATABASE_URL"] = database_url
        try:
            command.upgrade(Config(str(ROOT / "services/blackboard/alembic.ini")), "head")
            yield database_url, endpoint
        finally:
            if old_url is None:
                os.environ.pop("BBX_DATABASE_URL", None)
            else:
                os.environ["BBX_DATABASE_URL"] = old_url
