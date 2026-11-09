"""One business dispatcher for REST and the shared MCP transport."""

from uuid import UUID

from bbx_contracts.challenge_board import (
    DISPATCH_TOOL,
    UPDATE_CHALLENGE_TOOL,
    AnnouncementReadRequest,
    AnnouncementRequest,
    BoardChallengeRequest,
    BoardChallengeUpdateRequest,
    BoardDispatchChallengeRequest,
    BoardFinishRequest,
    BoardPageRequest,
    ChallengeFactRequest,
    ChallengeIntentRequest,
    FlagCandidateRequest,
    FlagSubmissionRequest,
)
from bbx_contracts.runner_vpn import (
    NETWORK_USE_DEFAULT,
    VPN_CONFIGURE,
    VPN_DISCONNECT,
    VPN_STATUS,
    VPN_TOOLS,
    NativeTaskVpnConfigure,
    NativeVpnConfigure,
    NativeVpnDisconnect,
    NativeVpnStatus,
)
from fastapi import HTTPException
from pydantic import ValidationError

from bbx_blackboard.challenge_board import ChallengeBoardService
from bbx_blackboard.flag_board import FlagBoardService
from bbx_blackboard.task_board import TaskBoardService

MUTATIONS = {
    "board_publish_announcement",
    "board_create_challenge",
    DISPATCH_TOOL,
    UPDATE_CHALLENGE_TOOL,
    "board_finish_task",
    "board_post_fact",
    "board_post_intent",
    "board_record_flag",
    "board_submit_flag",
}


class BusinessTools:
    def __init__(self, ctf, signing_key):
        self.board = ChallengeBoardService(ctf)
        self.tasks = TaskBoardService(ctf)
        self.flags = FlagBoardService(ctf, signing_key)

    async def invoke(self, task_id, identity, operation, arguments):
        if operation in VPN_TOOLS:
            raise HTTPException(403, "VPN tools require the original Runner journal")
        manifest = await self.tasks.manifest(task_id, identity)
        if not any(operation in names for names in manifest["roles"].values()):
            raise HTTPException(403, "Tool is not published for this role")
        arguments = dict(arguments)
        try:
            if operation == "board_announcements":
                parsed = AnnouncementReadRequest.model_validate(arguments)
                return await self.tasks.announcements(
                    task_id, identity, parsed.after_revision, parsed.limit
                )
            if operation == "board_challenges":
                parsed = BoardPageRequest.model_validate(arguments)
                return await self.tasks.challenges(task_id, identity, parsed.limit, parsed.offset)
            if operation == "board_publish_announcement":
                return await self.tasks.publish_announcement(task_id, identity, arguments)
            if operation == "board_create_challenge":
                return await self.tasks.create_challenge(task_id, identity, arguments)
            if operation == DISPATCH_TOOL:
                return await self.tasks.dispatch_challenge(task_id, identity, arguments)
            if operation == UPDATE_CHALLENGE_TOOL:
                return await self.tasks.update_challenge(task_id, identity, arguments)
            if operation == "board_finish_task":
                parsed = BoardFinishRequest.model_validate(arguments)
                return await self.tasks.finish(
                    task_id, identity, parsed.request_id, parsed.conclusion
                )
            cid = UUID(arguments.pop("challenge_id"))
            if operation in {"board_read", "board_search", "board_flags", "board_flag_value"}:
                if operation == "board_read" and not arguments:
                    return await self.board.board(task_id, cid, identity)
                if operation == "board_flags" and not arguments:
                    return await self.flags.board(task_id, cid, identity)
                if operation == "board_flag_value" and set(arguments) == {"candidate_id"}:
                    return await self.flags.value(
                        task_id, cid, UUID(arguments["candidate_id"]), identity
                    )
                if operation == "board_search" and set(arguments) <= {"q", "k"}:
                    limit = arguments.get("k", 20)
                    if (
                        type(limit) is not int
                        or not 1 <= limit <= 100
                        or not isinstance(arguments.get("q", ""), str)
                    ):
                        raise ValueError("Invalid search")
                    return await self.board.search(
                        task_id, cid, identity, arguments.get("q", ""), limit
                    )
                raise ValueError("Unexpected arguments")
            if operation in {"board_post_fact", "board_post_intent"}:
                return await self.board.write(
                    task_id,
                    cid,
                    identity,
                    arguments,
                    kind="fact" if operation == "board_post_fact" else "intent",
                )
            if operation == "board_record_flag":
                return await self.flags.candidate(task_id, cid, identity, arguments)
            if operation == "board_submit_flag":
                return await self.flags.prepare(task_id, cid, identity, arguments)
        except ValidationError as error:
            raise HTTPException(
                422, {"issues": error.errors(include_input=False, include_context=False)}
            ) from error
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(422, "Invalid business tool arguments") from error
        raise HTTPException(404, "Unknown business tool")


def schemas(*, dynamic_vpn=False):
    models = {
        "board_announcements": AnnouncementReadRequest,
        "board_challenges": BoardPageRequest,
        "board_publish_announcement": AnnouncementRequest,
        "board_create_challenge": BoardChallengeRequest,
        DISPATCH_TOOL: BoardDispatchChallengeRequest,
        UPDATE_CHALLENGE_TOOL: BoardChallengeUpdateRequest,
        "board_finish_task": BoardFinishRequest,
        "board_post_fact": ChallengeFactRequest,
        "board_post_intent": ChallengeIntentRequest,
        "board_record_flag": FlagCandidateRequest,
        "board_submit_flag": FlagSubmissionRequest,
        VPN_CONFIGURE: NativeTaskVpnConfigure if dynamic_vpn else NativeVpnConfigure,
        VPN_STATUS: NativeVpnStatus,
        VPN_DISCONNECT: NativeVpnDisconnect,
        **({NETWORK_USE_DEFAULT: NativeVpnStatus} if dynamic_vpn else {}),
    }
    result = {}
    for name, model in models.items():
        schema = model.model_json_schema()
        if name == VPN_CONFIGURE and dynamic_vpn:
            target = schema.pop("$defs")["NativeVpnTarget"]
            schema["properties"]["preflight"] = target
        schema["properties"].pop("request_id", None)
        schema["required"] = [item for item in schema.get("required", []) if item != "request_id"]
        if name in {
            "board_post_fact",
            "board_post_intent",
            "board_record_flag",
            "board_submit_flag",
        }:
            schema["properties"]["challenge_id"] = {"type": "string", "format": "uuid"}
            schema["required"].append("challenge_id")
        result[name] = schema
    for name in ("board_read", "board_search", "board_flags", "board_flag_value"):
        properties: dict[str, dict] = {"challenge_id": {"type": "string", "format": "uuid"}}
        required = ["challenge_id"]
        if name == "board_search":
            properties.update(
                q={"type": "string"}, k={"type": "integer", "minimum": 1, "maximum": 100}
            )
        if name == "board_flag_value":
            properties["candidate_id"] = {"type": "string", "format": "uuid"}
            required.append("candidate_id")
        result[name] = {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        }
    return result
