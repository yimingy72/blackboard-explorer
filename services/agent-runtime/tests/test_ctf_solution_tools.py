"""Native MAF tools expose bounded public progress and atomic solution delivery."""

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.tools import build_tools


def delivery() -> dict[str, Any]:
    return {
        "conclusion": "Delivered a reproducible partial result.",
        "approach": "Compare the observed behavior with the documented boundary.",
        "steps": [
            {
                "kind": "attempt",
                "title": "Check the boundary",
                "result": "ruled_out",
                "summary": "The observed output rules out the initial assumption.",
                "record_ids": [str(uuid4())],
                "artifact_ids": [str(uuid4())],
            }
        ],
        "remaining": ["An independent input is still needed."],
        "reproduction": "Use the registered script with the recorded input.",
    }


def native_tools(service):
    return {item.name: item for item in build_tools(service, "task", {"token": "token"}, "lead")}


@pytest.mark.parametrize("explicit_none", [False, True])
@pytest.mark.parametrize("name", ["append_record", "update_challenge", "record_candidate"])
async def test_absent_delivery_metadata_preserves_legacy_payload(name, explicit_none):
    service = AsyncMock()
    service.tool.return_value = {"id": "record"}
    arguments: dict[str, Any] = {"challenge_id": "challenge"}
    if name == "append_record":
        arguments["body"] = "Observed a boundary."
        field = "progress"
    else:
        arguments.update(expected_revision=2, work_status="in_progress")
        if name == "update_challenge":
            arguments["action"] = "set_status"
        else:
            arguments.update(status="unknown", summary="Needs another input.")
        field = "solution"
    if explicit_none:
        arguments[field] = None

    await native_tools(service)[name].invoke(arguments=arguments)

    service.tool.assert_awaited_once()
    assert field not in service.tool.call_args.kwargs


async def test_progress_is_serialized_with_its_record_and_existing_artifact():
    service = AsyncMock()
    service.tool.return_value = {"id": "record"}
    progress = {"stage": "attempt", "title": "Compare inputs", "summary": "No difference observed."}
    artifact_id = str(uuid4())

    await native_tools(service)["append_record"].invoke(
        arguments={
            "challenge_id": "challenge",
            "body": "The raw comparison is in the registered artifact.",
            "artifact_ids": [artifact_id],
            "progress": progress,
        }
    )

    service.tool.assert_awaited_once()
    assert service.tool.call_args.kwargs["progress"] == progress
    assert service.tool.call_args.kwargs["artifact_ids"] == [artifact_id]


@pytest.mark.parametrize("name", ["update_challenge", "record_candidate"])
async def test_completed_delivery_forwards_solution_in_the_same_call(name):
    service = AsyncMock()
    service.tool.return_value = {"revision": 3}
    solution = delivery()
    arguments = {
        "challenge_id": "challenge",
        "expected_revision": 2,
        "work_status": "completed",
        "solution": solution,
    }
    if name == "update_challenge":
        arguments["action"] = "set_status"
    else:
        arguments.update(
            status="unknown",
            summary="Partial result delivered.",
            evidence_refs=solution["steps"][0]["artifact_ids"],
        )

    await native_tools(service)[name].invoke(arguments=arguments)

    service.tool.assert_awaited_once()
    assert service.tool.call_args.kwargs["solution"] == solution
    assert service.tool.call_args.kwargs["work_status"] == "completed"
    assert service.tool.call_args.kwargs["expected_revision"] == 2
    if name == "record_candidate":
        assert service.tool.call_args.kwargs["evidence_refs"] == arguments["evidence_refs"]
        assert service.tool.call_args.kwargs["status"] == "unknown"


def test_native_schemas_expose_progress_and_solution_constraints():
    tools = native_tools(AsyncMock())
    progress_schema = tools["append_record"].parameters()
    progress = progress_schema["$defs"]["CtfProgress"]["properties"]
    assert progress["stage"]["enum"] == ["approach", "attempt", "breakthrough", "result"]
    assert progress["title"]["maxLength"] == 40
    assert progress["summary"]["maxLength"] == 160
    for name in ("update_challenge", "record_candidate"):
        schema = tools[name].parameters()
        assert "1600" in schema["properties"]["solution"]["description"]
        solution = schema["$defs"]["CtfSolution"]["properties"]
        assert solution["conclusion"]["maxLength"] == 200
        assert solution["approach"]["maxLength"] == 200
        assert solution["steps"]["minItems"] == 1
        assert solution["steps"]["maxItems"] == 6
        assert solution["remaining"]["maxItems"] == 3
        assert solution["remaining"]["items"]["maxLength"] == 200
        assert solution["reproduction"]["maxLength"] == 300
        step = schema["$defs"]["CtfSolutionStep"]["properties"]
        assert step["kind"]["enum"] == progress["stage"]["enum"]
        assert step["result"]["enum"] == ["supported", "ruled_out", "inconclusive"]
        assert step["title"]["maxLength"] == 40
        assert step["summary"]["maxLength"] == 160
        for field in ("record_ids", "artifact_ids"):
            assert step[field]["items"]["format"] == "uuid"
        assert "completed requires solution" in tools[name].description
        assert "actual author and source" in tools[name].description
        assert "budget/error closure needs no solution" in tools[name].description


