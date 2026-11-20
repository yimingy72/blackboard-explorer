"""Direct CTF commands share the Runtime network and retain assignment accounting."""

import json
import os
import time
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from testcontainers.core.container import DockerContainer

from bbx_sandbox_execution.connect import connect_bound_execd, read_execution_context
from bbx_sandbox_execution.execution import (
    AssignmentBinding,
    CommandJournal,
    CommandRequest,
    SandboxExecution,
)

pytestmark = pytest.mark.integration


async def test_direct_runtime_files_network_and_assignment_cleanup(tmp_path):
    token = "ctf-direct-fixture-token"
    pod = "ctf-direct-" + uuid4().hex
    container = DockerContainer(
        os.environ.get("BBX_CTF_DIRECT_TEST_IMAGE", "bbx-peer-sandbox:20261120"),
        name="bbx-ctf-direct-test-" + uuid4().hex[:10],
        cap_add=["SYS_ADMIN"],
        security_opt=["apparmor=unconfined"],
        cgroupns="private",
        entrypoint="/bin/sh",
    )
    container.with_env("EXECD_ACCESS_TOKEN", token)
    container.with_env("BBX_POD_UID", pod)
    container.with_env("BBX_ENVIRONMENT_REVISION", "ctf-direct-test")
    container.with_command(
        "-c '. /opt/bbx/control/kubernetes-setup.sh && "
        "exec /opt/bbx/control/launcher --execd-cgroup-v2'"
    )
    container.ports["44772/tcp"] = ("127.0.0.1", None)  # pyright: ignore[reportArgumentType]
    with container:
        endpoint = "127.0.0.1:" + str(container.get_exposed_port(44772))
        with httpx.Client(trust_env=False) as client:
            until = time.monotonic() + 30
            while time.monotonic() < until:
                try:
                    if client.get("http://" + endpoint + "/ready", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail(str(container.get_logs()))
        sandbox = connect_bound_execd("ctf-direct-test", endpoint, token)
        journal = CommandJournal(tmp_path / "private" / "commands.sqlite")
        try:
            context = await read_execution_context(sandbox)
            binding = AssignmentBinding(
                task_id="ctf-direct-test",
                agent_id="member-1",
                role="worker",
                assignment_id="assignment-1",
                generation=1,
                epoch=1,
                sandbox_id="ctf-direct-test",
                pod_uid=pod,
                environment_revision="ctf-direct-test",
                namespace="ctf-shared",
                network_identity="ctf-shared",
                ipv4="0.0.0.0",
                execution_profile="ctf-direct",
                process_accounting="cgroup-v2",
                execution_context_id=context["execution_context_id"],
            )
            first = SandboxExecution(sandbox, binding, journal)
            command = CommandRequest(
                command_id="direct-first",
                invocation_id="direct-first",
                command=(
                    "python3 -c 'import json,os,pwn; "
                    'print(json.dumps({"netns":os.stat("/proc/self/ns/net").st_ino,'
                    '"agent_ip":os.environ.get("BBX_AGENT_IP")}))\''
                ),
            )
            assert (await first.run(command)).exit_code == 0
            observed = json.loads(
                journal.output_preview(command.command_id)["text"].splitlines()[-1]
            )
            actual = container.get_wrapped_container().exec_run(
                ["stat", "-Lc", "%i", "/proc/self/ns/net"]
            )
            assert actual.exit_code == 0 and observed["netns"] == int(actual.output)
            assert observed["agent_ip"] is None
            assert (
                container.get_wrapped_container()
                .exec_run(
                    ["sh", "-c", "test ! -d /run/netns && test ! -d /var/lib/bbx/control/networks"]
                )
                .exit_code
                == 0
            )
            await first.files.write_file("/workspace/shared.bin", b"direct\x00\xff")
            second = SandboxExecution(
                sandbox,
                replace(binding, agent_id="member-2", assignment_id="assignment-2"),
                journal,
            )
            assert await second.files.read_bytes("/workspace/shared.bin") == b"direct\x00\xff"
            running = CommandRequest(
                command_id="direct-background",
                invocation_id="direct-background",
                command="sleep 60",
                background=True,
            )
            await first.run(running)
            receipt = await first.stop_assignment(running.command_id)
            assert receipt["state"] == "confirmed" and receipt["populated"] == 0
            next_command = CommandRequest(
                command_id="direct-next", invocation_id="direct-next", command="echo next"
            )
            assert (await second.run(next_command)).exit_code == 0
        finally:
            journal.close()
            await sandbox.close()
