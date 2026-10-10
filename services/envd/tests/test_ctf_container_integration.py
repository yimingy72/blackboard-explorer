"""Real CTF MCP fencing and process termination in disposable test containers."""

import asyncio
import json
import os
import shlex
import time
from contextlib import contextmanager, suppress
from uuid import uuid4

import httpx
import pytest
from docker.errors import NotFound
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from testcontainers.core.container import DockerContainer

pytestmark = pytest.mark.integration
TOKEN = "ctf-isolated-container-test-token"
IMAGE = os.environ.get("BBX_CTF_TEST_IMAGE", "bbx-exec-env:latest")
# A daemon-only crash preserves command processes, unlike restarting a container.
SUPERVISOR = """
import pathlib, subprocess
while True:
    child = subprocess.Popen(['uvicorn', 'bbx_envd.app:app', '--host', '0.0.0.0', '--port', '8080'])
    pathlib.Path('/tmp/ctf-test-envd.pid').write_text(str(child.pid))
    child.wait()
"""


@contextmanager
def running_container(task_id):
    container = (
        DockerContainer(IMAGE)
        .with_name(f"bbx-ctf-mcp-{uuid4().hex[:8]}")
        .with_env("ENVD_TOKEN", TOKEN)
        .with_env("ENVD_MODE", "ctf")
        .with_env("ENVD_TASK_ID", task_id)
        .with_exposed_ports(8080)
        .with_command(["python", "-u", "-c", SUPERVISOR])
        .with_kwargs(nano_cpus=1_000_000_000, mem_limit="1g", pids_limit=128)
    )
    try:
        container.start()
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        with httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False) as http:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if http.get(f"{url}/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Isolated CTF envd did not become ready")
        yield container, url, task_id
    finally:
        # The replacement scenario deliberately removes the first container early.
        with suppress(NotFound):
            container.stop()
        container.get_docker_client().client.close()


@pytest.fixture
def ctf_container():
    with running_container(str(uuid4())) as running:
        yield running