@pytest.mark.parametrize(
    "patch",
    [
        {"conclusion": " "},
        {"steps": []},
        {"remaining": ["open"] * 4},
        {"reproduction": "x" * 301},
        {
            "conclusion": "x" * 200,
            "approach": "x" * 200,
            "reproduction": "x" * 300,
            "remaining": ["x" * 200] * 3,
            "steps": [
                {
                    "kind": "result",
                    "title": "x" * 40,
                    "result": "inconclusive",
                    "summary": "x" * 160,
                }
            ]
            * 2,
        },
        {
            "steps": [
                {
                    "kind": "result",
                    "title": "Observed",
                    "result": "supported",
                    "summary": "Result",
                    "record_ids": ["/workspace/result"],
                }
            ]
        },
    ],
)
@pytest.mark.parametrize("name", ["update_challenge", "record_candidate"])
async def test_invalid_solution_never_reaches_the_service(name, patch):
    service = AsyncMock()
    solution = delivery()
    solution.update(patch)
    arguments = {
        "challenge_id": "challenge",
        "expected_revision": 2,
        "work_status": "completed",
        "solution": solution,
    }
    if name == "update_challenge":
        arguments["action"] = "set_status"
    else:
        arguments.update(status="unknown", summary="Partial result.")

    with pytest.raises(TypeError, match="Invalid arguments"):
        await native_tools(service)[name].invoke(arguments=arguments)

    service.tool.assert_not_awaited()


@pytest.mark.parametrize(
    "progress",
    [
        {"stage": "success", "title": "Observed", "summary": "Result"},
        {"stage": "attempt", "title": "x" * 41, "summary": "Result"},
        {"stage": "attempt", "title": "Observed", "summary": "x" * 161},
    ],
)
async def test_invalid_progress_never_reaches_the_service(progress):
    service = AsyncMock()
    with pytest.raises(TypeError, match="Invalid arguments"):
        await native_tools(service)["append_record"].invoke(
            arguments={"challenge_id": "challenge", "body": "Observation", "progress": progress}
        )
    service.tool.assert_not_awaited()


@pytest.mark.parametrize("name", ["update_challenge", "record_candidate"])
async def test_delivery_error_is_safe_and_actionable_without_automatic_replay(name):
    writes = []

    def respond(request):
        writes.append(request)
        return httpx.Response(
            422,
            json={
                "detail": {
                    "code": "invalid_solution_delivery",
                    "message": "private-server-sentinel",
                    "private": "private-detail-sentinel",
                }
            },
        )

    arguments = {"challenge_id": "challenge", "expected_revision": 2, "work_status": "completed"}
    if name == "update_challenge":
        arguments["action"] = "set_status"
    else:
        arguments.update(status="unknown", summary="Partial result.")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as http:
        service = CtfClient(BlackboardClient("http://board.test", "token", http))
        result = await native_tools(service)[name].invoke(arguments=arguments)

    assert len(writes) == 1
    feedback = result[0].text
    assert "invalid_solution_delivery" in feedback
    assert "completed requires solution in the same call" in feedback
    assert "conclusion, approach, 1-6 steps and reproduction" in feedback
    assert "get_challenge" in feedback
    assert "sentinel" not in feedback


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (422, "unknown_delivery_error"),
        (403, "invalid_solution_delivery"),
        (409, "invalid_solution_delivery"),
    ],
)
async def test_other_delivery_errors_keep_the_original_exception(status, code):
    service = AsyncMock()
    error = RemoteError(status, "private-server-sentinel", code)
    service.tool.side_effect = error
    with pytest.raises(RemoteError) as caught:
        await native_tools(service)["update_challenge"].invoke(
            arguments={
                "challenge_id": "challenge",
                "expected_revision": 2,
                "action": "set_status",
                "work_status": "completed",
            }
        )
    assert caught.value is error
    service.tool.assert_awaited_once()
