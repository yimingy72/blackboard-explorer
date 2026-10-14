"""A closed task's review tools cannot execute, communicate, or mutate its board."""

from typing import Annotated, Any

from agent_framework import tool
from pydantic import Field

from bbx_runtime.ctf.board_tools import build_board_tools
from bbx_runtime.ctf.tool_results import ToolJsonResult, json_tool_result


def build_review_tools(service: Any, task_id: str, member_id: str, turn: dict) -> list[Any]:
    readonly = {"list_challenges", "get_challenge", "list_records"}
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

    return [*tools, read_artifact]
