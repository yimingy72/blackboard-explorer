"""Exercise JSON transport, field feedback and persisted status through MAF's real loop."""

import copy
import json
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from agent_framework import Agent, FunctionInvocationContext, tool
from bbx_contracts.ctf import CtfSolution
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.failures import CtfToolFailure, propagate_tool_failure
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.ctf.telemetry import ToolArgumentFeedbackMiddleware, ToolObservationMiddleware
from bbx_runtime.ctf.tools import build_tools
from bbx_runtime.session import CheckpointHistoryProvider, ToolResultCheckpointMiddleware
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_runtime import TURN, MemoryCtf
from test_ctf_solution_tools import delivery


class WireService(MemoryCtf):
    def __init__(self, rpc):
        super().__init__()
        self.rpc = rpc
        self.snapshots = []

    async def tool(self, *args, **kwargs):
        return await self.rpc.tool(*args, **kwargs)

    async def runtime(self, task_id, operation, **body):
        if operation == "record_observation":
            await self.rpc.runtime(task_id, operation, **body)
        result = await super().runtime(task_id, operation, **body)
        if operation == "checkpoint":
            self.snapshots.append(copy.deepcopy(self.saved))
        return result

    def saved_result(self, call_id):
        return next(
            saved["session"]["state"]["bbx_tool_results"][call_id]
            for saved in self.snapshots
            if call_id in saved["session"]["state"].get("bbx_tool_results", {})
        )


async def run_native(service, steps):
    turn = {**TURN, "token": "synthetic-member-token"}
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", turn, "Work")
    client = ScriptedChatClient(steps)
    async with Agent(
        client=client,
        tools=build_tools(cast(Any, service), "task", turn, "lead"),
        context_providers=[CheckpointHistoryProvider(checkpoint)],
        middleware=[
            ToolObservationMiddleware(cast(Any, service), "task", "member-1", turn),
            ToolResultCheckpointMiddleware(checkpoint),
            ToolArgumentFeedbackMiddleware(),
        ],
        require_per_service_call_history_persistence=True,
    ) as agent:
        with propagate_tool_failure():
            response = await agent.run("Synthetic delivery", session=checkpoint.session)
    return client, checkpoint, response


@pytest.mark.parametrize("name", ["update_challenge", "record_candidate"])
async def test_uuid_references_reach_board_with_native_observation_and_checkpoint(name):
    calls = []
    observations = []

    def respond(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/record_observation"):
            observations.append(payload)
            return httpx.Response(200, json={})
        calls.append(payload)
        return httpx.Response(200, json={"revision": 2})

    solution = delivery()
    arguments = dict(
        challenge_id="challenge", expected_revision=1, work_status="completed", solution=solution
    )
    arguments.update(
        dict(action="set_status")
        if name == "update_challenge"
        else dict(status="unknown", summary="Reproducible delivery")
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        service = WireService(CtfClient(BlackboardClient("http://board", "synthetic", http)))
        _, checkpoint, response = await run_native(
            service,
            [
                ScriptStep(calls=(ScriptToolCall(name, arguments, call_id="delivery"),)),
                ScriptStep(text="Delivered", expect_contains='"revision": 2'),
            ],
        )
    assert response.text == "Delivered"
    assert len(calls) == 1 and calls[0]["solution"] == solution
    assert [row["kind"] for row in observations] == ["tool_call", "tool_result"]
    assert (
        observations[0]["body"]["arguments"]["solution"]["steps"][0]["record_ids"]
        == (solution["steps"][0]["record_ids"])
    )
    assert type(observations[1]["body"]["elapsed_ms"]) is int
    assert service.saved_result("delivery")["items"][0]["text"] == '{"revision": 2}'


async def test_observation_serialization_does_not_change_typed_arguments():
    captured = []

    @tool
    async def typed_reference(solution: CtfSolution, ctx: FunctionInvocationContext) -> str:
        assert isinstance(ctx.arguments, dict)
        captured.append(
            (
                CtfSolution.model_validate(solution).steps[0].record_ids[0],
                ctx.arguments["solution"]["steps"][0]["record_ids"][0],
            )
        )
        return "Observed"

    service = MemoryCtf()
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("typed_reference", {"solution": delivery()}),)),
            ScriptStep(text="Done"),
        ]
    )
    async with Agent(
        client=client,
        tools=[typed_reference],
        middleware=[ToolObservationMiddleware(cast(Any, service), "task", "member-1", TURN)],
    ) as agent:
        await agent.run("Read")
    assert len(captured) == 1 and all(isinstance(item, UUID) for item in captured[0])
    json.dumps(service.observations)


