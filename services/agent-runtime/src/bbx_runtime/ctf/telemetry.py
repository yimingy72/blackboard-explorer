"""Persist CTF observations without inventing blackboard roles or identities."""

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from agent_framework import FunctionInvocationContext, FunctionMiddleware
from bbx_contracts.storage import storage_safe
from pydantic import BaseModel

from bbx_runtime.ctf.client import CtfClient


async def observe(
    service: CtfClient,
    task_id: str,
    member_id: str,
    turn: dict[str, Any],
    kind: str,
    body: dict[str, Any],
    key: str,
) -> Any:
    return await service.runtime(
        task_id,
        "record_observation",
        agent_id=member_id,
        turn_id=turn["id"],
        generation=turn["generation"],
        kind=kind,
        body=storage_safe(body),
        request_id=str(uuid5(NAMESPACE_URL, f"ctf:{task_id}:{member_id}:{key}")),
    )


class ToolObservationMiddleware(FunctionMiddleware):
    def __init__(
        self, service: CtfClient, task_id: str, member_id: str, turn: dict[str, Any]
    ) -> None:
        self.service, self.task_id, self.member_id, self.turn = service, task_id, member_id, turn

    async def process(self, context: FunctionInvocationContext, call_next: Any) -> None:
        occurrence = context.metadata.get("function_call_occurrence_id") or context.metadata.get(
            "call_id"
        )
        key = f"{self.turn['id']}:{occurrence}"
        arguments = context.arguments
        payload = {
            "tool": context.function.name,
            "call_id": str(occurrence),
            "arguments": arguments.model_dump(mode="json")
            if isinstance(arguments, BaseModel)
            else dict(arguments),
        }
        await observe(
            self.service,
            self.task_id,
            self.member_id,
            self.turn,
            "tool_call",
            payload,
            f"{key}:call",
        )
        try:
            await call_next()
        except BaseException as error:
            await observe(
                self.service,
                self.task_id,
                self.member_id,
                self.turn,
                "tool_result",
                {**payload, "error_type": type(error).__name__},
                f"{key}:result",
            )
            raise
        await observe(
            self.service,
            self.task_id,
            self.member_id,
            self.turn,
            "tool_result",
            {**payload, "result": str(context.result)},
            f"{key}:result",
        )
