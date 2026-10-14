"""CTF-specific HTTP routes with explicit runtime and member authority."""

from typing import Any, Literal
from uuid import UUID

from bbx_contracts import ctf as ctf_models
from bbx_contracts.ctf import CtfDrainProof, CtfMemberOperationRequest
from bbx_contracts.tool_errors import validation_issues
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from bbx_blackboard.auth import (
    issue_agent_token,
    require_agent_writer,
    require_service,
    require_task_reader,
    require_user_or_service,
)
from bbx_blackboard.ctf_board import SOLUTION_DELIVERY_ERROR


def invalid_request(error: ValueError, message: str) -> HTTPException:
    if isinstance(error, ValidationError) and any(
        item["loc"][:1] == ("solution",)
        or "Solution is required only when completing challenge work" in item["msg"]
        for item in error.errors(include_input=False, include_context=False)
    ):
        return HTTPException(
            422,
            {
                **SOLUTION_DELIVERY_ERROR,
                "fields": validation_issues(
                    error, ctf_models.CtfChallengeUpdateRequest.model_json_schema()
                ),
            },
        )
    return HTTPException(422, message)


router = APIRouter(prefix="/api/tasks/{task_id}/ctf", tags=["ctf"])


def service(request: Request) -> Any:
    return request.app.state.ctf_service


def public_member(member: dict[str, Any]) -> dict[str, Any]:
    """Expose roster state without executor registrations or control operation records."""
    return {
        key: value
        for key, value in member.items()
        if key
        not in {
            "execution",
            "pending_operation",
            "operation_history",
            "create_request_id",
            "normalized_name",
        }
    }


@router.get("/runtime-state")
async def runtime_state(request: Request, task_id: UUID) -> dict[str, Any]:
    require_service(request)
    return await service(request).runtime_state(task_id)


@router.get("/state")
async def state(request: Request, task_id: UUID) -> dict[str, Any]:
    identity = require_task_reader(request, task_id)
    data = await service(request).state(task_id)
    if identity.kind == "service":
        return data
    public = {
        key: value for key, value in data.items() if key not in {"turns", "messages", "sessions"}
    }
    public["members"] = [public_member(member) for member in data.get("members", [])]
    public["task"] = {key: value for key, value in data["task"].items() if key != "ctf_control"}
    public["task"]["ctf_phase"] = data["task"].get("ctf_control", {}).get("phase")
    public["task"]["ctf_cleanup"] = data["task"].get("ctf_control", {}).get("cleanup", {})
    public["task"]["ctf_review_usage"] = data["task"].get("ctf_control", {}).get("review_usage", {})
    if identity.kind == "user":
        requirements = data["task"].get("ctf_control", {}).get("completion_requirements")
        public["task"]["completion_requirements"] = (
            requirements if isinstance(requirements, str) else None
        )
        public["task"]["ctf_conclusion_history"] = (
            data["task"].get("ctf_control", {}).get("conclusion_history", [])
        )
    return public


