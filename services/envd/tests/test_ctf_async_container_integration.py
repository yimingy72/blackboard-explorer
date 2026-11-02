"""Async MCP jobs in the same disposable Ubuntu/Kali fixture as synchronous CTF commands."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_ctf_container_integration import (
    TOKEN,
    data,
    execute,
    long_command,
    process_group_exists,
    wait_file,
)
from test_ctf_container_integration import (
    ctf_container as ctf_container,
)

pytestmark = pytest.mark.integration


async def start(url, command, identity, command_id):
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Agent-Id": identity["agent_id"]}
    async with httpx.AsyncClient(headers=headers, timeout=20, trust_env=False) as http:
        async with streamable_http_client(f"{url}/mcp", http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                return await session.call_tool(
                    "start_command",
                    {"command": command, "timeout_sec": 120},
                    meta={"ctf": {**identity, "command_id": command_id}},
                )


async def terminal(http, url, identity, command_id):
    async with asyncio.timeout(15):
        while True:
            response = await http.get(
                f"{url}/ctf/status", params={**identity, "command_id": command_id}
            )
            assert response.status_code == 200, response.text
            status = response.json()
            if status["state"] != "running":
                return status
            await asyncio.sleep(0.05)


async def test_async_start_dedupe_readback_cancel_and_member_stop(ctf_container):
    container, url, task_id = ctf_container
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False, timeout=20
    ) as http:
        root = (await http.get(f"{url}/ctf/status")).json()
        assert "ctf_async_commands_v1" in root.get("capabilities", [])
        identity = {
            "task_id": task_id,
            "boot_id": root["boot_id"],
            "agent_id": "agent-2",
            "member_id": "member-1",
            "generation": 1,
        }
        assert (await http.post(f"{url}/users", json={"agent_id": "agent-2"})).status_code == 200
        assert (await http.post(f"{url}/ctf/register", json=identity)).status_code == 200

        command_id = str(uuid4())
        command = "echo once >> async-count.txt; sleep 2; cat async-count.txt"
        first, duplicate = await asyncio.gather(
            start(url, command, identity, command_id),
            start(url, command, identity, command_id),
        )
        assert data(first)["command_id"] == data(duplicate)["command_id"] == command_id
        completed = await terminal(http, url, identity, command_id)
        assert completed["state"] == "completed"
        assert completed["result"]["exit_code"] == 0
        assert completed["result"]["stdout"] == "<command_output>once\n</command_output>"
        assert completed["result"]["execution"]["command_id"] == command_id
        assert data(await start(url, command, identity, command_id)) == completed
        assert (await start(url, "echo changed", identity, command_id)).isError
        count = await http.get(
            f"{url}/files", params={"path": "/workspace/agents/agent-2/async-count.txt"}
        )
        assert count.content == b"once\n"
        assert (
            await http.post(
                f"{url}/ctf/cancel-command", json={**identity, "command_id": command_id}
            )
        ).json() == completed

        marker = "/workspace/shared/async-cancel.json"
        cancelled_id = str(uuid4())
        assert (
            data(await start(url, long_command(marker), identity, cancelled_id))["state"]
            == "running"
        )
        process = await wait_file(http, url, marker)
        assert await process_group_exists(container, process["pgid"])
        assert data(await execute(url, "echo must-not-run", identity))["state"] == "busy"
        assert (
            data(await start(url, "echo must-not-run", identity, str(uuid4())))["state"] == "busy"
        )
        for wrong in ({"member_id": "member-9"}, {"boot_id": str(uuid4())}, {"generation": 2}):
            denied = await http.get(
                f"{url}/ctf/status", params={**identity, "command_id": cancelled_id, **wrong}
            )
            assert denied.status_code == 409
        cancellation = await http.post(
            f"{url}/ctf/cancel-command", json={**identity, "command_id": cancelled_id}
        )
        assert cancellation.status_code == 200, cancellation.text
        assert cancellation.json()["state"] in {"cancelled", "running"}
        assert (await terminal(http, url, identity, cancelled_id))["state"] == "cancelled"
        assert not await process_group_exists(container, process["pgid"])
        member = (await http.get(f"{url}/ctf/status", params={"agent_id": "agent-2"})).json()
        assert member["state"] == "registered"

        stop_marker = "/workspace/shared/async-stop.json"
        stopped_id = str(uuid4())
        assert (
            data(await start(url, long_command(stop_marker), identity, stopped_id))["state"]
            == "running"
        )
        stopped_process = await wait_file(http, url, stop_marker)
        stopped = await http.post(f"{url}/ctf/stop", json=identity)
        assert stopped.status_code == 200 and stopped.json()["drained"] is True
        assert not await process_group_exists(container, stopped_process["pgid"])
        assert (await terminal(http, url, identity, stopped_id))["state"] == "cancelled"
        assert (await start(url, "echo forbidden", identity, str(uuid4()))).isError
        assert (await execute(url, "echo forbidden", identity)).isError
        next_identity = {**identity, "generation": 2}
        assert (await http.post(f"{url}/ctf/register", json=next_identity)).status_code == 200
        assert (await start(url, "echo stale", identity, str(uuid4()))).isError
        final_id = str(uuid4())
        data(await start(url, "echo next-generation", next_identity, final_id))
        final = await terminal(http, url, next_identity, final_id)
        assert final["state"] == "completed" and final["result"]["exit_code"] == 0
        assert (await http.post(f"{url}/ctf/drain", json=next_identity)).json()["drained"] is True
