"""Expose existing typed Python handlers and official MCP clients to Pi tools."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Callable
from contextlib import asynccontextmanager, nullcontext
from datetime import timedelta
from typing import TYPE_CHECKING, Any, get_type_hints

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import EmbeddedResource, TextResourceContents, Tool
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from bbx_runtime.pi.data import Content, result_contents
from bbx_runtime.pi.hooks import FunctionInvocationContext

if TYPE_CHECKING:
    from bbx_runtime.session import SessionCheckpoint


class FunctionTool:
    def __init__(
        self,
        *,
        name: str,
        description: str = "",
        func: Callable,
        input_schema: dict | None = None,
        **_kwargs: Any,
    ) -> None:
        self.name, self.description, self.func = name, description, func
        self.input_model: type[BaseModel] | None = None
        self.context_parameter: str | None = None
        if input_schema is None:
            hints = get_type_hints(func, include_extras=True)
            fields: dict[str, Any] = {}
            for key, parameter in inspect.signature(func).parameters.items():
                annotation = hints.get(key, Any)
                if annotation is FunctionInvocationContext:
                    self.context_parameter = key
                    continue
                fields[key] = (
                    annotation,
                    ... if parameter.default is inspect.Parameter.empty else parameter.default,
                )
            self.input_model = create_model(
                f"{name}Input", __config__=ConfigDict(extra="forbid"), **fields
            )
            input_schema = self.input_model.model_json_schema()
        self.input_schema = input_schema

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.input_schema}

    def parameters(self) -> dict[str, Any]:
        return self.input_schema

    @staticmethod
    def parse_result(result: Any) -> list[Content]:
        return result_contents(result)

    async def invoke(
        self,
        *,
        arguments: Any,
        context: FunctionInvocationContext | None = None,
        skip_parsing: bool = False,
        **_kwargs: Any,
    ) -> Any:
        try:
            values: dict[str, Any] = (
                dict(self.input_model.model_validate(arguments).__dict__)
                if self.input_model is not None
                else dict(arguments)
            )
        except ValidationError:
            raise TypeError("Invalid arguments; follow the tool schema") from None
        if self.context_parameter:
            values[self.context_parameter] = context or FunctionInvocationContext(self, values)
        value = self.func(**values)
        if inspect.isawaitable(value):
            value = await value
        return value if skip_parsing else self.parse_result(value)


class _PublishedMcpFunction(FunctionTool):
    def __init__(self, connector: MCPStreamableHTTPTool, wire_name: str, **options):
        super().__init__(func=connector.call_tool, **options)
        self.connector, self.wire_name = connector, wire_name

    async def invoke(
        self,
        *,
        arguments: Any,
        context: FunctionInvocationContext | None = None,
        skip_parsing: bool = False,
        **_kwargs: Any,
    ) -> Any:
        result = await self.connector._call_tool(self.wire_name, dict(arguments), context)
        return result if skip_parsing else self.parse_result(result)


def tool(func: Callable | None = None, **options: Any) -> Any:
    def decorate(handler):
        return FunctionTool(
            name=options.get("name", handler.__name__),
            description=options.get("description", inspect.getdoc(handler) or ""),
            func=handler,
        )

    return decorate(func) if func is not None else decorate


class _Headers:
    """Per-connection headers without modifying a borrowed HTTP client's state."""

    def __init__(self, client: httpx.AsyncClient, headers: dict[str, str]) -> None:
        self.client, self.extra = client, headers

    def __getattr__(self, name):
        if name in {"stream", "post", "get", "delete", "request", "build_request"}:

            def call(*args, **kwargs):
                kwargs["headers"] = {**dict(kwargs.get("headers") or {}), **self.extra}
                return getattr(self.client, name)(*args, **kwargs)

            return call
        return getattr(self.client, name)


