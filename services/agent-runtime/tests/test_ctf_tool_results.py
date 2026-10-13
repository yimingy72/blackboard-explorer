"""Offline model-visible board feedback, safe errors and checkpoint recovery."""

import copy
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from agent_framework import Agent
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.review import build_review_tools
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.ctf.telemetry import ToolObservationMiddleware
from bbx_runtime.ctf.tool_results import json_tool_result
from bbx_runtime.ctf.tools import build_tools
from bbx_runtime.session import CheckpointHistoryProvider, ToolResultCheckpointMiddleware
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_runtime import TURN, MemoryCtf


async def invoke(item, **arguments):
    result = await item.invoke(arguments=arguments)
    return result[0].text


async def test_native_maf_conflict_refresh_update_and_recover_saved_feedback():
    writes = []

    def respond(request):
        operation = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if operation == "update_challenge":
            writes.append(body)
            if body["expected_revision"] == 1:
                return httpx.Response(
                    409,
                    json={
                        "detail": {
                            "code": "revision_conflict",
                            "current_revision": 2,
                            "private": "secret-sentinel",
                        }
                    },
                )
            assert body["expected_revision"] == 2
            return httpx.Response(200, json={"revision": 3, "owner_id": "member-1"})
        assert operation == "get_challenge"
        return httpx.Response(200, json={"revision": 2, "owner_id": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        rpc = CtfClient(BlackboardClient("http://board", "service-token", http))

        class Service(MemoryCtf):
            def __init__(self):
                super().__init__()
                self.snapshots = []

            async def tool(self, *args, **kwargs):
                return await rpc.tool(*args, **kwargs)

            async def runtime(self, task_id, operation, **body):
                result = await super().runtime(task_id, operation, **body)
                if operation == "checkpoint":
                    self.snapshots.append(copy.deepcopy(self.saved))
                return result

        service = Service()
        turn = {**TURN, "token": "member-token"}
        checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", turn, "Work")
        client = ScriptedChatClient(
            [
                ScriptStep(
                    calls=(
                        ScriptToolCall(
                            "update_challenge",
                            {
                                "challenge_id": "challenge",
                                "expected_revision": 1,
                                "action": "claim",
                            },
                            call_id="stale-update",
                        ),
                    )
                ),
                ScriptStep(
                    calls=(ScriptToolCall("get_challenge", {"challenge_id": "challenge"}),),
                    expect_contains='"current_revision": 2',
                ),
                ScriptStep(
                    calls=(
                        ScriptToolCall(
                            "update_challenge",
                            {
                                "challenge_id": "challenge",
                                "expected_revision": 2,
                                "action": "claim",
                            },
                        ),
                    ),
                    expect_contains='"owner_id": null',
                ),
                ScriptStep(text="claimed", expect_contains='"revision": 3'),
            ]
        )
        async with Agent(
            client=client,
            tools=build_tools(cast(Any, service), "task", turn, "teammate"),
            context_providers=[CheckpointHistoryProvider(checkpoint)],
            middleware=[
                ToolObservationMiddleware(cast(Any, service), "task", "member-1", turn),
                ToolResultCheckpointMiddleware(checkpoint),
            ],
            require_per_service_call_history_persistence=True,
        ) as agent:
            assert (await agent.run("claim", session=checkpoint.session)).text == "claimed"
        assert [body["expected_revision"] for body in writes] == [1, 2]
        assert any(
            "revision_conflict" in row["body"].get("result", "") for row in service.observations
        ), service.observations
        # Restore the actual snapshot saved between tool completion and the next model call.
        service.saved = next(
            saved
            for saved in service.snapshots
            if "stale-update" in saved["session"]["state"].get("bbx_tool_results", {})
        )
        restored = await load_checkpoint(cast(Any, service), "task", "member-1", turn, "Work")
        feedback = next(
            content.result
            for message in restored.session.state["in_memory"]["messages"]
            for content in message.contents
            if content.type == "function_result" and content.call_id == "stale-update"
        )
        assert '"current_revision": 2' in str(feedback) and "secret-sentinel" not in str(feedback)
        assert restored.session.state["bbx_recovery_repairs"][-1]["kind"] == "saved_tool_result"


@pytest.mark.parametrize(
    "error",
    [
        RemoteError(403, "secret-sentinel"),
        RemoteError(409, "secret-sentinel", "generation_revoked"),
        RemoteError(409, "secret-sentinel", "budget_exhausted"),
        RemoteError(422, "secret-sentinel"),
        RemoteError(503, "secret-sentinel"),
        httpx.ReadError("secret-sentinel"),
    ],
)
async def test_unknown_errors_keep_original_exception_and_are_not_echoed_to_model(error):
    service = AsyncMock()
    service.tool.side_effect = error
    tools = build_tools(service, "task", {"token": "token"}, "teammate")
    with pytest.raises(type(error)) as caught:
        await invoke(next(item for item in tools if item.name == "get_challenge"), challenge_id="x")
    assert caught.value is error
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("get_challenge", {"challenge_id": "x"}),)),
            ScriptStep(text="failed", expect_contains="Error: Function failed."),
        ]
    )
    async with Agent(client=client, tools=tools) as agent:
        await agent.run("read")
    results = [
        content.result
        for message in client.received_messages[-1]
        for content in message.contents
        if content.type == "function_result"
    ]
    assert results == ["Error: Function failed."]


