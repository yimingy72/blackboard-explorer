"""Installed-MAF behavior checks that run without Docker or a model service."""

import asyncio

from agent_framework import (
    Agent,
    ChatMiddleware,
    FunctionMiddleware,
    Message,
    MessageInjectionMiddleware,
    enqueue_messages,
    tool,
)
from bbx_runtime.testing.scripted_client import (
    ScriptedChatClient,
    ScriptStep,
    ScriptToolCall,
)


async def test_03_chat_middleware_list_order_is_outer_to_inner():
    order: list[str] = []

    class Mark(ChatMiddleware):
        def __init__(self, name: str) -> None:
            self.name = name

        async def process(self, context, call_next) -> None:
            order.append(f"{self.name}:before")
            await call_next()
            order.append(f"{self.name}:after")

    agent = Agent(
        client=ScriptedChatClient([ScriptStep(text="done")]),
        middleware=[Mark("first"), Mark("second")],
    )
    assert (await agent.run("start")).text == "done"
    assert order == ["first:before", "second:before", "second:after", "first:after"]


async def test_04_enqueued_message_is_not_retained_after_a_tool_round_trip():
    class InjectOnce(ChatMiddleware):
        def __init__(self) -> None:
            self.done = False

        async def process(self, context, call_next) -> None:
            if not self.done:
                assert context.session is not None
                enqueue_messages(
                    context.session, Message(role="user", contents=["[board delta] F2"])
                )
                self.done = True
            await call_next()

    @tool
    async def step() -> str:
        return "completed"

    client = ScriptedChatClient(
        [ScriptStep(calls=(ScriptToolCall("step"),)), ScriptStep(text="done")]
    )
    agent = Agent(
        client=client, tools=[step], middleware=[InjectOnce(), MessageInjectionMiddleware()]
    )
    assert (await agent.run("start", session=agent.create_session())).text == "done"
    assert len(client.received_messages) == 2
    assert any("[board delta] F2" in message.text for message in client.received_messages[0])
    assert not any("[board delta] F2" in message.text for message in client.received_messages[1])


async def test_05_pending_injection_after_final_response_causes_one_more_model_call():
    class EnqueueAfterResponse(ChatMiddleware):
        def __init__(self) -> None:
            self.done = False

        async def process(self, context, call_next) -> None:
            await call_next()
            if not self.done:
                assert context.session is not None
                enqueue_messages(context.session, Message(role="user", contents=["[late update]"]))
                self.done = True

    client = ScriptedChatClient(
        [ScriptStep(text="first receipt"), ScriptStep(text="second receipt")]
    )
    agent = Agent(
        client=client,
        middleware=[MessageInjectionMiddleware(), EnqueueAfterResponse()],
    )
    assert (await agent.run("start", session=agent.create_session())).text == "second receipt"
    assert len(client.received_messages) == 2
    assert any("[late update]" in message.text for message in client.received_messages[1])


async def test_06_function_middleware_can_replace_a_result_without_stopping_agent():
    executed = False

    @tool
    async def execute_command() -> str:
        nonlocal executed
        executed = True
        return "real result"

    class Replace(FunctionMiddleware):
        async def process(self, context, call_next) -> None:
            if context.function.name == "execute_command":
                context.result = "blocked by gate"
                return
            await call_next()

    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("execute_command"),)),
            ScriptStep(text="ended normally", expect_contains="blocked by gate"),
        ]
    )
    response = await Agent(client=client, tools=[execute_command], middleware=[Replace()]).run(
        "start"
    )
    assert response.text == "ended normally"
    assert not executed


async def test_07_max_iterations_disables_tools_then_requests_final_response():
    calls = 0

    @tool
    async def step() -> str:
        nonlocal calls
        calls += 1
        return "one"

    client = ScriptedChatClient(
        [ScriptStep(calls=(ScriptToolCall("step"),)), ScriptStep(text="final")],
        max_iterations=1,
    )
    assert (await Agent(client=client, tools=[step]).run("start")).text == "final"
    assert calls == 1
    assert len(client.received_messages) == 2
    assert client.received_options[0].get("tool_choice") == "auto"
    assert client.received_options[1].get("tool_choice") == "none"


async def test_09_five_parallel_calls_share_one_atomic_grace_budget():
    executed: list[int] = []
    active = 0
    max_active = 0

    @tool
    async def handoff(value: int) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        executed.append(value)
        active -= 1
        return f"saved {value}"

    class GraceGate(FunctionMiddleware):
        def __init__(self) -> None:
            self.left = 3
            self.denied = 0
            self.lock = asyncio.Lock()

        async def process(self, context, call_next) -> None:
            async with self.lock:
                if self.left == 0:
                    self.denied += 1
                    context.result = "grace exhausted"
                    return
                self.left -= 1
            await call_next()

    gate = GraceGate()
    client = ScriptedChatClient(
        [
            ScriptStep(calls=tuple(ScriptToolCall("handoff", {"value": n}) for n in range(5))),
            ScriptStep(text="done", expect_contains="grace exhausted"),
        ]
    )
    assert (
        await Agent(client=client, tools=[handoff], middleware=[gate]).run("start")
    ).text == "done"
    assert len(executed) == 3
    assert max_active >= 2
    assert gate.denied == 2 and gate.left == 0
