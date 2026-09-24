"""Deterministic MAF chat client for real agent and tool-loop tests."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, cast

from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
    UsageDetails,
)


@dataclass(frozen=True)
class ScriptToolCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True)
class ScriptUsage:
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    def as_maf(self) -> UsageDetails:
        return cast(
            UsageDetails,
            {
                "input_token_count": self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens,
                "output_token_count": self.completion_tokens,
                "total_token_count": self.prompt_cache_hit_tokens
                + self.prompt_cache_miss_tokens
                + self.completion_tokens,
                "cache_read_input_token_count": self.prompt_cache_hit_tokens,
                "reasoning_output_token_count": self.reasoning_tokens,
                "prompt_cache_hit_tokens": self.prompt_cache_hit_tokens,
                "prompt_cache_miss_tokens": self.prompt_cache_miss_tokens,
            },
        )


@dataclass(frozen=True)
class ScriptStep:
    text: str | None = None
    calls: tuple[ScriptToolCall, ...] = ()
    usage: ScriptUsage = field(default_factory=ScriptUsage)
    expect_contains: str | None = None


class ScriptedChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """Runs MAF's own function loop while replacing only the model response."""

    def __init__(
        self,
        steps: Sequence[ScriptStep],
        *,
        jump_on: Mapping[str, int] | None = None,
        max_iterations: int = 40,
        allow_concurrent_invocation: bool = True,
    ) -> None:
        super().__init__(
            function_invocation_configuration={
                "max_iterations": max_iterations,
                "allow_concurrent_invocation": allow_concurrent_invocation,
            }
        )
        self.steps = list(steps)
        self.jump_on = dict(jump_on or {})
        self.received_messages: list[list[Message]] = []
        self.received_options: list[dict[str, Any]] = []
        self._index = 0
        self._used_jumps: set[str] = set()

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        self.received_messages.append(deepcopy(list(messages)))
        self.received_options.append(deepcopy(dict(options)))
        message_text = (
            fragment
            for message in messages
            for fragment in [
                message.text,
                *(
                    str(content.result)
                    for content in message.contents
                    if content.type == "function_result"
                ),
            ]
        )
        instructions = options.get("instructions")
        seen = "\n".join(
            [instructions, *message_text] if isinstance(instructions, str) else message_text
        )
        for trigger, target in self.jump_on.items():
            if trigger in seen and trigger not in self._used_jumps:
                self._index = target
                self._used_jumps.add(trigger)
                break
        if self._index >= len(self.steps):
            raise AssertionError("ScriptedChatClient ran out of steps")
        step = self.steps[self._index]
        self._index += 1
        response_id = f"script-{self._index}"
        if step.expect_contains and step.expect_contains not in seen:
            raise AssertionError(f"Expected message fragment: {step.expect_contains}")

        contents: list[Content] = [
            Content.from_function_call(
                call_id=call.call_id or f"script-{self._index}-{ordinal}",
                name=call.name,
                arguments=dict(call.arguments),
            )
            for ordinal, call in enumerate(step.calls, start=1)
        ]
        if step.text is not None:
            contents.append(Content.from_text(step.text))
        raw_usage = {
            "prompt_cache_hit_tokens": step.usage.prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": step.usage.prompt_cache_miss_tokens,
            "completion_tokens": step.usage.completion_tokens,
            "reasoning_tokens": step.usage.reasoning_tokens,
        }
        if stream:

            async def updates():
                yield ChatResponseUpdate(
                    role="assistant",
                    contents=[*contents, Content.from_usage(step.usage.as_maf())],
                    response_id=response_id,
                    raw_representation={"usage": raw_usage},
                )

            return self._build_response_stream(updates())

        async def response() -> ChatResponse:
            return ChatResponse(
                messages=[Message(role="assistant", contents=contents)],
                response_id=response_id,
                model="scripted",
                usage_details=step.usage.as_maf(),
                raw_representation={"usage": raw_usage},
            )

        return response()
