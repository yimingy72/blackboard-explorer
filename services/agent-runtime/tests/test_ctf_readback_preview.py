"""Default readback pages fit the maximum tool-preview allowance, not every window."""

import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from agent_framework import Content, Message
from bbx_contracts.ctf import (
    CtfDeferredMessagesReadRequest,
    CtfSavedToolResultReadRequest,
    CtfTurnResultReadRequest,
)
from bbx_runtime.ctf.context import estimate_text_tokens, stable_tool_previews
from bbx_runtime.ctf.tools import build_tools


def test_five_deferred_previews_remain_complete_at_maximum_preview_allowance():
    page_size = CtfDeferredMessagesReadRequest().limit
    assert page_size == 5
    result = json.dumps(
        {
            "messages": [
                {
                    "id": str(UUID(int=index + 1)),
                    "recipient_id": "member-8",
                    "sender_id": "lead",
                    "sender_kind": "agent",
                    "kind": "instruction",
                    "created_at": "2026-10-14T01:00:00.000000+00:00",
                    "source_turn_id": str(UUID(int=100 + index)),
                    "notification_purpose": "assignment_result",
                    "status": "queued",
                    "deferred": True,
                    "body_preview": "汉" * 240,
                }
                for index in range(page_size)
            ],
            "next_offset": page_size,
        },
        ensure_ascii=False,
    )
    assert estimate_text_tokens(result) <= 8192
    message = Message(
        role="tool",
        message_id="deferred-readback",
        contents=[Content.from_function_result("readback-call", result=result)],
    )
    state: dict[str, Any] = {}
    view = stable_tool_previews([message], state, 8192 * 8)
    assert view[0].contents[0].result == result
    assert list(state["ctf_tool_previews"].values()) == [None]


def test_readback_contract_defaults_match_native_maf_tool_schemas():
    tools = {
        tool.name: tool
        for tool in build_tools(cast(Any, SimpleNamespace()), "task", {"token": "test"}, "lead")
    }
    requests = [
        ("read_turn_result", CtfTurnResultReadRequest(turn_id=UUID(int=1)), 2000),
        ("read_saved_tool_result", CtfSavedToolResultReadRequest(call_id="original-call"), 2000),
        ("list_deferred_messages", CtfDeferredMessagesReadRequest(), 5),
    ]
    for name, request, expected_limit in requests:
        parameters = tools[name].parameters()["properties"]
        assert request.limit == parameters["limit"]["default"] == expected_limit
        assert request.offset == parameters["offset"]["default"] == 0
