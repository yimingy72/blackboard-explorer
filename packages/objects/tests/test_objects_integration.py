"""Real MinIO round trip, including an unknown-length multipart upload."""

import time
from typing import BinaryIO, cast
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import pytest
from bbx_objects import ObjectStore
from testcontainers.core.container import DockerContainer

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def endpoint():
    container = (
        DockerContainer("pgsty/minio:RELEASE.2026-04-17T00-00-00Z")
        .with_name(f"bbx-m1b-minio-{uuid4().hex[:8]}")
        .with_exposed_ports(9000)
        .with_env("MINIO_ROOT_USER", "bbxm1buser")
        .with_env("MINIO_ROOT_PASSWORD", "bbxm1b-test-password")
        .with_command("server /data --console-address :9001")
    )
    with container:
        address = f"{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        deadline = time.monotonic() + 30
        while True:
            try:
                with urlopen(f"http://{address}/minio/health/live", timeout=2) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        yield address


class GeneratedStream:
    def __init__(self, size):
        self.remaining = size
        self.max_read = 0

    def read(self, size=-1):
        assert size > 0, "unknown-length upload must read bounded parts"
        self.max_read = max(self.max_read, size)
        count = min(size, self.remaining)
        self.remaining -= count
        return b"x" * count


async def test_minio_storage_and_unknown_length_upload(endpoint):
    store = ObjectStore(
        endpoint,
        "bbxm1buser",
        "bbxm1b-test-password",
        f"bbxm1b{uuid4().hex[:12]}",
    )
    await store.ensure_bucket()
    await store.ensure_bucket()
    assert not await store.exists("evidence/task/missing")
    await store.put("evidence/task/one.txt", b"proof", content_type="text/plain")
    assert await store.exists("evidence/task/one.txt")
    assert await store.get("evidence/task/one.txt") == b"proof"
    assert [part async for part in store.stream("evidence/task/one.txt", chunk_size=2)] == [
        b"pr",
        b"oo",
        b"f",
    ]

    size = 11 * 1024 * 1024 + 7
    source = GeneratedStream(size)
    await store.put("workspace/task.tar.zst", cast(BinaryIO, source))
    assert source.remaining == 0
    assert source.max_read <= 10 * 1024 * 1024 + 1
    assert sum([len(part) async for part in store.stream("workspace/task.tar.zst")]) == size
    assert await store.list("evidence/task/") == ["evidence/task/one.txt"]
