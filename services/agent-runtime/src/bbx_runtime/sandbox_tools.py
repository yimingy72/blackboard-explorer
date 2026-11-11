"""Stateless official Sandbox I/O; admission, identities and receipts belong to Blackboard."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import httpx
from bbx_sandbox_execution.connect import read_execution_context
from bbx_sandbox_execution.execution import (
    LAUNCHER,
    SOLVER_GID,
    SOLVER_UID,
    AlreadyAdmitted,
    AssignmentBinding,
    CommandRequest,
    SolverFiles,
    UnknownAssignmentCleanup,
    UnknownCommandOutcome,
)
from opensandbox import Sandbox
from opensandbox.models.execd import ExecutionHandlers, ExecutionInit, OutputMessage, RunCommandOpts

OUTPUT_LIMIT = 64 * 1024
NATIVE_LOG_RESPONSE_LIMIT = 1024 * 1024
Persist = Callable[[str, dict], Awaitable[None]]


class NativeLogResponseLimit(RuntimeError):
    """The original log response could not be safely retained; its cursor is unknown."""


class _BoundedLogBody(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self):
        total = 0
        async for chunk in self.stream:
            total += len(chunk)
            if total > NATIVE_LOG_RESPONSE_LIMIT:
                await self.aclose()
                raise NativeLogResponseLimit("Native log body exceeds the receive limit")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class BoundedExecdLogTransport(httpx.AsyncBaseTransport):
    """Limit the fixed SDK's aggregate log response without changing other native I/O."""

    def __init__(self):
        self.transport = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        logs = request.method == "GET" and re.search(r"/command/[^/]+/logs$", request.url.path)
        if logs:
            request.headers["Accept-Encoding"] = "identity"
        response = await self.transport.handle_async_request(request)
        if logs:
            length = response.headers.get("content-length", "")
            if response.headers.get("content-encoding", "identity").lower() != "identity" or (
                length.isdecimal() and int(length) > NATIVE_LOG_RESPONSE_LIMIT
            ):
                await response.aclose()
                raise NativeLogResponseLimit("Native log response is too large or encoded")
            assert isinstance(response.stream, httpx.AsyncByteStream)
            response.stream = _BoundedLogBody(response.stream)
        return response

    async def aclose(self):
        await self.transport.aclose()


def _binding(sandbox, binding):
    if (
        sandbox.id != binding.sandbox_id
        or binding.process_accounting != "cgroup-v2"
        or not binding.execution_context_id
    ):
        raise ValueError("An original cgroup-v2 Assignment is required")
    binding.scope_request()


def _path(value):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("A workspace path is required")
    path = PurePosixPath(value)
    if not path.is_absolute() or not path.is_relative_to("/workspace") or ".." in path.parts:
        raise ValueError("File tools are restricted to the workspace")
    return value


def _execution_id(args):
    value = args.get("execution_id")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", value):
        raise ValueError("A trusted, previously bound execution ID is required")
    return value


async def _context(sandbox, binding):
    actual = await read_execution_context(sandbox)
    if any(
        actual[k] != getattr(binding, k)
        for k in ("pod_uid", "environment_revision", "execution_context_id")
    ):
        raise UnknownCommandOutcome("The original Sandbox execution context changed")


async def _run(sandbox, binding, request, persist):
    observed_id = None
    text = {"stdout": bytearray(), "stderr": bytearray()}
    offsets = {"stdout": 0, "stderr": 0}
    truncation_sent = set()

    async def initialized(event: ExecutionInit):
        nonlocal observed_id
        if not event.id or (observed_id and observed_id != event.id):
            raise UnknownCommandOutcome("The official execution ID changed")
        if observed_id is None:
            await persist("execution_bound", {"execution_id": event.id})
            observed_id = event.id

    async def output(stream: str, event: OutputMessage):
        if not observed_id:
            raise UnknownCommandOutcome("Output arrived before the official execution ID")
        data = event.text.encode("utf-8")
        kept = data[: max(0, OUTPUT_LIMIT - len(text[stream]))]
        truncated = len(kept) < len(data)
        if kept or (truncated and stream not in truncation_sent):
            await persist(
                "output",
                {
                    "execution_id": observed_id,
                    "stream": stream,
                    "offset": offsets[stream],
                    "size": len(data),
                    "data": base64.b64encode(kept).decode(),
                    "truncated": truncated,
                },
            )
        if truncated:
            truncation_sent.add(stream)
        text[stream].extend(kept)
        offsets[stream] += len(data)

    argv = [
        LAUNCHER,
        binding.namespace,
        request.command_id,
        binding.network_identity,
        binding.ipv4,
        request.cwd,
        "-",
        "-",
        request.command,
        binding.execution_profile,
        *binding.scope_request(),
        binding.pod_uid,
        binding.environment_revision,
        binding.execution_context_id or "",
    ]
    async with asyncio.timeout(request.timeout_seconds + 5):
        result = await sandbox.commands.run(
            argv,
            opts=RunCommandOpts(
                working_directory="/opt/bbx/control",
                background=request.background,
                timeout=timedelta(seconds=request.timeout_seconds),
            ),
            handlers=ExecutionHandlers(
                on_init=initialized,
                on_stdout=lambda event: output("stdout", event),
                on_stderr=lambda event: output("stderr", event),
                skip_accumulation=True,
            ),
        )
    if not observed_id or result.id != observed_id:
        raise UnknownCommandOutcome("Official initialization evidence is missing")
    terminal_error = (
        result.error is not None
        and result.error.exit_code is not None
        and result.error.exit_code == result.exit_code
    )
    if (request.background and result.complete is None) or (
        not request.background
        and (result.exit_code is None or not (result.complete is not None or terminal_error))
    ):
        raise UnknownCommandOutcome("Official command terminal or start evidence is missing")
    receipt = {
        "execution_id": observed_id,
        "background": request.background,
        "evidence": ("background_start_error" if result.error else "background_start_ack")
        if request.background
        else "foreground_terminal",
        "exit_code": result.exit_code,
        "error": {"name": result.error.name, "exit_code": result.error.exit_code}
        if result.error
        else None,
        "observed_output_bytes": dict(offsets),
        "retained_output_bytes": {stream: len(data) for stream, data in text.items()},
        "output_truncated": {stream: offsets[stream] > len(data) for stream, data in text.items()},
    }
    await persist("returned", receipt)
    return {
        **receipt,
        "command_id": request.command_id,
        "output": {
            stream: {
                "text": data.decode("utf-8", errors="replace"),
                "retained_bytes": len(data),
                "observed_text_bytes": offsets[stream],
                "truncated": offsets[stream] > len(data),
            }
            for stream, data in text.items()
        },
        "raw_bytes_retained": False,
    }


