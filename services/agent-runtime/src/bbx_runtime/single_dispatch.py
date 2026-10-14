"""Single-dispatch invocation for the built-in envd command's JSON contract."""

from datetime import timedelta
from typing import Any

from agent_framework import Content, FunctionTool, MCPStreamableHTTPTool


class SingleDispatchExecutionTool(MCPStreamableHTTPTool):
    """Keep MAF discovery and lifecycle while bypassing automatic command replay."""

    async def call_tool(self, tool_name: str, **kwargs: Any) -> list[Content]:
        if tool_name != "execute_command" or self.session is None:
            raise RuntimeError("Built-in execution session or tool is unavailable")
        arguments = {
            key: kwargs[key]
            for key in ("command", "cwd", "timeout_sec", "privileged")
            if key in kwargs
        }
        timeout = self.request_timeout or max(1, int(arguments.get("timeout_sec", 120))) + 30
        result = await self.session.call_tool(
            tool_name, arguments, read_timeout_seconds=timedelta(seconds=timeout)
        )
        if result.isError:
            raise RuntimeError(
                "Execution tool returned an error; the command outcome is unconfirmed"
            )
        if result.structuredContent is None:
            raise RuntimeError("Execution result does not match the built-in JSON contract")
        # envd returns a dict; MAF's default structured_first policy selects this same JSON text.
        return FunctionTool.parse_result(result.structuredContent)