@pytest.mark.parametrize("message", ["Invalid CTF board request", "Invalid after_version"])
async def test_known_argument_error_is_safe_json_without_raw_error_text(message):
    request = AsyncMock(side_effect=RemoteError(422, message))
    result = json.loads(await json_tool_result(request()))
    assert result["error"]["code"] == "invalid_arguments"
    assert message not in result["error"]["message"]


@pytest.mark.parametrize("revision", [2, 0, True, "2", None])
async def test_http_client_preserves_only_valid_cas_revision_detail(revision):
    response = httpx.Response(
        409,
        json={"detail": {"code": "revision_conflict", "current_revision": revision, "secret": "x"}},
    )
    with pytest.raises(RemoteError) as caught:
        await BlackboardClient._check(response)
    error = caught.value
    assert error.status == 409 and error.code == "revision_conflict"
    assert error.message == response.text
    assert error.detail == (
        {"current_revision": 2} if type(revision) is int and revision == 2 else None
    )
    result = json.loads(await json_tool_result(AsyncMock(side_effect=error)()))
    assert result["error"].get("current_revision") == (
        2 if type(revision) is int and revision == 2 else None
    )
    assert "secret" not in json.dumps(result)


async def test_json_artifact_review_pagination_and_incremental_records_keep_data():
    payload = {"text": "中文\n原件", "next_offset": None, "required": True, "offset": 0}
    service = AsyncMock()
    service.tool.return_value = payload
    artifacts = AsyncMock()
    artifacts.register.return_value = {"id": "artifact", "filename": "中文.txt"}
    tools = {
        item.name: item
        for item in build_tools(service, "task", {"token": "t"}, "lead", artifacts=artifacts)
    }
    assert json.loads(await invoke(tools["get_challenge"], challenge_id="c")) == payload
    assert (
        json.loads(await invoke(tools["register_artifact"], path="/workspace/shared/x"))["filename"]
        == "中文.txt"
    )
    await invoke(tools["list_records"], challenge_id="c")
    assert "after_version" not in service.tool.call_args.kwargs
    await invoke(tools["list_records"], challenge_id="c", after_version=0, offset=100, limit=50)
    assert service.tool.call_args.kwargs == {
        "challenge_id": "c",
        "after_version": 0,
        "offset": 100,
        "limit": 50,
    }
    review = {
        item.name: item for item in build_review_tools(service, "task", "member-1", {"token": "t"})
    }
    assert json.loads(await invoke(review["read_artifact"], artifact_id="a")) == payload


async def test_command_output_remains_raw_text():
    output = "raw output\n中文"
    tools = build_tools(
        AsyncMock(),
        "task",
        {**TURN, "token": "t"},
        "teammate",
        member_id="member-1",
        envd=SimpleNamespace(execute=AsyncMock(return_value=output)),
    )
    execute = next(item for item in tools if item.name == "execute_command")
    assert await invoke(execute, command="inspect") == output


def test_tool_schemas_express_server_constraints_and_action_requirements():
    tools = {item.name: item for item in build_tools(AsyncMock(), "task", {"token": "t"}, "lead")}
    for name in (
        "update_challenge",
        "request_help",
        "record_candidate",
        "set_verification_required",
    ):
        assert tools[name].parameters()["properties"]["expected_revision"]["minimum"] == 1
    statuses = tools["update_challenge"].parameters()["properties"]["work_status"]["anyOf"][0][
        "enum"
    ]
    assert statuses == ["in_progress", "blocked", "completed", "cancelled"]
    assert "assign requires owner_id" in tools["update_challenge"].description
    assert "Only the teammate owner" in tools["request_help"].description
    assert "no_artifacts_reason" in tools["request_help"].description
    assert (
        "Registered artifact IDs only"
        in tools["record_candidate"].parameters()["properties"]["evidence_refs"]["description"]
    )
    for name, field, maximum in (
        ("send_message", "body", 20000),
        ("create_teammate", "display_name", 100),
        ("create_challenge", "title", 200),
    ):
        parameter = tools[name].parameters()["properties"][field]
        assert (parameter["minLength"], parameter["maxLength"]) == (1, maximum)
    cursor = tools["list_records"].parameters()["properties"]["after_version"]
    assert cursor["anyOf"][0]["minimum"] == 0
    assert "Keep after_version fixed" in tools["list_records"].description
    assert "Never advance both cursor and offset" in tools["list_records"].description
