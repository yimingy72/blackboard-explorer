"""Real official 1.2.0 SDK, execd, shared UID, namespaces and existing CTF tools."""

import asyncio
import json
import shlex
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from conftest import TOKEN, admin, binding, write_grants
from opensandbox.exceptions import SandboxException
from testcontainers.core.container import DockerContainer

from bbx_sandbox_execution.connect import ACCESS_HEADER, connect_bound_execd
from bbx_sandbox_execution.execution import (
    AlreadyAdmitted,
    CommandJournal,
    CommandRequest,
    SandboxExecution,
)
from bbx_sandbox_execution.proxy import issue_proxy_grant

pytestmark = pytest.mark.integration


def request(command: str, **kwargs) -> CommandRequest:
    identifier = uuid4().hex
    return CommandRequest(
        command_id=identifier, invocation_id="inv-" + identifier, command=command, **kwargs
    )


def python(code: str) -> str:
    return "python3 -c " + shlex.quote(code)


async def wait_exited(adapter: SandboxExecution, command_id: str) -> dict:
    for _ in range(100):
        status = await adapter.status(command_id)
        if status["running"] is False:
            return status
        await asyncio.sleep(0.05)
    pytest.fail("Known official execution did not stop")


async def test_identity_files_foreground_and_output(container: DockerContainer, tmp_path: Path):
    sandbox = connect_bound_execd(
        "sandbox-p1", "127.0.0.1:" + str(container.get_exposed_port(44772)), TOKEN
    )
    journal = CommandJournal(tmp_path / "private" / "commands.sqlite")
    adapter = SandboxExecution(sandbox, binding(container), journal)
    try:
        identity = request(
            python(
                "import json,os;from pathlib import Path;"
                "s=Path('/proc/self/status').read_text();"
                "print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'groups':os.getgroups(),"
                "'env':dict(os.environ),'status':s,'fds':os.listdir('/proc/self/fd')}))"
            )
        )
        result = await adapter.run(identity)
        assert result.exit_code == 0
        observed = json.loads(journal.output_preview(identity.command_id)["text"])
        assert (observed["uid"], observed["gid"], observed["groups"]) == (10000, 10000, [])
        for name in (
            "BBX_CONTROL_MARKER",
            "BBX_FILE_MARKER",
            "EXECD_ENVS",
            "EXECD_ACCESS_TOKEN",
            "LD_PRELOAD",
            "BASH_ENV",
            "PYTHONPATH",
            "PYTHONSTARTUP",
        ):
            assert name not in observed["env"]
        assert "CapEff:\t0000000000000000" in observed["status"]
        assert "CapBnd:\t0000000000000000" in observed["status"]
        assert "NoNewPrivs:\t1" in observed["status"]
        assert len(observed["fds"]) <= 4  # Python's directory listing temporarily owns one FD.
        events = journal.events(identity.command_id)
        assert [event["kind"] for event in events[:3]] == [
            "admitted",
            "dispatching",
            "execution_bound",
        ]
        assert journal.execution_id(identity.command_id, adapter.binding) == result.id
        evidence = json.loads(
            admin(container, "cat", f"/var/lib/bbx/control/launches/{identity.command_id}.json")
        )
        assert evidence["uid"] == 10000 and evidence["capabilities"] == "none"
        assert evidence["network_identity"] == adapter.binding.network_identity
        assert evidence["pid"] == evidence["pgid"]
        assert evidence["start_ticks"] > 0
        assert (
            admin(
                container,
                "sh",
                "-c",
                "test ! -e /var/lib/bbx/control/loader-poisoned && "
                "test ! -e /var/lib/bbx/control/shell-poisoned",
            )
            == ""
        )

        binary = b"shared\x00binary\xff\n"
        await adapter.files.write_file("/workspace/shared.bin", binary)
        assert await adapter.files.read_bytes("/workspace/shared.bin") == binary
        second = SandboxExecution(sandbox, binding(container, 2), journal)
        assert await second.files.read_bytes("/workspace/shared.bin") == binary
        await second.files.write_file("/workspace/shared.txt", "second worker")
        assert await adapter.files.read_file("/workspace/shared.txt") == "second worker"
        assert (
            admin(container, "stat", "-c", "%u:%g:%a", "/workspace/shared.bin").strip()
            == "10000:10000:644"
        )
        with pytest.raises(ValueError, match="special bits"):
            await adapter.files.set_permissions("/workspace/shared.bin", mode=4644)
        for operation in (
            adapter.files.read_bytes("/var/lib/bbx/control/protected"),
            adapter.files.write_file("/opt/bbx/control/launcher", "overwrite"),
        ):
            with pytest.raises(SandboxException):
                await operation
        symlink = request("ln -s /var/lib/bbx/control/protected /workspace/control-link")
        assert (await adapter.run(symlink)).exit_code == 0
        with pytest.raises(SandboxException):
            await adapter.files.read_bytes("/workspace/control-link")

        failed = request(
            "set -o pipefail; printf 'pipeline\\n' | tr a-z A-Z; "
            "printf 'known stderr\\n' >&2; exit 7"
        )
        result = await adapter.run(failed)
        assert result.exit_code == 7 and result.error is not None and result.error.exit_code == 7
        assert "PIPELINE" in journal.output_preview(failed.command_id)["text"]
        assert not any(event["kind"] == "unknown" for event in journal.events(failed.command_id))

        large = request(python("print('x'*100000)"))
        result = await adapter.run(large)
        preview = journal.output_preview(large.command_id)
        assert result.logs.stdout == [] and result.logs.stderr == []
        assert preview["bytes"] >= 100000 and preview["truncated"] and preview["retained"]
        assert len(preview["text"].encode()) == 65536
    finally:
        await sandbox.close()
        journal.close()


