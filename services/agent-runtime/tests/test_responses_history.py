"""Lossless platform projections and Pi-compatible historical conversations."""

import json
from pathlib import Path

import pytest
from bbx_runtime.middleware import append_board_update
from bbx_runtime.models import close_model_client, load_runtime_profile, make_client
from bbx_runtime.pi import AgentSession, Content, Message, PiChatClient
from bbx_runtime.pi.data import from_pi_message, pi_messages
from bbx_runtime.responses_history import project_tool_observations

PROFILE = Path(__file__).resolve().parents[3] / "profiles" / "default"


def test_unencrypted_native_pi_reasoning_still_uses_the_gateway_projection():
    assistant = from_pi_message(
        {
            "role": "assistant",
            "api": "openai-responses",
            "provider": "openai",
            "model": "offline",
            "timestamp": 1,
            "stopReason": "toolUse",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "public summary",
                    "thinkingSignature": json.dumps(
                        {
                            "type": "reasoning",
                            "id": "rs_plain",
                            "summary": [{"type": "summary_text", "text": "public summary"}],
                        }
                    ),
                },
                {"type": "toolCall", "id": "call-1", "name": "lookup", "arguments": {}},
            ],
        }
    )
    messages = [
        assistant,
        Message("tool", [Content.from_function_result("call-1", result="saved evidence")]),
    ]
    before = [message.to_dict() for message in messages]
    assert assistant.contents[0].protected_data is None
    assert "thinkingSignature" in pi_messages([assistant])[0]["content"][0]
    projected = project_tool_observations(messages)
    rendered = json.dumps(pi_messages(projected))
    assert "thinkingSignature" not in rendered
    assert "saved evidence" in rendered
    assert [message.to_dict() for message in messages] == before


@pytest.mark.parametrize("reasoning", [None, "", "private summary"])
@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("encrypted", [False, True])
def test_projection_is_request_only_and_requires_complete_group(reasoning, complete, encrypted):
    messages = history(encrypted=encrypted)
    messages[1].contents[0].text = reasoning
    if not complete:
        messages.pop()
    before = [message.to_dict() for message in messages]
    projected = project_tool_observations(messages)
    assert [message.to_dict() for message in messages] == before
    if encrypted or not complete:
        assert projected is messages
    else:
        rendered = json.dumps(pi_messages(projected), ensure_ascii=False)
        assert "full result " + "x" * 5000 in rendered
        assert "call-1" in rendered
        assert "private summary" not in rendered
        assert all(m.role != "tool" for m in projected)


def test_legacy_encrypted_reasoning_translates_to_native_pi_signature():
    messages = history(encrypted=True)
    session = AgentSession()
    session.state["in_memory"] = {"messages": messages}
    before = session.to_dict()
    restored = AgentSession.from_dict(before)
    native = pi_messages(restored.state["in_memory"]["messages"], api="openai-responses")
    thinking = native[1]["content"][0]
    signature = json.loads(thinking["thinkingSignature"])
    assert signature["encrypted_content"] == "encrypted-fixture"
    assert signature["id"] == "rs_fixture"
    assert signature["type"] == "reasoning"
    assert session.to_dict() == before == restored.to_dict()


@pytest.mark.parametrize("problem", ["missing", "duplicate_call", "duplicate_result", "foreign"])
def test_incomplete_or_ambiguous_tool_groups_are_never_rewritten(problem):
    messages = history()
    if problem == "missing":
        messages.pop()
    elif problem == "duplicate_call":
        messages[1].contents.append(messages[1].contents[-1].model_copy(deep=True))
    elif problem == "duplicate_result":
        messages.append(messages[-1].model_copy(deep=True))
    else:
        messages[-1].contents[0].call_id = "foreign"
    before = [message.to_dict() for message in messages]
    assert project_tool_observations(messages) is messages
    assert [message.to_dict() for message in messages] == before


def test_blackboard_update_and_full_result_survive_snapshot_and_projection():
    session = AgentSession()
    messages = history()
    append_board_update(messages, "[黑板更新] Important independent evidence")
    session.state["in_memory"] = {"messages": messages}
    restored = AgentSession.from_dict(session.to_dict())
    original = restored.to_dict()
    projected = project_tool_observations(restored.state["in_memory"]["messages"])
    rendered = json.dumps(pi_messages(projected), ensure_ascii=False)
    assert "Important independent evidence" in rendered
    assert "full result " + "x" * 5000 in rendered
    assert restored.to_dict() == original


def history(*, encrypted=False):
    return [
        Message(role="user", contents=[Content.from_text("original user")], message_id="input"),
        Message(
            role="assistant",
            message_id="output",
            contents=[
                Content.from_text_reasoning(
                    id="rs_fixture",
                    text="private reasoning trace",
                    protected_data="encrypted-fixture" if encrypted else None,
                ),
                Content.from_text("assistant explanation"),
                Content.from_function_call(
                    call_id="call-1", name="lookup", arguments='{"name":"a"}'
                ),
            ],
        ),
        Message(
            role="tool",
            message_id="result",
            contents=[Content.from_function_result("call-1", result="full result " + "x" * 5000)],
        ),
    ]


@pytest.mark.parametrize(
    ("provider", "base_url", "requested", "expected"),
    [
        ("openai_responses", "https://api.openai.com/v1", "auto", "native"),
        (
            "openai_responses",
            "https://api.openai.com.attacker.test/v1",
            "auto",
            "tool_observations",
        ),
        ("openai_responses", "http://127.0.0.1:3425/v1", "auto", "tool_observations"),
        ("azure_openai_responses", "https://example.azure.com", "auto", "native"),
        ("openai_responses", "https://gateway.test/v1", "native", "native"),
        ("openai_responses", "https://api.openai.com/v1", "tool_observations", "tool_observations"),
    ],
)
async def test_factory_resolves_explicit_replay_policy(provider, base_url, requested, expected):
    model = load_runtime_profile(PROFILE).models.explore.model_copy(
        update={
            "provider": provider,
            "base_url": base_url,
            "provider_options": {"responses_replay_mode": requested, "api_version": "test-version"},
        }
    )
    transport = make_client(
        model,
        api_key="test-key",
        explore_max_steps=2,
        conclude_grace_calls=0,
        max_duration_seconds=10,
    )
    assert isinstance(transport, PiChatClient)
    assert transport.responses_replay_mode == expected
    await close_model_client(transport)


def test_factory_rejects_invalid_policy():
    model = load_runtime_profile(PROFILE).models.explore.model_copy(
        update={
            "provider": "openai_responses",
            "provider_options": {"responses_replay_mode": "invalid"},
        }
    )
    with pytest.raises(ValueError, match="Responses replay mode"):
        make_client(
            model,
            api_key="test-key",
            explore_max_steps=2,
            conclude_grace_calls=0,
            max_duration_seconds=10,
        )


async def test_replaced_rich_result_text_does_not_repeat_stale_items():
    messages = history()
    result = Content.from_function_result(
        "call-1",
        result=[
            Content.from_text("stale body"),
            Content.from_data(b"image-fixture", media_type="image/png"),
        ],
    )
    result.result = "authoritative replacement"
    messages[-1].contents[0] = result
    projected = project_tool_observations(messages)
    text = "\n".join(c.text or "" for m in projected for c in m.contents if c.type == "text")
    assert "stale body" not in text
    assert text.count("authoritative replacement") == 1
    assert sum(c.type == "data" for m in projected for c in m.contents) == 1