class MCPStreamableHTTPTool:
    """Reuse the MCP SDK lifecycle, discovery and invocation, without reconnect replay."""

    def __init__(
        self,
        *,
        name: str,
        url: str,
        static_headers=None,
        allowed_tools=None,
        tool_name_prefix: str = "",
        load_tools: bool = True,
        load_prompts: bool = False,
        sampling_max_requests: int = 0,
        http_client: httpx.AsyncClient | None = None,
        request_timeout: float | None = None,
        session: ClientSession | None = None,
        approved_tools: dict[str, dict[str, Any]] | None = None,
        trusted_transport: httpx.AsyncBaseTransport | None = None,
        protected_secret: str | None = None,
        checkpoint: SessionCheckpoint | None = None,
        **_kwargs: Any,
    ) -> None:
        self.name, self.url = name, url
        if load_prompts or sampling_max_requests != 0:
            raise ValueError("Platform MCP connections do not load prompts or sample models")
        self.load_prompts_flag = False
        self.sampling_max_requests = 0
        self._static_headers = dict(static_headers or {})
        self.allowed_tools = frozenset(allowed_tools) if allowed_tools is not None else None
        self.tool_name_prefix = tool_name_prefix
        self.load_tools, self.request_timeout = load_tools, request_timeout
        self._httpx_client = http_client
        self._trusted_transport = trusted_transport
        self._approved_tools = approved_tools
        self._protected_secret = protected_secret
        self._checkpoint = checkpoint
        self.session = session
        self.functions: list[FunctionTool] = []
        self._owner: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._closed: asyncio.Event | None = None

    def _function(self, definition: Tool) -> FunctionTool:
        remote_name = definition.name

        async def invoke(**arguments):
            return await self.call_tool(remote_name, **arguments)

        exposed = f"{self.tool_name_prefix}_{remote_name}" if self.tool_name_prefix else remote_name
        frozen = self._approved_tools.get(remote_name) if self._approved_tools is not None else None
        if frozen is not None:
            digest = hashlib.sha256(exposed.encode()).hexdigest()[:16]
            exposed = f"{re.sub(r'[^A-Za-z0-9_-]', '_', exposed)[:47]}_{digest}"
            return _PublishedMcpFunction(
                self,
                remote_name,
                name=exposed,
                description=frozen["description"],
                input_schema=frozen["input_schema"],
            )
        return FunctionTool(
            name=exposed,
            description=definition.description or "",
            func=invoke,
            input_schema=definition.inputSchema,
        )

    def _sdk_log_scope(self):
        if self._approved_tools is None:
            return nullcontext()
        from bbx_contracts.mcp_catalog import quiet_external_sdk

        return quiet_external_sdk()

    @asynccontextmanager
    async def _sdk_transport(self, client):
        with self._sdk_log_scope():
            async with streamable_http_client(
                self.url,
                http_client=_Headers(client, self._static_headers),  # type: ignore[arg-type]
            ) as streams:
                yield streams

    async def _lifecycle(self) -> None:
        assert self._ready is not None and self._closed is not None
        owned = self._httpx_client is None
        client = self._httpx_client or httpx.AsyncClient(
            timeout=self.request_timeout or 120,
            trust_env=self._trusted_transport is None,
            transport=self._trusted_transport,
            follow_redirects=False,
        )
        try:
            async with self._sdk_transport(client) as (read, write, _):
                timeout = timedelta(seconds=self.request_timeout) if self.request_timeout else None
                async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                    self.session = session
                    await session.initialize()
                    if self.load_tools:
                        functions = []
                        cursor = None
                        seen = set()
                        registered = set()
                        discovered = 0
                        while True:
                            catalog = await session.list_tools(cursor=cursor)
                            discovered += len(catalog.tools)
                            if self._approved_tools is not None and discovered > 256:
                                raise ValueError("MCP discovery exceeded the approved bound")
                            for definition in catalog.tools:
                                if (
                                    self.allowed_tools is not None
                                    and definition.name not in self.allowed_tools
                                ):
                                    continue
                                if self._approved_tools is not None:
                                    from bbx_contracts.mcp_catalog import tool_schema_digest

                                    frozen = self._approved_tools.get(definition.name)
                                    if (
                                        frozen is None
                                        or definition.name in registered
                                        or tool_schema_digest(
                                            definition.inputSchema, definition.outputSchema
                                        )
                                        != frozen["schema_digest"]
                                    ):
                                        raise ValueError("The selected MCP tool schema changed")
                                    registered.add(definition.name)
                                functions.append(self._function(definition))
                            cursor = catalog.nextCursor
                            if not cursor:
                                break
                            if cursor in seen:
                                raise ValueError("Repeated MCP cursor")
                            if self._approved_tools is not None and len(seen) >= 32:
                                raise ValueError("MCP discovery exceeded the page bound")
                            seen.add(cursor)
                        if (
                            self._approved_tools is not None
                            and registered != self._approved_tools.keys()
                        ):
                            raise ValueError("A selected published MCP tool is missing")
                        self.functions = functions
                    self._ready.set_result(None)
                    await self._closed.wait()
        except BaseException as error:
            if not self._ready.done():
                if self._approved_tools is not None and isinstance(error, Exception):
                    error = RuntimeError("Published MCP connection or schema verification failed")
                self._ready.set_exception(error)
            elif self._approved_tools is not None and isinstance(error, Exception):
                raise RuntimeError("Published MCP connection terminated") from None
            else:
                raise
        finally:
            self.session = None
            if owned:
                await client.aclose()

    async def connect(self) -> None:
        if self.session is not None:
            return
        if self._owner is None:
            self._ready = asyncio.get_running_loop().create_future()
            self._closed = asyncio.Event()
            self._owner = asyncio.create_task(self._lifecycle())
        assert self._ready is not None
        try:
            await self._ready
        except BaseException:
            await self.close()
            raise

    async def call_tool(self, tool_name: str, /, **arguments: Any) -> list[Content]:
        return await self._call_tool(tool_name, arguments)

    async def _call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: FunctionInvocationContext | None = None,
    ) -> list[Content]:
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            raise PermissionError("MCP tool is outside the pinned allowlist")
        if self.session is None:
            raise RuntimeError("MCP connection is unavailable")
        if self._approved_tools is not None:
            from jsonschema.validators import validator_for
            from referencing import Registry

            frozen = self._approved_tools.get(tool_name)
            if frozen is None:
                raise PermissionError("The MCP tool is outside the published allowlist")
            try:
                schema = frozen["input_schema"]
                validator_for(schema)(schema, registry=Registry()).validate(arguments)
            except Exception:
                raise ValueError("Arguments did not match the approved MCP tool schema") from None
            from bbx_runtime.session import EXTERNAL_MCP_PENDING, ExternalMcpUnconfirmed

            call_id = context.metadata.get("call_id") if context is not None else None
            if (
                self._checkpoint is None
                or context is None
                or context.session is not self._checkpoint.session
                or not isinstance(call_id, str)
            ):
                raise ExternalMcpUnconfirmed("The external MCP call lacks its fenced checkpoint")
            context.metadata[EXTERNAL_MCP_PENDING] = await self._checkpoint.begin_external_mcp(
                call_id
            )
        try:
            with self._sdk_log_scope():
                result = await self.session.call_tool(tool_name, arguments)
        except Exception:
            if self._approved_tools is not None:
                raise RuntimeError(
                    "Published MCP result is unconfirmed; do not replay it"
                ) from None
            raise
        if self._protected_secret and (
            json.dumps(self._protected_secret, ensure_ascii=False)[1:-1]
            in json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        ):
            raise RuntimeError("Published MCP returned protected credential data; do not replay it")
        if result.isError:
            raise RuntimeError("MCP tool failed; its effect must not be replayed")
        if result.structuredContent is not None:
            return FunctionTool.parse_result(result.structuredContent)
        contents = []
        for item in result.content:
            if item.type == "text":
                contents.append(Content.from_text(item.text))
            elif item.type == "image":
                contents.append(Content.from_data(item.data, item.mimeType))
            elif isinstance(item, EmbeddedResource) and isinstance(
                item.resource, TextResourceContents
            ):
                contents.append(
                    Content.from_text(
                        item.resource.text,
                        additional_properties={
                            "mcp_resource": item.resource.model_dump(
                                mode="json", by_alias=True, exclude={"text"}, exclude_none=True
                            )
                        },
                    )
                )
            else:
                contents.append(Content.from_text(item.model_dump_json(by_alias=True)))
        return contents

    async def close(self) -> None:
        if self._closed is not None:
            self._closed.set()
        if self._owner is not None:
            if self._ready is not None and (not self._ready.done() or self._ready.cancelled()):
                self._owner.cancel()
            current = asyncio.current_task()
            cancelling = current.cancelling() if current else 0
            try:
                await self._owner
            except asyncio.CancelledError:
                if not self._owner.cancelled() or (current and current.cancelling() > cancelling):
                    raise
            finally:
                self._owner = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_args):
        await self.close()
