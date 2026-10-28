"""Thin task-board tools; ownership, CAS and atomic help requests stay server-side."""

from typing import Annotated, Any, Literal
from uuid import uuid4

from agent_framework import tool
from bbx_contracts.ctf import (
    CTF_BOARD_RESULT_MAX_BYTES,
    CtfAssignmentInstruction,
    CtfChallengeAssignItem,
    CtfChallengeBatchAssignRequest,
    CtfChallengeBatchCreateRequest,
    CtfChallengeCreateRequest,
    CtfProgress,
    CtfSolution,
    CtfWorkStatus,
)
from pydantic import Field

from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.tool_results import ToolJsonResult, json_tool_result


def build_board_tools(
    service: CtfClient,
    task_id: str,
    member_id: str,
    turn: dict[str, Any],
    artifacts: Any = None,
    *,
    role: str = "teammate",
) -> list[Any]:
    token = turn["token"]

    @tool
    async def list_challenges(
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        owner_id: str | None = None,
        work_status: CtfWorkStatus | None = None,
        cursor: str | None = None,
        max_bytes: Annotated[int, Field(ge=1024, le=CTF_BOARD_RESULT_MAX_BYTES)] = 6000,
    ) -> ToolJsonResult:
        """Read a bounded index of IDs, owners, statuses and revisions; no full descriptions.

        Filter by owner_id/work_status. Follow next_cursor with the same filters and
        offset=0; do not combine cursor and offset. A page may contain fewer than limit
        entries because max_bytes bounds its complete JSON. Read get_challenge by ID
        for the exact description, requirements and evidence. Do not repeatedly scan
        the whole board when an assignment already names the challenge. This is a live
        index, not a frozen snapshot; start a new cursor sequence when changing filters.
        """
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "list_challenges",
                view="summary",
                offset=offset,
                limit=limit,
                max_bytes=max_bytes,
                **({"owner_id": owner_id} if owner_id is not None else {}),
                **({"work_status": work_status} if work_status is not None else {}),
                **({"cursor": cursor} if cursor is not None else {}),
            )
        )

    @tool
    async def get_challenge(challenge_id: str) -> ToolJsonResult:
        """Read one challenge's complete description, requirements and current revision."""
        return await json_tool_result(
            service.tool(task_id, token, "get_challenge", challenge_id=challenge_id)
        )

    @tool
    async def create_challenges(
        items: Annotated[list[CtfChallengeCreateRequest], Field(min_length=1, max_length=20)],
    ) -> ToolJsonResult:
        """Lead: register a small batch of independently deliverable challenges.

        Each item needs its own stable request_id UUID. Each item commits independently;
        inspect every receipt. If delivery is uncertain, resend the identical item with
        the same request_id to read its committed outcome, never a fresh ID. Correct a
        rejected item before a new request. Do not bundle independent goals into one
        challenge or wait for the full inventory before assigning ready work.
        """
        request = CtfChallengeBatchCreateRequest(items=items)
        return await json_tool_result(
            service.tool(task_id, token, "create_challenges", **request.model_dump(mode="json"))
        )

    @tool
    async def assign_challenges(
        items: Annotated[list[CtfChallengeAssignItem], Field(min_length=1, max_length=20)],
    ) -> ToolJsonResult:
        """Lead: assign a small batch, with one atomic assignment and instruction per item.

        Each item has a stable request_id UUID, challenge_id, expected_revision, owner_id
        and instructions. Include scope, completion requirements and existing evidence
        in each instruction. Partial errors do not undo successful items. Resend an
        uncertain identical item with its original request_id; after a revision conflict,
        read get_challenge and reconsider before a new request. Do not send duplicate
        instruction messages after successful atomic dispatch.
        """
        request = CtfChallengeBatchAssignRequest(items=items)
        return await json_tool_result(
            service.tool(task_id, token, "assign_challenges", **request.model_dump(mode="json"))
        )

    @tool
    async def create_challenge(
        title: Annotated[str, Field(min_length=1, max_length=200)],
        description: str = "",
        connection: str | None = None,
        external_id: str | None = None,
        requirements: str | None = None,
    ) -> ToolJsonResult:
        """Create an unowned task-board entry; this does not assign or wake a member.

        Preserve supplied challenge text and delivery requirements faithfully; reference
        complete source materials when needed. Never invent missing original text.
        Put the connection in connection without repeating it throughout description.
        """
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "create_challenge",
                request_id=str(uuid4()),
                title=title,
                description=description,
                connection=connection,
                external_id=external_id,
                requirements=requirements,
            )
        )

    @tool
    async def update_challenge(
        challenge_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        action: Literal[
            "claim", "assign", "set_status", "release", "reopen", "collaborators", "delete"
        ],
        owner_id: Annotated[str | None, Field(min_length=1)] = None,
        collaborator_ids: list[str] | None = None,
        work_status: Literal["in_progress", "blocked", "completed", "cancelled"] | None = None,
        instructions: list[CtfAssignmentInstruction] | None = None,
        solution: Annotated[
            CtfSolution | None,
            Field(description="Public solution delivery; at most 1600 text characters in total."),
        ] = None,
    ) -> ToolJsonResult:
        """Update with the current revision; optionally dispatch work atomically.

        claim assigns yourself. assign requires owner_id; collaborators requires
        collaborator_ids; set_status requires work_status. Only provide the field
        required by the chosen action. release/reopen/delete require none of them.
        assign/collaborators/delete require Lead; status/release/reopen need owner or Lead.
        Lead may include instructions with assign/collaborators to register permissions
        and send work in one transaction. Each recipient must be the resulting owner
        or collaborator. Omit instructions to update only the board without waking anyone.
        set_status completed requires solution in this same call, submitted by owner or
        Lead. Helpers cannot complete work. Lead must name the actual author and source
        when relaying delivery. Use existing same-challenge records and registered
        artifact IDs; summarize public findings, not private step-by-step reasoning.
        Include remaining blockers honestly; do not invent success or breakthroughs.
        Omit solution for other statuses. Stopping or budget/error closure needs no solution.
        On revision_conflict, get_challenge and reconsider before another update.
        """
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "update_challenge",
                request_id=str(uuid4()),
                challenge_id=challenge_id,
                expected_revision=expected_revision,
                action=action,
                owner_id=owner_id,
                collaborator_ids=collaborator_ids,
                work_status=work_status,
                **(
                    {"solution": CtfSolution.model_validate(solution).model_dump(mode="json")}
                    if solution is not None
                    else {}
                ),
                **(
                    {
                        "instructions": [
                            CtfAssignmentInstruction.model_validate(item).model_dump(mode="json")
                            for item in instructions
                        ]
                    }
                    if instructions is not None
                    else {}
                ),
            )
        )

    @tool
    async def list_records(
        challenge_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        after_version: Annotated[int | None, Field(ge=0)] = None,
    ) -> ToolJsonResult:
        """Read shared attempts, help details and registered artifacts in version order.

        Set after_version to the last seen created_version to read only newer records.
        Keep after_version fixed across a page sequence and follow next_offset.
        Only after reading all pages, use the highest fully read created_version for
        the next sequence, starting at offset=0. Never advance both cursor and offset.
        """
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "list_records",
                challenge_id=challenge_id,
                offset=offset,
                limit=limit,
                **({"after_version": after_version} if after_version is not None else {}),
            )
        )

    @tool
    async def append_record(
        challenge_id: str,
        body: Annotated[str, Field(min_length=1)],
        kind: Literal["note", "correction"] = "note",
        artifact_ids: Annotated[
            list[str] | None, Field(description="Artifact IDs returned by register_artifact.")
        ] = None,
        progress: CtfProgress | None = None,
    ) -> ToolJsonResult:
        """Append an immutable observation using registered artifact IDs, not file paths.

        Only Lead, the challenge owner or registered collaborators may append records.
        Read get_challenge first. If unassigned, ask Lead to add you to collaborator_ids,
        or send_message the existing owner to relay your findings.
        record_candidate requires the same assignment and cannot bypass this permission.
        Add short progress for meaningful approaches, effective or failed attempts,
        real breakthroughs and results. Keep the detailed basis in body and use
        registered artifact IDs. Stages need not be linear; do not invent a breakthrough.
        """
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "append_record",
                request_id=str(uuid4()),
                challenge_id=challenge_id,
                body=body,
                kind=kind,
                artifact_ids=artifact_ids or [],
                **(
                    {"progress": CtfProgress.model_validate(progress).model_dump(mode="json")}
                    if progress is not None
                    else {}
                ),
            )
        )

    @tool
    async def register_artifact(path: Annotated[str, Field(min_length=1)]) -> ToolJsonResult:
        """Persist a canonical own/shared /workspace file; return its registered artifact ID.

        Use this ID in append_record.artifact_ids and record_candidate.evidence_refs.
        """
        if artifacts is None:
            raise RuntimeError("Task execution storage is not connected")
        return await json_tool_result(
            artifacts.register(task_id, member_id, turn, path, str(uuid4()))
        )

    @tool
    async def request_help(
        challenge_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        body: Annotated[str, Field(min_length=1)],
        attempted_routes: Annotated[str, Field(min_length=1)],
        observations_and_basis: Annotated[str, Field(min_length=1)],
        failure_conditions: Annotated[str, Field(min_length=1)],
        current_blocker: Annotated[str, Field(min_length=1)],
        help_needed: Annotated[str, Field(min_length=1)],
        artifact_ids: Annotated[
            list[str] | None, Field(description="Artifact IDs returned by register_artifact.")
        ] = None,
        no_artifacts_reason: str | None = None,
    ) -> ToolJsonResult:
        """Only the teammate owner can block the challenge and request Lead help atomically.

        Register necessary scripts first and provide their artifact IDs. Without artifacts,
        no_artifacts_reason must explicitly explain why none exist. Lead owners cannot use this.
        The owner remains responsible while helpers append their own records.
        """
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "request_help",
                request_id=str(uuid4()),
                challenge_id=challenge_id,
                expected_revision=expected_revision,
                body=body,
                attempted_routes=attempted_routes,
                observations_and_basis=observations_and_basis,
                failure_conditions=failure_conditions,
                current_blocker=current_blocker,
                help_needed=help_needed,
                artifact_ids=artifact_ids or [],
                no_artifacts_reason=no_artifacts_reason,
            )
        )

    return [
        list_challenges,
        get_challenge,
        create_challenge,
        update_challenge,
        list_records,
        append_record,
        register_artifact,
        request_help,
        *([create_challenges, assign_challenges] if role == "lead" else []),
    ]