@router.post("/runtime/{operation}")
async def runtime(request: Request, task_id: UUID, operation: str, body: dict[str, Any]) -> Any:
    require_service(request)
    allowed = {
        "start",
        "fail_start",
        "cleanup_status",
        "claim_review_turn",
        "recover_reviews",
        "authorize_review",
        "read_review_artifact",
        "claim_turn",
        "claim_messages",
        "recovery_input_proofs",
        "checkpoint",
        "checkpoint_failed",
        "record_observation",
        "finish_turn",
        "enqueue_continuation",
        "recover",
        "finalize_close",
        "renew_turn",
        "authorize_member",
        "mark_reservation_sent",
        "bill_usage",
        "reconcile_usage_outbox",
        "reconcile_budget",
        "idle_watchdog",
        "request_finish",
        "record_execution",
        "execution_replacement",
        "record_execution_drained",
        "complete_member_operation",
        "register_artifact",
        "lookup_artifact",
        "authorize_platform_call",
        "begin_platform_call",
        "record_platform_result",
    }
    if operation not in allowed:
        raise HTTPException(404, "Unknown CTF runtime operation")
    arguments = dict(body)
    if operation == "recovery_input_proofs":
        try:
            arguments = ctf_models.CtfRecoveryInputProofRequest.model_validate(body).model_dump(
                mode="json"
            )
        except ValueError as error:
            raise HTTPException(422, "Valid recovery input identity is required") from error
    if operation == "reconcile_usage_outbox" and (
        set(arguments) != {"agent_id"}
        or not isinstance(arguments.get("agent_id"), str)
        or not arguments["agent_id"]
    ):
        raise HTTPException(422, "Agent identity is required for usage reconciliation")
    if operation == "idle_watchdog" and arguments:
        raise HTTPException(422, "Unexpected watchdog arguments")
    if operation == "reconcile_budget" and arguments:
        raise HTTPException(422, "Unexpected budget reconciliation arguments")
    if operation in {"authorize_platform_call", "begin_platform_call", "record_platform_result"}:
        identity_fields = {
            key: arguments.pop(key)
            for key in ("agent_id", "turn_id", "generation", "challenge_id")
            if key in arguments
        }
        if len(identity_fields) != 4:
            raise HTTPException(422, "Platform call requires execution and challenge identity")
        model = {
            "authorize_platform_call": ctf_models.CtfPlatformAuthorizeRequest,
            "begin_platform_call": ctf_models.CtfPlatformBeginRequest,
            "record_platform_result": ctf_models.CtfPlatformResultRequest,
        }[operation]
        try:
            arguments = model.model_validate(arguments).model_dump(mode="json")
        except ValueError as error:
            raise HTTPException(422, "Invalid platform result registration") from error
        arguments.update(identity_fields)
    if operation == "register_artifact":
        identity_fields = {
            key: arguments.pop(key)
            for key in ("agent_id", "turn_id", "generation")
            if key in arguments
        }
        if len(identity_fields) != 3:
            raise HTTPException(422, "Artifact registration requires execution identity")
        try:
            arguments = ctf_models.CtfArtifactRegisterRequest.model_validate(arguments).model_dump(
                mode="json"
            )
        except ValueError as error:
            raise HTTPException(422, "Invalid artifact registration") from error
        arguments.update(identity_fields)
    if operation in {"record_execution_drained", "complete_member_operation"}:
        try:
            arguments["proof"] = CtfDrainProof.model_validate(arguments.get("proof")).model_dump(
                mode="json",
                exclude_none=True,
            )
        except ValueError as error:
            raise HTTPException(422, "Valid drain proof required") from error
    for key in ("turn_id", "request_id", "message_id", "reply_to"):
        if arguments.get(key) is not None:
            try:
                arguments[key] = str(UUID(str(arguments[key])))
            except (ValueError, TypeError) as error:
                raise HTTPException(422, "Invalid identifier") from error
    result = await getattr(service(request), operation)(task_id, **arguments)
    if operation in {"claim_turn", "claim_review_turn"} and result:
        result["token"] = issue_agent_token(
            request.app.state.settings,
            task_id,
            result["agent_id"],
            generation=result["generation"],
            turn_id=result["id"],
        )
    return result


