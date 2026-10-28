"""Small role-specific tool surface; the backend independently enforces authority."""

from typing import Annotated, Any, Literal
from uuid import uuid4

from agent_framework import FunctionInvocationContext, tool
from bbx_contracts.ctf import CtfSolution
from pydantic import Field

from bbx_runtime.ctf.board_tools import build_board_tools
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.images import make_view_image_tool
from bbx_runtime.ctf.tool_results import ToolJsonResult, json_tool_result


def build_tools(
    service: CtfClient,
    task_id: str,
    turn: dict[str, Any],
    role: str,
    *,
    envd: Any = None,
    member_id: str = "",
    checkpoint_provider: Any = None,
    artifacts: Any = None,
    model: Any = None,
) -> list[Any]:
    token = turn["token"]
    envd_client = getattr(artifacts, "envd", None)
    view_image = (
        make_view_image_tool(
            service=service,
            task_id=task_id,
            member_id=member_id,
            envd=envd_client,
            objects=artifacts.objects_factory,
            checkpoint_provider=checkpoint_provider or (lambda: None),
            model=model,
        )
        if envd_client is not None and model is not None
        else None
    )

    @tool
    async def list_members() -> ToolJsonResult:
        """Read the shared team roster without private conversations."""
        return await json_tool_result(service.tool(task_id, token, "list_team"))

    @tool
    async def send_message(
        recipient_id: Annotated[str, Field(min_length=1)],
        body: Annotated[str, Field(min_length=1, max_length=20000)],
        instruction: bool = False,
        reply_to: str | None = None,
        challenge_id: str | None = None,
    ) -> ToolJsonResult:
        """Send a directed message with an explicit challenge reference when applicable.

        instruction explicitly assigns work. challenge_id is the existing board ID;
        include scope and record/artifact references in body. reply_to is the original
        message UUID. This does not assign ownership or change challenge permissions.
        """
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "post_message",
                recipient_id=recipient_id,
                body=body,
                message_id=str(uuid4()),
                kind="instruction" if instruction else "message",
                reply_to=reply_to,
                **({"challenge_id": challenge_id} if challenge_id is not None else {}),
            )
        )

    @tool
    async def read_turn_result(
        turn_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=16000)] = 2000,
    ) -> ToolJsonResult:
        """Read a page of a settled result_ref. Lead can read assignment results;
        members can read only their own turns. This never reads another private Session.
        """
        return await json_tool_result(
            service.tool(
                task_id, token, "read_turn_result", turn_id=turn_id, offset=offset, limit=limit
            )
        )

    @tool
    async def read_saved_tool_result(
        call_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=16000)] = 2000,
    ) -> ToolJsonResult:
        """Read a page of your saved tool result by the call_id in its preview.
        Use this to recover omitted content without executing the tool again.
        """
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "read_saved_tool_result",
                call_id=call_id,
                offset=offset,
                limit=limit,
            )
        )

    @tool
    async def list_deferred_messages(
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 5,
    ) -> ToolJsonResult:
        """Lead: inspect deferred input metadata before confirm_messages.
        Follow next_offset to inspect the next page. This does not release messages
        and does not expose teammates' private conversation bodies.
        """
        return await json_tool_result(
            service.tool(task_id, token, "list_deferred_messages", offset=offset, limit=limit)
        )

    @tool
    async def create_teammate(
        display_name: Annotated[str, Field(min_length=1, max_length=100)],
    ) -> ToolJsonResult:
        """Create a named teammate with a persistent session."""
        return await json_tool_result(
            service.tool(
                task_id, token, "create_member", display_name=display_name, request_id=str(uuid4())
            )
        )

    @tool
    async def finish_task(
        summary: str,
        unresolved_items: list[str],
        evidence_refs: list[str],
        lead_claim: bool = False,
    ) -> ToolJsonResult:
        """Register closing and return immediately; do not wait for this turn to drain."""
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "request_finish",
                request_id=str(uuid4()),
                conclusion={
                    "end_reason": "goal_claimed" if lead_claim else "partial",
                    "summary": summary,
                    "unresolved_items": unresolved_items,
                    "evidence_refs": evidence_refs,
                    "lead_claim": lead_claim,
                },
            )
        )

    @tool
    async def record_candidate(
        challenge_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        status: Literal["candidate", "unknown"],
        summary: Annotated[str, Field(min_length=1)],
        evidence_refs: Annotated[
            list[str] | None,
            Field(description="Registered artifact IDs only, never record IDs or file paths."),
        ] = None,
        work_status: Literal["in_progress", "blocked", "completed"] | None = None,
        solution: Annotated[
            CtfSolution | None,
            Field(description="Public solution delivery; at most 1600 text characters in total."),
        ] = None,
    ) -> ToolJsonResult:
        """Record a candidate/unknown claim with registered artifact IDs as evidence.

        Only Lead, the challenge owner or registered collaborators may declare candidates.
        Read get_challenge first. If unassigned, ask Lead to add you to collaborator_ids,
        or send_message the existing owner to relay your findings.
        Owner or Lead may explicitly set work_status in the same transaction.
        completed means your work is delivered; candidate/unknown remains unverified.
        completed requires solution in this same call, with the concise approach,
        meaningful attempts and evidence, actual breakthroughs if any, conclusion and
        reproduction. Cite existing same-challenge records and registered artifact IDs.
        Report remaining blockers honestly; never invent success or expose private
        step-by-step reasoning. Lead must name the actual author and source when relaying.
        Collaborators must omit work_status and solution. Omit solution unless completed.
        Stopping or budget/error closure needs no solution. Trusted verification is preserved.
        A final chat answer alone does not deliver work; do not broadcast it repeatedly.
        This never establishes platform acceptance. Read get_challenge on revision_conflict.
        """
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "record_candidate",
                challenge_id=challenge_id,
                request_id=str(uuid4()),
                expected_revision=expected_revision,
                status=status,
                summary=summary,
                evidence_refs=evidence_refs or [],
                work_status=work_status,
                **(
                    {"solution": CtfSolution.model_validate(solution).model_dump(mode="json")}
                    if solution is not None
                    else {}
                ),
            )
        )

    @tool
    async def set_verification_required(
        challenge_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        required: bool,
        basis: Annotated[str, Field(min_length=1)],
    ) -> ToolJsonResult:
        """Set whether platform verification is required, with explicit user-goal basis."""
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "set_verification_required",
                challenge_id=challenge_id,
                request_id=str(uuid4()),
                expected_revision=expected_revision,
                required=required,
                basis=basis,
            )
        )

    @tool
    async def confirm_messages(
        message_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
    ) -> ToolJsonResult:
        """After checking resumed targets, release relevant queued deferred message IDs
        from the resume notice. Already confirmed active or delivered IDs are safe
        to repeat. Never confirm cancelled/failed messages or inactive recipients.
        """
        return await json_tool_result(
            service.tool(task_id, token, "confirm_messages", message_ids=message_ids)
        )

    @tool
    async def execute_command(
        command: str,
        ctx: FunctionInvocationContext,
        cwd: str | None = None,
        timeout_sec: Annotated[int, Field(ge=1)] = 120,
        privileged: bool = False,
    ) -> str:
        """Execute in this member's fenced workspace; timeout uses the configured envd maximum.

        Prefer short, coherent operations and inspect each result before starting the
        next independent step. Publish a useful result or blocker before continuing a
        long investigation. Do not use long sleeps or repeated shell polling to wait
        for teammate replies. A timeout does not prove an external action had no effect.

        When BBX_AGENT_IP is set, localhost belongs to this Agent's network namespace.
        For cross-Agent services, bind to 0.0.0.0 and read peer IPs from
        /workspace/shared/network.json. BBX_TASK_GATEWAY identifies the shared gateway.
        """
        await service.runtime(
            task_id,
            "authorize_member",
            agent_id=member_id,
            turn_id=turn["id"],
            generation=turn["generation"],
        )
        if hasattr(envd, "execute_call"):
            return await envd.execute_call(
                task_id,
                member_id,
                turn,
                checkpoint_provider(),
                ctx,
                {
                    "command": command,
                    "cwd": cwd,
                    "timeout_sec": timeout_sec,
                    "privileged": privileged,
                },
            )
        return str(await envd.execute(task_id, member_id, command))

    @tool
    async def stop_teammate(member_id: str) -> ToolJsonResult:
        """Request a teammate stop; completion waits for remote process drain."""
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "stop_teammate",
                member_id=member_id,
                request_id=str(uuid4()),
            )
        )

    @tool
    async def resume_teammate(member_id: str) -> ToolJsonResult:
        """Resume a stopped named teammate using its existing session."""
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "resume_teammate",
                member_id=member_id,
                request_id=str(uuid4()),
            )
        )

    @tool
    async def remove_teammate(member_id: str) -> ToolJsonResult:
        """Retire a teammate after proven stop while preserving history."""
        return await json_tool_result(
            service.tool(
                task_id,
                token,
                "remove_teammate",
                member_id=member_id,
                request_id=str(uuid4()),
            )
        )

    return [
        *build_board_tools(service, task_id, member_id, turn, artifacts, role=role),
        *([execute_command] if envd is not None else []),
        *([view_image] if view_image is not None else []),
        list_members,
        send_message,
        read_turn_result,
        read_saved_tool_result,
        record_candidate,
        *(
            [
                create_teammate,
                stop_teammate,
                resume_teammate,
                remove_teammate,
                finish_task,
                set_verification_required,
                confirm_messages,
                list_deferred_messages,
            ]
            if role == "lead"
            else []
        ),
    ]
