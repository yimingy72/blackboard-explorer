"""A closed task's review tools cannot execute, communicate, or mutate its board."""

from typing import Annotated, Any

from agent_framework import tool
from pydantic import Field

from bbx_runtime.ctf.board_tools import build_board_tools
from bbx_runtime.ctf.tool_results import ToolJsonResult, json_tool_result


def build_review_tools(service: Any, task_id: str, member_id: str, turn: dict) -> list[Any]:
    readonly = {"list_challenges", "get_challenge", "get_record", "list_records"}
    tools = [
        item
        for item in build_board_tools(service, task_id, member_id, turn)
        if item.name in readonly
    ]

    @tool
    async def read_artifact(
        artifact_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=16384)] = 16384,
    ) -> ToolJsonResult:
        """Read a bounded section of a persisted task artifact by registered ID."""
        return await json_tool_result(
            service.tool(
                task_id,
                turn["token"],
                "read_artifact",
                artifact_id=artifact_id,
                offset=offset,
                limit=limit,
            )
        )

    @tool
    async def read_saved_tool_result(
        call_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=16000)] = 2000,
    ) -> ToolJsonResult:
        """Read a bounded page of this member's saved tool result, including long records."""
        return await json_tool_result(
            service.tool(
                task_id,
                turn["token"],
                "read_saved_tool_result",
                call_id=call_id,
                offset=offset,
                limit=limit,
            )
        )

    @tool
    async def read_command_result(
        command_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=16000)] = 2000,
    ) -> ToolJsonResult:
        """Read your persisted async command result; never execute, poll or resume a command.

        Follow next_offset for more saved output. Unknown means the original outcome
        was not confirmed; a completed process is not automatically a successful task.
        """
        return await json_tool_result(
            service.tool(
                task_id,
                turn["token"],
                "read_command_result",
                command_id=command_id,
                offset=offset,
                limit=limit,
            )
        )

    return [*tools, read_artifact, read_saved_tool_result, read_command_result]