async def test_background_byte_logs_pid_exec_and_cancellation(
    container: DockerContainer, tmp_path: Path
):
    sandbox = connect_bound_execd(
        "sandbox-p1", "127.0.0.1:" + str(container.get_exposed_port(44772)), TOKEN
    )
    journal = CommandJournal(tmp_path / "private" / "commands.sqlite")
    adapter = SandboxExecution(sandbox, binding(container), journal)
    try:
        running = request(
            "printf '你好\\n\\377\\n'; sleep 60 & child=$!; "
            'printf \'%s %s\\n\' "$$" "$child" > /workspace/background.pids; wait',
            background=True,
        )
        result = await adapter.run(running)
        assert result.id == journal.execution_id(running.command_id, adapter.binding)
        assert journal.events(running.command_id)[-1]["evidence"] == "background_start_ack"
        for _ in range(100):
            try:
                pids = [
                    int(value)
                    for value in (
                        await adapter.files.read_file("/workspace/background.pids")
                    ).split()
                ]
                if len(pids) == 2:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.05)
        else:
            pytest.fail("Background shell did not publish its real child")
        evidence = json.loads(
            admin(container, "cat", f"/var/lib/bbx/control/launches/{running.command_id}.json")
        )
        assert evidence["pid"] == pids[0] == evidence["pgid"]
        logs = await adapter.background_logs(running.command_id)
        assert "你好" in logs["content"]
        assert logs["cursor"] == len("你好\n".encode()) + 2
        assert logs["source_bytes"] != logs["text_bytes"]
        assert logs["text_integrity"] == "loss_possible"
        assert logs["raw_bytes_retained"] is False
        split = await adapter.background_logs(running.command_id, 1)
        assert split["cursor"] == logs["cursor"] and "\ufffd" in split["content"]
        assert split["text_integrity"] == "loss_possible"
        tail = await adapter.background_logs(running.command_id, logs["cursor"])
        assert tail["content"] == "" and tail["cursor"] == logs["cursor"]
        await adapter.interrupt(running.command_id)
        status = await wait_exited(adapter, running.command_id)
        assert status["exit_code"] is not None
        assert "content" not in status
        alive = "unknown"
        for _ in range(100):
            alive = admin(
                container,
                "/usr/bin/python3",
                "-I",
                "-c",
                "from pathlib import Path;"
                f"print([p for p in {pids!r} if Path('/proc/'+str(p)).exists()])",
            ).strip()
            if alive == "[]":
                break
            await asyncio.sleep(0.05)
        assert alive == "[]", "A cancelled shell or child remained"
        await adapter.background_logs(running.command_id, 0)
        preview = journal.output_preview(running.command_id)
        assert preview["bytes"] == logs["cursor"]
        assert preview["text"] == logs["content"] and preview["source_gaps"] == []
        assert preview["text_integrity"] == "loss_possible" and preview["coverage"] == "partial"
        assert any(
            event["kind"] == "cancel_requested" for event in journal.events(running.command_id)
        )
        stale = SandboxExecution(sandbox, replace(adapter.binding, generation=2), journal)
        with pytest.raises(ValueError, match="binding"):
            await stale.interrupt(running.command_id)
        with pytest.raises(AlreadyAdmitted):
            await adapter.run(running)
    finally:
        await sandbox.close()
        journal.close()


