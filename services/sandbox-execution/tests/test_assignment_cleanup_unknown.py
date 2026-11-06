"""Only response uncertainty is scripted; scope ownership/drain use the real container."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from opensandbox import Sandbox
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import SandboxEndpoint

from bbx_sandbox_execution.execution import (
    AssignmentBinding,
    CommandJournal,
    CommandRequest,
    SandboxExecution,
    UnknownAssignmentCleanup,
)


async def test_control_clean_eof_retains_id_and_never_repeats_stop(tmp_path: Path):
    operations = []

    async def eof(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        operations.append(body)
        event = {"type": "init", "text": "cleanup-id-" + str(len(operations)), "timestamp": 1}
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content="data: " + json.dumps(event) + "\n\n",
        )

    transport = httpx.MockTransport(eof)
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
        pod_uid="pod-a",
        environment_revision="revision-a",
        network_identity="network-a",
        namespace="worker-a",
        ipv4="198.19.1.2",
        process_accounting="cgroup-v2",
        execution_context_id="a" * 64,
    )
    path = tmp_path / "private" / "commands.sqlite"
    journal = CommandJournal(path)
    command = CommandRequest(command_id="original", invocation_id="inv-original", command="exit 7")
    journal.admit(binding, command)
    try:
        adapter = SandboxExecution(sandbox, binding, journal)
        with pytest.raises(UnknownAssignmentCleanup):
            await adapter.stop_assignment(command.command_id)
        assert journal.events(command.command_id)[-1]["kind"] == "assignment_cleanup_unknown"
        journal.close()
        journal = CommandJournal(path)
        adapter = SandboxExecution(sandbox, binding, journal)
        with pytest.raises(UnknownAssignmentCleanup):
            await adapter.stop_assignment(command.command_id)
        encoded = [json.dumps(item) for item in operations]
        assert "--assignment-stop" in encoded[0] and "--assignment-status" in encoded[1]
        events = journal.events(command.command_id)
        assert [
            item["execution_id"] for item in events if item["kind"] == "assignment_control_bound"
        ] == ["cleanup-id-1", "cleanup-id-2"]
        assert not any(item["kind"] == "assignment_cleanup_confirmed" for item in events)
        for changed in (
            replace(binding, generation=2),
            replace(binding, pod_uid="replacement-pod"),
            replace(binding, environment_revision="revision-b"),
        ):
            assert changed.scope_request()[0] != binding.scope_request()[0]
            with pytest.raises(ValueError, match="binding"):
                await SandboxExecution(sandbox, changed, journal).stop_assignment(
                    command.command_id
                )
        assert len(operations) == 2
    finally:
        journal.close()
        await sandbox.close()
        await transport.aclose()
