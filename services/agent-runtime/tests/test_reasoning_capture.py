"""Native Pi reasoning stays separate from visible assistant text."""

import json
from pathlib import Path

import httpx2
import pytest
from bbx_runtime.models import load_runtime_profile
from bbx_runtime.pi import Message
from bbx_runtime.pi.data import pi_messages
from bbx_runtime.testing.pi_client import make_test_client as make_client
from bbx_runtime.trace import model_content
from test_runtime_streaming import chat_chunk, sse


@pytest.mark.parametrize("tool_only", [False, True])
async def test_native_reasoning_is_observable_and_remains_a_reasoning_block(tool_only):
    model = load_runtime_profile(
        Path(__file__).resolve().parents[3] / "profiles/default"
    ).models.explore
    client = make_client(
        model,
        api_key="test-only",
        explore_max_steps=2,
        conclude_grace_calls=0,
        max_duration_seconds=30,
    )

    def respond(_request):
        delta = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": '{"q":"date"}'},
                    }
                ]
            }
            if tool_only
            else {"content": "Visible answer"}
        )
        return sse(
            chat_chunk(1, {"reasoning_content": "Provider-returned reasoning"}),
            chat_chunk(1, delta),
            chat_chunk(
                1,
                {},
                "tool_calls" if tool_only else "stop",
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            ),
        )

    client.request_transport = httpx2.MockTransport(respond)
    try:
        response = await client.get_response([Message("user", ["Read"])])
        text, reasoning = model_content(response)
        assert text == ("调用工具：search" if tool_only else "Visible answer")
        assert reasoning == "Provider-returned reasoning"
        native = pi_messages(response.messages)[0]
        assert any(
            block["type"] == "thinking" and block["thinking"] == reasoning
            for block in native["content"]
        )
        assert all(reasoning not in block.get("text", "") for block in native["content"])
        assert "Provider-returned reasoning" in json.dumps(response.messages[0].to_dict())
    finally:
        await client.close()
