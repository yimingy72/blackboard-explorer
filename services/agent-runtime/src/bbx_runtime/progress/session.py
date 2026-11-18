"""Use the durable conversation checkpoint with an independent progress fence."""

import json
from typing import Any, cast

from bbx_contracts.model_limits import effective_max_input_tokens
from bbx_contracts.models import ModelConfig

from bbx_runtime.clients.blackboard import BlackboardClient
from bbx_runtime.ctf.context import (
    align_history_start,
    estimate_messages_tokens,
    fixed_prompt_tokens,
)
from bbx_runtime.pi import AgentSession, Content, Message
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


def require_resolved_calls(checkpoint: SessionCheckpoint, service: ProgressClient) -> None:
    assert service.job is not None
    state = checkpoint.session.state
    if state.get("bbx_progress_active_request") is not None or any(
        receipt.get("job_id") == str(service.job["job_id"]) and not receipt.get("settled")
        for receipt in state.get("bbx_progress_receipts", {}).values()
    ):
        raise ProgressUsageUnknown(
            "Prior model execution requires confirmation; it will not be replayed"
        )


class ProgressHistoryProvider(CheckpointHistoryProvider):
    """Keep complete history while bounding the model's view at batch boundaries."""

    def __init__(
        self,
        checkpoint: SessionCheckpoint,
        service: ProgressClient,
        model: ModelConfig,
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
        require_resolved_calls(self.checkpoint, self.service)
        messages = await super().get_messages(session_id, state=state, **kwargs)
        input_limit = effective_max_input_tokens(self.model)
        threshold = int(input_limit * 0.8) if input_limit is not None else None
        start = int(self.checkpoint.session.state.get("bbx_progress_history_start", 0))
        if (
            threshold is not None
            and self.fixed_tokens
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
        if response is not None and active is not None and active.get("pi_direct"):
            # The sender already settled before exposing this response.
            session.state.pop("bbx_progress_active_request", None)
        # The shared provider saves the exact model messages before tools.
        await super().after_run(agent=agent, session=session, context=context, state=state)
        require_resolved_calls(self.checkpoint, self.service)