@router.post("/tools/{operation}")
async def tool(request: Request, task_id: UUID, operation: str, body: dict[str, Any]) -> Any:
    identity = require_agent_writer(request, task_id)
    if identity.mode != "ctf" or identity.generation is None or identity.turn_id is None:
        raise HTTPException(403, "CTF execution token required")
    context = await service(request).authorize_tool(
        task_id,
        identity.agent_id,
        identity.generation,
        identity.turn_id,
    )
    if context["purpose"] == "review":
        if operation not in {"list_challenges", "get_challenge", "list_records", "read_artifact"}:
            raise HTTPException(403, "Tool is unavailable during review")
        if operation == "read_artifact":
            if set(body) - {"artifact_id", "offset", "limit"}:
                raise HTTPException(422, "Unexpected artifact arguments")
            return await service(request).read_review_artifact(
                task_id,
                agent_id=identity.agent_id,
                generation=identity.generation,
                turn_id=identity.turn_id,
                **body,
            )
        return await board_tool(request, task_id, operation, body, identity)
    task, member = context["task"], context["member"]
    profile = await request.app.state.profile_store.get(
        task["agent_profile"],
        task["agent_profile_version"],
    )
    builtin = {
        "list_team": "list_members",
        "post_message": "send_message",
        "create_member": "create_teammate",
        "request_finish": "finish_task",
        "claim_challenge": "update_challenge",
    }.get(operation, operation)
    if builtin not in profile["worker_tools"][member["role"]]["builtin"]:
        raise HTTPException(403, "Tool is disabled in the task profile")
    if operation in {"read_turn_result", "read_saved_tool_result", "list_deferred_messages"}:
        model = {
            "read_turn_result": ctf_models.CtfTurnResultReadRequest,
            "read_saved_tool_result": ctf_models.CtfSavedToolResultReadRequest,
            "list_deferred_messages": ctf_models.CtfDeferredMessagesReadRequest,
        }[operation]
        try:
            arguments = model.model_validate(body).model_dump(mode="json")
        except ValueError as error:
            raise HTTPException(422, "Invalid readback page") from error
        return await getattr(service(request), operation)(
            task_id, actor=identity.agent_id, **arguments
        )
    if operation == "confirm_messages":
        try:
            validated = ctf_models.CtfConfirmMessagesRequest.model_validate(body)
        except ValueError as error:
            raise HTTPException(422, "Message IDs must be UUIDs") from error
        return await service(request).confirm_messages(
            task_id,
            actor=identity.agent_id,
            message_ids=validated.message_ids,
            generation=identity.generation,
            turn_id=identity.turn_id,
        )
    if operation in {"record_candidate", "set_verification_required"}:
        arguments = dict(body)
        challenge_id = arguments.pop("challenge_id", None)
        if not challenge_id:
            raise HTTPException(422, "Challenge is required")
        model = (
            ctf_models.CtfVerificationCandidateRequest
            if operation == "record_candidate"
            else ctf_models.CtfVerificationRequiredRequest
        )
        try:
            validated = model.model_validate(arguments).model_dump(mode="json")
        except ValueError as error:
            if operation == "record_candidate":
                raise invalid_request(error, "Invalid verification request") from error
            raise HTTPException(422, "Invalid verification request") from error
        return await getattr(service(request), operation)(
            task_id,
            actor=identity.agent_id,
            challenge_id=str(challenge_id),
            generation=identity.generation,
            turn_id=identity.turn_id,
            **validated,
        )
    if operation in {
        "list_challenges",
        "get_challenge",
        "create_challenge",
        "update_challenge",
        "claim_challenge",
        "list_records",
        "append_record",
        "request_help",
    }:
        return await board_tool(request, task_id, operation, body, identity)
    if operation in {"list_members", "list_team"}:
        roster = await service(request).list_members(task_id)
        return [public_member(member) for member in roster["members"]]
    lifecycle = {"stop_teammate": "stop", "resume_teammate": "resume", "remove_teammate": "remove"}
    if operation in lifecycle:
        if member["role"] != "lead":
            raise HTTPException(403, "Only Lead can manage teammates")
        try:
            request_body = CtfMemberOperationRequest.model_validate(
                {"request_id": body.get("request_id")}
            )
        except ValueError as error:
            raise HTTPException(422, "Valid request ID required") from error
        member_id = body.get("member_id")
        if not isinstance(member_id, str) or not member_id:
            raise HTTPException(422, "Target member is required")
        result = await service(request).request_member_operation(
            task_id,
            actor=identity.agent_id,
            member_id=member_id,
            operation=lifecycle[operation],
            request_id=str(request_body.request_id),
            generation=identity.generation,
            turn_id=identity.turn_id,
        )
        return public_member(result)
    method = {
        "create_teammate": "create_member",
        "send_message": "post_message",
        "finish_task": "request_finish",
        "create_member": "create_member",
        "post_message": "post_message",
        "request_finish": "request_finish",
    }.get(operation)
    if method is None:
        raise HTTPException(404, "Unknown CTF tool")
    arguments = {
        key: value
        for key, value in body.items()
        if key not in {"actor", "generation", "turn_id", "task_id"}
    }
    for key in ("request_id", "message_id", "reply_to"):
        if arguments.get(key) is not None:
            try:
                arguments[key] = str(UUID(str(arguments[key])))
            except (ValueError, TypeError) as error:
                raise HTTPException(422, "Invalid identifier") from error
    return await getattr(service(request), method)(
        task_id,
        actor=identity.agent_id,
        generation=identity.generation,
        turn_id=identity.turn_id,
        **arguments,
    )


