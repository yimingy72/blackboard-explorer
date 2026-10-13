"""Thin task-board tools; ownership, CAS and atomic help requests stay server-side."""

from typing import Annotated, Any, Literal
from uuid import uuid4

from agent_framework import tool
from pydantic import Field

from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.tool_results import json_tool_result


def build_board_tools(
    service: CtfClient, task_id: str, member_id: str, turn: dict[str, Any], artifacts: Any = None
) -> list[Any]:
    token = turn["token"]

    @tool
    async def list_challenges(
        offset: Annotated[int, Field(ge=0)] = 0, limit: Annotated[int, Field(ge=1, le=500)] = 100
    ) -> str:
        """List the shared task board, including owners, collaborators and revisions."""
        return await json_tool_result(
            service.tool(task_id, token, "list_challenges", offset=offset, limit=limit)
        )

    @tool
    async def get_challenge(challenge_id: str) -> str:
        """Read a challenge and its current revision before changing it."""
        return await json_tool_result(
            service.tool(task_id, token, "get_challenge", challenge_id=challenge_id)
        )

    @tool
    async def create_challenge(
        title: Annotated[str, Field(min_length=1, max_length=200)],
        description: str = "",
        connection: str | None = None,
        external_id: str | None = None,
        requirements: str | None = None,
    ) -> str:
        """Create an unowned task-board entry; this does not assign or wake a member."""
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
    ) -> str:
        """Update with the current revision; ownership changes do not wake members.

        claim assigns yourself. assign requires owner_id; collaborators requires
        collaborator_ids; set_status requires work_status. Only provide the field
        required by the chosen action. release/reopen/delete require none of them.
        assign/collaborators/delete require Lead; status/release/reopen need owner or Lead.
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
            )
        )

    @tool
    async def list_records(
        challenge_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        after_version: Annotated[int | None, Field(ge=0)] = None,
    ) -> str:
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
    ) -> str:
        """Append an immutable observation using registered artifact IDs, not file paths."""
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
            )
        )

    @tool
    async def register_artifact(path: Annotated[str, Field(min_length=1)]) -> str:
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
    ) -> str:
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
    ]