@pytest.mark.parametrize(
    "field,value,issue_type",
    [
        ("record_ids", ["synthetic-private-id"], "uuid_parsing"),
        ("kind", "synthetic-private-kind", "literal_error"),
        ("summary", "synthetic-private-text" * 20, "string_too_long"),
    ],
)
async def test_invalid_native_fields_are_actionable_saved_and_never_dispatched(
    field, value, issue_type
):
    calls = []

    def respond(request):
        if not request.url.path.endswith("/record_observation"):
            calls.append(json.loads(request.content))
        return httpx.Response(200, json={"revision": 2})

    solution = delivery()
    invalid = copy.deepcopy(solution)
    invalid["steps"][0][field] = value
    arguments = dict(
        challenge_id="challenge",
        expected_revision=1,
        work_status="completed",
        status="unknown",
        summary="Delivery",
        solution=invalid,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        service = WireService(CtfClient(BlackboardClient("http://board", "synthetic", http)))
        client, checkpoint, _ = await run_native(
            service,
            [
                ScriptStep(
                    calls=(ScriptToolCall("record_candidate", arguments, call_id="invalid"),)
                ),
                ScriptStep(
                    calls=(
                        ScriptToolCall("record_candidate", {**arguments, "solution": solution}),
                    ),
                    expect_contains=issue_type,
                ),
                ScriptStep(text="Delivered", expect_contains='"revision": 2'),
            ],
        )
    assert len(calls) == 1
    item = service.saved_result("invalid")["items"][0]
    feedback = json.loads(item["text"])["error"]
    assert feedback["code"] == "invalid_arguments"
    assert feedback["fields"][0]["path"][:3] == ["solution", "steps", 0]
    assert "synthetic-private" not in item["text"]
    assert item["additional_properties"]["ctf_tool_error"] == "invalid_arguments"
    assert all(options.get("tool_choice") != "none" for options in client.received_options)


async def test_domain_refusal_is_visible_to_model_observations_and_saved_conversation():
    calls = []

    def respond(request):
        if request.url.path.endswith("/record_observation"):
            return httpx.Response(200, json={})
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if calls[-1] == "update_challenge":
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "code": "challenge_reopen_required",
                        "message": "synthetic-private-diagnostic",
                    }
                },
            )
        return httpx.Response(200, json={"revision": 3, "work_status": "completed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        service = WireService(CtfClient(BlackboardClient("http://board", "synthetic", http)))
        _, checkpoint, _ = await run_native(
            service,
            [
                ScriptStep(
                    calls=(
                        ScriptToolCall(
                            "update_challenge",
                            {
                                "challenge_id": "challenge",
                                "expected_revision": 2,
                                "action": "assign",
                                "owner_id": "member-1",
                            },
                            call_id="refused",
                        ),
                    )
                ),
                ScriptStep(
                    calls=(ScriptToolCall("get_challenge", {"challenge_id": "challenge"}),),
                    expect_contains="challenge_reopen_required",
                ),
                ScriptStep(text="Already delivered"),
            ],
        )
    assert calls == ["update_challenge", "get_challenge"]
    saved = service.saved_result("refused")
    assert saved["items"][0]["additional_properties"]["ctf_tool_error"] == (
        "challenge_reopen_required"
    )
    assert "synthetic-private" not in json.dumps(saved)
    result = next(row for row in service.observations if row["kind"] == "tool_result")
    assert result["body"]["tool_error_code"] == "challenge_reopen_required"


@pytest.mark.parametrize("stage", ["tool_call", "tool_result", "checkpoint", "tool"])
async def test_native_control_failure_never_replays_a_tool(stage):
    effects = []
    original = httpx.ReadError("synthetic private disconnect")

    class FailingService(MemoryCtf):
        async def runtime(self, task_id, operation, **body):
            if operation == "record_observation" and body["kind"] == stage:
                raise original
            if operation == stage and effects:
                raise original
            return await super().runtime(task_id, operation, **body)

        async def tool(self, *args, **kwargs):
            effects.append("effect")
            if stage == "tool":
                raise original
            return {"revision": 2}

    service = FailingService()
    arguments = {"challenge_id": "challenge", "expected_revision": 1, "action": "claim"}
    with pytest.raises(httpx.ReadError) as caught:
        await run_native(
            service,
            [
                ScriptStep(calls=(ScriptToolCall("update_challenge", arguments),)),
                ScriptStep(calls=(ScriptToolCall("update_challenge", arguments),)),
                ScriptStep(text="Must not repeat"),
            ],
        )
    assert caught.value is original
    assert effects == ([] if stage == "tool_call" else ["effect"])


@pytest.mark.parametrize("status", [401, 403, 409, 422, 503])
async def test_unrecognized_control_errors_use_public_maf_failure_boundary(status):
    class FailingService(MemoryCtf):
        async def tool(self, *args, **kwargs):
            raise RemoteError(status, "synthetic-private-response", "generation_revoked")

    service = FailingService()
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("get_challenge", {"challenge_id": "challenge"}),)),
            ScriptStep(text="Must not continue"),
        ]
    )
    async with Agent(
        client=client,
        tools=build_tools(cast(Any, service), "task", {"token": "t"}, "lead"),
        middleware=[ToolObservationMiddleware(cast(Any, service), "task", "member-1", TURN)],
    ) as agent:
        with pytest.raises(CtfToolFailure) as caught:
            await agent.run("Read")
    assert isinstance(caught.value.original_error, RemoteError)
    assert caught.value.original_error.status == status
    assert len(client.received_messages) == 1
    assert "synthetic-private" not in str(caught.value)