class ReaderEvents:
    """Revalidate long-lived readers on every fetch, including after a database wait."""

    def __init__(self, board: Any, ctf: Any, identity: Any) -> None:
        self.board, self.ctf, self.identity = board, ctf, identity

    async def events(
        self, tid: UUID, since: int, for_agent: str | None = None
    ) -> list[dict[str, Any]]:
        await self.ctf.authorize_reader(tid, self.identity.agent_id, self.identity.generation)
        events = await self.board.events(tid, since, for_agent=self.identity.agent_id)
        await self.ctf.authorize_reader(tid, self.identity.agent_id, self.identity.generation)
        return events


@router.get("/turns/{turn_id}/result")
async def turn_result(request: Request, task_id: UUID, turn_id: UUID) -> dict[str, Any]:
    identity = require_task_reader(request, task_id)
    actor = identity.agent_id if identity.kind == "agent" else "user"
    return await service(request).turn_result(task_id, str(turn_id), actor)


@router.post("/members/{member_id}/{operation}", status_code=202)
async def member_operation(
    request: Request,
    task_id: UUID,
    member_id: str,
    operation: Literal["stop", "resume", "remove"],
    body: CtfMemberOperationRequest,
) -> dict[str, Any]:
    identity = require_user_or_service(request)
    if identity.kind != "user":
        raise HTTPException(403, "User identity required")
    result = await service(request).request_member_operation(
        task_id,
        actor="user",
        member_id=member_id,
        operation=operation,
        request_id=str(body.request_id),
    )
    return public_member(result)


async def board_tool(
    request: Request, task_id: UUID, operation: str, body: dict[str, Any], identity: Any
) -> Any:
    arguments = dict(body)
    challenge_id = None
    if operation != "create_challenge" and operation != "list_challenges":
        try:
            challenge_id = str(UUID(str(arguments.pop("challenge_id"))))
        except (KeyError, ValueError) as error:
            raise HTTPException(422, "Valid challenge ID required") from error
    if operation in {"list_challenges", "get_challenge", "list_records"}:
        if operation == "get_challenge":
            if arguments:
                raise HTTPException(422, "Unexpected query arguments")
            return await service(request).get_challenge(task_id, challenge_id)
        limit, offset = arguments.pop("limit", 100), arguments.pop("offset", 0)
        after_version = None
        if operation == "list_records" and "after_version" in arguments:
            after_version = arguments.pop("after_version")
            if type(after_version) is not int or after_version < 0:
                raise HTTPException(422, "Invalid after_version")
        if (
            arguments
            or type(limit) is not int
            or type(offset) is not int
            or not 1 <= limit <= 500
            or offset < 0
        ):
            raise HTTPException(422, "Invalid pagination")
        if operation == "list_challenges":
            return await service(request).list_challenges(task_id, limit=limit, offset=offset)
        return await service(request).list_records(
            task_id, challenge_id, limit=limit, offset=offset, after_version=after_version
        )
    if operation == "claim_challenge":
        arguments["action"] = "claim"
        operation = "update_challenge"
    bodies = {
        "create_challenge": ctf_models.CtfChallengeCreateRequest,
        "update_challenge": ctf_models.CtfChallengeUpdateRequest,
        "append_record": ctf_models.CtfRecordAppendRequest,
        "request_help": ctf_models.CtfHelpRequest,
    }
    try:
        arguments = bodies[operation].model_validate(arguments).model_dump(mode="json")
    except ValueError as error:
        if operation in {"update_challenge", "append_record"}:
            raise invalid_request(error, "Invalid CTF board request") from error
        raise HTTPException(422, "Invalid CTF board request") from error
    if challenge_id is not None:
        arguments["challenge_id"] = challenge_id
    return await getattr(service(request), operation)(
        task_id,
        actor=identity.agent_id,
        generation=identity.generation,
        turn_id=identity.turn_id,
        **arguments,
    )


