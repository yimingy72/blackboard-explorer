"""Durable snapshots of the real Agent Framework conversation session."""

from __future__ import annotations

import asyncio
from typing import Any, Literal
from uuid import uuid4

import httpx
from agent_framework import (
    AgentSession,
    Content,
    FunctionInvocationContext,
    FunctionMiddleware,
    InMemoryHistoryProvider,
    Message,
)
from bbx_contracts.billing import add_usage
from bbx_contracts.storage import storage_safe

from bbx_runtime.billing import _usage
from bbx_runtime.clients import BlackboardClient, RemoteError
from bbx_runtime.image_view import hydrate_image_messages, strip_image_history

INTERRUPTED_TOOL_RESULT = (
    "此前工具执行中断或结果未保存，不能认为调用成功；原工具没有在复盘中重新执行。"
)


def _unpaired_calls(messages: list[Message]) -> list[Content]:
    pending: list[Content] = []
    for message in messages:
        for content in message.contents:
            if (
                content.type == "function_call"
                and content.call_id
                and not content.informational_only
            ):
                pending.append(content)
            elif content.type == "function_result" and content.call_id:
                for index, call in enumerate(pending):
                    if call.call_id == content.call_id:
                        pending.pop(index)
                        break
    return pending


def repair_unpaired_tool_calls(session: AgentSession, *, include_interrupted: bool) -> int:
    """Complete broken tool pairs without rerunning a prior tool."""
    history = session.state.get("in_memory", {})
    messages = history.get("messages", []) if isinstance(history, dict) else []
    pending = _unpaired_calls(messages)
    saved_results = session.state.get("bbx_tool_results", {})
    repairs: list[Content] = []
    records: list[dict[str, str]] = []
    for call in pending:
        call_id = call.call_id
        assert call_id is not None
        saved = saved_results.get(call_id) if isinstance(saved_results, dict) else None
        if isinstance(saved, dict):
            result = Content.from_dict(saved)
            kind = "saved_tool_result"
        elif include_interrupted:
            result = Content.from_function_result(call_id, result=INTERRUPTED_TOOL_RESULT)
            kind = "interrupted"
        else:
            continue
        result.additional_properties["bbx_recovery_repair"] = kind
        repairs.append(result)
        records.append({"call_id": call_id, "kind": kind})
        if isinstance(saved_results, dict):
            saved_results.pop(call_id, None)
    if repairs:
        messages.append(Message(role="tool", contents=repairs))
        session.state.setdefault("bbx_recovery_repairs", []).extend(records)
    return len(repairs)


class SessionCheckpoint:
    def __init__(
        self,
        service: BlackboardClient,
        task_id: str,
        agent_id: str,
        session: AgentSession,
        *,
        opening_instructions: str = "",
        origin: Literal["native", "legacy"] = "native",
        revision: int = 0,
    ) -> None:
        self.service = service
        self.task_id = task_id
        self.agent_id = agent_id
        self.session = session
        self.opening_instructions = opening_instructions
        self.origin: Literal["native", "legacy"] = origin
        self.revision = revision
        self.deliveries: list[dict[str, str]] = []
        self.delivered_message_ids: set[str] = set()
        self.review_claim: dict[str, str] | None = None
        self.expected_derive_round: int | None = None
        self.review_request: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    @property
    def prompt_revision(self) -> int:
        return int(self.session.state.get("bbx_prompt_revision") or 0)

    @prompt_revision.setter
    def prompt_revision(self, value: int) -> None:
        self.session.state["bbx_prompt_revision"] = value

    @classmethod
    async def load(
        cls, service: BlackboardClient, task_id: str, agent_id: str
    ) -> SessionCheckpoint | None:
        try:
            saved = await service.get_agent_session(task_id, agent_id)
        except RemoteError as error:
            if error.status == 404:
                return None
            raise
        return cls(
            service,
            task_id,
            agent_id,
            AgentSession.from_dict(saved["session"]),
            opening_instructions=saved["opening_instructions"],
            origin=saved["origin"],
            revision=int(saved["revision"]),
        )

    def stage_delivery(self, message_id: str, claim_token: str) -> None:
        self.deliveries.append({"id": message_id, "claim_token": claim_token})

    async def save(self) -> None:
        async with self._lock:
            history = self.session.state.get("in_memory", {})
            messages = history.get("messages", []) if isinstance(history, dict) else []
            persisted_ids = {
                message.message_id
                for message in messages
                if getattr(message, "role", None) == "user"
            }
            deliveries = [item for item in self.deliveries if item["id"] in persisted_ids]
            self.session.state["bbx_checkpoint_id"] = str(uuid4())
            strip_image_history(self.session)
            payload = storage_safe(self.session.to_dict())
            try:
                saved = await self.service.put_agent_session(
                    self.task_id,
                    self.agent_id,
                    session=payload,
                    opening_instructions=self.opening_instructions,
                    origin=self.origin,
                    expected_revision=self.revision,
                    deliveries=deliveries,
                    review_claim=self.review_claim,
                    **(
                        {"expected_derive_round": self.expected_derive_round}
                        if self.expected_derive_round is not None
                        else {}
                    ),
                )
            except (httpx.TransportError, RemoteError) as error:
                if isinstance(error, RemoteError) and error.status < 500:
                    raise
                # A lost response can follow a committed PUT. Accept it only when the
                # durable revision and entire snapshot prove this exact write landed.
                try:
                    saved = await self.service.get_agent_session(self.task_id, self.agent_id)
                except Exception:
                    raise
                if (
                    int(saved["revision"]) != self.revision + 1
                    or saved["session"] != payload
                    or saved["opening_instructions"] != self.opening_instructions
                    or saved["origin"] != self.origin
                ):
                    raise
            self.revision = int(saved["revision"])
            self.delivered_message_ids.update(item["id"] for item in deliveries)
            self.deliveries = [item for item in self.deliveries if item not in deliveries]