async def test_three_assignment_sources_proxy_and_local_control_rejection(
    container: DockerContainer, tmp_path: Path
):
    endpoint = "127.0.0.1:" + str(container.get_exposed_port(44772))
    sandbox = connect_bound_execd("sandbox-p1", endpoint, TOKEN)
    journal = CommandJournal(tmp_path / "private" / "commands.sqlite")
    grants = tmp_path / "grants.json"
    adapters = [
        SandboxExecution(sandbox, binding(container, index), journal) for index in (1, 2, 3)
    ]
    commands = [
        request("curl -fsS --max-time 5 http://198.19.4.2:8080/same-target") for _ in adapters
    ]
    controls = [f"198.19.{index}.1/32" for index in (1, 2, 3, 4)]
    pod_ip = container.get_wrapped_container().attrs["NetworkSettings"]["Networks"]["bridge"][
        "IPAddress"
    ]
    controls.append(pod_ip + "/32")
    passwords = [
        issue_proxy_grant(grants, adapter.binding, command, ["0.0.0.0/0", "::/0"], controls)
        for adapter, command in zip(adapters, commands, strict=True)
    ]
    write_grants(container, grants)
    try:
        results = await asyncio.gather(
            *(
                adapter.run(
                    command,
                    proxy_url=f"http://{command.command_id}:{password}@198.19.{index}.1:18080",
                )
                for index, (adapter, command, password) in enumerate(
                    zip(adapters, commands, passwords, strict=True), start=1
                )
            )
        )
        assert [result.exit_code for result in results] == [0, 0, 0], [
            journal.output_preview(command.command_id) for command in commands
        ]
        records = [
            json.loads(line)
            for line in admin(
                container, "cat", "/var/lib/bbx/control/proxy/records.jsonl"
            ).splitlines()
        ]
        by_command = {record["command_id"]: record for record in records}
        for index, (adapter, command) in enumerate(zip(adapters, commands, strict=True), start=1):
            record = by_command[command.command_id]
            assert record["agent_id"] == adapter.binding.agent_id
            assert record["assignment_id"] == adapter.binding.assignment_id
            assert record["source_ip"] == adapter.binding.ipv4
            assert record["network_identity"] == adapter.binding.network_identity
            assert record["destination_ip"] == "198.19.4.2"
            packet_sources = ""
            for _ in range(20):
                packet_sources = admin(
                    container,
                    "tshark",
                    "-r",
                    f"/var/lib/bbx/control/capture-{index}.pcap",
                    "-T",
                    "fields",
                    "-e",
                    "ip.src",
                )
                if adapter.binding.ipv4 in packet_sources:
                    break
                await asyncio.sleep(0.02)
            assert adapter.binding.ipv4 in packet_sources, admin(
                container, "tcpdump", "-n", "-r", f"/var/lib/bbx/control/capture-{index}.pcap"
            )
        admin(container, "pkill", "-INT", "-x", "tcpdump")

        stolen = request(
            "curl -sS --max-time 3 --proxy "
            + shlex.quote(f"http://{commands[0].command_id}:{passwords[0]}@198.19.2.1:18080")
            + " -o /dev/null -w '%{http_code}' http://198.19.4.2:8080/same-target"
        )
        assert (await adapters[1].run(stolen)).exit_code == 0
        assert journal.output_preview(stolen.command_id)["text"].strip() == "403"

        for destination in (
            "198.19.1.1",
            pod_ip,
            "0.0.0.0",
            "127.0.0.1",
            "[::1]",
            f"[::ffff:{pod_ip}]",
        ):
            blocked_proxy = request(
                "curl -g -sS --max-time 3 -o /dev/null -w '%{http_code}' "
                f"http://{destination}:44772/command"
            )
            password = issue_proxy_grant(
                grants, adapters[0].binding, blocked_proxy, ["0.0.0.0/0", "::/0"], controls
            )
            write_grants(container, grants)
            assert (
                await adapters[0].run(
                    blocked_proxy,
                    proxy_url=f"http://{blocked_proxy.command_id}:{password}@198.19.1.1:18080",
                )
            ).exit_code == 0
            assert journal.output_preview(blocked_proxy.command_id)["text"].strip() == "403"

        redirect = request(
            "curl -LsS --max-time 3 -o /dev/null -w '%{http_code}' http://198.19.4.2:8080/redirect"
        )
        password = issue_proxy_grant(grants, adapters[0].binding, redirect, ["0.0.0.0/0"], controls)
        write_grants(container, grants)
        assert (
            await adapters[0].run(
                redirect, proxy_url=f"http://{redirect.command_id}:{password}@198.19.1.1:18080"
            )
        ).exit_code == 0
        assert journal.output_preview(redirect.command_id)["text"].strip() == "403"

        direct = request(
            python(
                "import json,socket\nr={}\n"
                f"for host in {['198.19.1.1', pod_ip, 'fd42:bb:1::1', '127.0.0.1']!r}:\n"
                " try:\n  c=socket.create_connection((host,44772),timeout=0.2);"
                "c.close();r[host]=True\n"
                " except OSError:r[host]=False\n"
                "print(json.dumps(r))"
            )
        )
        assert (await adapters[0].run(direct)).exit_code == 0
        assert not any(json.loads(journal.output_preview(direct.command_id)["text"]).values())
        forward = request("curl --noproxy '*' -fsS --max-time 3 http://198.19.4.2:8080/direct")
        assert (await adapters[0].run(forward)).exit_code == 0
        assert (
            json.loads(journal.output_preview(forward.command_id)["text"])["source_ip"]
            == "198.19.1.2"
        )
        grants.write_text("[]")
        write_grants(container, grants)
        malformed = request(
            "curl -sS --max-time 3 -o /dev/null -w '%{http_code}' http://198.19.4.2:8080/denied"
        )
        assert (
            await adapters[0].run(
                malformed,
                proxy_url=f"http://{commands[0].command_id}:{passwords[0]}@198.19.1.1:18080",
            )
        ).exit_code == 0
        assert journal.output_preview(malformed.command_id)["text"].strip() == "403"
        with httpx.Client(trust_env=False) as client:
            assert client.get("http://" + endpoint + "/command/status/missing").status_code == 401
            assert (
                client.get(
                    "http://" + endpoint + "/command/status/missing",
                    headers={ACCESS_HEADER: "wrong"},
                ).status_code
                == 401
            )
    finally:
        await sandbox.close()
        journal.close()


