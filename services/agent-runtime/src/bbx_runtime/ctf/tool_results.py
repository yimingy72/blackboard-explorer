"""Readable tool results and narrowly recoverable board errors."""

import json
from collections.abc import Awaitable
from typing import Any

from bbx_runtime.clients.blackboard import RemoteError

_ARGUMENT_ERRORS = frozenset(
    {
        "Invalid identifier",
        "Message IDs must be UUIDs",
        "Valid challenge ID required",
        "Challenge is required",
        "Invalid CTF board request",
        "Invalid verification request",
        "Invalid pagination",
        "Invalid after_version",
        "Unexpected query arguments",
        "Unexpected artifact arguments",
        "Unknown challenge action",
        "Target member is required",
    }
)


async def json_tool_result(request: Awaitable[Any]) -> str:
    try:
        result = await request
    except RemoteError as error:
        if error.status == 404 and error.message == "Challenge not found":
            result: dict[str, Any] = {
                "error": {
                    "code": "challenge_not_found",
                    "message": "Use list_challenges to read this task's real challenge IDs, "
                    "then choose the relevant ID before calling this tool again.",
                }
            }
        elif error.status == 409 and error.code == "revision_conflict":
            result = {
                "error": {
                    "code": "revision_conflict",
                    "message": "Read get_challenge before deciding whether to update again.",
                }
            }
            revision = (error.detail or {}).get("current_revision")
            if type(revision) is int and revision >= 1:
                result["error"]["current_revision"] = revision
        elif error.status == 409 and error.code == "message_confirmation_conflict":
            result = {
                "error": {
                    "code": "message_confirmation_conflict",
                    "message": "Confirm relevant queued deferred messages from the resume notice. "
                    "Already confirmed active or delivered messages are safe to repeat; "
                    "cancelled, failed or inactive recipients cannot be resumed.",
                    "conflicts": (error.detail or {}).get("conflicts", []),
                }
            }
        elif error.status == 422 and error.message in _ARGUMENT_ERRORS:
            result = {
                "error": {
                    "code": "invalid_arguments",
                    "message": "Check the tool schema and action-specific required fields.",
                }
            }
        else:
            raise
    return json.dumps(result, ensure_ascii=False)