class CheckpointHistoryProvider(InMemoryHistoryProvider):
    """Save the MAF session after each model call, including tool-loop turns."""

    def __init__(self, checkpoint: SessionCheckpoint, *, usage_key: str | None = None) -> None:
        super().__init__(source_id="in_memory")
        self.checkpoint = checkpoint
        self.usage_key = usage_key

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        messages = await super().get_messages(session_id, state=state, **kwargs)

        async def read_evidence(uri: str) -> bytes:
            return await self.checkpoint.service.read_evidence(uri)

        await hydrate_image_messages(
            self.checkpoint.task_id, self.checkpoint.session, read_evidence, messages
        )
        return messages

    async def after_run(
        self, *, agent: Any, session: AgentSession, context: Any, state: dict[str, Any]
    ) -> None:
        await super().after_run(agent=agent, session=session, context=context, state=state)
        saved_results = session.state.get("bbx_tool_results")
        if isinstance(saved_results, dict):
            for message in state.get("messages", []):
                for content in message.contents:
                    if content.type == "function_result" and content.call_id:
                        saved_results.pop(content.call_id, None)
        if self.usage_key is not None:
            response = context.response
            details = response.usage_details if response is not None else None
            request = self.checkpoint.review_request
            if details and request and request["message_id"] == self.usage_key:
                usage, warning = _usage(details, request["price"], request["provider"])
                ledger = session.state.setdefault("bbx_review_billed", {})
                entry = ledger.setdefault(self.usage_key, {"usage": {}, "calls": []})
                entry["usage"] = add_usage(entry["usage"], usage.model_dump(mode="json"))
                entry["calls"].append(
                    {
                        "pricing": request["pricing"],
                        "usage": usage.model_dump(mode="json"),
                        "warning": warning,
                    }
                )
                self.checkpoint.review_request = None
            if details:
                all_usage = session.state.setdefault("bbx_review_usage", {})
                running = all_usage.setdefault(self.usage_key, {})
                input_tokens = int(details.get("input_token_count") or 0)
                raw_hit = details.get("prompt_cache_hit_tokens")
                hit = int(
                    raw_hit
                    if raw_hit is not None
                    else details.get("cache_read_input_token_count") or 0
                )
                raw_miss = details.get("prompt_cache_miss_tokens")
                miss = int(raw_miss if raw_miss is not None else max(0, input_tokens - hit))
                increments = {
                    "input_token_count": input_tokens,
                    "prompt_cache_hit_tokens": hit,
                    "prompt_cache_miss_tokens": miss,
                    "output_token_count": int(details.get("output_token_count") or 0),
                    "reasoning_output_token_count": int(
                        details.get("reasoning_output_token_count") or 0
                    ),
                }
                for field, value in increments.items():
                    running[field] = int(running.get(field) or 0) + value
        await self.checkpoint.save()


class ToolResultCheckpointMiddleware(FunctionMiddleware):
    """Preserve a completed read-only tool result before the next model call."""

    def __init__(self, checkpoint: SessionCheckpoint) -> None:
        self.checkpoint = checkpoint

    async def process(self, context: FunctionInvocationContext, call_next: Any) -> None:
        await call_next()
        call_id = context.metadata.get("call_id")
        if context.session is not self.checkpoint.session or not isinstance(call_id, str):
            return
        result = Content.from_function_result(call_id, result=context.result)
        self.checkpoint.session.state.setdefault("bbx_tool_results", {})[call_id] = result.to_dict()
        await self.checkpoint.save()