def test_static_launcher_closes_inherited_fd_and_cannot_elevate(container: DockerContainer):
    command_id = "fd-" + uuid4().hex
    command = python("import os;print(os.path.exists('/proc/self/fd/9'));print(os.getuid())")
    argv = [
        "/opt/bbx/control/launcher",
        "worker-a",
        command_id,
        "network-1-generation-1",
        "198.19.1.2",
        "/workspace",
        "-",
        "-",
        command,
        "basic",
    ]
    script = (
        "import os;fd=os.open('/var/lib/bbx/control/protected',os.O_RDONLY);"
        "os.dup2(fd,9);os.set_inheritable(9,True);"
        f"os.execve({argv[0]!r},{argv!r},"
        "{'LD_PRELOAD':'/opt/bbx/control/poison.so',"
        "'BASH_ENV':'/opt/bbx/test/poison.sh','BBX_CONTROL_MARKER':'must-not-leak'})"
    )
    assert admin(container, "/usr/bin/python3", "-I", "-c", script).splitlines() == [
        "False",
        "10000",
    ]
    low = container.get_wrapped_container().exec_run(argv, user="10000:10000")
    assert low.exit_code == 126 and b"invalid trusted launch request" in low.output
    assert "INTERP" not in admin(container, "readelf", "-l", "/opt/bbx/control/launcher")
    assert "setuid" not in admin(container, "getcap", "/opt/bbx/control/launcher")
    empty_token = container.get_wrapped_container().exec_run(
        ["/opt/bbx/control/launcher", "--execd"], environment={"EXECD_ACCESS_TOKEN": ""}
    )
    assert empty_token.exit_code == 126


async def test_trusted_net_raw_profile_keeps_only_raw_capability(
    container: DockerContainer, tmp_path: Path
):
    sandbox = connect_bound_execd(
        "sandbox-p1", "127.0.0.1:" + str(container.get_exposed_port(44772)), TOKEN
    )
    journal = CommandJournal(tmp_path / "private" / "commands.sqlite")
    try:
        for profile, capability in (("basic", "0000000000000000"), ("net-raw", "0000000000002000")):
            adapter = SandboxExecution(
                sandbox, replace(binding(container), execution_profile=profile), journal
            )
            probe = request("python3 -I /opt/bbx/test/raw_probe.py")
            result = await adapter.run(probe)
            assert result.exit_code == 0, journal.output_preview(probe.command_id)
            observed = json.loads(journal.output_preview(probe.command_id)["text"])
            assert observed["uid"] == 10000 and observed["can_setuid"] is False
            assert observed["net_admin_exit"] != 0
            assert "Operation not permitted" in observed["net_admin_error"]
            assert observed["raw_socket"] is (profile == "net-raw")
            assert observed["icmp_reply"] is (profile == "net-raw")
            for name in ("CapEff", "CapPrm", "CapBnd"):
                assert name + ":\t" + capability in observed["status"]
            assert "NoNewPrivs:\t1" in observed["status"]
            file_worker = await adapter.files.read_file("/proc/self/status", offset=1, limit=100)
            assert "Uid:\t10000\t10000\t10000\t10000" in file_worker
            for name in ("CapEff", "CapPrm", "CapInh", "CapAmb"):
                assert name + ":\t0000000000000000" in file_worker
        with pytest.raises(TypeError):
            CommandRequest(
                command_id="cap-invalid",
                invocation_id="inv-invalid",
                command="true",
                execution_profile="net-raw",  # pyright: ignore[reportCallIssue]
            )
    finally:
        await sandbox.close()
        journal.close()
