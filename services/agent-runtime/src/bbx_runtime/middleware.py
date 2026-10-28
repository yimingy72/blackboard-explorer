"""Tool logging, conclude gating, and persistent board updates for one agent."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast

from agent_framework import (
    ChatContext,
    ChatMiddleware,
    ChatResponse,
    Content,
    FunctionInvocationContext,
    FunctionMiddleware,
    Message,
    MiddlewareFailure,
    MiddlewareTermination,
    ResponseStream,
)
from bbx_contracts.billing import effective_price
from bbx_contracts.storage import storage_safe
from bbx_contracts.tool_errors import validation_issues
from pydantic import BaseModel, TypeAdapter, ValidationError

from bbx_runtime.billing import _input_tokens, _usage
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.context import RunContext, execution_timestamp
from bbx_runtime.image_view import append_pending_images, finish_pending_images
from bbx_runtime.model_errors import model_attempt_limit, model_error_metadata
from bbx_runtime.opening import OpeningContextProvider
from bbx_runtime.recovery import (
    ModelCallPermit,
    ModelRecoveryGate,
    RecoveryEpisode,
    RecoveryExhausted,
    recoverable_transport,
)
from bbx_runtime.tool_failures import BlackboardToolFailure, tool_error_content
from bbx_runtime.trace import model_content, record_trace
from bbx_runtime.usage_receipts import flush_ordinary_usage

DIGEST_TYPES = {"fact.posted", "intent.posted", "intent.closed", "fact.disputed", "fact.undisputed"}
VERDICT_TYPES = {"acceptance.judged", "acceptance.reverted"}
GRACE_TOOLS = {"release", "post_fact", "post_intent"}
logger = logging.getLogger(__name__)
_JSON_ARGUMENTS = TypeAdapter(dict[str, Any])


def _call_id() -> str:
    return "c_" + base64.b32encode(secrets.token_bytes(8)).decode("ascii").lower()[:12]


def _arguments(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    return _JSON_ARGUMENTS.dump_python(
        value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value), mode="json"
    )


def _result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, Content) for item in value):
        return json.dumps([item.to_dict() for item in value], ensure_ascii=False, default=str)
    return json.dumps(value, ensure_ascii=False, default=str)


def _append_call_id(value: Any, call_id: str) -> Any:
    label = f"[call_id: {call_id}]"
    if isinstance(value, list) and all(isinstance(item, Content) for item in value):
        safe_items = []
        for item in value:
            serialized = item.to_dict()
            safe = storage_safe(serialized)
            safe_items.append(item if safe == serialized else Content.from_dict(safe))
        return [*safe_items, Content.from_text(label)]
    return f"{storage_safe(_result_text(value))}\n{label}"


class ToolLogMiddleware(FunctionMiddleware):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        failure = None
        try:
            await call_next()
        except (MiddlewareFailure, MiddlewareTermination):
            raise
        except Exception as exc:
            failure = exc
            context.result = [
                tool_error_content(
                    "tool_execution_unconfirmed",
                    "工具执行失败或结果未确认，系统停止本次执行。旧调用不得盲目重新执行。",
                    error_type=type(exc).__name__,
                )
            ]
        checkpoint = self.ctx.checkpoint
        model_call_id = context.metadata.get("call_id")
        saved_result = (
            checkpoint is not None
            and context.session is checkpoint.session
            and isinstance(model_call_id, str)
        )
        failure_type = type(failure).__name__ if failure is not None else None
        try:
            if saved_result:
                assert checkpoint is not None and isinstance(model_call_id, str)
                checkpoint.session.state.setdefault("bbx_tool_results", {})[model_call_id] = (
                    Content.from_function_result(
                        model_call_id, result=context.result, exception=failure_type
                    ).to_dict()
                )
            call_id = _call_id()
            args = _arguments(context.arguments)
            result = _result_text(context.result)
            uri = f"toolcalls/{self.ctx.task_id}/{call_id}.txt"
            full = json.dumps(
                {"tool": context.function.name, "args": args, "result": result},
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
            await self.ctx.objects.put(uri, full, content_type="application/json")
            await self.ctx.service.record_tool_call(
                self.ctx.task_id,
                {
                    "agent_id": self.ctx.agent_id,
                    "id": call_id,
                    "tool": context.function.name,
                    "args": args,
                    "result_head": result.encode("utf-8")[:4096].decode("utf-8", errors="ignore"),
                    "result_uri": uri,
                },
                **(
                    {"expected_derive_round": self.ctx.expected_derive_round}
                    if self.ctx.expected_derive_round is not None
                    else {}
                ),
            )
            context.result = _append_call_id(context.result, call_id)
            if saved_result:
                assert checkpoint is not None and isinstance(model_call_id, str)
                checkpoint.session.state["bbx_tool_results"][model_call_id] = (
                    Content.from_function_result(
                        model_call_id, result=context.result, exception=failure_type
                    ).to_dict()
                )
                await checkpoint.save()
        except Exception as error:
            original = failure if failure is not None else error
            raise BlackboardToolFailure(original) from original
        if failure is not None:
            raise BlackboardToolFailure(failure) from failure


class ToolArgumentFeedbackMiddleware(FunctionMiddleware):
    """Let MAF's public input model reject invalid arguments before tool dispatch."""

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        model = context.function.input_model
        if model is not None:
            try:
                model.model_validate(context.arguments)
            except ValidationError as error:
                fields = validation_issues(error, model.model_json_schema())
                descriptions = []
                for issue in fields:
                    path = "".join(
                        f"[{part}]" if isinstance(part, int) else f".{part}"
                        for part in issue["path"]
                    ).lstrip(".")
                    label = "缺少必填字段" if issue["type"] == "missing" else "字段值不合法"
                    descriptions.append(f"{label} {path}")
                context.result = [
                    tool_error_content(
                        "invalid_arguments",
                        "工具参数不合法：" + "；".join(descriptions) + "。请按工具 schema 修正。",
                        fields=fields,
                    )
                ]
                return
        await call_next()


