"""Execution-before-validation refusals remain correctable tool results."""

import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from bbx_runtime.clients import EnvdClient
from bbx_runtime.ctf.execution import CtfExecutionAdapter
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_cooperation_integration import cooperation_board as cooperation_board
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

from services.envd.tests.test_ctf_container_integration import TOKEN, running_container

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("tool_name", ["execute_command", "start_command"])
async def test_missing_cwd_does_not_run_command_or_escape_as_mcp_exception_group(tool_name):
    tid = str(uuid4())
    with running_container(tid) as (_, url, _):
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False
        ) as http:
            assert (
                await http.post(f"{url}/users", json={"agent_id": "agent-2"})
            ).status_code == 200
            root = (await http.get(f"{url}/ctf/status")).json()
            identity = {
                "task_id": tid,
                "boot_id": root["boot_id"],
                "agent_id": "agent-2",
                "member_id": "member-1",
                "generation": 1,
            }
            assert (await http.post(f"{url}/ctf/register", json=identity)).status_code == 200
            envd = EnvdClient(url, TOKEN)
            adapter = CtfExecutionAdapter(cast(Any, None), envd)
            directory = "/workspace/agents/agent-2/not-created"
            marker = directory + "/once.txt"
            command = f"mkdir -p {directory}; echo once >> {marker}; cat {marker}"
            rejected_id = str(uuid4())
            try:
                rejected = json.loads(
                    await adapter._request_tool(
                        identity,
                        {"command": command, "cwd": directory, "timeout_sec": 30},
                        {"ctf": {**identity, "command_id": rejected_id}},
                        tool_name,
                    )
                )["structuredContent"]
                if tool_name == "start_command":
                    assert rejected["state"] == "failed"
                    rejected = rejected["result"]
                assert rejected["state"] == "rejected"
                assert rejected["dispatched"] is False
                assert rejected["error"]["code"] == "invalid_cwd"
                assert not (await http.get(f"{url}/stat", params={"path": directory})).json()[
                    "exists"
                ]
                corrected = json.loads(
                    await adapter._request_tool(
                        identity,
                        {"command": command, "cwd": None, "timeout_sec": 30},
                        {"ctf": {**identity, "command_id": str(uuid4())}},
                        tool_name,
                    )
                )["structuredContent"]
                if tool_name == "start_command":
                    assert corrected["state"] == "completed"
                    corrected = corrected["result"]
                assert corrected["exit_code"] == 0
                assert (
                    await http.get(f"{url}/files", params={"path": marker})
                ).content == b"once\n"
            finally:
                await adapter.close()


async def test_native_agent_corrects_directory_input_without_failing_team(cooperation_board):
    board, ctf, _, configured, _, _ = cooperation_board
    app = cast(Any, board._http._transport.app)
    tid = (
        await board.create_task(
            {
                "mode": "ctf",
                "goal": "Correct a harmless missing directory parameter and keep working",
                "budget": {"max_cost": "10", "max_minutes": 5},
                "model_id": configured["model_id"],
                "model_version": configured["model_version"],
            }
        )
    )["id"]
    await board.start_task(tid)
    await ctf.runtime(tid, "start")
    turn = await ctf.runtime(tid, "claim_turn", agent_id="lead", runtime_instance="input-rejection")
    assert turn is not None
    directory = "/workspace/agents/agent-1/not-created"
    marker = directory + "/once.txt"
    command = f"mkdir -p {directory}; echo once >> {marker}; cat {marker}"
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(ScriptToolCall("execute_command", {"command": command, "cwd": directory}),)
            ),
            ScriptStep(
                calls=(ScriptToolCall("execute_command", {"command": command}),),
                expect_contains="invalid_cwd",
            ),
            ScriptStep(text="Completed after input correction", expect_contains="once"),
        ]
    )
    with running_container(tid) as (_, url, _):
        envd = EnvdClient(url, TOKEN)
        adapter = CtfExecutionAdapter(ctf, envd)
        runner = CtfRunner(
            ctf,
            cast(Any, SimpleNamespace()),
            envd=adapter,
            client_factory=lambda *_: client,
            objects=app.state.objects,
        )
        try:
            await runner.run(tid, {"id": "lead", "role": "lead", "display_name": "Lead"}, turn)
            state = await ctf.state(tid)
            saved_turn = next(t for t in state["turns"] if t["id"] == turn["id"])
            assert saved_turn["end_reason"] == "completed"
            assert state["task"]["status"] == "running"
            assert state["task"]["ctf_control"]["phase"] == "running"
            checkpoint = await ctf.get_agent_session(tid, "lead")
            entries = checkpoint["session"]["state"]["ctf_commands"].values()
            assert len(entries) == 2 and all(item["state"] == "finished" for item in entries)
            assert sum('"invalid_cwd"' in item["result"] for item in entries) == 1
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False
            ) as http:
                assert (
                    await http.get(f"{url}/files", params={"path": marker})
                ).content == b"once\n"
        finally:
            await adapter.close()
