"""Use the native MAF Session checkpoint with an independent progress fence."""

import json
from typing import Any, cast

from agent_framework import AgentSession, Content, Message
from bbx_contracts.models import Price

from bbx_runtime.billing import _usage, _usage_known
from bbx_runtime.clients.blackboard import BlackboardClient
from bbx_runtime.ctf.context import (
    align_history_start,
    estimate_messages_tokens,
    fixed_prompt_tokens,
)
from bbx_runtime.progress.client import ProgressClient
from bbx_runtime.session import CheckpointHistoryProvider, SessionCheckpoint


class ProgressUsageUnknown(RuntimeError):
    """Stop model dispatch while provider execution or its usage is uncertain."""


class ProgressSessionClient:
    def __init__(self, service: ProgressClient, source_version: int) -> None:
        self.service = service
        self.source_version = source_version
        assert service.job is not None
        self.knowledge_version = int(service.job["through_version"])

    async def get_agent_session(self, _task_id: str, _agent_id: str) -> Any:
        saved = await self.service.session()
        return {**saved, "origin": "native"}

    async def put_agent_session(self, _task_id: str, _agent_id: str, **payload: Any) -> Any:
        payload.pop("deliveries", None)
        payload.pop("review_claim", None)
        payload["origin"] = "progress"
        saved = await self.service.operation(
            "checkpoint",
            source_version=self.source_version,
            knowledge_version=self.knowledge_version,
            **payload,
        )
        return {**saved, "origin": "native"}


def checkpoint_for(
    service: ProgressClient, job: dict[str, Any], instructions: str
) -> SessionCheckpoint:
    saved = job.get("session")
    # Search, object reads and evidence can reveal the entire fixed job ceiling.
    # This knowledge fence is deliberately independent of the scan cursor.
    adapter = ProgressSessionClient(service, int(job["source_version"]))
    return SessionCheckpoint(
        cast(BlackboardClient, adapter),
        str(job["task_id"]),
        "progress",
        AgentSession.from_dict(saved["session"]) if saved else AgentSession(),
        opening_instructions=instructions,
        revision=int(saved["revision"]) if saved else 0,
    )


async def flush_receipts(checkpoint: SessionCheckpoint, service: ProgressClient) -> None:
    assert service.job is not None
    receipts = checkpoint.session.state.setdefault("bbx_progress_receipts", {})
    for request_id, receipt in receipts.items():
        if receipt["job_id"] != str(service.job["job_id"]) or receipt.get("settled"):
            continue
        if receipt.get("status") != "completed":
            raise ProgressUsageUnknown("Model request has no durable completed response")
        result = await service.operation(
            "settle",
            request_id=request_id,
            usage=receipt["usage"],
            usage_known=receipt["usage_known"],
            executed=True,
        )
        if result.get("status") != "settled":
            raise ProgressUsageUnknown("Model usage requires confirmation")
        receipt["settled"] = True
    active = checkpoint.session.state.get("bbx_progress_active_request")
    if active is not None:
        raise ProgressUsageUnknown(
            "Prior model execution requires confirmation; it will not be replayed"
        )


class ProgressHistoryProvider(CheckpointHistoryProvider):
    """Keep complete history while bounding the model's view at batch boundaries."""

    def __init__(
        self,
        checkpoint: SessionCheckpoint,
        service: ProgressClient,
        model: Any,
        tree: dict[str, Any],
        instructions: str,
        tools: list[Any],
    ) -> None:
        super().__init__(checkpoint)
        self.service, self.model, self.tree = service, model, tree
        self.fixed_tokens = fixed_prompt_tokens(instructions, tools)
        self.batch_message_id = ""

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        await flush_receipts(self.checkpoint, self.service)
        messages = await super().get_messages(session_id, state=state, **kwargs)
        window = self.model.context_window or 65536
        threshold = max(1024, int(window * 0.55))
        start = int(self.checkpoint.session.state.get("bbx_progress_history_start", 0))
        if (
            self.fixed_tokens
            + estimate_messages_tokens(messages[start:], provider=self.model.provider)
            > threshold
        ):
            boundary = next(
                (
                    i
                    for i, message in enumerate(messages)
                    if message.message_id == self.batch_message_id
                ),
                len(messages),
            )
            aligned = align_history_start(messages, boundary)
            if aligned is not None and aligned > start:
                start = aligned
                self.checkpoint.session.state["bbx_progress_history_start"] = start
                await self.checkpoint.save()
        if not start:
            return messages
        snapshot = Message(
            role="user",
            contents=[
                Content.from_text(
                    "已处理的较早批次原文仍保存在持久会话；本轮输入视图改用覆盖全局认识的候选树。"
                    "不要遗忘此前方向、反例或限制；必要时用受限来源工具复查。\n"
                    + json.dumps(self.tree, ensure_ascii=False, default=str)
                )
            ],
        )
        return [snapshot, *messages[start:]]

    async def after_run(
        self, *, agent: Any, session: AgentSession, context: Any, state: dict[str, Any]
    ) -> None:
        active = session.state.get("bbx_progress_active_request")
        response = context.response
        if response is not None and active is not None:
            details = response.usage_details
            usage, warning = _usage(
                details, Price.model_validate(active["price"]), self.model.provider
            )
            session.state.setdefault("bbx_progress_receipts", {})[active["request_id"]] = {
                "job_id": active["job_id"],
                "status": "completed",
                "usage": usage.model_dump(mode="json"),
                "usage_known": _usage_known(details, self.model.provider),
                "raw_usage": dict(details or {}),
                "pricing": active["pricing"],
                "warning": warning,
                "settled": False,
            }
            session.state.pop("bbx_progress_active_request", None)
        # The shared provider saves the model messages and receipt before billing or tools.
        await super().after_run(agent=agent, session=session, context=context, state=state)
        await flush_receipts(self.checkpoint, self.service)