class GraceGateMiddleware(FunctionMiddleware):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        board = await self.ctx.service.state(self.ctx.task_id)
        agent = board["agents"].get(self.ctx.agent_id)
        if agent is None:
            raise RuntimeError(f"Agent {self.ctx.agent_id} is missing from the blackboard")
        if (
            self.ctx.expected_started_at is not None
            and execution_timestamp(agent.get("started_at")) != self.ctx.expected_started_at
        ):
            raise asyncio.CancelledError("runtime_restart")
        if agent["status"] == "concluding":
            if context.function.name not in GRACE_TOOLS:
                context.result = (
                    "已进入结束阶段，只允许 release / post_fact / post_intent。"
                    "请立即返回回执 JSON。"
                )
                return
            try:
                await self.ctx.service.take_grace(
                    self.ctx.task_id,
                    self.ctx.agent_id,
                    **(
                        {"expected_derive_round": self.ctx.expected_derive_round}
                        if self.ctx.expected_derive_round is not None
                        else {}
                    ),
                )
            except RemoteError as exc:
                if exc.status != 409:
                    raise
                context.result = "交接次数已用完，请立即返回回执 JSON。"
                return
        await call_next()


def append_board_update(messages: Sequence[Message], update: str) -> None:
    """Extend only the newly appended prompt or tool result before its first send."""
    if not messages:
        raise RuntimeError("No new message is available for a board update")
    message = messages[-1]
    framed = f"{update}\n[黑板更新结束]"
    if message.role == "tool":
        for content in reversed(message.contents):
            if content.type == "function_result":
                items = (
                    content.items
                    or Content.from_function_result(
                        content.call_id or "", result=content.result
                    ).items
                    or []
                )
                # MAF serializes rich results from items; keep both representations current.
                content.items = [*items, Content.from_text(framed)]
                content.result = f"{_result_text(content.result)}\n{framed}"
                return
    if message.role == "user":
        for content in reversed(message.contents):
            if content.type == "text":
                content.text = f"{content.text}\n{framed}"
                return
    raise RuntimeError("Latest message is not a new user prompt or tool result")


def _full_event(event: Mapping[str, Any]) -> str:
    return json.dumps(
        {key: event.get(key) for key in ("version", "type", "actor", "object_id", "payload")},
        ensure_ascii=False,
        default=str,
    )


