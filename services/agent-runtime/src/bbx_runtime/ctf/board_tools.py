"""Thin task-board tools; ownership, CAS and atomic help requests stay server-side."""

from typing import Annotated, Any, Literal
from uuid import uuid4

from agent_framework import tool
from pydantic import Field

from bbx_runtime.ctf.client import CtfClient


def build_board_tools(
    service: CtfClient, task_id: str, member_id: str, turn: dict[str, Any], artifacts: Any = None
) -> list[Any]:
    token = turn["token"]

    @tool
    async def list_challenges(
        offset: Annotated[int, Field(ge=0)] = 0, limit: Annotated[int, Field(ge=1, le=500)] = 100
    ) -> str:
        """List the shared task board, including owners, collaborators and revisions."""
        return str(
            await service.tool(task_id, token, "list_challenges", offset=offset, limit=limit)
        )

    @tool
    async def get_challenge(challenge_id: str) -> str:
        """Read a challenge and its current revision before changing it."""
        return str(await service.tool(task_id, token, "get_challenge", challenge_id=challenge_id))

    @tool
    async def create_challenge(
        title: str,
        description: str = "",
        connection: str | None = None,
        external_id: str | None = None,
        requirements: str | None = None,
    ) -> str:
        """Create an unowned task-board entry; this does not assign or wake a member."""
        return str(
            await service.tool(
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
        expected_revision: int,
        action: Literal[
            "claim", "assign", "set_status", "release", "reopen", "collaborators", "delete"
        ],
        owner_id: str | None = None,
        collaborator_ids: list[str] | None = None,
        work_status: str | None = None,
    ) -> str:
        """CAS update. Claim is self-ownership; assign/collaborators require Lead. No wakeup."""
        return str(
            await service.tool(
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
    ) -> str:
        """Read shared attempts, failure conditions, help requests and registered artifact refs."""
        return str(
            await service.tool(
                task_id,
                token,
                "list_records",
                challenge_id=challenge_id,
                offset=offset,
                limit=limit,
            )
        )

    @tool
    async def append_record(
        challenge_id: str,
        body: str,
        kind: Literal["note", "correction"] = "note",
        artifact_ids: list[str] | None = None,
    ) -> str:
        """Append an immutable observation using registered artifact IDs, not file paths."""
        return str(
            await service.tool(
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
    async def register_artifact(path: str) -> str:
        """Persist an own/shared workspace file and return its trusted artifact ID for records."""
        if artifacts is None:
            raise RuntimeError("Task execution storage is not connected")
        return str(await artifacts.register(task_id, member_id, turn, path, str(uuid4())))

    @tool
    async def request_help(
        challenge_id: str,
        expected_revision: int,
        body: str,
        attempted_routes: str,
        observations_and_basis: str,
        failure_conditions: str,
        current_blocker: str,
        help_needed: str,
        artifact_ids: list[str] | None = None,
        no_artifacts_reason: str | None = None,
    ) -> str:
        """Owner records five help details, blocks the challenge and notifies Lead atomically.

        Register necessary scripts first. Explicitly state when no script exists.
        The owner remains responsible while helpers append their own records.
        """
        return str(
            await service.tool(
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
