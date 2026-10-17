"""Single-dispatch invocation for the built-in envd command's JSON contract."""

from datetime import timedelta
from typing import Any

import httpx
from agent_framework import Content, FunctionTool, MCPStreamableHTTPTool


class SingleDispatchExecutionTool(MCPStreamableHTTPTool):
    """Keep MAF discovery and lifecycle while bypassing automatic command replay."""

    async def call_tool(self, tool_name: str, **kwargs: Any) -> list[Content]:
        if tool_name != "execute_command":
            raise RuntimeError("Built-in execution tool is unavailable")
        arguments = {
            key: kwargs[key]
            for key in ("command", "cwd", "timeout_sec", "privileged")
            if key in kwargs
        }
        timeout = self.request_timeout or max(1, int(arguments.get("timeout_sec", 120))) + 30
        if self._httpx_client is not None:
            response = await self._httpx_client.post(
                self.url.removesuffix("/mcp") + "/execute",
                headers=self._static_headers,
                json=arguments,
                timeout=httpx.Timeout(timeout, connect=10),
            )
            if response.is_error:
                raise RuntimeError(
                    f"Execution request rejected with HTTP {response.status_code}; "
                    "the command outcome is unconfirmed"
                )
            structured = response.json()
        else:
            if self.session is None:
                raise RuntimeError("Built-in execution transport is unavailable")
            result = await self.session.call_tool(
                tool_name, arguments, read_timeout_seconds=timedelta(seconds=timeout)
            )
            if result.isError:
                raise RuntimeError(
                    "Execution tool returned an error; the command outcome is unconfirmed"
                )
            structured = result.structuredContent
        if not isinstance(structured, dict):
            raise RuntimeError("Execution result does not match the built-in JSON contract")
        return FunctionTool.parse_result(structured)
