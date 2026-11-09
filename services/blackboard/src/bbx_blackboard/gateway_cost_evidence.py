"""Validate attributed native fee evidence for every AgentGateway execution purpose."""

from decimal import Decimal, InvalidOperation

from fastapi import HTTPException


def gateway_cost_evidence(record, expected, key_name):
    attrs = record.get("attributes", {})
    if not isinstance(attrs, dict) or not isinstance(record.get("id"), str):
        raise ValueError("Native log has an unsupported shape")
    if any(str(attrs.get(key)) != value for key, value in expected.items()):
        raise HTTPException(409, "Native log attribution does not match its original attempt")
    if attrs.get("agw.api_key.name") != key_name:
        raise HTTPException(409, "Native log belongs to another gateway key")
    usage = record.get("usage") or {}
    if not isinstance(usage, dict):
        raise ValueError("Native log usage has an unsupported shape")
    try:
        amount = Decimal(str(record["cost"]))
        complete = (
            amount.is_finite()
            and amount >= 0
            and all(
                type(usage.get(field)) is int and usage[field] >= 0
                for field in ("inputTokens", "outputTokens", "totalTokens")
            )
        )
    except (KeyError, InvalidOperation):
        complete, amount = False, None
    evidence = {
        key: record.get(key)
        for key in (
            "id",
            "startedAt",
            "completedAt",
            "httpStatus",
            "error",
            "genAi",
            "usage",
            "cost",
        )
    }
    return {**evidence, "attributes": expected}, amount if complete else None
