"""Only transport uncertainty is scripted; successful execution uses real execd tests."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from opensandbox import Sandbox
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import SandboxEndpoint

from bbx_sandbox_execution.connect import connect_bound_execd
from bbx_sandbox_execution.execution import (
    AlreadyAdmitted,
    AssignmentBinding,
    CommandJournal,
    CommandRequest,
    SandboxExecution,
    UnknownCommandOutcome,
)


async def test_lost_response_is_durable_and_never_redispatched(tmp_path: Path, monkeypatch):
    sandbox = connect_bound_execd("sandbox-p1", "127.0.0.1:1", "fixture-token")
    binding = AssignmentBinding(
        task_id="task-p1",
        agent_id="worker-a",
        role="worker",
        assignment_id="assignment-a",
        generation=1,
        epoch=1,
        sandbox_id="sandbox-p1",
        pod_uid="pod-p1",
        environment_revision="revision-1",
        network_identity="network-a",
        namespace="worker-a",
        ipv4="198.19.1.2",
    )
    path = tmp_path / "private" / "commands.sqlite"
    journal = CommandJournal(path)
    adapter = SandboxExecution(sandbox, binding, journal)
    request = CommandRequest(command_id="command-lost", invocation_id="inv-lost", command="exit 0")
    sent = 0

    async def response_lost(*args, **kwargs):
        nonlocal sent
        sent += 1
        assert [entry["kind"] for entry in journal.events(request.command_id)] == [
            "admitted",
            "dispatching",
        ]
        raise httpx.ReadError("Scripted response loss after dispatch")

    monkeypatch.setattr(sandbox.commands, "run", response_lost)
    try:
        with pytest.raises(httpx.ReadError):
            await adapter.run(request)
        assert journal.events(request.command_id)[-1]["kind"] == "unknown"
        assert journal.output_preview(request.command_id)["coverage"] == "partial"
        with pytest.raises(AlreadyAdmitted):
            await adapter.run(request)
        journal.close()
        journal = CommandJournal(path)
        adapter = SandboxExecution(sandbox, binding, journal)
        with pytest.raises(AlreadyAdmitted):
            await adapter.run(request)
        with pytest.raises(ValueError, match="different input"):
            await adapter.run(replace(request, command="exit 1"))
        with pytest.raises(ValueError, match="unknown"):
            await adapter.status(request.command_id)
        assert sent == 1
    finally:
        journal.close()
        await sandbox.close()


def test_empty_endpoint_token_is_rejected_before_any_request():
    with pytest.raises(ValueError, match="empty"):
        connect_bound_execd("sandbox-p1", "127.0.0.1:1", "")


@pytest.mark.parametrize("background", [False, True])
async def test_clean_sdk_eof_keeps_init_and_partial_output_unknown(
    tmp_path: Path, background: bool
):
    sent = 0

    async def incomplete_response(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        assert request.method == "POST" and request.url.path == "/command"
        events = [
            {"type": "init", "text": "execution-eof", "timestamp": 1},
            {"type": "stdout", "text": "partial output", "timestamp": 2},
        ]
        data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=data)

    transport = httpx.MockTransport(incomplete_response)
    config = ConnectionConfig(transport=transport, disable_metrics=True)
    factory = AdapterFactory(config)
    endpoint = SandboxEndpoint(endpoint="fixture.test")
    sandbox = Sandbox(
        sandbox_id="sandbox-p1",
        sandbox_service=factory.create_sandbox_service(),
        filesystem_service=factory.create_filesystem_service(endpoint),
        command_service=factory.create_command_service(endpoint),
        health_service=factory.create_health_service(endpoint),
        metrics_service=factory.create_metrics_service(endpoint),
        egress_service=factory.create_network_policy_service("sandbox-p1"),
        connection_config=config,
    )
    binding = AssignmentBinding(
        task_id="task-p1",
        agent_id="worker-a",
        role="worker",
        assignment_id="assignment-a",
        generation=1,
        epoch=1,
        sandbox_id="sandbox-p1",
        pod_uid="pod-p1",
        environment_revision="revision-1",
        network_identity="network-a",
        namespace="worker-a",
        ipv4="198.19.1.2",
    )
    journal = CommandJournal(tmp_path / "private" / "commands.sqlite")
    adapter = SandboxExecution(sandbox, binding, journal)
    command = CommandRequest(
        command_id="command-eof", invocation_id="inv-eof", command="sleep 60", background=background
    )
    try:
        with pytest.raises(UnknownCommandOutcome, match="response EOF"):
            await adapter.run(command)
        assert journal.execution_id(command.command_id, binding) == "execution-eof"
        events = journal.events(command.command_id)
        assert [event["kind"] for event in events] == [
            "admitted",
            "dispatching",
            "execution_bound",
            "output",
            "unknown",
        ]
        with pytest.raises(AlreadyAdmitted):
            await adapter.run(command)
        assert sent == 1
    finally:
        journal.close()
        await sandbox.close()
        await transport.aclose()


def test_background_preview_backfills_out_of_order_prefix_without_duplicate_bytes(tmp_path: Path):
    journal = CommandJournal(tmp_path / "private" / "commands.sqlite")
    binding = AssignmentBinding(
        task_id="task-p1",
        agent_id="worker-a",
        role="worker",
        assignment_id="assignment-a",
        generation=1,
        epoch=1,
        sandbox_id="sandbox-p1",
        pod_uid="pod-p1",
        environment_revision="revision-1",
        network_identity="network-a",
        namespace="worker-a",
        ipv4="198.19.1.2",
    )
    command = CommandRequest(
        command_id="command-prefix",
        invocation_id="inv-prefix",
        command="echo output",
        background=True,
    )
    try:
        journal.admit(binding, command)
        journal.append(
            command.command_id, "background_output", from_cursor=5, cursor=10, content="fghij"
        )
        first = journal.output_preview(command.command_id)
        assert first["text"] == "fghij" and first["source_gaps"] == [[0, 5]]
        assert first["coverage"] == "partial" and first["bytes"] == 5
        journal.append(
            command.command_id, "background_output", from_cursor=0, cursor=10, content="abcdefghij"
        )
        journal.append(
            command.command_id, "background_output", from_cursor=5, cursor=10, content="fghij"
        )
        preview = journal.output_preview(command.command_id)
        assert preview["text"] == "abcdefghij" and preview["bytes"] == 10
        assert preview["source_gaps"] == [] and preview["coverage"] == "collected"
        assert preview["raw_bytes_retained"] is False
    finally:
        journal.close()