async def dispatch(
    sandbox: Sandbox,
    binding: AssignmentBinding,
    tool: str,
    args: dict,
    *,
    admit: Callable[[], Awaitable[bool]],
    persist: Persist,
    hard_deadline: datetime | None = None,
    read_capture: Callable[[str, int], Awaitable[dict]] | None = None,
) -> dict:
    """Host supplies IDs/binding; model arguments cannot select execution authority."""
    _binding(sandbox, binding)
    allowed = {
        "exec_command": {
            "command_id",
            "invocation_id",
            "command",
            "cwd",
            "background",
            "timeout_seconds",
        },
        "execution_status": {"execution_id"},
        "execution_output": {"execution_id", "cursor"},
        "interrupt_execution": {"execution_id"},
        "read_file": {"path", "offset", "limit"},
        "write_file": {"path", "content", "mode"},
    }
    if tool not in allowed or set(args) - allowed[tool]:
        raise ValueError("Unknown tool or unauthorized execution arguments")
    request = CommandRequest(**args) if tool == "exec_command" else None
    execution_id = (
        _execution_id(args)
        if tool in {"execution_status", "execution_output", "interrupt_execution"}
        else None
    )
    limit = args.get("limit", OUTPUT_LIMIT)
    if tool in {"read_file", "write_file"}:
        _path(args.get("path"))
        if tool == "read_file":
            if (
                args.get("offset", 0) != 0
                or type(limit) is not int
                or not 1 <= limit <= OUTPUT_LIMIT
            ):
                raise ValueError("File reads support a bounded byte prefix only")
        else:
            if (
                not isinstance(args.get("content"), str)
                or len(args["content"].encode()) > OUTPUT_LIMIT
            ):
                raise ValueError("File content exceeds the bounded write size")
            SolverFiles._mode(args.get("mode", 644))
    if tool != "exec_command":
        await _context(sandbox, binding)
    cursor = args.get("cursor", 0)
    if tool == "execution_output" and (
        type(cursor) is not int or not 0 <= cursor <= 9007199254740991
    ):
        raise ValueError("Invalid original output cursor")
    if not await admit():
        raise AlreadyAdmitted("The original tool invocation cannot be dispatched again")
    try:
        if hard_deadline is not None:
            remaining = int((hard_deadline - datetime.now(UTC)).total_seconds())
            if remaining <= 0:
                raise UnknownCommandOutcome("The original tool deadline elapsed before dispatch")
            if request is not None:
                request = replace(request, timeout_seconds=min(request.timeout_seconds, remaining))
        if request is not None:
            return await _run(sandbox, binding, request, persist)
        if tool == "execution_status":
            assert execution_id is not None
            status = await sandbox.commands.get_command_status(execution_id)
            if status.id != execution_id:
                raise UnknownCommandOutcome("Official status belongs to another execution")
            result = {
                **status.model_dump(mode="json", exclude={"content", "error"}),
                "execution_id": execution_id,
                "process_scope_drained": False,
            }
            await persist("status", result)
            return result
        if tool == "execution_output":
            assert execution_id is not None
            try:
                logs = await sandbox.commands.get_background_command_logs(execution_id, cursor)
            except Exception as error:
                if not isinstance(error, NativeLogResponseLimit) and not isinstance(
                    error.__cause__, NativeLogResponseLimit
                ):
                    raise
                unavailable = {"execution_id": execution_id, "reason": "native_log_response_limit"}
                await persist("logs_unavailable", unavailable)
                return {**unavailable, "output_unavailable": True}
            if logs.cursor is None or logs.cursor < cursor:
                raise UnknownCommandOutcome("The original output cursor moved backwards")
            source = logs.content.encode()
            kept = source[:OUTPUT_LIMIT]
            result = {
                "execution_id": execution_id,
                "content": kept.decode("utf-8", errors="replace"),
                "cursor": logs.cursor,
                "from_cursor": cursor,
                "source_bytes": logs.cursor - cursor,
                "retained_text_bytes": len(kept),
                "truncated": len(source) > len(kept),
                "text_integrity": "loss_possible"
                if len(source) != logs.cursor - cursor
                or b"\xef\xbf\xbd" in source
                or len(source) > len(kept)
                else "text_only",
                "raw_bytes_retained": False,
            }
            await persist("logs", result)
            return result
        if tool == "interrupt_execution":
            assert execution_id is not None
            await persist("cancel", {"execution_id": execution_id, "phase": "requested"})
            await sandbox.commands.interrupt(execution_id)
            result = {
                "execution_id": execution_id,
                "cancel_acknowledged": True,
                "process_scope_drained": False,
            }
            await persist("cancel", {**result, "phase": "acknowledged"})
            return result
        if tool == "read_file" and read_capture is not None:
            return await read_capture(args["path"], limit)
        files = sandbox.files_with_identity(SOLVER_UID, SOLVER_GID)
        if tool == "write_file":
            await SolverFiles(files).write_file(
                args["path"], args["content"], mode=args.get("mode", 644)
            )
            result = {"path": args["path"], "written_bytes": len(args["content"].encode())}
        else:
            data = bytearray()
            stream = await files.read_bytes_stream(args["path"], chunk_size=8192)
            async with aclosing(stream):
                async for chunk in stream:
                    data.extend(chunk[: max(0, limit + 1 - len(data))])
                    if len(data) > limit:
                        break
            result = {
                "path": args["path"],
                "content": data[:limit].decode("utf-8", errors="replace"),
                "data_base64": base64.b64encode(data[:limit]).decode(),
                "retained_bytes": min(len(data), limit),
                "truncated": len(data) > limit,
                "text_integrity": "loss_possible"
                if "\ufffd" in data[:limit].decode("utf-8", errors="replace")
                else "text_only",
                "offset": 0,
            }
        return result
    except BaseException as error:
        await persist(
            "unknown",
            {
                "reason": "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "sdk_or_persistence_unconfirmed"
            },
        )
        raise