def _digest(event: Mapping[str, Any]) -> str:
    payload = event.get("payload") or {}
    label = event.get("object_id") or payload.get("fact_id") or payload.get("intent_id") or "?"
    statement = payload.get("statement") or payload.get("result") or ""
    return f"[{label} {event.get('type')} by {event.get('actor')}] {str(statement)[:120]}"


def _digest_counts(events: Sequence[Mapping[str, Any]]) -> str:
    facts = sum(event.get("type") == "fact.posted" for event in events)
    intents = sum(event.get("type") == "intent.posted" for event in events)
    disputes = sum(event.get("type") in {"fact.disputed", "fact.undisputed"} for event in events)
    others = len(events) - facts - intents - disputes
    return (
        f"新增事实 {facts} / 意图 {intents} / 争议变化 {disputes} / "
        f"其他变化 {others}，用 get 或 search 查看"
    )


def _render_events(events: Sequence[Mapping[str, Any]], aid: str, max_lines: int) -> list[str]:
    targeted: list[str] = []
    verdicts: list[str] = []
    digest: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("type") == "agent.message.posted":
            continue
        addressed = event.get("addressed_to")
        if addressed:
            if aid in addressed:
                targeted.append(f"[点名] {_full_event(event)}")
        elif event.get("type") in VERDICT_TYPES:
            verdicts.append(f"[裁定] {_full_event(event)}")
        elif event.get("type") in DIGEST_TYPES and event.get("actor") != aid:
            digest.append(event)
    lines = [*targeted, *verdicts]
    if len(digest) > max_lines:
        lines.append(_digest_counts(digest))
    else:
        lines.extend(_digest(event) for event in digest)
    return lines


@dataclass
class ModelCallFailure:
    metadata: dict[str, Any]
    inputs: list[Message]
    episode: RecoveryEpisode | None
    step: int


