"""Necessary identity, once-dispatch, byte-bound and positive-scope receipts; no network."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bbx_runtime import sandbox_tools as tools
from bbx_runtime.sandbox_mcp import SandboxMCP
from bbx_sandbox_execution.execution import (
    AlreadyAdmitted,
    AssignmentBinding,
    UnknownAssignmentCleanup,
    UnknownCommandOutcome,
)
from opensandbox.models.execd import (
    CommandLogs,
    CommandStatus,
    Execution,
    ExecutionComplete,
    ExecutionInit,
    OutputMessage,
)


def fake(**values) -> Any:
    return SimpleNamespace(**values)


async def test_ordinary_file_capture_keeps_one_stream_and_full_bytes_beyond_model_preview():
    body = b"a" * (70 * 1024) + b"original-final-bytes"
    path = "/workspace/agents/agent-1/evidence.bin"
    info = fake(path=path, entry_type="file", size=len(body))
    opened = 0
    uploaded = bytearray()

    async def read(_path, *, chunk_size):
        nonlocal opened
        opened += 1
        assert _path == path and chunk_size == 65536

        async def chunks():
            for offset in range(0, len(body), 8192):
                yield body[offset : offset + 8192]

        return chunks()

    files = fake(get_file_info=AsyncMock(return_value={path: info}), read_bytes_stream=read)
    sandbox = fake(files_with_identity=lambda *_identity: files)
    host = SandboxMCP.__new__(SandboxMCP)
    host.config = {"hard_deadline": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}

    async def control(method, path, body=None, *, authorization=None, sdk=None, raw=None):
        assert method == "PUT" and path == "calls/original-read/evidence" and raw is not None
        async for block in raw:
            uploaded.extend(block)
        return {
            "uri": "evidence/task/agent-1/original.bin",
            "size": len(uploaded),
            "sha256": hashlib.sha256(uploaded).hexdigest(),
        }

    host.control = control
    result = await host.capture_board_file(
        sandbox, fake(invocation_id="original-read"), path, 65536
    )
    assert opened == 1 and bytes(uploaded) == body
    assert result["retained_bytes"] == 65536 and result["truncated"] is True
    assert result["content"] == "a" * 65536
    assert result["artifact"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert result["artifact"]["size"] == len(body)

    files.get_file_info.return_value = {path: fake(path=path, entry_type="directory", size=0)}
    assert (await host.capture_board_file(sandbox, fake(invocation_id="other"), path, 65536))[
        "output_unavailable"
    ] is True
    assert opened == 1


def binding():
    return AssignmentBinding(
        task_id="task-1",
        agent_id="lead",
        role="lead",
        assignment_id="assignment-1",
        generation=1,
        epoch=1,
        sandbox_id="sandbox-1",
        pod_uid="sandbox-pod-uid",
        environment_revision="environment-1",
        network_identity="worker-a",
        namespace="worker-a",
        ipv4="198.19.1.2",
        process_accounting="cgroup-v2",
        execution_context_id="a" * 64,
    )


def command():
    return {
        "command_id": "command-1",
        "invocation_id": "invocation-1",
        "command": "printf harmless",
        "cwd": "/workspace",
        "timeout_seconds": 1,
    }


async def test_exec_persists_actual_id_before_bounded_output_and_terminal():
    events = []

    async def persist(kind, payload):
        events.append((kind, payload))

    async def run(argv, *, opts, handlers):
        assert argv[0] == tools.LAUNCHER and argv[-1] == "a" * 64
        assert opts.working_directory == "/opt/bbx/control" and handlers.skip_accumulation
        await handlers.on_init(ExecutionInit(id="actual-1", timestamp=1))
        assert events[0] == ("execution_bound", {"execution_id": "actual-1"})
        for _ in range(3):
            await handlers.on_stdout(OutputMessage(text="x" * 40000, timestamp=1))
        return Execution(
            id="actual-1",
            exit_code=0,
            complete=ExecutionComplete(timestamp=1, execution_time_in_millis=1),
        )

    sandbox = fake(id="sandbox-1", commands=SimpleNamespace(run=run))
    result = await tools.dispatch(
        sandbox,
        binding(),
        "exec_command",
        command(),
        admit=AsyncMock(return_value=True),
        persist=persist,
    )
    assert result["output"]["stdout"]["retained_bytes"] == 65536
    assert result["output"]["stdout"]["observed_text_bytes"] == 120000
    assert result["output_truncated"]["stdout"] is True
    assert (
        events[-1][0] == "returned" and events[-1][1]["observed_output_bytes"]["stdout"] == 120000
    )
    assert len([e for e in events if e[0] == "output"]) == 2


async def test_lost_execution_persistence_never_replays_post():
    calls = []
    events = []
    admitted = False

    async def admit():
        nonlocal admitted
        first = not admitted
        admitted = True
        return first

    async def persist(kind, payload):
        events.append(kind)
        if kind == "execution_bound":
            raise RuntimeError("synthetic ledger unavailable")

    async def run(argv, *, opts, handlers):
        calls.append(argv)
        await handlers.on_init(ExecutionInit(id="actual-1", timestamp=1))
        raise AssertionError("Must not continue after persistence failure")

    sandbox = fake(id="sandbox-1", commands=SimpleNamespace(run=run))
    with pytest.raises(RuntimeError):
        await tools.dispatch(
            sandbox, binding(), "exec_command", command(), admit=admit, persist=persist
        )
    with pytest.raises(AlreadyAdmitted):
        await tools.dispatch(
            sandbox, binding(), "exec_command", command(), admit=admit, persist=persist
        )
    assert len(calls) == 1 and events == ["execution_bound", "unknown"]


async def test_unknown_authority_arguments_fail_before_admission():
    admit = AsyncMock(return_value=True)
    sandbox = fake(id="sandbox-1")
    with pytest.raises(ValueError):
        await tools.dispatch(
            sandbox,
            binding(),
            "exec_command",
            {**command(), "uid": 0},
            admit=admit,
            persist=AsyncMock(),
        )
    assert admit.await_count == 0


async def test_files_use_low_uid_stream_and_close_at_byte_prefix(monkeypatch):
    b = binding()
    closed = []
    identities = []
    monkeypatch.setattr(
        tools,
        "read_execution_context",
        AsyncMock(
            return_value={
                "pod_uid": b.pod_uid,
                "environment_revision": b.environment_revision,
                "execution_context_id": b.execution_context_id,
            }
        ),
    )

    async def read_bytes_stream(path, *, chunk_size):
        assert chunk_size == 8192

        async def stream():
            try:
                for _ in range(200):
                    yield b"x" * 8192
            finally:
                closed.append(True)

        return stream()

    def files(uid, gid):
        identities.append((uid, gid))
        return SimpleNamespace(read_bytes_stream=read_bytes_stream)

    result = await tools.dispatch(
        fake(id="sandbox-1", files_with_identity=files),
        b,
        "read_file",
        {"path": "/workspace/one-long-line", "limit": 100},
        admit=AsyncMock(return_value=True),
        persist=AsyncMock(),
    )
    assert result["retained_bytes"] == 100 and result["truncated"] is True
    assert len(result["content"]) == 100 and closed == [True] and identities == [(10000, 10000)]


async def test_file_context_replacement_rejects_before_admit(monkeypatch):
    monkeypatch.setattr(
        tools,
        "read_execution_context",
        AsyncMock(
            return_value={
                "pod_uid": "replacement",
                "environment_revision": "environment-1",
                "execution_context_id": "a" * 64,
            }
        ),
    )
    admit = AsyncMock(return_value=True)
    with pytest.raises(UnknownCommandOutcome):
        await tools.dispatch(
            fake(id="sandbox-1"),
            binding(),
            "write_file",
            {"path": "/workspace/local", "content": "harmless"},
            admit=admit,
            persist=AsyncMock(),
        )
    assert admit.await_count == 0


async def test_status_hides_trusted_argv_and_interrupt_ack_is_not_process_drain(monkeypatch):
    b = binding()
    monkeypatch.setattr(
        tools,
        "read_execution_context",
        AsyncMock(
            return_value={
                "pod_uid": b.pod_uid,
                "environment_revision": b.environment_revision,
                "execution_context_id": b.execution_context_id,
            }
        ),
    )
    commands = SimpleNamespace(
        get_command_status=AsyncMock(
            return_value=CommandStatus(
                id="actual-1",
                content="launcher private context",
                error="private metadata",
                running=True,
            )
        ),
        interrupt=AsyncMock(),
        get_background_command_logs=AsyncMock(return_value=CommandLogs(content="output", cursor=6)),
    )
    sandbox = fake(id="sandbox-1", commands=commands)
    kwargs = {"admit": AsyncMock(return_value=True), "persist": AsyncMock()}
    status = await tools.dispatch(
        sandbox, binding(), "execution_status", {"execution_id": "actual-1"}, **kwargs
    )
    assert "content" not in status and "error" not in status
    cancel = await tools.dispatch(
        sandbox, binding(), "interrupt_execution", {"execution_id": "actual-1"}, **kwargs
    )
    assert cancel["cancel_acknowledged"] and not cancel["process_scope_drained"]
    logs = await tools.dispatch(
        sandbox, binding(), "execution_output", {"execution_id": "actual-1", "cursor": 0}, **kwargs
    )
    assert logs["cursor"] == 6 and logs["text_integrity"] == "text_only"


async def test_assignment_empty_receipt_requires_original_nonce_and_positive_scope():
    b = binding()
    scope_id, _ = b.scope_request()
    events = []
    receipt = {
        "scope_id": scope_id,
        "execution_context_id": b.execution_context_id,
        "state": "confirmed",
        "reason": "observed_empty",
        "sealed": True,
        "populated": 0,
        "boot_id": "boot-1",
        **{
            k: 1
            for k in [
                "cgroup_namespace",
                "mount_namespace",
                "mount_id",
                "mount_device",
                "mount_inode",
                "scope_device",
                "scope_inode",
            ]
        },
    }

    async def run(argv, *, opts, handlers):
        assert argv[1] == "--assignment-status"
        await handlers.on_init(ExecutionInit(id="scope-1", timestamp=1))
        result = Execution(
            id="scope-1",
            exit_code=0,
            complete=ExecutionComplete(timestamp=1, execution_time_in_millis=1),
        )
        result.logs.add_stdout(OutputMessage(text=json.dumps(receipt), timestamp=1))
        return result

    async def persist(kind, payload):
        events.append(kind)

    sandbox = fake(id="sandbox-1", commands=SimpleNamespace(run=run))
    assert await tools.assignment_control(sandbox, b, stop=False, persist=persist) == receipt
    receipt["populated"] = True
    with pytest.raises(UnknownAssignmentCleanup):
        await tools.assignment_control(sandbox, b, stop=False, persist=persist)
    assert events[-1] == "scope_unknown"


async def test_native_log_transport_aborts_unbounded_body_without_claiming_cursor(monkeypatch):
    import httpx

    closed = []

    class Chunked(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * tools.NATIVE_LOG_RESPONSE_LIMIT
            yield b"y"

        async def aclose(self):
            closed.append(True)

    async def request(value):
        assert value.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, stream=Chunked())

    transport = tools.BoundedExecdLogTransport()
    monkeypatch.setattr(transport.transport, "handle_async_request", request)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(tools.NativeLogResponseLimit):
            await client.get("http://synthetic/command/actual-1/logs")
    assert closed
    b = binding()
    monkeypatch.setattr(
        tools,
        "read_execution_context",
        AsyncMock(
            return_value={
                "pod_uid": b.pod_uid,
                "environment_revision": b.environment_revision,
                "execution_context_id": b.execution_context_id,
            }
        ),
    )
    persist = AsyncMock()
    sandbox = fake(
        id="sandbox-1",
        commands=SimpleNamespace(
            get_background_command_logs=AsyncMock(
                side_effect=tools.NativeLogResponseLimit("synthetic limit")
            )
        ),
    )
    result = await tools.dispatch(
        sandbox,
        b,
        "execution_output",
        {"execution_id": "actual-1", "cursor": 12},
        admit=AsyncMock(return_value=True),
        persist=persist,
    )
    assert result["output_unavailable"] and "cursor" not in result
    persist.assert_awaited_once_with(
        "logs_unavailable", {"execution_id": "actual-1", "reason": "native_log_response_limit"}
    )


def test_bound_sdk_rejects_untrusted_ingress_headers_and_preserves_native_route(monkeypatch):
    from bbx_sandbox_execution import connect

    endpoints = []

    class Factory:
        def __init__(self, config):
            pass

        def __getattr__(self, name):
            def adapter(*args):
                if name == "create_command_service":
                    endpoints.append(args[0])
                return object()

            return adapter

    monkeypatch.setattr(connect, "AdapterFactory", Factory)
    monkeypatch.setattr(connect, "Sandbox", lambda **kwargs: kwargs)
    for headers in [{"Authorization": "forbidden"}, {"OpenSandbox-Ingress-To": "another-44772"}]:
        with pytest.raises(ValueError):
            connect.connect_bound_execd(
                "sandbox-1", "synthetic:80", "execd-key", endpoint_headers=headers
            )
    original = {
        "OpenSandbox-Ingress-To": "sandbox-1-44772",
        "OpenSandbox-Secure-Access": "synthetic-route-key",
    }
    connect.connect_bound_execd("sandbox-1", "synthetic:80", "execd-key", endpoint_headers=original)
    assert endpoints[0].headers == {**original, "X-EXECD-ACCESS-TOKEN": "execd-key"}
