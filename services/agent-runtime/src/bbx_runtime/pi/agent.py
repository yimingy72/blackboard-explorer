"""Host platform callbacks around the native pi-agent-core loop.

There is deliberately no Python model/tool loop here. Pi requests a model call
or a tool through its extension boundary; the existing business hooks authorize
and checkpoint those callbacks before Pi is allowed to advance.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from bbx_runtime.pi.client import BaseChatClient, PiChatClient
from bbx_runtime.pi.data import (
    AgentSession,
    ChatResponse,
    Content,
    Message,
    _encode,
    from_pi_message,
    pi_messages,
)
from bbx_runtime.pi.hooks import (
    ChatContext,
    ChatMiddleware,
    FunctionInvocationContext,
    FunctionMiddleware,
    InMemoryHistoryProvider,
    MessageInjectionMiddleware,
    SessionContext,
    invoke_hooks,
)
from bbx_runtime.pi.stream import ResponseStream, queued_stream
from bbx_runtime.pi.tools import FunctionTool, MCPStreamableHTTPTool, tool


class Agent:
    def __init__(
        self,
        *,
        client: BaseChatClient,
        instructions: str = "",
        tools=None,
        context_providers=None,
        middleware=None,
        name: str | None = None,
        **_kwargs: Any,
    ) -> None:
        self.client, self.instructions = client, instructions
        self.tools = [
            item if isinstance(item, (FunctionTool, MCPStreamableHTTPTool)) else tool(item)
            for item in tools or []
        ]
        self.middleware = list(middleware or [])
        self.context_providers = list(context_providers or [InMemoryHistoryProvider()])
        self.name = name
        self._entered = False
        self._stack: AsyncExitStack | None = None

    def create_session(self) -> AgentSession:
        return AgentSession()

    async def __aenter__(self):
        if self._entered:
            return self
        self._stack = AsyncExitStack()
        try:
            for item in self.tools:
                if isinstance(item, MCPStreamableHTTPTool) and item.session is None:
                    await self._stack.enter_async_context(item)
        except BaseException:
            await self._stack.aclose()
            raise
        self._entered = True
        return self

    async def __aexit__(self, *_args):
        try:
            if self._stack is not None:
                await self._stack.aclose()
        finally:
            self._entered = False
            await self.client.bridge.close()

    def run(
        self,
        messages=None,
        *,
        session: AgentSession | None = None,
        options=None,
        stream: bool = False,
        **kwargs,
    ) -> ResponseStream:
        async def produce(queue):
            entered_here = not self._entered
            if entered_here:
                await self.__aenter__()
            try:
                return await self._run(
                    messages,
                    session or self.create_session(),
                    dict(options or {}),
                    stream,
                    queue,
                    kwargs,
                )
            finally:
                if entered_here:
                    await self.__aexit__()

        return queued_stream(produce)

    async def _run(self, messages, session, options, stream, queue, kwargs) -> ChatResponse:
        if isinstance(messages, str):
            messages = [Message("user", contents=[messages], message_id=str(uuid4()))]
        elif isinstance(messages, Message):
            messages = [messages]
        pending = list(messages or [])
        tools = [
            function
            for item in self.tools
            for function in (item.functions if isinstance(item, MCPStreamableHTTPTool) else [item])
        ]
        by_name = {item.name: item for item in tools}
        if len(by_name) != len(tools):
            raise ValueError("Pi tool names must be unique")
        model_hooks = [item for item in self.middleware if isinstance(item, ChatMiddleware)]
        tool_hooks = [item for item in self.middleware if isinstance(item, FunctionMiddleware)]
        last_response: ChatResponse | None = None
        responses: list[ChatResponse] = []
        invocation = 0
        invocation_run = str(uuid4())

        async def model_call() -> dict[str, Any]:
            nonlocal pending, last_response
            source_context = SessionContext(
                session_id=session.session_id,
                input_messages=list(pending),
                options=options,
            )
            for provider in self.context_providers:
                state = session.state.setdefault(provider.source_id, {})
                await provider.before_run(
                    agent=self,
                    session=session,
                    context=source_context,
                    state=state,
                )
            outgoing = [
                message for source in source_context.context_messages.values() for message in source
            ]
            outgoing.extend(source_context.input_messages)
            loaded_objects = {
                id(message)
                for source in source_context.context_messages.values()
                for message in source
            }
            request_options = {
                **options,
                "instructions": "\n".join(
                    [self.instructions, *source_context.instructions.values()]
                ).strip(),
                "tools": tools,
                "session_id": session.session_id,
            }
            context = ChatContext(
                client=self.client,
                messages=outgoing,
                options=request_options,
                stream=stream,
                session=session,
                kwargs=kwargs,
            )

            async def dispatch():
                result = self.client._inner_get_response(
                    messages=context.messages,
                    options=context.options,
                    stream=stream,
                    **kwargs,
                )
                context.result = result if isinstance(result, ResponseStream) else await result

            await invoke_hooks(model_hooks, context, dispatch)
            if isinstance(context.result, ResponseStream):
                async for update in context.result:
                    await queue.put(update)
                response = await context.result.get_final_response()
            else:
                response = context.result
            if not isinstance(response, ChatResponse):
                raise TypeError("Pi model boundary returned an invalid response")
            for message in [*context.messages, *response.messages]:
                if id(message) not in loaded_objects and not message.message_id:
                    message.message_id = str(uuid4())
            source_context.input_messages = [
                message for message in context.messages if id(message) not in loaded_objects
            ]
            source_context.response = response
            # This acknowledgement is awaited by Pi before any requested tool.
            for provider in self.context_providers:
                await provider.after_run(
                    agent=self,
                    session=session,
                    context=source_context,
                    state=session.state.setdefault(provider.source_id, {}),
                )
            pending = []
            last_response = response
            responses.append(response)
            if len(response.messages) != 1 or response.messages[0].role != "assistant":
                raise ValueError("Pi requires one complete assistant message per model response")
            native = pi_messages(response.messages)[0]
            native.update(
                api=native.get("api", "openai-completions"),
                provider=native.get("provider", "scripted"),
                model=native.get("model", "scripted"),
                usage=native.get(
                    "usage",
                    {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "totalTokens": 0,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                            "total": 0,
                        },
                    },
                ),
                stopReason="toolUse"
                if any(
                    c.type == "function_call" and not c.informational_only
                    for c in response.messages[0].contents
                )
                else "stop",
            )
            return native

        async def callback(method, params):
            nonlocal invocation
            if method == "model":
                return await model_call()
            if method == "should_continue":
                return bool(session.state.get(MessageInjectionMiddleware.KEY))
            if method == "tool_message":
                pending.append(from_pi_message(params["message"]))
                return None
            if method not in {"tool", "invalid_tool"}:
                raise ValueError("Unsupported Pi platform callback")
            function = by_name[params["name"]]
            invocation += 1
            context = FunctionInvocationContext(
                function,
                params["arguments"],
                session=session,
                metadata={
                    "call_id": params["call_id"],
                    "function_call_occurrence_id": f"{invocation_run}:{invocation}",
                },
            )
            validation_error = None
            if function.input_model is not None:
                try:
                    context.arguments = function.input_model.model_validate(
                        context.arguments
                    ).model_dump(mode="python")
                except ValidationError as error:
                    # Domain hooks provide value-free, actionable field errors.
                    validation_error = error

            async def execute():
                if validation_error is not None:
                    from bbx_contracts.tool_errors import validation_issues

                    context.result = [
                        Content.from_text(
                            json.dumps(
                                {
                                    "error": {
                                        "code": "invalid_arguments",
                                        "message": "Correct these fields using the tool schema.",
                                        "fields": validation_issues(
                                            validation_error, function.input_schema
                                        ),
                                    }
                                }
                            ),
                            additional_properties={"bbx_tool_error": "invalid_arguments"},
                        )
                    ]
                    return
                if method == "invalid_tool":
                    context.result = [
                        Content.from_text(
                            json.dumps(
                                {
                                    "error": {
                                        "code": "invalid_arguments",
                                        "message": "Correct the fields using the tool schema.",
                                    }
                                }
                            )
                        )
                    ]
                    return
                context.result = await function.invoke(
                    arguments=context.arguments,
                    context=context,
                )

            from bbx_runtime.pi.observability import observe_tool, set_preview

            with observe_tool(function.name, context.arguments) as span:
                await invoke_hooks(tool_hooks, context, execute)
                set_preview(span, "gen_ai.tool.call.result", _encode(context.result))
            result = function.parse_result(context.result)
            contents = []
            for item in result:
                if item.type == "text":
                    contents.append({"type": "text", "text": item.text or ""})
                elif item.type == "data" and item.uri and item.uri.startswith("data:"):
                    mime, data = item.uri.split(",", 1)
                    contents.append(
                        {"type": "image", "mimeType": mime[5:].split(";", 1)[0], "data": data}
                    )
            return {
                "content": contents,
                "details": {
                    "bbx_result_content": Content.from_function_result(
                        params["call_id"],
                        result=result,
                    ).to_dict()
                },
                "isError": any(
                    c.additional_properties.get("ctf_tool_error")
                    or c.additional_properties.get("bbx_tool_error")
                    for c in result
                ),
            }

        config = self.client.function_invocation_configuration
        result = await self.client.bridge.call(
            "agent",
            {
                "model": self.client.model_definition()
                if isinstance(self.client, PiChatClient)
                else None,
                "tools": [item.definition() for item in tools],
                "parallel": config.get("allow_concurrent_invocation", True)
                and options.get("parallel_tool_calls", True),
                "max_iterations": config.get("max_iterations", 40),
                "follow_up": any(
                    isinstance(item, MessageInjectionMiddleware) for item in model_hooks
                ),
            },
            callback=callback,
        )
        if result.get("native_engine") != "pi-agent-core" or last_response is None:
            raise RuntimeError("Native Pi did not settle a model response")
        aggregate: dict[str, Any] = {}
        for response in responses:
            if response.usage_details is None:
                aggregate = {}
                break
            for key, value in response.usage_details.items():
                if isinstance(value, (int, float)):
                    aggregate[key] = aggregate.get(key, 0) + value
        return last_response.model_copy(update={"usage_details": aggregate or None})
