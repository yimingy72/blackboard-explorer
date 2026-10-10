"""Reconcile the durable mailbox before MAF's native injection boundary."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from agent_framework import (
    ChatContext,
    ChatMiddleware,
    Content,
    Message,
    MessageInjectionMiddleware,
)
from bbx_contracts.billing import effective_price
from bbx_contracts.models import ModelConfig

from bbx_runtime.billing import _usage
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.telemetry import observe
from bbx_runtime.session import CheckpointHistoryProvider, SessionCheckpoint

PENDING_KEY = "message_injection.pending_messages"


class TurnBoundary(RuntimeError):
    """The platform has closed admission to another model call."""


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
        if self.steps >= self.max_steps:
            raise TurnBoundary("step_limit")
        reservation_id = f"{self.turn['id']}:{self.steps + 1}"
        await self.service.runtime(
            self.checkpoint.task_id,
            "authorize_review" if self.turn.get("purpose") == "review" else "authorize_member",
            agent_id=self.checkpoint.agent_id,
            turn_id=self.turn["id"],
            generation=self.turn["generation"],
            **(
                {"reservation_id": reservation_id, "reservation_cost": str(self.reservation_cost)}
                if self.reservation_cost is not None and self.turn.get("purpose") != "review"
                else {}
            ),
        )
        await self.reconcile()
        self.steps += 1
        self.checkpoint.session.state.setdefault("ctf_steps", {})[str(self.turn["id"])] = self.steps
        if self.reservation_cost is not None and self.turn.get("purpose") != "review":
            await self.service.runtime(
                self.checkpoint.task_id,
                "mark_reservation_sent",
                agent_id=self.checkpoint.agent_id,
                turn_id=self.turn["id"],
                generation=self.turn["generation"],
                reservation_id=reservation_id,
            )
        await call_next()


class CtfHistoryProvider(CheckpointHistoryProvider):
    def __init__(
        self,
        service: CtfClient,
        checkpoint: SessionCheckpoint,
        turn: dict[str, Any],
        model: ModelConfig,
        context_threshold: int = 128000,
    ) -> None:
        super().__init__(checkpoint)
        self.service, self.turn, self.model = service, turn, model
        self.context_threshold = context_threshold
        self.usage_key = (
            "ctf_review_usage_outbox" if turn.get("purpose") == "review" else "ctf_usage_outbox"
        )

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        messages = await super().get_messages(session_id, state=state, **kwargs)
        # Conservative character estimate, with whole completed turns removed only
        # from the model view. Durable history and tool pairs remain unchanged.
        limit = max(1, int(self.context_threshold * 0.8))
        start = 0
        while sum(len(str(message.to_dict())) for message in messages[start:]) > limit:
            boundary = next(
                (
                    i + 1
                    for i in range(start, len(messages) - 1)
                    if (
                        messages[i].role == "tool"
                        or (
                            messages[i].role == "assistant"
                            and not any(c.type == "function_call" for c in messages[i].contents)
                        )
                    )
                ),
                None,
            )
            if boundary is None:
                raise TurnBoundary("context_limit")
            start = boundary
        self.checkpoint.session.state["ctf_history_view"] = {
            "excluded_before": start,
            "total_messages": len(messages),
        }
        return messages[start:]

    async def after_run(
        self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]
    ) -> None:
        response = context.response
        details = response.usage_details if response is not None else None
        step = session.state.get("ctf_steps", {}).get(str(self.turn["id"]), 0)
        if details:
            price, _ = effective_price(self.model, datetime.now(UTC))
            usage, _ = _usage(details, price, self.model.provider)
            # Persist request identity and usage alongside history before billing. Recovery
            # flushes this outbox idempotently, including a lost billing response.
            session.state.setdefault(self.usage_key, []).append(
                {
                    "request_id": str(uuid4()),
                    "reservation_id": f"{self.turn['id']}:{step}",
                    "usage": usage.model_dump(mode="json"),
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
                },
                f"{self.turn['id']}:model:{step}",
            )

    async def flush_usage(self) -> None:
        entries = self.checkpoint.session.state.get(self.usage_key, [])
        for entry in entries:
            await self.service.runtime(
                self.checkpoint.task_id,
                "bill_usage",
                agent_id=self.checkpoint.agent_id,
                turn_id=self.turn["id"],
                generation=self.turn["generation"],
                **entry,
            )
        if entries:
            entries.clear()
            await self.checkpoint.save()
