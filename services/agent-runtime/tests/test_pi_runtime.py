"""Pi context, history, dispatch and streaming boundaries without a model service."""

import asyncio
import contextvars
import json
from pathlib import Path

import httpx2
import pytest
from bbx_runtime.clients import RemoteError
from bbx_runtime.model_errors import ModelRequestValidationError
from bbx_runtime.models import load_runtime_profile, make_client
from bbx_runtime.pi import Agent, AgentSession, ChatMiddleware, Content, Message, tool
from bbx_runtime.pi.client import usage_details
from bbx_runtime.pi.data import from_pi_message, pi_messages
from bbx_runtime.pi.stream import queued_stream
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall


def configured_client():
    model = load_runtime_profile(
        Path(__file__).resolve().parents[3] / "profiles/default"
    ).models.explore
    model = model.model_copy(
        update={
            "provider": "openai_responses",
            "model": "offline",
            "base_url": "https://offline.invalid/v1",
        }
    )
    return make_client(
        model,
        api_key="test-only",
        explore_max_steps=2,
        conclude_grace_calls=0,
        max_duration_seconds=20,
    )


def test_native_signed_content_round_trips_without_duplicating_plaintext():
    source = {
        "role": "assistant",
        "api": "openai-responses",
        "provider": "openai",
        "model": "offline",
        "stopReason": "toolUse",
        "timestamp": 1,
        "content": [
            {
                "type": "thinking",
                "thinking": "public summary",
                "thinkingSignature": json.dumps(
                    {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
                ),
            },
            {"type": "text", "text": "public answer", "textSignature": "text-signature"},
            {
                "type": "toolCall",
                "id": "call-1|fc_1",
                "name": "inspect",
                "arguments": {"value": 3},
                "namespace": "workspace",
            },
        ],
    }
    message = from_pi_message(source)
    session = AgentSession()
    session.state["in_memory"] = {"messages": [message]}
    saved = session.to_dict()
    loaded = AgentSession.from_dict(saved)
    native = pi_messages(loaded.state["in_memory"]["messages"])[0]
    assert native["content"] == source["content"]
    assert "text" not in message.contents[1].additional_properties["pi_content"]
    assert loaded.to_dict() == saved


async def test_each_native_run_callback_inherits_its_own_context():
    actor = contextvars.ContextVar("pi-test-actor", default="missing")
    seen = []

    class Observe(ChatMiddleware):
        async def process(self, context, call_next):
            seen.append(actor.get())
            await call_next()

    client = ScriptedChatClient([ScriptStep(text="a"), ScriptStep(text="b")])
    async with Agent(client=client, middleware=[Observe()]) as agent:
        session = agent.create_session()
        for value in ("first", "second"):
            token = actor.set(value)
            try:
                await agent.run(value, session=session)
            finally:
                actor.reset(token)
    assert seen == ["first", "second"]


async def test_native_payload_barrier_refuses_before_any_http_dispatch():
    calls = []
    client = configured_client()

    def unexpected(request):
        calls.append(request)
        raise AssertionError("HTTP dispatch was not allowed")

    client.request_transport = httpx2.MockTransport(unexpected)
    denied = RemoteError(409, "synthetic stop", "generation_revoked")

    async def refuse():
        raise denied

    client.before_send = refuse
    try:
        with pytest.raises(RemoteError) as caught:
            await client.get_response([Message("user", ["inspect"])])
        assert caught.value is denied
        assert calls == []
    finally:
        await client.close()


async def test_invalid_native_signature_never_marks_a_model_request_sent():
    calls, prepared = [], []
    client = configured_client()

    def unexpected(request):
        calls.append(request)
        raise AssertionError("HTTP dispatch was not allowed")

    client.request_transport = httpx2.MockTransport(unexpected)

    async def before_send():
        prepared.append(True)

    client.before_send = before_send
    history = [
        Message("user", ["inspect"]),
        Message(
            "assistant",
            [
                Content.from_text_reasoning(
                    "summary",
                    protected_data="{",
                    additional_properties={"pi_content": {"type": "thinking"}},
                )
            ],
        ),
        Message("user", ["continue"]),
    ]
    before = [message.to_dict() for message in history]
    try:
        with pytest.raises(ModelRequestValidationError):
            await client.get_response(history)
        assert not prepared and not calls
        assert [message.to_dict() for message in history] == before
    finally:
        await client.close()


async def test_stream_backpressure_does_not_retain_all_token_updates():
    live, peak = 0, 0

    class Chunk:
        def __init__(self):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)

        def __del__(self):
            nonlocal live
            live -= 1

    async def produce(queue):
        for _ in range(10000):
            await queue.put(Chunk())
        return "done"

    stream = queued_stream(produce)
    count = 0
    async for _ in stream:
        count += 1
    assert count == 10000 and await stream.get_final_response() == "done"
    assert peak < 256


@pytest.mark.parametrize(
    "field,value", [("input", True), ("cacheRead", -1), ("output", 2.5), ("totalTokens", 999)]
)
def test_invalid_native_usage_is_unknown_instead_of_underbilled(field, value):
    usage = {"input": 10, "output": 2, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 12}
    usage[field] = value
    assert usage_details({"usage": usage, "bbxUsageObserved": True}, "deepseek") is None


async def test_unknown_tool_keeps_native_pi_feedback_without_platform_failure():
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("not_registered"),)),
            ScriptStep(text="corrected", expect_contains="not found"),
        ]
    )
    assert (await Agent(client=client).run("inspect")).text == "corrected"
    assert len(client.received_messages) == 2


async def test_python_only_input_constraint_is_correctable_without_a_side_effect():
    from typing import Annotated

    from pydantic import AfterValidator

    effects = []

    def nonblank(value):
        if not value.strip():
            raise ValueError("synthetic private input")
        return value

    @tool
    async def apply(value: Annotated[str, AfterValidator(nonblank)]):
        effects.append(value)
        return "applied"

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("apply", {"value": " "}),)),
            ScriptStep(
                calls=(ScriptToolCall("apply", {"value": "ready"}),),
                expect_contains="invalid_arguments",
            ),
            ScriptStep(text="done"),
        ]
    )
    assert (await Agent(client=client, tools=[apply]).run("inspect")).text == "done"
    assert effects == ["ready"]
    assert "synthetic private input" not in str(client.received_messages)


async def test_cancelled_mcp_initialization_releases_its_owned_lifecycle():
    import httpx
    from bbx_runtime.pi import MCPStreamableHTTPTool

    entered, released = asyncio.Event(), asyncio.Event()

    async def pending(request):
        if request.method != "POST":
            return httpx.Response(405)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()
        raise AssertionError("MCP initialization should have been cancelled")

    async with httpx.AsyncClient(transport=httpx.MockTransport(pending)) as http:
        item = MCPStreamableHTTPTool(name="pending", url="http://mcp.invalid/mcp", http_client=http)
        connecting = asyncio.create_task(item.connect())
        await asyncio.wait_for(entered.wait(), 1)
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(connecting, 1)
        await asyncio.wait_for(item.close(), 1)
        assert released.is_set() and item.session is None
        assert not http.is_closed