class BoardSyncMiddleware(ChatMiddleware):
    def __init__(self, ctx: RunContext, recovery: ModelRecoveryGate | None = None) -> None:
        self.ctx = ctx
        self.conclude_injected = False
        self.last_appended_version = 0
        self.price_warning: str | None = None
        self._warned_missing_price = False
        self.last_publication_check_version = int(ctx.state["task"].get("version") or 0)
        self.prompt_provider = OpeningContextProvider(ctx, ctx.checkpoint)
        self.recovery = recovery
        self.failure: ModelCallFailure | None = None
        self.recovery_deadline: float | None = None
        self.recovery_metadata: dict[str, Any] = {}
        self._recovery_agent: dict[str, Any] = {}

    async def check_recovery_control(self) -> None:
        board = await self.ctx.service.state(self.ctx.task_id)
        agent = board["agents"].get(self.ctx.agent_id)
        if (
            agent is None
            or agent["status"] not in {"running", "concluding"}
            or board.get("task", {}).get("status") in {"finished", "failed", "stopped"}
        ):
            raise asyncio.CancelledError("grace_timeout")
        self._recovery_agent = agent
        if (
            self.ctx.expected_derive_round is not None
            and int(agent.get("derive_round") or 1) != self.ctx.expected_derive_round
        ):
            raise asyncio.CancelledError("runtime_restart")
        if (
            self.ctx.expected_started_at is not None
            and execution_timestamp(agent.get("started_at")) != self.ctx.expected_started_at
        ):
            raise asyncio.CancelledError("runtime_restart")
        requested = agent.get("conclude_requested_at")
        if requested:
            stamp = (
                datetime.fromisoformat(requested.replace("Z", "+00:00"))
                if isinstance(requested, str)
                else requested
            )
            if (datetime.now(UTC) - stamp).total_seconds() >= self.ctx.params.grace_timeout * 60:
                raise asyncio.CancelledError("grace_timeout")

    def reset_failed_injections(self) -> None:
        # Failed inputs were never acknowledged by the model heartbeat. The next
        # call must load the board's committed watermark and re-inject controls.
        self.last_appended_version = 0
        self.last_publication_check_version = 0
        self.conclude_injected = False

    def take_failure(self) -> ModelCallFailure | None:
        failure, self.failure = self.failure, None
        return failure

    async def _inject_user_messages(self, context: ChatContext) -> None:
        checkpoint = self.ctx.checkpoint
        if checkpoint is None:
            return
        queued = await self.ctx.service.agent_messages(
            self.ctx.task_id, self.ctx.agent_id, status="queued"
        )
        for message in queued["messages"]:
            try:
                claimed = await self.ctx.service.claim_agent_message(
                    self.ctx.task_id, self.ctx.agent_id, str(message["id"]), "active"
                )
            except RemoteError as error:
                if error.status == 409:
                    continue
                raise
            item = claimed["message"]
            context.messages = [
                *context.messages,
                Message(
                    role="user",
                    contents=[Content.from_text(item["content"])],
                    message_id=str(item["id"]),
                ),
            ]
            checkpoint.stage_delivery(str(item["id"]), str(claimed["claim_token"]))

    async def _publication_reminder(self, step: int, last_seen: int) -> str | None:
        if step <= 1 or (step - 1) % 5:
            return None
        events = await self.ctx.board.events(
            self.ctx.task_id, since=self.last_publication_check_version
        )
        self.last_publication_check_version = max(
            [last_seen, *(int(event["version"]) for event in events)]
        )
        if any(
            event.get("actor") == self.ctx.agent_id
            and event.get("type") in {"fact.posted", "intent.posted"}
            for event in events
        ):
            return None
        return (
            "[协作检查] 若已有可复核的中间发现，请现在提交 Fact；若发现可独立推进的方向，"
            "请基于已发布事实提出 Intent 供其他 Agent 认领；证据不足则继续调查，"
            "不要为凑并发伪造或重复提交。不要等结束统一整理。"
        )

    async def process(self, context: ChatContext, call_next) -> None:
        if self.recovery_deadline is not None and monotonic() >= self.recovery_deadline:
            raise RecoveryExhausted(self.recovery_metadata)
        board = await self.ctx.service.state(self.ctx.task_id)
        agent = board["agents"].get(self.ctx.agent_id)
        if agent is None:
            raise RuntimeError(f"Agent {self.ctx.agent_id} is missing from the blackboard")
        last_seen = int(agent["last_seen_version"])
        if (
            self.ctx.expected_started_at is not None
            and execution_timestamp(agent.get("started_at")) != self.ctx.expected_started_at
        ):
            raise asyncio.CancelledError("runtime_restart")
        step = int(agent.get("steps") or 0) + 1
        checkpoint = self.ctx.checkpoint
        persisted = (
            {
                message.message_id
                for message in checkpoint.session.state.get("in_memory", {}).get("messages", [])
            }
            if checkpoint
            else set()
        )
        # Capture inputs before appending board deltas. Re-sending a failed input
        # must keep its id and claim token without duplicating those deltas.
        inputs = deepcopy(
            [
                message
                for message in context.messages
                if message.role == "user" and message.message_id not in persisted
            ]
        )
        if checkpoint is not None:
            self.prompt_provider.current_instructions = checkpoint.opening_instructions
            self.prompt_provider.prompt_revision = checkpoint.prompt_revision
        if (
            getattr(self.ctx.service, "get_worker_prompt", None) is not None
            or self.prompt_provider.current_instructions
        ):
            instructions = await self.prompt_provider.refresh(
                board if "task" in board else self.ctx.state, step=step
            )
            # The opening provider's instructions are already in these options.
            cast(dict[str, Any], context.options)["instructions"] = instructions
        if self.ctx.task_type in {"explore", "derive"}:
            last_seen = max(last_seen, self.last_appended_version)
            events = await self.ctx.board.events(
                self.ctx.task_id, since=last_seen, for_agent=self.ctx.agent_id
            )
            lines = _render_events(events, self.ctx.agent_id, self.ctx.params.delta_max_lines)
            if (
                self.ctx.task_type == "explore"
                and agent.get("conclude_requested_at")
                and not self.conclude_injected
            ):
                reason = agent.get("conclude_reason") or "closing"
                lines.insert(
                    0,
                    f"[结束指令] 原因：{reason}。立即停止探索；最多再调用 "
                    f"{self.ctx.params.conclude_grace_calls} 次 release / post_fact / post_intent "
                    "完成交接，然后返回回执 JSON。",
                )
                self.conclude_injected = True
            if self.ctx.task_type == "explore" and not agent.get("conclude_requested_at"):
                reminder = await self._publication_reminder(step, last_seen)
                if reminder:
                    lines.append(reminder)
            last_seen = max([last_seen, *(int(event["version"]) for event in events)])
            if lines:
                update = "[黑板更新]\n" + "\n".join(lines)
                append_board_update(context.messages, update)
                await record_trace(self.ctx, "board_update", step, update + "\n[黑板更新结束]")
                self.last_appended_version = last_seen
        await self._inject_user_messages(context)
        input_ids = {message.message_id for message in inputs}
        inputs.extend(
            deepcopy(message)
            for message in context.messages
            if message.role == "user"
            and message.message_id not in persisted
            and message.message_id not in input_ids
        )
        context.messages = list(context.messages)
        messages = context.messages
        injection = await append_pending_images(self.ctx, messages)
        requested_at = datetime.now(UTC)
        started_at = monotonic()
        model = getattr(self.ctx.profile.models, self.ctx.task_type)
        permit: ModelCallPermit | None = None
        deadline = self.recovery_deadline
        deadline_metadata = self.recovery_metadata
        completed_model = False

        def complete_model() -> None:
            nonlocal completed_model
            completed_model = True
            if self.recovery and permit:
                self.recovery.succeeded(permit)
                self.recovery_deadline = None

        async def trace_response(response: ChatResponse) -> None:
            output, reasoning = model_content(response)
            await record_trace(self.ctx, "model_output", step, output, reasoning=reasoning)

        async def before_model() -> None:
            nonlocal permit, deadline, deadline_metadata, requested_at, started_at
            if self.ctx.checkpoint is not None and self.ctx.checkpoint.session.state.get(
                "bbx_usage_outbox"
            ):
                await flush_ordinary_usage(self.ctx.checkpoint)
            if self.recovery:
                delayed = self.recovery.paused and (
                    permit is None or permit.episode is not self.recovery.episode
                )
                # Control preflight and lazy stream construction are not an
                # in-flight model request. Re-check admission immediately before
                # both call setup and consumption, retaining a probe reservation.
                if permit is None or permit.episode is not self.recovery.episode:
                    if permit:
                        self.recovery.release(permit)
                    permit = await self.recovery.acquire(
                        self.check_recovery_control, deadline=self.recovery_deadline
                    )
                if delayed and self.ctx.task_type == "explore" and not self.conclude_injected:
                    current = self._recovery_agent
                    if current.get("conclude_requested_at"):
                        note = (
                            f"[结束指令] 原因：{current.get('conclude_reason') or 'closing'}。"
                            "立即停止探索；仅完成允许的交接，然后返回回执 JSON。"
                        )
                        append_board_update(context.messages, note)
                        self.conclude_injected = True
                self.recovery.started(permit)
                deadline = self.recovery_deadline
                if permit.episode:
                    deadline = (
                        min(deadline, permit.episode.deadline)
                        if deadline is not None
                        else permit.episode.deadline
                    )
                    deadline_metadata = self.recovery_metadata or permit.episode.metadata
            requested_at = datetime.now(UTC)
            started_at = monotonic()
            if self.ctx.checkpoint is not None:
                selected, _ = effective_price(
                    model, requested_at, mode_override=board.get("task", {}).get("billing_mode")
                )
                self.ctx.checkpoint.ordinary_usage_request = {
                    "price": selected,
                    "provider": model.provider,
                    "on_complete": complete_model,
                    "trace_response": trace_response,
                    "heartbeat": {
                        "steps": 1,
                        "last_seen_version": last_seen,
                        "requested_at": requested_at,
                        "expected_run_number": self.ctx.state["task"].get("run_number") or 1,
                        **(
                            {"expected_started_at": self.ctx.expected_started_at}
                            if self.ctx.expected_started_at is not None
                            else {}
                        ),
                        "expected_derive_round": (
                            self.ctx.expected_derive_round
                            or board["agents"][self.ctx.agent_id].get("derive_round")
                            or 1
                        )
                        if self.ctx.task_type == "derive"
                        else None,
                    },
                    "scope": {
                        "task_id": self.ctx.task_id,
                        "agent_id": self.ctx.agent_id,
                        "task_type": self.ctx.task_type,
                        "profile_name": self.ctx.state["task"]["agent_profile"],
                        "profile_version": self.ctx.state["task"]["agent_profile_version"],
                        "billing_mode": board.get("task", {}).get("billing_mode"),
                    },
                }

        async def record_error(error: Exception) -> None:
            if isinstance(error, RecoveryExhausted) or getattr(
                error, "bbx_usage_settlement_error", False
            ):
                return
            metadata = model_error_metadata(
                error,
                elapsed_ms=int((monotonic() - started_at) * 1000),
                messages=messages,
                attempt_limit=model_attempt_limit(model.provider),
            )
            try:
                vars(error)["bbx_model_error"] = metadata
            except Exception:
                pass
            if self.recovery and permit:
                if recoverable_transport(metadata):
                    episode = self.recovery.failed(permit, metadata)
                    self.failure = ModelCallFailure(metadata, inputs, episode, step)
                else:
                    self.recovery.abort(
                        permit,
                        metadata,
                        hold_dispatch=metadata.get("scope") == "provider",
                    )
            await record_trace(self.ctx, "model_error", step, json.dumps(metadata, sort_keys=True))

        async def record(response: ChatResponse) -> ChatResponse:
            if self.ctx.checkpoint is None:
                complete_model()
            details = response.usage_details
            price, _ = effective_price(
                model, requested_at, mode_override=board.get("task", {}).get("billing_mode")
            )
            usage, self.price_warning = _usage(details, price, model.provider)
            if self.price_warning and not self._warned_missing_price:
                logger.warning("Model price is not configured; cost is recorded as zero")
                self._warned_missing_price = True
            heartbeat: dict[str, Any] = dict(
                steps=1,
                context_tokens=_input_tokens(details, model.provider),
                usage=usage,
                last_seen_version=last_seen,
                requested_at=requested_at,
                **(
                    {"expected_started_at": self.ctx.expected_started_at}
                    if self.ctx.expected_started_at is not None
                    else {}
                ),
                **(
                    {"expected_derive_round": self.ctx.expected_derive_round}
                    if self.ctx.expected_derive_round is not None
                    else {}
                ),
            )
            if self.ctx.checkpoint is None:
                await self.ctx.service.heartbeat(self.ctx.task_id, self.ctx.agent_id, **heartbeat)
            if self.ctx.checkpoint is None:
                await trace_response(response)
            return response

        try:
            await before_model()
        except BaseException:
            finish_pending_images(messages, injection, sent=completed_model)
            if self.recovery and permit:
                self.recovery.release(permit)
            raise
        timeout = asyncio.timeout(max(0, deadline - monotonic()) if deadline else None)
        try:
            async with timeout:
                await call_next()
        except BaseException as error:
            finish_pending_images(messages, injection, sent=completed_model)
            if self.recovery and permit:
                self.recovery.release(permit)
            if isinstance(error, TimeoutError) and timeout.expired():
                raise RecoveryExhausted(deadline_metadata) from None
            if isinstance(error, Exception):
                await record_error(error)
            raise
        response = context.result
        if isinstance(response, ResponseStream):
            inner = response

            async def updates():
                sent = False
                try:
                    await before_model()
                except BaseException:
                    finish_pending_images(messages, injection, sent=False)
                    if self.recovery and permit:
                        self.recovery.release(permit)
                    raise
                read_timeout = asyncio.timeout(max(0, deadline - monotonic()) if deadline else None)
                try:
                    async with read_timeout:
                        async for update in inner:
                            yield update
                        result = await inner.get_final_response()
                    sent = True
                except BaseException as error:
                    if self.recovery and permit:
                        self.recovery.release(permit)
                    if isinstance(error, TimeoutError) and read_timeout.expired():
                        raise RecoveryExhausted(deadline_metadata) from None
                    if isinstance(error, Exception):
                        await record_error(error)
                    raise
                finally:
                    finish_pending_images(messages, injection, sent=sent or completed_model)
                await record(result)

            context.result = ResponseStream(
                updates(), finalizer=lambda _: inner.get_final_response()
            )
        else:
            finish_pending_images(messages, injection, sent=True)
            if isinstance(response, ChatResponse):
                await record(response)
