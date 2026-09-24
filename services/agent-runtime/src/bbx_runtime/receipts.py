"""Parse the final agent reply into a task-specific receipt."""

import json
from typing import Any, Literal

from bbx_contracts.models import CloseReceipt, DeriveReceipt, ExploreReceipt, RefusalReceipt
from pydantic import ValidationError


def _last_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((end, -index, value))
    return max(candidates)[2] if candidates else None


def parse_receipt(text: str, task_type: Literal["explore", "derive", "close"]) -> dict[str, Any]:
    """Use the last complete JSON object; keep invalid raw text for task logs."""
    value = _last_object(text)
    if value is not None:
        model = {
            "explore": ExploreReceipt,
            "derive": DeriveReceipt,
            "close": CloseReceipt,
        }[task_type]
        try:
            receipt = RefusalReceipt if value.get("accepted") is False else model
            return receipt.model_validate(value).model_dump(mode="json")
        except ValidationError:
            pass
    return {"accepted": True, "data": {"note": "回执格式错误"}, "raw_text": text}
