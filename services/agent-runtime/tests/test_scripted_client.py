"""The scripted client exercises MAF's own function loop without a model API."""

from agent_framework import Agent, BaseChatClient, Message, tool
from bbx_runtime.testing.scripted_client import (
    ScriptedChatClient,
    ScriptStep,
    ScriptToolCall,
    ScriptUsage,
)


async def test_scripted_agent_runs_a_real_tool_loop():
    invoked: list[int] = []

    @tool
    async def increment(value: int) -> str:
        invoked.append(value)
        return str(value + 1)

    client = ScriptedChatClient(
        [
            ScriptStep(
                calls=(ScriptToolCall("increment", {"value": 3}),),
                usage=ScriptUsage(4, 6, 2, 1),
            ),
            ScriptStep(text="Finished after 4", expect_contains="4"),
        ]
    )
    assert isinstance(client, BaseChatClient)
    response = await Agent(client=client, tools=[increment]).run("Start")
    assert response.text == "Finished after 4"
    assert invoked == [3]
    assert len(client.received_messages) == 2
    assert [message.role for message in client.received_messages[1]] == [
        "user",
        "assistant",
        "tool",
    ]


async def test_script_jump_and_deepseek_usage_fields():
    client = ScriptedChatClient(
        [ScriptStep(text="ordinary"), ScriptStep(text="unused"), ScriptStep(text="handoff")],
        jump_on={"[conclude]": 2},
    )
    response = await client.get_response([Message(role="user", contents=["[conclude]"])])
    assert response.text == "handoff"
    assert len(client.received_messages) == 1
    usage = ScriptUsage(11, 13, 5, 2).as_maf()
    assert usage.get("cache_read_input_token_count") == 11
    assert usage["prompt_cache_miss_tokens"] == 13
    assert usage.get("reasoning_output_token_count") == 2


async def test_scripted_streaming_response():
    client = ScriptedChatClient([ScriptStep(text="streamed")])
    updates = [
        update
        async for update in client.get_response(
            [Message(role="user", contents=["Hi"])], stream=True
        )
    ]
    assert any(content.text == "streamed" for update in updates for content in update.contents)
