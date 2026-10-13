"""Reconcile the durable mailbox before MAF's native injection boundary."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import Any
from uuid import uuid4

from agent_framework import (
    GROUP_ANNOTATION_KEY,
    ChatContext,
    ChatMiddleware,
    Content,
    Message,
    MessageInjectionMiddleware,
)
from bbx_contracts.billing import effective_price
from bbx_contracts.models import ModelConfig

from bbx_runtime.billing import _input_tokens, _usage
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.context import (
    align_history_start,
    estimate_message_lengths,
    estimate_messages_tokens,
    input_token_limit,
    message_signature,
    safe_history_boundaries,
    stable_tool_previews,
    visible_message_text,
)
from bbx_runtime.ctf.telemetry import observe
from bbx_runtime.session import CheckpointHistoryProvider, SessionCheckpoint

logger = logging.getLogger(__name__)

PENDING_KEY = "message_injection.pending_messages"


class TurnBoundary(RuntimeError):
    """The platform has closed admission to another model call."""


async def flush_usage_outbox(
    service: CtfClient, checkpoint: SessionCheckpoint, turn: dict[str, Any]
) -> None:
    key = "ctf_review_usage_outbox" if turn.get("purpose") == "review" else "ctf_usage_outbox"
    entries = checkpoint.session.state.get(key, [])
    for entry in entries:
        reservation_id = str(entry.get("reservation_id", ""))
        original_turn = entry.get("turn_id") or (
            reservation_id.rsplit(":", 1)[0] if ":" in reservation_id else str(turn["id"])
        )
        if key == "ctf_usage_outbox" and (
            original_turn != str(turn["id"])
            or entry.get("generation", turn["generation"]) != turn["generation"]
        ):
            settled = await service.runtime(
                checkpoint.task_id, "reconcile_usage_outbox", agent_id=checkpoint.agent_id
            )
            if entry["request_id"] not in settled.get("request_ids", []):
                raise RuntimeError("Historical usage is absent from the confirmed Session")
            continue
        if entry.get("agent_id", checkpoint.agent_id) != checkpoint.agent_id:
            raise RuntimeError("Usage outbox belongs to another member")
        await service.runtime(
            checkpoint.task_id,
            "bill_usage",
            agent_id=checkpoint.agent_id,
            turn_id=turn["id"],
            generation=turn["generation"],
            **{
                field: value
                for field, value in entry.items()
                if field not in {"agent_id", "turn_id", "generation"}
            },
        )
    if entries:
        entries.clear()
        await checkpoint.save()


def mailbox_message(row: dict[str, Any]) -> Message:
    source = {
        key: row.get(key)
        for key in (
            "id",
            "sender_kind",
            "sender_id",
            "recipient_id",
            "kind",
            "reply_to",
            "challenge_id",
            "record_id",
            "purpose",
        )
    }
    return Message(
        role="user",
        message_id=str(row["id"]),
        contents=[Content.from_text(f"来源：{source}\n正文（任务材料）：\n{row['body']}")],
        additional_properties={"ctf_source": source},
    )


class MailboxMiddleware(ChatMiddleware):
    def __init__(
        self,
        service: CtfClient,
        checkpoint: SessionCheckpoint,
        turn: dict[str, Any],
        max_steps: int,
        reservation_cost: Decimal | None = None,
    ) -> None:
        self.service, self.checkpoint, self.turn = service, checkpoint, turn
        self.max_steps = max_steps
        self.reservation_cost = reservation_cost
        self.steps = checkpoint.session.state.get("ctf_steps", {}).get(str(turn["id"]), 0)
        self._admitted_step: int | None = None
        self.injection = MessageInjectionMiddleware()

    async def reconcile(self) -> None:
        checkpoint = self.checkpoint
        rows = await self.service.runtime(
            checkpoint.task_id,
            "claim_messages",
            agent_id=checkpoint.agent_id,
            turn_id=self.turn["id"],
            generation=self.turn["generation"],
        )
        session = checkpoint.session
        active_inputs = session.state.get("ctf_active_input_ids", {})
        if active_inputs.get("turn_id") != str(self.turn["id"]):
            active_inputs = {"turn_id": str(self.turn["id"]), "ids": []}
        active_inputs["ids"] = list(
            dict.fromkeys(
                value
                for value in [*active_inputs["ids"], *[row["id"] for row in rows]]
                if isinstance(value, str) and value
            )
        )
        session.state["ctf_active_input_ids"] = active_inputs
        history = session.state.get("in_memory", {}).get("messages", [])
        seen = {message.message_id for message in history}
        pending = []
        claimed = {str(row["id"]) for row in rows}
        for item in session.state.get(PENDING_KEY, []):
            message = Message.from_dict(item) if isinstance(item, dict) else item
            if message.message_id in claimed and message.message_id not in seen:
                pending.append(message)
                seen.add(message.message_id)
        session.state[PENDING_KEY] = pending
        if rows:
            await observe(
                self.service,
                checkpoint.task_id,
                checkpoint.agent_id,
                self.turn,
                "turn_context",
                {
                    "trigger_message_ids": [str(row["id"]) for row in rows],
                    "session_revision": checkpoint.revision,
                },
                f"{self.turn['id']}:inputs:" + ":".join(str(row["id"]) for row in rows),
            )
        for row in rows:
            message_id = str(row["id"])
            if not any(item["id"] == message_id for item in checkpoint.deliveries):
                checkpoint.stage_delivery(message_id, str(row["claim_token"]))
            if message_id not in seen:
                self.injection.enqueue_messages(session, mailbox_message(row))
                seen.add(message_id)

    async def process(self, context: ChatContext, call_next: Any) -> None:
        await self.authorize_call()
        await self.reconcile()
        await call_next()

    async def authorize_call(self) -> None:
        if self.steps >= self.max_steps:
            raise TurnBoundary("step_limit")
        reservation_id = f"{self.turn['id']}:{self.steps + 1}"
        wait_seconds = 0.2
        while True:
            try:
                await self.service.runtime(
                    self.checkpoint.task_id,
                    "authorize_review"
                    if self.turn.get("purpose") == "review"
                    else "authorize_member",
                    agent_id=self.checkpoint.agent_id,
                    turn_id=self.turn["id"],
                    generation=self.turn["generation"],
                    **(
                        {
                            "reservation_id": reservation_id,
                            "reservation_cost": str(self.reservation_cost),
                        }
                        if self.reservation_cost is not None
                        and self.turn.get("purpose") != "review"
                        else {}
                    ),
                )
                self._admitted_step = self.steps + 1
                break
            except RemoteError as error:
                if error.status != 409 or self.turn.get("purpose") == "review":
                    raise
                if error.code == "ctf_budget_wait":
                    # Keep this turn and its input intact while in-flight usage settles.
                    await asyncio.sleep(wait_seconds)
                    wait_seconds = min(wait_seconds * 2, 2.0)
                    continue
                if error.code != "ctf_budget_exhausted":
                    raise
                try:
                    await self.service.runtime(
                        self.checkpoint.task_id,
                        "request_finish",
                        actor="system",
                        request_id=str(uuid4()),
                        conclusion={
                            "end_reason": "budget_exhausted",
                            "summary": "Task budget cannot admit another model call",
                            "unresolved_items": [],
                            "evidence_refs": [],
                            "lead_claim": False,
                        },
                    )
                except RemoteError as close_error:
                    if close_error.status != 409:
                        raise
                    task = (await self.service.runtime_state(self.checkpoint.task_id))["task"]
                    if task["ctf_control"]["phase"] not in {"closing", "closed"}:
                        raise
                raise TurnBoundary("budget_exhausted") from error

    async def enter_provider(self) -> None:
        # Native injection can make another leaf call inside the same chat middleware.
        if self._admitted_step != self.steps + 1:
            await self.authorize_call()
        if self.checkpoint.session.state.get("ctf_history_view", {}).get("epoch", 0) != getattr(
            self.checkpoint, "confirmed_context_epoch", 0
        ):
            await self.checkpoint.save()
        if self.reservation_cost is not None and self.turn.get("purpose") != "review":
            await self.service.runtime(
                self.checkpoint.task_id,
                "mark_reservation_sent",
                agent_id=self.checkpoint.agent_id,
                turn_id=self.turn["id"],
                generation=self.turn["generation"],
                reservation_id=f"{self.turn['id']}:{self.steps + 1}",
            )
        self.steps += 1
        self.checkpoint.session.state.setdefault("ctf_steps", {})[str(self.turn["id"])] = self.steps
        self.checkpoint.session.state["ctf_model_call"] = {
            "turn_id": str(self.turn["id"]),
            "step": self.steps,
            "started_at": datetime.now(UTC).isoformat(),
            "_started_monotonic": monotonic(),
            "context_view": dict(self.checkpoint.session.state.get("ctf_model_view", {})),
        }


class CtfHistoryProvider(CheckpointHistoryProvider):
    def __init__(
        self,
        service: CtfClient,
        checkpoint: SessionCheckpoint,
        turn: dict[str, Any],
        model: ModelConfig,
        context_threshold: int = 128000,
        *,
        fixed_input_tokens: int = 0,
    ) -> None:
        super().__init__(checkpoint)
        self.service, self.turn, self.model = service, turn, model
        self.context_threshold = context_threshold
        self.input_limit = input_token_limit(model, context_threshold)
        self.fixed_input_tokens = fixed_input_tokens
        self._loaded_messages: list[Message] = []
        self._loaded_signatures: list[str] = []
        self._loaded_start = 0
        self._loaded_prefix_count = 0
        self._confirmed_count = 0
        self.usage_key = (
            "ctf_review_usage_outbox" if turn.get("purpose") == "review" else "ctf_usage_outbox"
        )

    def prepare_provider_messages(self, outgoing: Sequence[Message]) -> list[Message]:
        """Bound model-visible input while preserving every prior preview decision."""
        messages = stable_tool_previews(outgoing, self.checkpoint.session.state, self.input_limit)
        estimated = self.fixed_input_tokens + estimate_messages_tokens(
            messages, provider=self.model.provider
        )
        if estimated > int(self.input_limit * 0.9):
            compacted = self._compact_confirmed_prefix(outgoing, messages)
            if compacted is not None:
                messages = compacted
                estimated = self.fixed_input_tokens + estimate_messages_tokens(
                    messages, provider=self.model.provider
                )
        if estimated > self.input_limit:
            self.block_context(
                messages,
                self.input_limit,
                self.checkpoint.session.state.get("ctf_history_view", {}).get("excluded_before", 0),
            )
            raise TurnBoundary("context_blocked")
        self.checkpoint.session.state["ctf_model_view"] = {
            "compaction": dict(self.checkpoint.session.state.get("ctf_context_compaction", {})),
            "message_count": len(messages),
            "estimated_chars": sum(
                len(visible_message_text(message, provider=self.model.provider))
                for message in messages
            ),
            "estimated_input_tokens": estimated,
            "input_limit_tokens": self.input_limit,
            "fixed_input_tokens": self.fixed_input_tokens,
            "excluded_message_count": self.checkpoint.session.state.get("ctf_history_view", {}).get(
                "excluded_before", 0
            ),
            "context_epoch": self.checkpoint.session.state.get("ctf_history_view", {}).get(
                "epoch", 0
            ),
            "truncated_tool_result_count": sum(
                content.type == "function_result"
                and isinstance(content.result, str)
                and content.result.startswith("[Truncated model preview:")
                for message in messages
                for content in message.contents
            ),
        }
        return messages

    def _compact_confirmed_prefix(
        self, outgoing: Sequence[Message], messages: list[Message]
    ) -> list[Message] | None:
        """Only the exact confirmed prefix loaded for this leaf may be evicted."""
        durable = self.checkpoint.session.state
        diagnostic = {
            "branch": "no_loaded_prefix",
            "eligible": 0,
            "cut": 0,
            "remaining": self.fixed_input_tokens
            + estimate_messages_tokens(messages, provider=self.model.provider),
            "input_limit": self.input_limit,
            "protected_hits": [],
        }
        durable["ctf_context_compaction"] = diagnostic
        if not self._loaded_messages or len(outgoing) < len(self._loaded_messages):
            return None
        for index, saved in enumerate(self._loaded_messages):
            current = outgoing[index]
            attribution = current.additional_properties.get("_attribution")
            if (
                not saved.contents
                or len(saved.contents) != len(current.contents)
                or any(a is not b for a, b in zip(saved.contents, current.contents, strict=True))
                or message_signature(current) != self._loaded_signatures[index]
                or (
                    current is not saved
                    and (
                        not isinstance(attribution, dict)
                        or attribution.get("source_id") != self.source_id
                    )
                )
            ):
                diagnostic["branch"] = "loaded_prefix_mismatch"
                return None
        diagnostic["branch"] = "unconfirmed_prefix"
        raw = durable.get("in_memory", {}).get("messages", [])
        confirmed = getattr(self.checkpoint, "confirmed_history", ())
        if (
            self._confirmed_count <= self._loaded_start
            or self._confirmed_count > len(raw)
            or tuple(message_signature(message) for message in raw[: self._confirmed_count])
            != confirmed[: self._confirmed_count]
            or durable.get("ctf_history_view", {}).get("excluded_before", 0) != self._loaded_start
        ):
            return None
        seed = durable.get("ctf_context_recovery_seed")
        if not seed:
            diagnostic["branch"] = "missing_recovery_seed"
            return None
        prefix = messages[: self._loaded_prefix_count] or [
            seed if isinstance(seed, Message) else Message.from_dict(seed)
        ]
        body = messages[self._loaded_prefix_count :]
        lengths = estimate_message_lengths(body, provider=self.model.provider)
        eligible = self._confirmed_count - self._loaded_start
        protected = self._protected_input_ids()
        protected_hits = []
        for index, message in enumerate(raw[self._loaded_start : self._confirmed_count]):
            if message.message_id in protected:
                protected_hits.append(index)
                # Only plain confirmed user inputs can be moved without detaching replay groups.
                if not self._can_relocate_input(message):
                    eligible = min(eligible, index)
        diagnostic["eligible"] = eligible
        diagnostic["protected_hits"] = protected_hits
        overhead = self.fixed_input_tokens + estimate_messages_tokens(
            prefix, provider=self.model.provider
        )
        remaining = overhead + sum(lengths)
        target = self.fixed_input_tokens + int((self.input_limit - self.fixed_input_tokens) * 0.6)
        cut = 0
        recovered_inputs = self._recovered_inputs()
        pinned = list(recovered_inputs)
        diagnostic["branch"] = "no_safe_boundary"
        for boundary in safe_history_boundaries(body, 0):
            if boundary > eligible:
                break
            if align_history_start(raw, self._loaded_start + boundary) != (
                self._loaded_start + boundary
            ):
                diagnostic["branch"] = "unsafe_tool_boundary"
                return None
            moved = [
                message
                for message in raw[self._loaded_start : self._loaded_start + boundary]
                if message.message_id in protected
            ]
            pinned = [
                *recovered_inputs,
                *[
                    message
                    for message in moved
                    if message.message_id not in {item.message_id for item in recovered_inputs}
                ],
            ]
            # Relocated inputs stay whole and count against the same hard input budget.
            remaining = (
                overhead
                + sum(lengths[boundary:])
                + estimate_messages_tokens(
                    pinned[len(recovered_inputs) :], provider=self.model.provider
                )
            )
            cut = boundary
            diagnostic["cut"] = cut
            diagnostic["remaining"] = remaining
            if remaining <= target:
                break
        if not cut or remaining > self.input_limit:
            diagnostic["branch"] = "no_safe_boundary" if not cut else "remaining_over_limit"
            return None
        diagnostic["branch"] = "compacted"
        prefix = [*prefix, *pinned[len(recovered_inputs) :]]
        previous = durable["ctf_history_view"]
        durable["ctf_history_view"] = {
            "excluded_before": self._loaded_start + cut,
            "total_messages": len(raw),
            "epoch": previous.get("epoch", 0) + 1,
        }
        durable["ctf_context_recovery"] = {
            "turn_id": str(self.turn["id"]),
            "message": durable["ctf_context_recovery_seed"],
            "protected_inputs": [message.to_dict() for message in pinned],
        }
        return [*prefix, *body[cut:]]

    @staticmethod
    def _can_relocate_input(message: Message) -> bool:
        return (
            message.role == "user"
            and bool(message.contents)
            and all(content.type == "text" for content in message.contents)
            and not message.additional_properties.get(GROUP_ANNOTATION_KEY)
        )

    def _recovered_inputs(self) -> list[Message]:
        recovery = self.checkpoint.session.state.get("ctf_context_recovery", {})
        return [
            item if isinstance(item, Message) else Message.from_dict(item)
            for item in recovery.get("protected_inputs", [])
        ]

    def _protected_input_ids(self) -> set[str]:
        active = self.checkpoint.session.state.get("ctf_active_input_ids", {})
        values = {
            *self.turn.get("assignment_message_ids", []),
            *getattr(self.checkpoint, "delivered_message_ids", set()),
            *[item["id"] for item in getattr(self.checkpoint, "deliveries", [])],
            *(active.get("ids", []) if active.get("turn_id") == str(self.turn["id"]) else []),
        }
        return {value for value in values if isinstance(value, str) and value}

    def _confirmed_history_count(self, messages: Sequence[Message]) -> int:
        count = 0
        for message, signature in zip(
            messages, getattr(self.checkpoint, "confirmed_history", ()), strict=False
        ):
            if message_signature(message) != signature:
                break
            count += 1
        return count

    async def _recovery_seed(self) -> Message:
        durable = self.checkpoint.session.state
        seed = durable.get("ctf_context_recovery_seed")
        if seed:
            return seed if isinstance(seed, Message) else Message.from_dict(seed)
        recovery = durable.get("ctf_context_recovery")
        if recovery:
            seed = recovery["message"]
            seed = seed.to_dict() if isinstance(seed, Message) else seed
        else:
            snapshot = await self.service.runtime_state(self.checkpoint.task_id)
            task = snapshot.get("task", {})
            summary = {"task_id": self.checkpoint.task_id, "goal": task.get("goal", "")}
            seed = Message(
                role="user",
                message_id=f"ctf-context-recovery:{self.turn['id']}",
                contents=[
                    Content.from_text(
                        "Context recovery: earlier conversation remains in the saved Session. "
                        "Use list_challenges, list_records and list_members to read current "
                        "blackboard progress before continuing. Do not assume omitted actions "
                        "succeeded. Saved task snapshot: " + json.dumps(summary, ensure_ascii=False)
                    )
                ],
            ).to_dict()
        durable["ctf_context_recovery_seed"] = seed
        return Message.from_dict(seed)

    def block_context(
        self, messages: list[Message], limit: int, start: int, *, branch: str | None = None
    ) -> None:
        durable = self.checkpoint.session.state
        diagnostic = dict(durable.get("ctf_context_compaction", {}))
        diagnostic.setdefault("eligible", 0)
        diagnostic.setdefault("cut", 0)
        diagnostic.setdefault("protected_hits", [])
        diagnostic["branch"] = branch or diagnostic.get("branch", "unshrinkable_loaded_history")
        diagnostic["remaining"] = self.fixed_input_tokens + estimate_messages_tokens(
            messages, provider=self.model.provider
        )
        diagnostic["input_limit"] = limit
        logger.warning(
            "CTF context blocked turn_id=%s generation=%s diagnostics=%s",
            self.turn["id"],
            self.turn.get("generation"),
            diagnostic,
        )
        durable["ctf_context_blocked"] = {
            **diagnostic,
            "last_provider_usage": durable.get("ctf_last_provider_usage"),
            "input_message_ids": sorted(self._protected_input_ids()),
            "turn_id": str(self.turn["id"]),
            "excluded_before": start,
            "limit_tokens": limit,
            "estimated_input_tokens": self.fixed_input_tokens
            + estimate_messages_tokens(messages, provider=self.model.provider),
            "remaining_chars": sum(
                len(visible_message_text(message, provider=self.model.provider))
                for message in messages
            ),
            "message_ids": [
                message.message_id
                for message in messages
                if isinstance(message.message_id, str) and message.message_id
            ],
            "fingerprint": hashlib.sha256(
                (
                    str(limit)
                    + str(
                        [
                            visible_message_text(message, provider=self.model.provider)
                            for message in messages
                        ]
                    )
                ).encode()
            ).hexdigest(),
        }

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        messages = await super().get_messages(session_id, state=state, **kwargs)
        durable = self.checkpoint.session.state
        previous = durable.get("ctf_history_view", {})
        start = min(max(0, int(previous.get("excluded_before", 0))), len(messages))
        epoch = int(previous.get("epoch", 0))
        view = stable_tool_previews(messages, durable, self.input_limit)
        confirmed_count = self._confirmed_history_count(messages)
        protected = self._protected_input_ids()
        recovery = durable.get("ctf_context_recovery")
        prefix = []
        if recovery:
            recovered = recovery["message"]
            prefix = [
                recovered if isinstance(recovered, Message) else Message.from_dict(recovered),
                *self._recovered_inputs(),
            ]
        blocked = durable.get("ctf_context_blocked", {})
        new_turn = bool(blocked and blocked.get("turn_id") != str(self.turn["id"]))
        aligned = align_history_start(view, start)
        if aligned is None and not new_turn:
            self.block_context(
                [*prefix, *view[start:]], self.input_limit, start, branch="unsafe_loaded_boundary"
            )
            raise TurnBoundary("context_blocked")
        if (aligned is None and confirmed_count != len(messages)) or (
            aligned is not None and aligned != start and aligned > confirmed_count
        ):
            self.block_context(
                [*prefix, *view[start:]], self.input_limit, start, branch="unsafe_loaded_boundary"
            )
            raise TurnBoundary("context_blocked")
        migrated = aligned != start
        # An unfinished legacy group has no valid replay suffix. Only a new
        # admitted recovery turn may bypass it; no original tool is re-executed.
        start = len(messages) if aligned is None else aligned
        limit = max(
            0,
            self.input_limit
            - self.fixed_input_tokens
            - estimate_messages_tokens(prefix, provider=self.model.provider),
        )
        lengths = estimate_message_lengths(view, provider=self.model.provider)
        remaining = sum(lengths[start:])
        eligible = confirmed_count
        for index, message in enumerate(messages[start:confirmed_count], start=start):
            if message.message_id in protected and not self._can_relocate_input(message):
                eligible = index
                break
        boundaries = [
            boundary for boundary in safe_history_boundaries(view, start) if boundary <= eligible
        ]
        seed = await self._recovery_seed() if new_turn or migrated or boundaries else None
        if new_turn or migrated or (not prefix and remaining > limit and boundaries):
            # Historical inputs remain durable; newly claimed mailbox inputs are
            # appended later and still checked in full at the provider boundary.
            assert seed is not None
            recovered = seed
            prefix = [recovered, *self._recovered_inputs()]
            durable["ctf_context_recovery"] = {
                "turn_id": str(self.turn["id"]),
                "message": durable["ctf_context_recovery_seed"],
                "protected_inputs": [message.to_dict() for message in prefix[1:]],
            }
            limit = max(
                0,
                self.input_limit
                - self.fixed_input_tokens
                - estimate_messages_tokens(prefix, provider=self.model.provider),
            )
        if remaining > limit:
            # Evict down to a low watermark once, instead of changing the prefix
            # on every model call. Never split parallel calls/results.
            original_start = start
            previous_inputs = self._recovered_inputs()
            for boundary in boundaries:
                moved = [
                    message
                    for message in view[original_start:boundary]
                    if message.message_id in protected
                ]
                pinned = [
                    *previous_inputs,
                    *[
                        message
                        for message in moved
                        if message.message_id not in {item.message_id for item in previous_inputs}
                    ],
                ]
                candidate_prefix = [prefix[0], *pinned] if prefix else []
                candidate_limit = max(
                    0,
                    self.input_limit
                    - self.fixed_input_tokens
                    - estimate_messages_tokens(candidate_prefix, provider=self.model.provider),
                )
                candidate_remaining = sum(lengths[boundary:])
                if candidate_remaining <= candidate_limit:
                    start, remaining, limit = boundary, candidate_remaining, candidate_limit
                    prefix = candidate_prefix
                    durable["ctf_context_recovery"] = {
                        "turn_id": str(self.turn["id"]),
                        "message": durable["ctf_context_recovery_seed"],
                        "protected_inputs": [message.to_dict() for message in pinned],
                    }
                    if remaining <= int(limit * 0.6):
                        break
            if (
                remaining > limit
                and new_turn
                and eligible == len(messages)
                and not any(message.message_id in protected for message in messages[start:])
            ):
                # Only a new admission can bypass a prior unshrinkable input.
                # Newly claimed inputs cannot be silently discarded here.
                start = len(messages)
                remaining = 0
            if remaining > limit:
                self.checkpoint.session.state["ctf_context_compaction"] = {
                    "branch": "unshrinkable_loaded_history",
                    "eligible": eligible - start,
                    "cut": start - original_start,
                    "protected_hits": [
                        index
                        for index, message in enumerate(messages[start:confirmed_count])
                        if message.message_id in protected
                    ],
                }
                self.block_context([*prefix, *view[start:]], self.input_limit, start)
                raise TurnBoundary("context_blocked")
        if start != previous.get("excluded_before", 0) or new_turn:
            epoch += 1
        durable["ctf_history_view"] = {
            "excluded_before": start,
            "total_messages": len(messages),
            "epoch": epoch,
        }
        durable.pop("ctf_context_blocked", None)
        result = [*prefix, *view[start:]]
        self._loaded_messages = result
        self._loaded_signatures = [message_signature(message) for message in result]
        self._loaded_start = start
        self._loaded_prefix_count = len(prefix)
        self._confirmed_count = confirmed_count
        return result

    async def after_run(
        self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]
    ) -> None:
        response = context.response
        details = response.usage_details if response is not None else None
        step = session.state.get("ctf_steps", {}).get(str(self.turn["id"]), 0)
        call = session.state.get("ctf_model_call", {})
        timing = {}
        if call.get("turn_id") == str(self.turn["id"]) and call.get("step") == step:
            timing = {
                "started_at": call["started_at"],
                "elapsed_ms": max(0, int((monotonic() - call["_started_monotonic"]) * 1000)),
                "context_view": call["context_view"],
            }
        successful_usage = None
        if details:
            price, _ = effective_price(self.model, datetime.now(UTC))
            usage, _ = _usage(details, price, self.model.provider)
            successful_usage = usage.model_dump(mode="json")
            session.state["ctf_last_provider_usage"] = {
                "turn_id": str(self.turn["id"]),
                "step": step,
                "provider": self.model.provider,
                "input_tokens": _input_tokens(details, self.model.provider)
                if details.get("input_token_count") is not None
                else None,
                "output_tokens": int(details.get("output_token_count") or 0),
                "response_id": response.response_id if response is not None else None,
                "context_view": timing.get("context_view", {}),
            }
            # Persist request identity and usage alongside history before billing. Recovery
            # flushes this outbox idempotently, including a lost billing response.
            session.state.setdefault(self.usage_key, []).append(
                {
                    "request_id": str(uuid4()),
                    "reservation_id": f"{self.turn['id']}:{step}",
                    "agent_id": self.checkpoint.agent_id,
                    "turn_id": str(self.turn["id"]),
                    "generation": self.turn["generation"],
                    "usage": successful_usage,
                }
            )
        await super().after_run(agent=agent, session=session, context=context, state=state)
        await self.flush_usage()
        if response is not None:
            await observe(
                self.service,
                self.checkpoint.task_id,
                self.checkpoint.agent_id,
                self.turn,
                "model_output",
                {
                    "text": response.text,
                    "step": step,
                    "response_id": response.response_id,
                    "message_ids": [m.message_id for m in response.messages],
                    "usage": successful_usage,
                    **timing,
                },
                f"{self.turn['id']}:model:{step}",
            )

    async def flush_usage(self) -> None:
        await flush_usage_outbox(self.service, self.checkpoint, self.turn)
