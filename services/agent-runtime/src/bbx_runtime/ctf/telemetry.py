"""Persist CTF observations without inventing blackboard roles or identities."""

import asyncio
import json
import logging
from time import monotonic
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from agent_framework import Content, FunctionInvocationContext, FunctionMiddleware
from bbx_contracts.storage import storage_safe
from bbx_contracts.tool_errors import validation_issues
from pydantic import TypeAdapter, ValidationError

from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.failures import CtfToolFailure, tool_failure_cause
from bbx_runtime.ctf.tool_results import tool_error_content

logger = logging.getLogger(__name__)
_JSON_ARGUMENTS = TypeAdapter(Any)


class ToolArgumentFeedbackMiddleware(FunctionMiddleware):
    """Use MAF's own Pydantic input model for bounded, value-free field feedback."""

    async def process(self, context: FunctionInvocationContext, call_next: Any) -> None:
        model = context.function.input_model
        if model is not None:
            try:
                model.model_validate(context.arguments)
            except ValidationError as error:
                context.result = [
                    tool_error_content(
                        {
                            "code": "invalid_arguments",
                            "message": "Correct the listed fields using the tool schema. "
                            "Use UUIDs for record/artifact references; omit unavailable "
                            "references rather than inventing them.",
                            "fields": validation_issues(error, model.model_json_schema()),
                        }
                    )
                ]
                return
        await call_next()


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
        started = monotonic()
        try:
            payload = {
                "tool": context.function.name,
                "call_id": str(occurrence),
                # MAF normalizes nested models to Python values (including UUIDs).
                # Serialize only the observation; preserve the tool's typed arguments.
                "arguments": _JSON_ARGUMENTS.dump_python(context.arguments, mode="json"),
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
        except Exception as error:
            raise CtfToolFailure(error) from error
        try:
            await call_next()
        except BaseException as error:
            original = tool_failure_cause(error) or error
            try:
                await observe(
                    self.service,
                    self.task_id,
                    self.member_id,
                    self.turn,
                    "tool_result",
                    {
                        **payload,
                        "error_type": type(original).__name__,
                        "elapsed_ms": round((monotonic() - started) * 1000),
                    },
                    f"{key}:result",
                )
            except asyncio.CancelledError:
                # Do not replace an already-cancelled tool with a second cancellation
                # of best-effort telemetry. New cancellation still wins otherwise.
                if not isinstance(error, asyncio.CancelledError):
                    raise
            except Exception as observation_error:
                logger.warning(
                    "CTF failed tool observation task=%s member=%s turn_id=%s generation=%s "
                    "error_type=%s observation_error_type=%s",
                    self.task_id,
                    self.member_id,
                    self.turn["id"],
                    self.turn["generation"],
                    type(original).__name__,
                    type(observation_error).__name__,
                )
            if isinstance(error, Exception) and not isinstance(error, CtfToolFailure):
                # Recoverable board refusals are explicit Content results. Everything
                # else must leave the native loop, including failed checkpoint ACKs.
                raise CtfToolFailure(error) from error
            raise
        try:
            result = context.result
            error_code = None
            if isinstance(result, list):
                error_code = next(
                    (
                        item.additional_properties.get("ctf_tool_error")
                        for item in result
                        if isinstance(item, Content) and item.additional_properties
                    ),
                    None,
                )
                result = [item.to_dict() if isinstance(item, Content) else item for item in result]
            await observe(
                self.service,
                self.task_id,
                self.member_id,
                self.turn,
                "tool_result",
                {
                    **payload,
                    "result": json.dumps(result, ensure_ascii=False, default=str),
                    "elapsed_ms": round((monotonic() - started) * 1000),
                    **({"tool_error_code": error_code} if error_code else {}),
                },
                f"{key}:result",
            )
        except Exception as error:
            raise CtfToolFailure(error) from error
