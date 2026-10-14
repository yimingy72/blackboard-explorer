"""Stable recoverable board errors and content-free validation feedback."""

from typing import Any, get_args

from pydantic import ValidationError
from pydantic_core.core_schema import ErrorType

_ERROR_TYPES = frozenset(get_args(ErrorType))
_SOLUTION_FIELDS = frozenset(
    {
        "solution",
        "conclusion",
        "approach",
        "steps",
        "kind",
        "title",
        "result",
        "summary",
        "record_ids",
        "artifact_ids",
        "remaining",
        "reproduction",
        "<field>",
    }
)

# Only domain refusals which a caller can reconsider with the current board state.
# Authentication, fencing, budget, task phase and uncertain execution stay fatal.
CTF_BOARD_ERROR_CODES = {
    "Reopen the challenge first": "challenge_reopen_required",
    "Reopen the challenge before requesting help": "challenge_reopen_required",
    "Challenge is not available": "challenge_unavailable",
    "Challenge is deleted": "challenge_deleted",
    "Challenge is not closed": "challenge_not_closed",
    "Challenge needs an owner": "challenge_needs_owner",
    "Member is unavailable": "member_unavailable",
    "Only the owner or Lead can update work status": "challenge_owner_required",
    "Only the owner or Lead can release": "challenge_owner_required",
    "Only the owner or Lead can reopen": "challenge_owner_required",
    "Only the owner or Lead can deliver a solution": "challenge_owner_required",
    "Only Lead can assign an owner": "lead_required",
    "Only Lead can set collaborators": "lead_required",
    "Only Lead can delete a challenge": "lead_required",
    "Only the teammate owner can request Lead assistance": "teammate_owner_required",
    (
        "Only Lead, owner or collaborators may append records. Read get_challenge; "
        "ask Lead to add you to collaborator_ids, or send_message the existing owner "
        "to relay your findings. record_candidate requires the same assignment."
    ): "challenge_assignment_required",
    "Artifact is not registered in this task": "artifact_not_registered",
    "Artifacts must not belong exclusively to another challenge": "artifact_challenge_mismatch",
    "Solution records must belong to this challenge": "solution_record_mismatch",
    (
        "Instruction recipient must own or collaborate on the updated challenge"
    ): "assignment_recipient_mismatch",
}


def validation_issues(error: ValidationError, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose bounded schema field paths and error types, never input values or messages."""
    names: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                names.update(properties)
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(schema)
    issues = []
    for item in error.errors(include_input=False, include_context=False, include_url=False)[:8]:
        # Unknown/extra keys may contain user input; only schema names can appear.
        path = [
            part if type(part) is int or part in names else "<field>" for part in item["loc"][:12]
        ]
        kind = item["type"] if item["type"] in _ERROR_TYPES else "value_error"
        issues.append({"path": path, "type": kind})
    return issues


def solution_validation_details(value: Any) -> list[dict[str, Any]]:
    """Validate remote field feedback without accepting raw validation diagnostics."""
    issues = []
    for row in value[:8] if isinstance(value, list) else []:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("type"), str)
            or row["type"] not in _ERROR_TYPES
        ):
            continue
        path = row.get("path")
        if not isinstance(path, list) or len(path) > 12:
            continue
        if all(
            type(part) is int
            and 0 <= part <= 1000
            or isinstance(part, str)
            and part in _SOLUTION_FIELDS
            for part in path
        ):
            issues.append({"path": path, "type": row["type"]})
    return issues