@router.get("/challenges")
async def challenges(
    request: Request,
    task_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    require_task_reader(request, task_id)
    if set(request.query_params) - {"limit", "offset"}:
        raise HTTPException(422, "Unexpected query arguments")
    return await service(request).list_challenges(task_id, limit=limit, offset=offset)


@router.get("/challenges/{challenge_id}")
async def challenge(request: Request, task_id: UUID, challenge_id: UUID) -> dict[str, Any]:
    require_task_reader(request, task_id)
    if request.query_params:
        raise HTTPException(422, "Unexpected query arguments")
    return await service(request).get_challenge(task_id, str(challenge_id))


@router.post("/challenges")
async def create_challenge(
    request: Request, task_id: UUID, body: ctf_models.CtfChallengeCreateRequest
) -> Any:
    return await tool(request, task_id, "create_challenge", body.model_dump(mode="json"))


@router.patch("/challenges/{challenge_id}")
async def update_challenge(
    request: Request, task_id: UUID, challenge_id: UUID, body: ctf_models.CtfChallengeUpdateRequest
) -> Any:
    return await tool(
        request,
        task_id,
        "update_challenge",
        {
            **body.model_dump(mode="json"),
            "challenge_id": str(challenge_id),
        },
    )


@router.post("/challenges/{challenge_id}/claim")
async def claim_challenge(
    request: Request, task_id: UUID, challenge_id: UUID, body: ctf_models.CtfChallengeClaimRequest
) -> Any:
    return await tool(
        request,
        task_id,
        "claim_challenge",
        {
            **body.model_dump(mode="json"),
            "challenge_id": str(challenge_id),
        },
    )


@router.get("/challenges/{challenge_id}/records")
async def records(
    request: Request,
    task_id: UUID,
    challenge_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    after_version: int | None = Query(default=None, ge=0),
) -> dict[str, Any]:
    require_task_reader(request, task_id)
    if set(request.query_params) - {"limit", "offset", "after_version"}:
        raise HTTPException(422, "Unexpected query arguments")
    return await service(request).list_records(
        task_id, str(challenge_id), limit=limit, offset=offset, after_version=after_version
    )


@router.get("/challenges/{challenge_id}/records/{record_id}")
async def record(
    request: Request,
    task_id: UUID,
    challenge_id: UUID,
    record_id: UUID,
) -> dict[str, Any]:
    require_task_reader(request, task_id)
    if request.query_params:
        raise HTTPException(422, "Unexpected query arguments")
    return await service(request).get_record(task_id, str(challenge_id), str(record_id))


@router.post("/challenges/{challenge_id}/records")
async def append_record(
    request: Request, task_id: UUID, challenge_id: UUID, body: ctf_models.CtfRecordAppendRequest
) -> Any:
    return await tool(
        request,
        task_id,
        "append_record",
        {
            **body.model_dump(mode="json"),
            "challenge_id": str(challenge_id),
        },
    )


@router.post("/challenges/{challenge_id}/help")
async def request_help(
    request: Request, task_id: UUID, challenge_id: UUID, body: ctf_models.CtfHelpRequest
) -> Any:
    return await tool(
        request,
        task_id,
        "request_help",
        {
            **body.model_dump(mode="json"),
            "challenge_id": str(challenge_id),
        },
    )


@router.get("/artifacts/{artifact_id}")
async def artifact(request: Request, task_id: UUID, artifact_id: UUID) -> dict[str, Any]:
    require_task_reader(request, task_id)
    items = (await service(request).list_artifacts(task_id))["artifacts"]
    found = next((item for item in items if str(item["id"]) == str(artifact_id)), None)
    if found is None:
        raise HTTPException(404, "Artifact not found")
    return found


@router.post("/challenges/{challenge_id}/verification/manual")
async def manual_verification(
    request: Request,
    task_id: UUID,
    challenge_id: UUID,
    body: ctf_models.CtfManualVerificationRequest,
) -> Any:
    identity = require_user_or_service(request)
    if identity.kind != "user":
        raise HTTPException(403, "Authenticated user confirmation required")
    return await service(request).manual_verification(
        task_id,
        user_id=identity.name,
        challenge_id=str(challenge_id),
        **body.model_dump(mode="json"),
    )


@router.post("/challenges/{challenge_id}/verification/required")
async def verification_required(
    request: Request,
    task_id: UUID,
    challenge_id: UUID,
    body: ctf_models.CtfVerificationRequiredRequest,
) -> Any:
    identity = require_user_or_service(request)
    if identity.kind != "user":
        raise HTTPException(403, "Authenticated user setting required")
    return await service(request).set_verification_required(
        task_id, actor="user", challenge_id=str(challenge_id), **body.model_dump(mode="json")
    )
