"""Small role-specific tool surface; the backend independently enforces authority."""

from typing import Any, Literal
from uuid import uuid4

from agent_framework import FunctionInvocationContext, tool

from bbx_runtime.ctf.board_tools import build_board_tools
from bbx_runtime.ctf.client import CtfClient


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
) -> list[Any]:
    token = turn["token"]

    @tool
    async def list_members() -> str:
        """Read the shared team roster without private conversations."""
        return str(await service.tool(task_id, token, "list_team"))

    @tool
    async def send_message(
        recipient_id: str, body: str, instruction: bool = False, reply_to: str | None = None
    ) -> str:
        """Send a durable directed message; instruction explicitly assigns work."""
        return str(
            await service.tool(
                task_id,
                token,
                "post_message",
                recipient_id=recipient_id,
                body=body,
                message_id=str(uuid4()),
                kind="instruction" if instruction else "message",
                reply_to=reply_to,
            )
        )

    @tool
    async def create_teammate(display_name: str) -> str:
        """Create a named teammate with a persistent session."""
        return str(
            await service.tool(
                task_id, token, "create_member", display_name=display_name, request_id=str(uuid4())
            )
        )

    @tool
    async def finish_task(
        summary: str,
        unresolved_items: list[str],
        evidence_refs: list[str],
        lead_claim: bool = False,
    ) -> str:
        """Register closing and return immediately; do not wait for this turn to drain."""
        return str(
            await service.tool(
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
        expected_revision: int,
        status: Literal["candidate", "unknown"],
        summary: str,
        evidence_refs: list[str] | None = None,
    ) -> str:
        """Record a member claim; this never establishes platform acceptance."""
        return str(
            await service.tool(
                task_id,
                token,
                "record_candidate",
                challenge_id=challenge_id,
                request_id=str(uuid4()),
                expected_revision=expected_revision,
                status=status,
                summary=summary,
                evidence_refs=evidence_refs or [],
            )
        )

    @tool
    async def set_verification_required(
        challenge_id: str,
        expected_revision: int,
        required: bool,
        basis: str,
    ) -> str:
        """Set whether platform verification is required, with explicit user-goal basis."""
        return str(
            await service.tool(
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
    async def confirm_messages(message_ids: list[str]) -> str:
        """After checking resumed targets, release selected deferred execution messages."""
        return str(await service.tool(task_id, token, "confirm_messages", message_ids=message_ids))

    @tool
    async def execute_command(
        command: str,
        ctx: FunctionInvocationContext,
        cwd: str | None = None,
        timeout_sec: int = 120,
        privileged: bool = False,
    ) -> str:
        """Execute in this member's fenced task workspace."""
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
    async def stop_teammate(member_id: str) -> str:
        """Request a teammate stop; completion waits for remote process drain."""
        return str(
            await service.tool(
                task_id,
                token,
                "stop_teammate",
                member_id=member_id,
                request_id=str(uuid4()),
            )
        )

    @tool
    async def resume_teammate(member_id: str) -> str:
        """Resume a stopped named teammate using its existing session."""
        return str(
            await service.tool(
                task_id,
                token,
                "resume_teammate",
                member_id=member_id,
                request_id=str(uuid4()),
            )
        )

    @tool
    async def remove_teammate(member_id: str) -> str:
        """Retire a teammate after proven stop while preserving history."""
        return str(
            await service.tool(
                task_id,
                token,
                "remove_teammate",
                member_id=member_id,
                request_id=str(uuid4()),
            )
        )

    return [
        *build_board_tools(service, task_id, member_id, turn, artifacts),
        *([execute_command] if envd is not None else []),
        list_members,
        send_message,
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
            ]
            if role == "lead"
            else []
        ),
    ]
