"""Check public Message/Content mutation as a persistent board-sync fallback."""

from agent_framework import Agent, ChatMiddleware, tool
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall


class AppendBoardDelta(ChatMiddleware):
    def __init__(self) -> None:
        self.pending = ["[delta-one]", "[delta-two]"]
        self.placements: list[str] = []

    async def process(self, context, call_next) -> None:
        if self.pending:
            delta = self.pending.pop(0)
            for message in reversed(context.messages):
                for content in reversed(message.contents):
                    if content.type == "function_result":
                        content.result = f"{content.result}\n{delta}"
                        self.placements.append("function_result")
                        break
                else:
                    continue
                break
            else:
                for message in context.messages:
                    if message.role == "user":
                        for content in message.contents:
                            if content.type == "text":
                                content.text = f"{content.text}\n{delta}"
                                self.placements.append("user")
                                break
                        break
        await call_next()


def seen(messages) -> str:
    return "\n".join(
        value
        for message in messages
        for value in [
            message.text,
            *(
                str(content.result)
                for content in message.contents
                if content.type == "function_result"
            ),
        ]
    )


async def test_public_message_content_append_persists_through_tool_loop() -> None:
    @tool
    async def step(value: int) -> str:
        return f"tool-output-{value}"

    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(ScriptToolCall("step", {"value": 1}),), expect_contains="[delta-one]"
            ),
            ScriptStep(
                calls=(ScriptToolCall("step", {"value": 2}),), expect_contains="[delta-two]"
            ),
            ScriptStep(text="done", expect_contains="[delta-two]"),
        ]
    )
    middleware = AppendBoardDelta()
    agent = Agent(client=client, tools=[step], middleware=[middleware])
    session = agent.create_session()
    response = await agent.run("starting-user-text", session=session)

    assert response.text == "done"
    assert middleware.placements == ["user", "function_result"]
    assert len(client.received_messages) == 3
    first, second, third = map(seen, client.received_messages)
    assert "starting-user-text" in first
    assert "tool-output-1" in second
    assert "tool-output-1" in third
    assert "tool-output-2" in third
    for text in (first, second, third):
        assert text.count("[delta-one]") == 1
    for text in (second, third):
        assert text.count("[delta-two]") == 1
