"""Bounded board data survives the native model view without changing frozen history."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from agent_framework import Agent, Content, Message
from bbx_contracts.ctf import CTF_BOARD_RESULT_MAX_BYTES, load_ctf_profile
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.context import stable_tool_previews
from bbx_runtime.ctf.middleware import TurnBoundary, mailbox_message
from bbx_runtime.ctf.session import load_checkpoint
from bbx_runtime.ctf.tool_results import recoverable_tool_error
from bbx_runtime.ctf.tools import build_tools
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
from test_ctf_context import provider
from test_ctf_provider_boundary import DispatchService, native
from test_ctf_runtime import TURN


def board_messages(payload, name="list_challenges"):
    return [
        Message(
            role="assistant",
            message_id="call-message",
            contents=[Content.from_function_call("board-call", name, arguments={})],
        ),
        Message(
            role="tool",
            message_id="result-message",
            contents=[
                Content.from_function_result(
                    "board-call", result=json.dumps(payload, ensure_ascii=False)
                )
            ],
        ),
    ]


def summary_page(start=0, count=6, total=63):
    end = min(start + count, total)
    return {
        "view": "summary",
        "challenges": [
            {
                "id": str(UUID(int=index + 1)),
                "title": "独立目标" * 40,
                "external_id": str(index),
                "owner_id": "member-1",
                "work_status": "in_progress",
                "revision": index + 1,
                "verification": {"status": "unverified"},
            }
            for index in range(start, end)
        ],
        "next_offset": end if end < total else None,
        "next_cursor": f"cursor-{end}" if end < total else None,
    }


def test_all_63_unicode_index_entries_survive_history_provider_and_cursor_pages():
    seen = []
    for start in range(0, 63, 6):
        page = summary_page(start)
        messages = board_messages(page)
        assert len(str(messages[-1].contents[0].result).encode()) < 6000
        history = provider(messages, threshold=20000)
        view = history.prepare_provider_messages(messages)
        result = json.loads(view[-1].contents[0].result)
        assert result == page
        seen.extend((row["id"], row["revision"]) for row in result["challenges"])
        assert (
            history.checkpoint.session.state["ctf_model_view"]["truncated_tool_result_count"] == 0
        )
    assert seen == [(str(UUID(int=index + 1)), index + 1) for index in range(63)]


def test_small_window_blocks_complete_index_without_corrupting_json():
    page = summary_page()
    messages = board_messages(page)
    history = provider([], threshold=500)
    with pytest.raises(TurnBoundary, match="context_blocked"):
        history.prepare_provider_messages(messages)
    assert list(history.checkpoint.session.state["ctf_tool_previews"].values()) == [None]
    assert json.loads(messages[-1].contents[0].result) == page


@pytest.mark.parametrize("name", ["execute_command", "platform_custom_status", "get_challenge"])
def test_other_tool_cannot_claim_complete_index_exception(name):
    messages = board_messages(summary_page(), name=name)
    view = stable_tool_previews(messages, {}, 2000)
    assert view[-1].contents[0].result.startswith("[Truncated model preview:")


def test_oversized_or_unpaired_summary_does_not_bypass_preview_bound():
    page = summary_page()
    page["extra"] = "x" * CTF_BOARD_RESULT_MAX_BYTES
    messages = board_messages(page)
    assert (
        stable_tool_previews(messages, {}, 2000)[-1]
        .contents[0]
        .result.startswith("[Truncated model preview:")
    )
    unpaired = board_messages(summary_page())[-1:]
    assert (
        stable_tool_previews(unpaired, {}, 2000)[-1]
        .contents[0]
        .result.startswith("[Truncated model preview:")
    )


def test_existing_preview_stays_identical_even_for_new_index_shape():
    messages = board_messages(summary_page())
    state = {"ctf_tool_previews": {"result-message:board-call:0": "saved old preview"}}
    assert (
        stable_tool_previews(messages, state, 20000)[-1].contents[0].result == "saved old preview"
    )
    assert state["ctf_tool_previews"] == {"result-message:board-call:0": "saved old preview"}


@pytest.mark.parametrize("name", ["create_challenges", "assign_challenges"])
def test_every_batch_receipt_remains_complete(name):
    payload = {
        "results": [
            {
                "request_id": str(UUID(int=index + 1)),
                "status": "ok" if index % 2 else "error",
                **(
                    {"challenge": {"id": str(UUID(int=index + 30)), "revision": 2}}
                    if index % 2
                    else {"error": {"status_code": 409, "detail": "界" * 133}}
                ),
            }
            for index in range(20)
        ]
    }
    messages = board_messages(payload, name=name)
    view = stable_tool_previews(messages, {}, 1000)
    assert json.loads(view[-1].contents[0].result) == payload


async def test_native_board_tool_result_reaches_next_provider_input_as_complete_json():
    page = summary_page(count=2)

    class Service(DispatchService):
        async def tool(self, task_id, token, operation, **body):
            assert operation == "list_challenges" and body["view"] == "summary"
            return page

    service = Service()
    checkpoint = await load_checkpoint(cast(Any, service), "task", "member-1", TURN, "role")
    built = build_tools(cast(Any, service), "task", {**TURN, "token": "t"}, "teammate")
    listing = next(item for item in built if item.name == "list_challenges")
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("list_challenges", {"limit": 2}),)),
            ScriptStep(text="Read the exact IDs"),
        ]
    )
    await native(service, checkpoint, client, tools=[listing])
    received = [
        content.result
        for message in client.received_messages[-1]
        for content in message.contents
        if content.type == "function_result"
    ]
    assert len(received) == 1 and json.loads(received[0]) == page
    restored = checkpoint.session.to_dict()
    assert "cursor-2" in json.dumps(restored)


async def test_native_tools_preserve_filters_and_stable_batch_request_ids_on_retry():
    calls = []

    async def request(task_id, token, operation, **body):
        calls.append((operation, copy.deepcopy(body)))
        if operation == "list_challenges":
            return summary_page(count=1)
        return {"results": [{"request_id": body["items"][0]["request_id"], "status": "ok"}]}

    tools = build_tools(cast(Any, SimpleNamespace(tool=request)), "task", {"token": "t"}, "lead")
    create = {"request_id": str(UUID(int=91)), "title": "Independent objective"}
    assign = {
        "request_id": str(UUID(int=92)),
        "challenge_id": str(UUID(int=1)),
        "expected_revision": 1,
        "owner_id": "member-1",
        "instructions": [{"recipient_id": "member-1", "body": "Read this objective"}],
    }
    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "list_challenges",
                        {"owner_id": "member-1", "work_status": "in_progress", "cursor": "c"},
                    ),
                )
            ),
            *[
                ScriptStep(calls=(ScriptToolCall(name, {"items": [item]}),))
                for name, item in [
                    ("create_challenges", create),
                    ("create_challenges", create),
                    ("assign_challenges", assign),
                    ("assign_challenges", assign),
                ]
            ],
            ScriptStep(text="Dispatched"),
        ]
    )
    async with Agent(client=client, tools=tools) as agent:
        await agent.run("Register and assign")
    assert calls[0] == (
        "list_challenges",
        {
            "view": "summary",
            "offset": 0,
            "limit": 100,
            "owner_id": "member-1",
            "work_status": "in_progress",
            "cursor": "c",
            "max_bytes": 6000,
        },
    )
    assert calls[1] == calls[2] and calls[3] == calls[4]
    assert calls[1][1]["items"][0]["request_id"] == create["request_id"]
    assert calls[3][1]["items"][0]["request_id"] == assign["request_id"]


def test_frozen_tool_allowlist_is_not_expanded_and_new_profile_matches_actual_tools():
    profile = load_ctf_profile(Path("profiles/ctf"))
    for role in ("lead", "teammate"):
        built = build_tools(cast(Any, SimpleNamespace()), "task", {"token": "t"}, role)
        names = {item.name for item in built}
        expected = set(profile.worker_tools[role].builtin) - {"execute_command", "view_image"}
        assert expected == names
        legacy_allowed = expected - {
            "create_challenges",
            "assign_challenges",
            "read_saved_tool_result",
        }
        assembled = [item for item in built if item.name in legacy_allowed]
        assert {item.name for item in assembled} == legacy_allowed
        for item in built:
            if item.name in {"create_challenges", "assign_challenges"}:
                schema = item.parameters()
                assert schema["properties"]["items"]["maxItems"] == 20
                assert "request_id" in json.dumps(schema)


async def test_explicit_challenge_reference_reaches_message_source_and_legacy_null():
    calls = []

    async def request(task_id, token, operation, **body):
        calls.append((operation, body))
        return body

    built = build_tools(cast(Any, SimpleNamespace(tool=request)), "task", {"token": "t"}, "lead")
    send = next(item for item in built if item.name == "send_message")
    await send.invoke(
        arguments={"recipient_id": "member-1", "body": "Review", "challenge_id": str(UUID(int=1))}
    )
    assert calls[0][1]["challenge_id"] == str(UUID(int=1))
    row = {"id": str(UUID(int=2)), "body": "Review", "challenge_id": str(UUID(int=1))}
    assert mailbox_message(row).additional_properties["ctf_source"]["challenge_id"] == str(
        UUID(int=1)
    )
    del row["challenge_id"]
    assert mailbox_message(row).additional_properties["ctf_source"]["challenge_id"] is None


@pytest.mark.parametrize("message", ["Invalid challenge pagination", "Invalid challenge cursor"])
def test_invalid_index_arguments_are_model_feedback(message):
    feedback = recoverable_tool_error(RemoteError(422, message))
    assert feedback is not None
    assert isinstance(feedback.text, str)
    assert json.loads(feedback.text)["error"]["code"] == "invalid_arguments"


def test_too_small_index_feedback_does_not_echo_unrecognized_server_fields():
    feedback = recoverable_tool_error(
        RemoteError(
            422,
            "Page too small",
            "challenge_page_too_small",
            detail={"minimum_bytes": 1600, "challenge_id": str(UUID(int=1)), "private": "sentinel"},
        )
    )
    assert feedback is not None
    assert isinstance(feedback.text, str)
    error = json.loads(feedback.text)["error"]
    assert error["minimum_bytes"] == 1600 and error["challenge_id"] == str(UUID(int=1))
    assert "sentinel" not in feedback.text