async def execute(url, command, identity=None, command_id=None):
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Agent-Id": "agent-2"}
    async with httpx.AsyncClient(headers=headers, timeout=20, trust_env=False) as http:
        async with streamable_http_client(f"{url}/mcp", http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                meta = (
                    None
                    if identity is None
                    else {"ctf": {**identity, "command_id": command_id or str(uuid4())}}
                )
                return await session.call_tool(
                    "execute_command", {"command": command, "timeout_sec": 60}, meta=meta
                )


def data(result):
    assert not result.isError, result
    assert result.structuredContent is not None
    return result.structuredContent


async def process_group_exists(container, pgid):
    code = (
        "import os,sys\n"
        "try: os.killpg(int(sys.argv[1]),0)\n"
        "except ProcessLookupError: sys.exit(1)\n"
    )
    result = await asyncio.to_thread(
        container.get_wrapped_container().exec_run, ["python", "-c", code, str(pgid)]
    )
    assert result.exit_code in {0, 1}, result.output
    return result.exit_code == 0


async def wait_file(http, url, path):
    async with asyncio.timeout(10):
        while True:
            status = await http.get(f"{url}/stat", params={"path": path})
            assert status.status_code == 200, status.text
            if status.json()["exists"]:
                response = await http.get(f"{url}/files", params={"path": path})
                assert response.status_code == 200, response.text
                return json.loads(response.content)
            await asyncio.sleep(0.05)


def long_command(path):
    script = (
        "import json,os,pathlib,subprocess,time; "
        "child=subprocess.Popen(['sleep','120']); "
        f"path=pathlib.Path({path!r}); temp=path.with_suffix('.tmp'); "
        "temp.write_text(json.dumps({'pgid':os.getpgrp(),'child':child.pid})); "
        "temp.replace(path); "
        "time.sleep(120)"
    )
    return f"python3 -c {shlex.quote(script)}"


async def test_ctf_mcp_boot_dedupe_remote_stop_and_daemon_crash(ctf_container):
    container, url, task_id = ctf_container
    headers = {"Authorization": f"Bearer {TOKEN}"}
    pending = []
    try:
        async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=15) as http:
            assert (
                await http.post(f"{url}/users", json={"agent_id": "agent-2"})
            ).status_code == 200
            status = (await http.get(f"{url}/ctf/status")).json()
            assert status["state"] == "ready" and not status["drained"]
            identity = {
                "task_id": task_id,
                "boot_id": status["boot_id"],
                "agent_id": "agent-2",
                "member_id": "member-1",
                "generation": 1,
            }
            assert (await execute(url, "echo forbidden")).isError
            assert (await execute(url, "echo forbidden", identity)).isError
            registered = await http.post(f"{url}/ctf/register", json=identity)
            assert registered.status_code == 200, registered.text
            assert (
                await execute(url, "echo forbidden", {**identity, "boot_id": str(uuid4())})
            ).isError
            assert (await execute(url, "echo forbidden", {**identity, "generation": 2})).isError
            command_id = str(uuid4())
            command = "echo once >> count.txt; cat count.txt"
            first, duplicate = await asyncio.gather(
                execute(url, command, identity, command_id),
                execute(url, command, identity, command_id),
            )
            assert data(first) == data(duplicate)
            assert data(first)["execution"]["command_id"] == command_id
            assert data(first)["stdout"].count("once") == 1
            assert data(await execute(url, command, identity, command_id)) == data(first)
            assert (await execute(url, "echo changed", identity, command_id)).isError
            count = await http.get(
                f"{url}/files", params={"path": "/workspace/agents/agent-2/count.txt"}
            )
            assert count.content == b"once\n"
            assert (
                await http.post(f"{url}/users", json={"agent_id": "agent-3"})
            ).status_code == 200
            scoped_setup = await asyncio.to_thread(
                container.get_wrapped_container().exec_run,
                [
                    "python",
                    "-c",
                    "from pathlib import Path; "
                    "Path('/workspace/agents/agent-3/private.txt').write_text('private'); "
                    "Path('/workspace/agents/agent-2/alias.txt').symlink_to('/workspace/agents/agent-3/private.txt')",
                ],
            )
            assert scoped_setup.exit_code == 0
            for endpoint in ("files", "stat"):
                scoped = await http.get(
                    f"{url}/{endpoint}",
                    params={
                        "path": "/workspace/agents/agent-2/count.txt",
                        "scope_agent_id": "agent-2",
                    },
                )
                assert scoped.status_code == 200
                for restricted in (
                    "/workspace/agents/agent-3/private.txt",
                    "/workspace/agents/agent-2/alias.txt",
                ):
                    denied = await http.get(
                        f"{url}/{endpoint}",
                        params={
                            "path": restricted,
                            "scope_agent_id": "agent-2",
                        },
                    )
                    assert denied.status_code == 400
            marker = "/workspace/shared/ctf-process.json"
            running = asyncio.create_task(execute(url, long_command(marker), identity))
            pending.append(running)
            process = await wait_file(http, url, marker)
            assert await process_group_exists(container, process["pgid"])
            stopped = await http.post(f"{url}/ctf/stop", json=identity)
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["drained"] is True
            assert not await process_group_exists(container, process["pgid"])
            assert (await execute(url, "echo after-stop", identity)).isError
            next_identity = {**identity, "generation": 2}
            assert (await http.post(f"{url}/ctf/register", json=next_identity)).status_code == 200
            assert (await execute(url, "echo stale", identity)).isError
            assert (await http.post(f"{url}/ctf/stop", json=identity)).status_code == 409
            marker = "/workspace/shared/ctf-orphan.json"
            orphan = asyncio.create_task(execute(url, long_command(marker), next_identity))
            pending.append(orphan)
            process = await wait_file(http, url, marker)
            crashed = await asyncio.to_thread(
                container.get_wrapped_container().exec_run,
                [
                    "python",
                    "-c",
                    "import os,pathlib,signal; "
                    "os.kill(int(pathlib.Path('/tmp/ctf-test-envd.pid').read_text()),signal.SIGKILL)",
                ],
            )
            assert crashed.exit_code == 0, crashed.output
            async with asyncio.timeout(15):
                while True:
                    try:
                        restarted = (await http.get(f"{url}/ctf/status")).json()
                        if restarted.get("boot_id") != identity["boot_id"]:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.05)
            assert restarted["state"] == "unknown" and not restarted["drained"]
            assert await process_group_exists(container, process["pgid"])
            assert (await execute(url, "echo old-boot", next_identity)).isError
            new_boot = {**next_identity, "boot_id": restarted["boot_id"]}
            assert (await execute(url, "echo before-registration", new_boot)).isError
            assert (await http.post(f"{url}/ctf/register", json=new_boot)).status_code == 409
            rejected_drain = await http.post(f"{url}/ctf/drain", json=new_boot)
            assert rejected_drain.status_code == 409
            assert not (await http.get(f"{url}/ctf/status")).json()["drained"]

            archived = await http.post(f"{url}/archive", timeout=30)
            assert archived.status_code == 200, archived.text
            assert archived.headers["x-archive-fallback"] == "none"
            archive_bytes = archived.content
            assert archive_bytes
            previous = container.get_wrapped_container()
            old_container_id = previous.id
            await asyncio.to_thread(previous.remove, force=True, v=True)
            with pytest.raises(NotFound):
                await asyncio.to_thread(previous.client.containers.get, old_container_id)

            with running_container(task_id) as (replacement, replacement_url, _):
                assert replacement.get_container_id() != old_container_id
                async with httpx.AsyncClient(
                    headers=headers, trust_env=False, timeout=30
                ) as restored_http:
                    fresh = (await restored_http.get(f"{replacement_url}/ctf/status")).json()
                    assert fresh["state"] == "ready" and not fresh["drained"]
                    assert fresh["boot_id"] not in {identity["boot_id"], restarted["boot_id"]}
                    restored = await restored_http.post(
                        f"{replacement_url}/restore",
                        content=archive_bytes,
                        headers={"Content-Type": "application/zstd"},
                    )
                    assert restored.status_code == 200, restored.text
                    assert restored.json()["restored"] is True
                    # Restoring the workspace must not restore the daemon's old boot marker.
                    after_restore = (
                        await restored_http.get(f"{replacement_url}/ctf/status")
                    ).json()
                    assert after_restore["state"] == "ready"
                    assert after_restore["boot_id"] == fresh["boot_id"]
                    recovered_identity = {
                        **next_identity,
                        "boot_id": fresh["boot_id"],
                        "generation": 3,
                    }
                    assert (
                        await execute(replacement_url, "cat count.txt", recovered_identity)
                    ).isError
                    registered = await restored_http.post(
                        f"{replacement_url}/ctf/register", json=recovered_identity
                    )
                    assert registered.status_code == 200, registered.text
                    restored_file = data(
                        await execute(replacement_url, "cat count.txt", recovered_identity)
                    )
                    assert restored_file["exit_code"] == 0
                    assert restored_file["stdout"] == "<command_output>once\n</command_output>"
                    shared_marker = await restored_http.get(
                        f"{replacement_url}/files", params={"path": marker}
                    )
                    assert shared_marker.status_code == 200
                    assert shared_marker.json() == process
                    final_drain = await restored_http.post(
                        f"{replacement_url}/ctf/drain", json=recovered_identity
                    )
                    assert final_drain.status_code == 200
                    assert final_drain.json()["drained"] is True
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
