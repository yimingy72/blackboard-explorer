"""Settle only model usage already confirmed in a durable ordinary Session."""

import asyncio
from typing import TYPE_CHECKING

from bbx_contracts.heartbeat import (
    UNKNOWN_USAGE_WARNING,
    USAGE_RECEIPT_PROTOCOL,
    OrdinaryUsageReceipt,
    canonical_task_id,
    heartbeat_digest,
)

from bbx_runtime.clients.blackboard import is_control_transport_error

if TYPE_CHECKING:
    from bbx_runtime.session import SessionCheckpoint


async def flush_ordinary_usage(checkpoint: "SessionCheckpoint") -> None:
    entries = checkpoint.session.state.get("bbx_usage_outbox", [])
    if entries and (
        type(checkpoint.usage_receipt_protocol) is not int
        or checkpoint.usage_receipt_protocol != USAGE_RECEIPT_PROTOCOL
    ):
        error = ValueError("Blackboard has not confirmed support for idempotent usage receipts")
        vars(error)["bbx_usage_settlement_error"] = True
        raise error
    for saved in list(entries):
        receipt = OrdinaryUsageReceipt.model_validate(saved)
        if (
            receipt.task_id != canonical_task_id(checkpoint.task_id)
            or receipt.agent_id != checkpoint.agent_id
        ):
            raise ValueError("Usage receipt belongs to another conversation")
        request = receipt.request
        payload = request.model_dump(mode="json")
        result = None
        for attempt in range(3):
            try:
                result = await checkpoint.service.heartbeat(
                    checkpoint.task_id,
                    checkpoint.agent_id,
                    steps=request.steps,
                    context_tokens=request.context_tokens,
                    usage=request.usage,
                    last_seen_version=request.last_seen_version,
                    requested_at=request.requested_at,
                    expected_derive_round=request.expected_derive_round,
                    request_id=str(request.request_id),
                    expected_run_number=request.expected_run_number,
                    usage_known=request.usage_known,
                    **(
                        {"expected_started_at": request.expected_started_at}
                        if request.expected_started_at is not None
                        else {}
                    ),
                )
                break
            except Exception as error:
                if not is_control_transport_error(error) or attempt == 2:
                    vars(error)["bbx_usage_settlement_error"] = True
                    raise
                await asyncio.sleep(0.1 * 2**attempt)
        if (
            not isinstance(result, dict)
            or result.get("confirmed") is not True
            or result.get("request_id") != str(request.request_id)
            or result.get("receipt_digest")
            != heartbeat_digest(checkpoint.task_id, checkpoint.agent_id, payload)
        ):
            raise ValueError("Heartbeat lacks confirmed usage receipt")
        if result.get("usage_known", True) != request.usage_known:
            raise ValueError("Heartbeat usage completeness does not match its receipt")
        if not request.usage_known:
            # Retain the confirmed receipt across restart; duplicate settlement cannot rebill it.
            error = ValueError(UNKNOWN_USAGE_WARNING)
            vars(error)["bbx_usage_settlement_error"] = True
            raise error
        # Persist cleanup with the next required snapshot; old receipts are idempotent.
        entries.remove(saved)