async def assignment_control(
    sandbox: Sandbox, binding: AssignmentBinding, *, stop: bool, persist: Persist
) -> dict:
    """Caller durably claims a stop once; uncertain retries must use status, not another kill."""
    _binding(sandbox, binding)
    scope_id, encoded = binding.scope_request()
    observed_id = None

    async def initialized(event: ExecutionInit):
        nonlocal observed_id
        if not event.id or (observed_id and observed_id != event.id):
            raise UnknownAssignmentCleanup("Assignment control execution ID changed")
        await persist(
            "scope_bound",
            {
                "scope_id": scope_id,
                "execution_id": event.id,
                "operation": "stop" if stop else "status",
            },
        )
        observed_id = event.id

    try:
        async with asyncio.timeout(15):
            result = await sandbox.commands.run(
                [
                    LAUNCHER,
                    "--assignment-stop" if stop else "--assignment-status",
                    scope_id,
                    encoded,
                    binding.pod_uid,
                    binding.environment_revision,
                    binding.execution_context_id or "",
                ],
                opts=RunCommandOpts(
                    working_directory="/opt/bbx/control", timeout=timedelta(seconds=10)
                ),
                handlers=ExecutionHandlers(on_init=initialized),
            )
        if (
            not observed_id
            or result.id != observed_id
            or result.complete is None
            or result.exit_code != 0
            or result.error is not None
        ):
            raise UnknownAssignmentCleanup("Original Assignment terminal evidence is missing")
        receipt = json.loads("".join(message.text for message in result.logs.stdout))
        if (
            receipt.get("scope_id") != scope_id
            or receipt.get("execution_context_id") != binding.execution_context_id
            or receipt.get("state") != "confirmed"
            or receipt.get("reason") != "observed_empty"
            or receipt.get("sealed") is not True
            or type(receipt.get("populated")) is not int
            or receipt["populated"] != 0
            or any(
                type(receipt.get(k)) is not int or receipt[k] <= 0
                for k in [
                    "cgroup_namespace",
                    "mount_namespace",
                    "mount_id",
                    "mount_device",
                    "mount_inode",
                    "scope_device",
                    "scope_inode",
                ]
            )
            or not isinstance(receipt.get("boot_id"), str)
            or not receipt["boot_id"]
        ):
            raise UnknownAssignmentCleanup(
                "Original Assignment scope is not confirmed sealed and empty"
            )
        await persist(
            "scope_observed",
            {"scope_id": scope_id, "execution_id": observed_id, "receipt": receipt},
        )
        return receipt
    except BaseException as error:
        await persist(
            "scope_unknown",
            {
                "scope_id": scope_id,
                "reason": "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "scope_evidence_unconfirmed",
            },
        )
        raise
