"""Provider reasoning remains observable without changing follow-up model input."""

from pathlib import Path

import pytest
from bbx_runtime.models import load_runtime_profile, make_client
from bbx_runtime.trace import model_content
from openai.types.chat import ChatCompletion


@pytest.mark.parametrize("tool_only", [False, True])
def test_provider_reasoning_is_recorded_but_not_echoed(tool_only):
    profile = load_runtime_profile(Path(__file__).resolve().parents[3] / "profiles/default")
    client = make_client(
        profile.models.explore,
        api_key="test-only-key",
        explore_max_steps=60,
        conclude_grace_calls=3,
        max_duration_seconds=600,
    )
    message = {
        "role": "assistant",
        "content": None if tool_only else "Visible answer",
        "reasoning_content": "Provider-returned reasoning",
    }
    if tool_only:
        message["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": '{"q":"date"}'},
            }
        ]
    raw = ChatCompletion.model_validate(
        {
            "id": "response_1",
            "created": 1,
            "model": "fixture",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if tool_only else "stop",
                    "message": message,
                }
            ],
        }
    )
    parsed = client._parse_response_from_openai(raw, {})
    text, reasoning = model_content(parsed)
    assert reasoning == "Provider-returned reasoning"
    assert text == ("调用工具：search" if tool_only else "Visible answer")
    outgoing = client._prepare_message_for_openai(parsed.messages[0])
    assert "Provider-returned reasoning" not in str(outgoing)
    assert "reasoning_content" not in str(outgoing)
    assert "search" in str(outgoing) if tool_only else outgoing[0]["content"] == "Visible answer"
