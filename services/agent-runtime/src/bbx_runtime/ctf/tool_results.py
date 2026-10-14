"""Readable tool results and narrowly recoverable board errors."""

import json
from collections.abc import Awaitable
from typing import Any

from agent_framework import Content
from bbx_contracts.tool_errors import CTF_BOARD_ERROR_CODES, solution_validation_details

from bbx_runtime.clients.blackboard import RemoteError

ToolJsonResult = str | Content

_BOARD_ERROR_HINTS = {
    "challenge_reopen_required": "Read get_challenge. Owner or Lead must explicitly reopen a "
    "closed challenge before changing status, assigning work or requesting help.",
    "challenge_unavailable": "Read get_challenge: claim requires an unowned pending challenge. "
    "Ask Lead to assign or reopen it rather than repeating the claim.",
    "challenge_deleted": "This challenge was deleted. Read list_challenges and work only on "
    "an existing task-board entry; do not repeat writes to the deleted entry.",
    "challenge_not_closed": "Read get_challenge. Reopen only applies to completed or cancelled "
    "work; do not repeat reopen on an active challenge.",
    "challenge_needs_owner": "Read get_challenge and ask Lead to assign an available owner "
    "before updating work status.",
    "member_unavailable": "Read list_members. Choose an active member who is not stopping; "
    "only Lead can explicitly resume a stopped teammate.",
    "challenge_owner_required": "Read get_challenge. Only the current owner or Lead can "
    "complete, change status, release or reopen this work. Send your findings to the owner.",
    "lead_required": "Only Lead can assign owners, set collaborators or delete challenges. "
    "Send the request to Lead; do not repeat this action as a teammate.",
    "teammate_owner_required": "Only the assigned teammate owner can request Lead assistance. "
    "Read get_challenge and send findings to the owner or Lead.",
    "challenge_assignment_required": "Read get_challenge. Only Lead, the owner and registered "
    "collaborators can append records or candidate evidence. Ask Lead to register your "
    "collaboration, or send findings to the existing owner; do not bypass assignment.",
    "artifact_not_registered": "Use registered artifact IDs from this task, not workspace "
    "paths or invented IDs. Register the necessary file before citing it.",
    "artifact_challenge_mismatch": "The artifact is associated exclusively with another "
    "challenge. Read the challenge's records and cite evidence belonging to this work.",
    "solution_record_mismatch": "Every solution step record_id must identify an existing "
    "record on this challenge. Read list_records; do not invent IDs or use another challenge.",
    "assignment_recipient_mismatch": "Instruction recipients must be the resulting owner or "
    "registered collaborators. Read get_challenge and reconsider the assignment atomically.",
}


def tool_error_content(error: dict[str, Any]) -> Content:
    """Keep JSON feedback model-visible and carry an explicit public MAF failure marker."""
    return Content.from_text(
        json.dumps({"error": error}, ensure_ascii=False),
        additional_properties={"ctf_tool_error": error["code"]},
    )


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


def recoverable_tool_error(error: RemoteError) -> Content | None:
    """Translate only known domain refusals; all control/unknown failures remain fatal."""
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
    elif (
        error.status == 409
        and (code := error.code or CTF_BOARD_ERROR_CODES.get(error.message)) in _BOARD_ERROR_HINTS
    ):
        result = {"error": {"code": code, "message": _BOARD_ERROR_HINTS[code]}}
    elif error.status == 422 and error.code == "invalid_solution_delivery":
        result = {
            "error": {
                "code": "invalid_solution_delivery",
                "message": "Owner or Lead: completed requires solution in the same call. "
                "Supply conclusion, approach, 1-6 steps and reproduction per the schema; "
                "cite existing challenge records and registered artifact IDs. "
                "Omit solution for other statuses; helpers must not complete work. "
                "Read get_challenge before deciding whether to submit again.",
            }
        }
        if error.detail and error.detail.get("fields"):
            result["error"]["fields"] = solution_validation_details(error.detail["fields"])
    elif error.status == 422 and error.message in _ARGUMENT_ERRORS:
        result = {
            "error": {
                "code": "invalid_arguments",
                "message": "Check the tool schema and action-specific required fields.",
            }
        }
    else:
        return None
    return tool_error_content(result["error"])


async def json_tool_result(request: Awaitable[Any]) -> ToolJsonResult:
    try:
        result = await request
    except RemoteError as error:
        feedback = recoverable_tool_error(error)
        if feedback is None:
            raise
        return feedback
    return json.dumps(result, ensure_ascii=False)
