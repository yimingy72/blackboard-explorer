"""Gateway replay uses completed observations without mutating MAF history."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import httpx2 as httpx
import pytest
from agent_framework import Agent, AgentSession, Content, Message
from agent_framework.exceptions import ChatClientInvalidRequestException
from bbx_runtime.clients.blackboard import BlackboardClient
from bbx_runtime.middleware import append_board_update
from bbx_runtime.models import (
    CompleteResponsesClient,
    close_model_client,
    load_runtime_profile,
    make_client,
    model_run_options,
)
from bbx_runtime.responses_history import project_tool_observations
from bbx_runtime.session import CheckpointHistoryProvider, SessionCheckpoint
from openai import AsyncOpenAI

PROFILE = Path(__file__).resolve().parents[3] / "profiles" / "default"


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


def client(mode="native"):
    return CompleteResponsesClient(
        model="test-model",
        async_client=AsyncOpenAI(api_key="test-key", base_url="https://invalid.test/v1"),
        responses_replay_mode=mode,
    )


def assert_preserved_maf_guard(error):
    assert error.args[0] == "Local Responses history validation failed"
    assert isinstance(error.__cause__, ChatClientInvalidRequestException)
    assert "encrypted reasoning content is missing" in str(error.__cause__)


async def test_projection_is_lossless_request_only_with_provenance_and_bounded_logs(caplog):
    messages = history()
    before = [message.to_dict() for message in messages]
    with caplog.at_level("INFO", logger="bbx_runtime.responses_history"):
        projected = project_tool_observations(messages)
    assert [message.to_dict() for message in messages] == before
    assert projected[0].to_dict() == messages[0].to_dict()
    assert "private reasoning trace" not in str([message.to_dict() for message in projected])
    text = "\n".join(content.text or "" for message in projected for content in message.contents)
    assert "assistant explanation" in text
    assert "full result " + "x" * 5000 in text
    assert '"call_id": "call-1"' in text
    assert '"name": "lookup"' in text
    assert '"arguments": "{\\"name\\":\\"a\\"}"' in text
    observations = [
        m for m in projected if m.additional_properties.get("bbx_responses_observation")
    ]
    assert observations
    assert all(
        m.additional_properties["bbx_responses_observation"]["source_group_id"]
        for m in observations
    )
    assert {
        m.additional_properties["bbx_responses_observation"]["source_message_id"]
        for m in observations
    } == {"output", "result"}
    assert "groups=1 messages=2" in caplog.text
    assert "full result" not in caplog.text
    transport = client("tool_observations")
    items = transport._prepare_messages_for_openai(
        messages, request_uses_service_side_storage=False
    )
    assert all(item["type"] == "message" for item in items)
    await close_model_client(transport)


@pytest.mark.parametrize("encrypted", [False, True])
async def test_native_guard_and_encrypted_groups_unchanged(encrypted):
    messages = history(encrypted=encrypted)
    native = client()
    adapted = client("tool_observations")
    before = [message.to_dict() for message in messages]
    if encrypted:
        assert project_tool_observations(messages) is messages
        assert native._prepare_messages_for_openai(
            messages, request_uses_service_side_storage=False
        ) == adapted._prepare_messages_for_openai(messages, request_uses_service_side_storage=False)
    else:
        with pytest.raises(
            ChatClientInvalidRequestException, match="Local Responses history validation failed"
        ) as caught:
            native._prepare_messages_for_openai(messages, request_uses_service_side_storage=False)
        assert_preserved_maf_guard(caught.value)
    assert [message.to_dict() for message in messages] == before
    await close_model_client(native)
    await close_model_client(adapted)


@pytest.mark.parametrize("reasoning_text", [None, "", " ", "\n\t"])
@pytest.mark.parametrize("complete", [False, True])
async def test_reasoning_placeholders_project_only_with_complete_tool_evidence(
    reasoning_text, complete
):
    messages = history()
    messages[1].contents[0].text = reasoning_text
    if not complete:
        messages.pop()
    before = [message.to_dict() for message in messages]
    adapted = client("tool_observations")
    native = client()
    with pytest.raises(
        ChatClientInvalidRequestException, match="Local Responses history validation failed"
    ) as caught:
        native._prepare_messages_for_openai(messages, request_uses_service_side_storage=False)
    assert_preserved_maf_guard(caught.value)
    if complete:
        items = adapted._prepare_messages_for_openai(
            messages, request_uses_service_side_storage=False
        )
        assert all(item["type"] == "message" for item in items)
        assert "full result " + "x" * 5000 in json.dumps(items)
        assert "call-1" in json.dumps(items)
    else:
        with pytest.raises(
            ChatClientInvalidRequestException, match="Local Responses history validation failed"
        ) as caught:
            adapted._prepare_messages_for_openai(messages, request_uses_service_side_storage=False)
        assert_preserved_maf_guard(caught.value)
    assert [message.to_dict() for message in messages] == before
    await close_model_client(adapted)
    await close_model_client(native)


@pytest.mark.parametrize(
    "broken",
    [
        "missing",
        "duplicate_call",
        "duplicate_result",
        "orphan",
        "hosted",
        "no_reasoning_id",
        "mixed_encrypted",
    ],
)
async def test_ambiguous_or_unsupported_groups_keep_maf_guard(broken):
    messages = history()
    if broken == "missing":
        messages.pop()
    elif broken == "duplicate_call":
        messages[1].contents.append(deepcopy(messages[1].contents[-1]))
    elif broken == "duplicate_result":
        messages[2].contents.append(deepcopy(messages[2].contents[0]))
    elif broken == "orphan":
        messages[2].contents.append(Content.from_function_result("unknown", result="orphan"))
    elif broken == "hosted":
        messages[1].contents[-1].additional_properties["openai.responses.shell.output_type"] = (
            "shell_call"
        )
    elif broken == "no_reasoning_id":
        messages[1].contents[0].id = None
    else:
        messages[1].contents.append(
            Content.from_text_reasoning(id="rs_encrypted", text="", protected_data="encrypted")
        )
    assert project_tool_observations(messages) is messages
    transport = client("tool_observations")
    with pytest.raises(
        ChatClientInvalidRequestException, match="Local Responses history validation failed"
    ) as caught:
        transport._prepare_messages_for_openai(messages, request_uses_service_side_storage=False)
    assert_preserved_maf_guard(caught.value)
    await close_model_client(transport)


async def test_noncontiguous_group_preserves_intervening_input_and_typed_images():
    messages = history()
    image = Content.from_data(b"image-fixture", media_type="image/png")
    messages[0].contents.append(deepcopy(image))
    messages[2].contents[0] = Content.from_function_result(
        "call-1", result=[Content.from_text("seen image"), image]
    )
    messages.insert(
        2,
        Message(
            role="user", contents=[Content.from_text("intervening input")], message_id="intervening"
        ),
    )
    before = [message.to_dict() for message in messages]
    projected = project_tool_observations(messages)
    assert [m.to_dict() for m in messages] == before
    text = [c.text for m in projected for c in m.contents if c.type == "text"]
    assert text.index("intervening input") < text.index("seen image")
    assert sum(c.type == "data" for m in projected for c in m.contents) == 2
    assert all("aW1hZ2UtZml4dHVyZQ==" not in (c.text or "") for m in projected for c in m.contents)
    transport = client("tool_observations")
    items = transport._prepare_messages_for_openai(
        messages, request_uses_service_side_storage=False
    )
    assert (
        sum(part["type"] == "input_image" for item in items for part in item.get("content", []))
        == 2
    )
    await close_model_client(transport)


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
    assert isinstance(transport, CompleteResponsesClient)
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


class MemorySessions:
    saved: dict[str, Any]

    async def put_agent_session(self, *_args, **kwargs):
        self.saved = {
            "session": json.loads(json.dumps(kwargs["session"])),
            "opening_instructions": kwargs["opening_instructions"],
            "origin": kwargs["origin"],
            "revision": kwargs["expected_revision"] + 1,
        }
        return self.saved

    async def get_agent_session(self, *_args):
        return self.saved


@pytest.mark.parametrize("rich", [False, True])
async def test_board_update_survives_checkpoint_reload_and_result_projection(rich):
    messages = history()
    body = "original tool result"
    result = [Content.from_text(body)]
    if rich:
        result.append(Content.from_data(b"image-fixture", media_type="image/png"))
    messages[-1].contents[0] = Content.from_function_result("call-1", result=result)
    update = "[黑板更新]\n已进入结束阶段，请立即返回回执。"
    append_board_update(messages, update)
    assert messages[-1].contents[0].items is not None
    assert messages[-1].contents[0].items[0].text == body
    session = AgentSession()
    session.state["in_memory"] = {"messages": messages}
    service = MemorySessions()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", session)
    await checkpoint.save()
    restored = await SessionCheckpoint.load(cast(BlackboardClient, service), "task", "agent")
    assert restored is not None
    before = restored.session.to_dict()
    messages = restored.session.state["in_memory"]["messages"]
    projected = project_tool_observations(messages)
    text = "\n".join(c.text or "" for m in projected for c in m.contents if c.type == "text")
    assert text.count(body) == 1
    assert text.count(update) == 1
    assert text.count("[黑板更新结束]") == 1
    assert sum(c.type == "data" for m in projected for c in m.contents) == int(rich)
    transport = client("tool_observations")
    wire = json.dumps(
        transport._prepare_messages_for_openai(messages, request_uses_service_side_storage=False),
        ensure_ascii=False,
    )
    assert wire.count("已进入结束阶段，请立即返回回执。") == 1
    assert wire.count(body) == 1
    assert restored.session.to_dict() == before
    await close_model_client(transport)


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


async def test_encrypted_rich_board_update_survives_checkpoint_and_native_request():
    messages = history(encrypted=True)
    messages[-1].contents[0] = Content.from_function_result(
        "call-1",
        result=[
            Content.from_text("native rich tool result"),
            Content.from_data(b"image-fixture", media_type="image/png"),
        ],
    )
    update = "[黑板更新]\n已进入结束阶段，请立即返回回执。"
    append_board_update(messages, update)
    session = AgentSession()
    session.state["in_memory"] = {"messages": messages}
    service = MemorySessions()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", session)
    await checkpoint.save()
    restored = await SessionCheckpoint.load(cast(BlackboardClient, service), "task", "agent")
    assert restored is not None
    before = restored.session.to_dict()
    messages = restored.session.state["in_memory"]["messages"]
    assert project_tool_observations(messages) is messages
    transport = client()
    items = transport._prepare_messages_for_openai(
        messages, request_uses_service_side_storage=False
    )
    reasoning = [item for item in items if item["type"] == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["encrypted_content"] == "encrypted-fixture"
    results = [item for item in items if item["type"] == "function_call_output"]
    assert len(results) == 1
    parts = results[0]["output"]
    assert isinstance(parts, list)
    text = "\n".join(part["text"] for part in parts if part["type"] == "input_text")
    assert text.count("native rich tool result") == 1
    assert text.count(update) == 1
    assert text.count("[黑板更新结束]") == 1
    assert sum(part["type"] == "input_image" for part in parts) == 1
    assert restored.session.to_dict() == before
    await close_model_client(transport)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reasoning_text", ["private reasoning trace", " "])
async def test_real_maf_tool_loop_and_checkpoint_restore_keep_original_history(
    monkeypatch, stream, reasoning_text
):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            output = [
                {
                    "type": "reasoning",
                    "id": "rs_fixture",
                    "summary": [{"type": "summary_text", "text": reasoning_text}],
                }
            ]
            output.extend(
                {
                    "type": "function_call",
                    "id": f"fc_{name}",
                    "call_id": f"call_{name}",
                    "name": "lookup",
                    "arguments": json.dumps({"name": name}),
                    "status": "completed",
                }
                for name in ["a", "b"]
            )
        else:
            output = [
                {
                    "type": "message",
                    "id": "msg_answer",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "finished", "annotations": []}],
                }
            ]
        response = {
            "id": f"resp_{len(requests)}",
            "object": "response",
            "created_at": 1,
            "model": "test-model",
            "status": "completed",
            "output": output,
        }
        if not stream:
            return httpx.Response(200, json=response)
        events = []
        for index, item in enumerate(output):
            if item["type"] == "reasoning":
                events.append(
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": item["id"],
                        "output_index": index,
                        "summary_index": 0,
                        "delta": reasoning_text,
                    }
                )
            elif item["type"] == "function_call":
                events.extend(
                    [
                        {
                            "type": "response.output_item.added",
                            "output_index": index,
                            "item": {**item, "arguments": ""},
                        },
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": item["id"],
                            "output_index": index,
                            "delta": item["arguments"],
                        },
                    ]
                )
            else:
                events.append(
                    {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": index,
                        "content_index": 0,
                        "delta": "finished",
                    }
                )
        events.append({"type": "response.completed", "response": response})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(
                "data: " + json.dumps({**event, "sequence_number": index}) + "\n\n"
                for index, event in enumerate(events)
            ),
        )

    monkeypatch.setattr(
        "bbx_runtime.models.AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        ),
    )
    model = load_runtime_profile(PROFILE).models.explore.model_copy(
        update={
            "provider": "openai_responses",
            "model": "test-model",
            "base_url": "https://gateway.test/v1",
        }
    )
    transport = make_client(
        model,
        api_key="test-key",
        explore_max_steps=3,
        conclude_grace_calls=0,
        max_duration_seconds=10,
    )
    executions = []

    def lookup(name: str) -> str:
        executions.append(name)
        return f"result for {name}"

    service = MemorySessions()
    checkpoint = SessionCheckpoint(cast(BlackboardClient, service), "task", "agent", AgentSession())
    async with Agent(
        client=transport,
        tools=[lookup],
        context_providers=[CheckpointHistoryProvider(checkpoint)],
        require_per_service_call_history_persistence=True,
    ) as agent:
        if stream:
            result = await agent.run(
                "Find a and b",
                session=checkpoint.session,
                options=model_run_options(model),
                stream=True,
            ).get_final_response()
        else:
            result = await agent.run(
                "Find a and b", session=checkpoint.session, options=model_run_options(model)
            )
    assert result.text == "finished"
    assert executions == ["a", "b"]
    assert len(requests) == 2
    assert requests[1]["store"] is False
    assert all(item["type"] == "message" for item in requests[1]["input"])
    wire = json.dumps(requests[1]["input"])
    assert "private reasoning trace" not in wire
    assert all(
        text in wire for text in ["call_a", "call_b", "lookup", "result for a", "result for b"]
    )
    restored = await SessionCheckpoint.load(cast(BlackboardClient, service), "task", "agent")
    assert restored is not None
    before = restored.session.to_dict()
    messages = restored.session.state["in_memory"]["messages"]
    assert any(
        c.type == "text_reasoning" and c.text == reasoning_text
        for m in messages
        for c in m.contents
    )
    assert sum(c.type == "function_call" for m in messages for c in m.contents) == 2
    assert sum(c.type == "function_result" for m in messages for c in m.contents) == 2
    assert isinstance(transport, CompleteResponsesClient)
    transport._prepare_messages_for_openai(messages, request_uses_service_side_storage=False)
    assert restored.session.to_dict() == before
    assert executions == ["a", "b"]
    await close_model_client(transport)
