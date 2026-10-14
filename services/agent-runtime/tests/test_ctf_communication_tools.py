"""Native MAF executes atomic dispatch and readback without another provider loop."""

import json
from types import SimpleNamespace
from typing import Any, cast

from agent_framework import Agent
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.tools import build_tools
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall


async def test_native_maf_passes_atomic_assignment_as_validated_instructions():
    calls = []

    async def request(task_id, token, operation, **body):
        calls.append((operation, body))
        assert operation == "update_challenge"
        assert body["instructions"] == [{"recipient_id": "member-1", "body": "Read the note"}]
        return {"id": "challenge-1", "owner_id": "member-1", "revision": 2}

    service = cast(CtfClient, SimpleNamespace(tool=request))
    tools = build_tools(service, "task", {"token": "test"}, "lead")
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "update_challenge",
                        {
                            "challenge_id": "challenge-1",
                            "expected_revision": 1,
                            "action": "assign",
                            "owner_id": "member-1",
                            "instructions": [{"recipient_id": "member-1", "body": "Read the note"}],
                        },
                    ),
                )
            ),
            ScriptStep(text="Assigned", expect_contains='"revision": 2'),
        ]
    )
    async with Agent(client=client, tools=tools) as agent:
        result = await agent.run("Assign the note")
    assert result.text == "Assigned"
    assert len(calls) == 1
    assert not any(name == "post_message" for name, _ in calls)


async def test_native_maf_reads_truncated_result_instead_of_executing_it_again():
    calls: list[tuple[str, dict[str, Any]]] = []

    async def request(task_id, token, operation, **body):
        calls.append((operation, body))
        assert operation == "read_saved_tool_result"
        return {"call_id": "original-call", "result": "Saved middle", "next_offset": None}

    service = cast(CtfClient, SimpleNamespace(tool=request))
    tools = build_tools(service, "task", {"token": "test"}, "teammate")
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(ScriptToolCall("read_saved_tool_result", {"call_id": "original-call"}),)
            ),
            ScriptStep(text="Read saved evidence", expect_contains="Saved middle"),
        ]
    )
    async with Agent(client=client, tools=tools) as agent:
        await agent.run("Read the omitted saved result")
    assert len(calls) == 1
    assert calls[0][1] == {"call_id": "original-call", "offset": 0, "limit": 2000}
    assert "execute_command" not in json.dumps(calls)
