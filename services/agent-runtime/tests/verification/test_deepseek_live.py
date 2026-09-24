"""Explicit, bounded DeepSeek checks. Never collected by make check/integration."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from agent_framework import Agent, ChatMiddleware, Message, tool
from bbx_runtime.models import load_runtime_profile, make_client, model_run_options

pytestmark = pytest.mark.live
PROFILE_DIR = Path(__file__).resolve().parents[4] / "profiles" / "default"


def _client(*, max_steps: int = 8):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key or key == "replace-me":
        pytest.skip("DEEPSEEK_API_KEY is required for explicit live tests")
    model = load_runtime_profile(PROFILE_DIR).models.explore
    if base_url := os.environ.get("DEEPSEEK_BASE_URL"):
        model = model.model_copy(update={"base_url": base_url})
    client = make_client(
        model,
        api_key=key,
        explore_max_steps=max_steps,
        conclude_grace_calls=3,
        max_duration_seconds=90,
    )
    return client, model


async def _safe(awaitable):
    try:
        return await asyncio.wait_for(awaitable, timeout=90)
    except Exception as exc:
        raise AssertionError(f"Live model call failed: {type(exc).__name__}") from None


async def test_01_deepseek_completes_five_sequential_maf_tool_calls():
    client, model = _client(max_steps=8)
    calls: list[int] = []

    class CountRounds(ChatMiddleware):
        def __init__(self) -> None:
            self.rounds = 0

        async def process(self, context, call_next) -> None:
            self.rounds += 1
            await call_next()

    @tool
    async def advance() -> str:
        calls.append(len(calls) + 1)
        return json.dumps({"step": len(calls), "complete": len(calls) == 5})

    counter = CountRounds()
    agent = Agent(
        client=client,
        tools=[advance],
        middleware=[counter],
        instructions=(
            "这是一个确定性的工具流程验证。连续调用 advance 工具，直到其返回 complete=true。"
            '只有那时才回复 JSON：{"accepted":true,"step":5}。不要提前结束。'
        ),
    )
    options = model_run_options(model)
    options["max_tokens"] = 512
    options["parallel_tool_calls"] = False
    response = await _safe(agent.run("开始。", session=agent.create_session(), options=options))
    assert calls == [1, 2, 3, 4, 5]
    assert counter.rounds >= 6
    assert "5" in response.text


async def test_02_deepseek_cache_usage_is_available_after_adapter():
    client, model = _client()
    options = model_run_options(model)
    options["max_tokens"] = 64
    response = await _safe(
        client.get_response(
            [Message(role="user", contents=["请只回复：好。"])],
            options=options,
        )
    )
    usage = response.usage_details or {}
    assert usage.get("prompt_cache_hit_tokens") is not None
    assert usage.get("prompt_cache_miss_tokens") is not None
    assert usage.get("input_token_count") is not None


async def test_10_five_normal_concurrent_requests_succeed_without_forcing_a_429():
    client, model = _client()
    options = model_run_options(model)
    options["max_tokens"] = 512
    responses = await _safe(
        asyncio.gather(
            *(
                client.get_response(
                    [Message(role="user", contents=[f"只回答数字 {number}。"])], options=options
                )
                for number in range(1, 6)
            )
        )
    )
    assert len(responses) == 5
    assert all(response.text for response in responses)


async def test_11_chinese_long_context_tool_and_receipt_three_runs():
    client, model = _client()
    context = "系统在高峰期出现间歇性网关 502，需要验证上游连接池是否耗尽。" * 512
    options = model_run_options(model)
    options["max_tokens"] = 1024
    calls = 0

    @tool
    async def read_observation() -> str:
        nonlocal calls
        calls += 1
        return "可复核观察：慢查询持续时间超过连接池超时，时间戳与 502 重合。"

    for _ in range(3):
        before = calls
        agent = Agent(
            client=client,
            tools=[read_observation],
            instructions=(
                "阅读上下文，必须调用一次 read_observation，然后仅返回 JSON 回执，"
                '格式为 {"accepted":true,"data":{"note":"简要中文结论"}}。'
            ),
        )
        response = await _safe(agent.run(context, options=options))
        try:
            receipt = json.loads(response.text)
        except json.JSONDecodeError:
            pytest.fail("Live model returned a non-JSON receipt")
        assert receipt.get("accepted") is True
        assert isinstance(receipt.get("data", {}).get("note"), str)
        assert calls == before + 1
