"""Platform business hooks used at Pi's model and tool boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from bbx_runtime.pi.data import AgentSession, Message


class MiddlewareFailure(RuntimeError):
    """A platform failure must leave Pi's recoverable tool-result path."""


class MiddlewareTermination(RuntimeError):
    pass


@dataclass
class ChatContext:
    client: Any = None
    messages: list[Message] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    stream: bool = False
    session: AgentSession | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class FunctionInvocationContext:
    function: Any
    arguments: dict[str, Any]
    session: AgentSession | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    result: Any = None


class ChatMiddleware:
    async def process(self, context: ChatContext, call_next) -> None:
        await call_next()


class FunctionMiddleware:
    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        await call_next()


@dataclass
class SessionContext:
    session_id: str | None = None
    input_messages: list[Message] = field(default_factory=list)
    context_messages: dict[str, list[Message]] = field(default_factory=dict)
    instructions: dict[str, str] = field(default_factory=dict)
    response: Any = None
    options: dict[str, Any] = field(default_factory=dict)

    def extend_instructions(self, source_id: str, value: str) -> None:
        self.instructions[source_id] = value


class ContextProvider:
    def __init__(self, source_id: str = "context", **_kwargs: Any) -> None:
        self.source_id = source_id

    async def before_run(self, *, agent, session, context, state) -> None:
        pass

    async def after_run(self, *, agent, session, context, state) -> None:
        pass


class InMemoryHistoryProvider(ContextProvider):
    """Expose the existing transactional conversation to Pi's per-call hooks."""

    def __init__(self, source_id: str = "in_memory", **kwargs: Any) -> None:
        super().__init__(source_id)
        self.store_inputs = kwargs.get("store_inputs", True)
        self.store_outputs = kwargs.get("store_outputs", True)
        self.store_context_from = kwargs.get("store_context_from", set())

    async def get_messages(self, session_id, *, state=None, **_kwargs) -> list[Message]:
        return list((state or {}).get("messages", []))

    async def save_messages(self, session_id, messages, *, state=None, **_kwargs) -> None:
        if state is not None:
            state["messages"] = [*state.get("messages", []), *messages]

    async def before_run(self, *, agent, session, context, state) -> None:
        context.context_messages[self.source_id] = await self.get_messages(
            session.session_id, state=state
        )

    async def after_run(self, *, agent, session, context, state) -> None:
        messages = []
        for source_id in self.store_context_from:
            messages.extend(context.context_messages.get(source_id, []))
        if self.store_inputs:
            messages.extend(context.input_messages)
        if self.store_outputs and context.response:
            messages.extend(context.response.messages)
        existing = {m.message_id for m in state.get("messages", []) if m.message_id}
        await self.save_messages(
            session.session_id,
            [
                message
                for message in messages
                if not message.message_id or message.message_id not in existing
            ],
            state=state,
        )


class MessageInjectionMiddleware(ChatMiddleware):
    KEY = "message_injection.pending_messages"

    def enqueue_messages(
        self, session: AgentSession, messages: Sequence[Message] | Message
    ) -> None:
        if isinstance(messages, Message):
            messages = [messages]
        session.state.setdefault(self.KEY, []).extend(messages)

    async def process(self, context: ChatContext, call_next) -> None:
        if context.session is not None:
            pending = context.session.state.pop(self.KEY, [])
            context.messages.extend(
                Message.from_dict(item) if isinstance(item, dict) else item for item in pending
            )
        await call_next()


def enqueue_messages(session: AgentSession, messages: Sequence[Message] | Message) -> None:
    MessageInjectionMiddleware().enqueue_messages(session, messages)


async def invoke_hooks(hooks, context, terminal) -> None:
    async def invoke(index):
        if index == len(hooks):
            await terminal()
        else:
            await hooks[index].process(context, lambda: invoke(index + 1))

    await invoke(0)
