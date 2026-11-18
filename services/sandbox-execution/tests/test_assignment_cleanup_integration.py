"""One real A/B scenario separates shell completion, cancellation and scope drain."""

import asyncio
import json
import shlex
import textwrap
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from conftest import TOKEN, admin, binding, running_container
from testcontainers.core.container import DockerContainer

from bbx_sandbox_execution.connect import connect_bound_execd, read_execution_context
from bbx_sandbox_execution.execution import (
    LAUNCHER,
    CommandJournal,
    CommandRequest,
    SandboxExecution,
    UnknownAssignmentCleanup,
    UnknownCommandOutcome,
)

pytestmark = pytest.mark.integration
CONTROL = "/var/lib/bbx/control"


def request(command: str, *, background: bool = False) -> CommandRequest:
    key = uuid4().hex
    return CommandRequest(
        command_id=key, invocation_id="inv-" + key, command=command, background=background
    )


def heartbeat(name: str, *, detached: bool) -> str:
    code = textwrap.dedent(f"""
        import json,os,time
        from pathlib import Path
        if {detached!r}:
            if os.fork(): os._exit(0)
            os.setsid()
            if os.fork(): os._exit(0)
        devnull=os.open('/dev/null',os.O_RDWR)
        for fd in (0,1,2): os.dup2(devnull,fd)
        if devnull>2: os.close(devnull)
        try:
            os.open('{CONTROL}/cgroup2',os.O_RDONLY)
            private=False
        except PermissionError:
            private=True
        identity={{'pid':os.getpid(),'uid':os.getuid(),'gid':os.getgid(),
            'groups':os.getgroups(),'status':Path('/proc/self/status').read_text(),
            'fds':os.listdir('/proc/self/fd'),'control_private':private,
            'control_environment_absent':not any(key in os.environ for key in (
                'EXECD_ACCESS_TOKEN','BBX_POD_UID','BBX_ENVIRONMENT_REVISION','BBX_CONTROL_MARKER'))}}
        Path('/workspace/{name}.json').write_text(json.dumps(identity))
        counter=0
        while True:
            counter+=1
            Path('/workspace/{name}.heartbeat').write_text(str(counter))
            time.sleep(.02)
    """)
    return "python3 -I -c " + shlex.quote(code)


async def read_identity(adapter: SandboxExecution, name: str) -> dict:
    for _ in range(100):
        try:
            return json.loads(await adapter.files.read_file("/workspace/" + name + ".json"))
        except Exception:
            await asyncio.sleep(0.05)
    pytest.fail("Owned fake descendant did not publish its identity")


def members(container: DockerContainer, scope_id: str) -> str:
    return admin(container, "cat", CONTROL + "/cgroup2/" + scope_id + "/cgroup.events")


async def test_assignment_descendant_drain_and_uncertainty(
    accounting_container: DockerContainer,
    tmp_path: Path,
    monkeypatch,
):
    container = accounting_container
    sandbox = connect_bound_execd(
        "sandbox-p1", "127.0.0.1:" + str(container.get_exposed_port(44772)), TOKEN
    )
    context = await read_execution_context(sandbox)
    assert context["pod_uid"] == binding(container).pod_uid
    assert context["environment_revision"] == "environment-1"
    journal = CommandJournal(tmp_path / "private" / "commands.sqlite")
    a = SandboxExecution(
        sandbox,
        replace(
            binding(container, 1),
            process_accounting="cgroup-v2",
            execution_context_id=context["execution_context_id"],
        ),
        journal,
    )
    b = SandboxExecution(
        sandbox,
        replace(
            binding(container, 2),
            process_accounting="cgroup-v2",
            execution_context_id=context["execution_context_id"],
        ),
        journal,
    )
    a_scope, _ = a.binding.scope_request()
    evidence = {
        "container_id": container.get_wrapped_container().id,
        "pod_uid": a.binding.pod_uid,
        "execution_context_id": context["execution_context_id"],
        "checks": {},
    }
    try:
        failed = request(heartbeat("escaped-a", detached=True) + "; exit 7")
        assert (await a.run(failed)).exit_code == 7
        observed = await read_identity(a, "escaped-a")
        assert (observed["uid"], observed["gid"], observed["groups"]) == (10000, 10000, [])
        assert "CapEff:\t0000000000000000" in observed["status"]
        assert "NoNewPrivs:\t1" in observed["status"] and observed["control_private"]
        assert observed["control_environment_absent"]
        assert len(observed["fds"]) <= 4
        assert "populated 1" in members(container, a_scope)
        assert not any(
            event["kind"] == "assignment_cleanup_confirmed"
            for event in journal.events(failed.command_id)
        )
        evidence["checks"]["exit_7_is_not_descendant_cleanup"] = True
        await a.files.write_file("/workspace/task-shared.txt", "same Task identity")
        assert await b.files.read_file("/workspace/task-shared.txt") == "same Task identity"

        b_command = request(heartbeat("running-b", detached=False), background=True)
        await b.run(b_command)
        await read_identity(b, "running-b")
        running = request("sleep 60", background=True)
        await a.run(running)
        interrupt = sandbox.commands.interrupt

        async def cancel_response_lost(execution_id):
            await interrupt(execution_id)
            raise httpx.ReadError("Scripted cancellation acknowledgement loss")

        with monkeypatch.context() as patch:
            patch.setattr(sandbox.commands, "interrupt", cancel_response_lost)
            with pytest.raises(httpx.ReadError):
                await a.interrupt(running.command_id)
        assert journal.events(running.command_id)[-1]["kind"] == "cancel_unknown"
        assert "populated 1" in members(container, a_scope)
        evidence["checks"]["cancel_unknown_keeps_escaped_descendant_unconfirmed"] = True

        run = sandbox.commands.run
        mutations = 0

        async def stop_response_lost(argv, **kwargs):
            nonlocal mutations
            result = await run(argv, **kwargs)
            if argv[1] == "--assignment-stop":
                mutations += 1
                raise httpx.ReadError("Scripted stop receipt loss after native completion")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(sandbox.commands, "run", stop_response_lost)
            race = request("echo admitted > /workspace/racing-launch; sleep 60")
            racing_launch = asyncio.create_task(a.run(race))
            with pytest.raises(httpx.ReadError):
                await a.stop_assignment(failed.command_id)
            assert journal.events(failed.command_id)[-1]["kind"] == "assignment_cleanup_unknown"
            try:
                racing_result = await racing_launch
                assert racing_result.exit_code is not None
            except UnknownCommandOutcome:
                # The command remains unknown; the independent scope receipt is checked below.
                pass
            confirmed = await a.stop_assignment(failed.command_id)
        assert mutations == 1 and confirmed["state"] == "confirmed"
        assert confirmed["sealed"] and confirmed["populated"] == 0
        assert "populated 0" in members(container, a_scope)
        evidence["a_receipt"] = confirmed
        evidence["checks"]["lost_stop_receipt_reconciled_without_second_kill_request"] = True
        evidence["checks"]["concurrent_launch_is_drained_or_refused"] = True
        first = await b.files.read_file("/workspace/escaped-a.heartbeat")
        await asyncio.sleep(0.1)
        assert await b.files.read_file("/workspace/escaped-a.heartbeat") == first

        late = request("echo late > /workspace/late-must-not-exist")
        assert (await a.run(late)).exit_code == 126
        assert not admin(container, "sh", "-c", "test ! -e /workspace/late-must-not-exist")
        evidence["checks"]["sealed_late_launch_refused"] = True

        newer = SandboxExecution(
            sandbox,
            replace(
                a.binding,
                generation=2,
                network_identity="network-1-generation-2",
                execution_profile="net-raw",
            ),
            journal,
        )
        newer_command = request(heartbeat("new-generation", detached=False), background=True)
        await newer.run(newer_command)
        newer_identity = await read_identity(newer, "new-generation")
        assert newer_identity["uid"] == newer_identity["gid"] == 10000
        assert "CapEff:\t0000000000002000" in newer_identity["status"]
        assert newer_identity["control_private"] and len(newer_identity["fds"]) <= 4
        assert newer_identity["control_environment_absent"]
        with pytest.raises(ValueError, match="binding"):
            await a.stop_assignment(newer_command.command_id)
        await a.stop_assignment(failed.command_id)
        before = [
            await b.files.read_file("/workspace/" + name + ".heartbeat")
            for name in ("running-b", "new-generation")
        ]
        await asyncio.sleep(0.1)
        after = [
            await b.files.read_file("/workspace/" + name + ".heartbeat")
            for name in ("running-b", "new-generation")
        ]
        assert all(left != right for left, right in zip(before, after, strict=True))
        evidence["checks"]["b_and_new_generation_survive_old_scope_reconciliation"] = True

        identity_path = CONTROL + "/process-scopes/" + a_scope + "/identity"
        original = admin(container, "cat", identity_path)
        wrong = original.split()
        wrong[4] = wrong[
            -1
        ]  # A bare matching scope inode cannot replace the unique mount identity.
        try:
            admin(
                container,
                "python3",
                "-I",
                "-c",
                "from pathlib import Path;Path("
                + repr(identity_path)
                + ").write_text("
                + repr(" ".join(wrong) + "\n")
                + ")",
            )
            with pytest.raises(UnknownAssignmentCleanup):
                await a.assignment_cleanup_status(failed.command_id)
        finally:
            admin(
                container,
                "python3",
                "-I",
                "-c",
                "from pathlib import Path;Path("
                + repr(identity_path)
                + ").write_text("
                + repr(original)
                + ")",
            )
        admin(container, "rmdir", CONTROL + "/cgroup2/" + a_scope)
        with pytest.raises(UnknownAssignmentCleanup):
            await a.assignment_cleanup_status(failed.command_id)
        evidence["checks"]["missing_scope_and_incomplete_identity_never_confirmed"] = True

        # A held A lock must not stall another scope's real command admission.
        lock_path = CONTROL + "/process-scopes/" + a_scope + "/lock"
        locked = container.get_wrapped_container().exec_run(
            ["flock", lock_path, "sh", "-c", "touch " + CONTROL + "/lock-test-ready; sleep 2"],
            detach=True,
        )
        assert locked is not None
        for _ in range(100):
            ready = container.get_wrapped_container().exec_run(
                ["test", "-e", CONTROL + "/lock-test-ready"]
            )
            if ready.exit_code == 0:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("Owned scope lock did not become ready")
        status_task = asyncio.create_task(a.assignment_cleanup_status(failed.command_id))
        await asyncio.sleep(0.05)
        assert (await asyncio.wait_for(b.run(request("true")), timeout=1.2)).exit_code == 0
        assert not status_task.done()
        with pytest.raises(UnknownAssignmentCleanup):
            await status_task
        evidence["checks"]["a_scope_lock_does_not_block_b_launch"] = True

        evidence["b_receipt"] = await b.stop_assignment(b_command.command_id)
        evidence["new_generation_receipt"] = await newer.stop_assignment(newer_command.command_id)
        b_scope, _ = b.binding.scope_request()
        inode_before = admin(container, "stat", "-c", "%i", CONTROL + "/cgroup2/" + b_scope)
        replaced_mount = await sandbox.commands.run(
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                "import ctypes;lib=ctypes.CDLL(None,use_errno=True);p=b'"
                + CONTROL
                + "/cgroup2';result=lib.mount(p,p,None,4096,None);"
                "assert result==0, ctypes.get_errno()",
            ]
        )
        assert replaced_mount.exit_code == 0
        assert admin(container, "stat", "-c", "%i", CONTROL + "/cgroup2/" + b_scope) == inode_before
        with pytest.raises(UnknownAssignmentCleanup):
            await b.assignment_cleanup_status(b_command.command_id)
        evidence["mount_replacement"] = {
            "unchanged_scope_inode": int(inode_before),
            "original_mount_id": evidence["b_receipt"]["mount_id"],
            "observation": [
                event["receipt"]
                for event in journal.events(b_command.command_id)
                if event["kind"] == "assignment_scope_observed"
            ][-1],
        }
        evidence["checks"]["remount_with_same_cgroup_inode_is_unknown"] = True
        legacy = [
            LAUNCHER,
            "worker-a",
            uuid4().hex,
            "network-1",
            "198.19.1.2",
            "/workspace",
            "-",
            "-",
            "echo bad > /workspace/legacy-bypass",
            "basic",
        ]
        assert (await sandbox.commands.run(legacy)).exit_code == 126
        assert not admin(container, "sh", "-c", "test ! -e /workspace/legacy-bypass")
        evidence["checks"]["accounting_profile_rejects_legacy_entry_after_context_change"] = True

        # Container replacement can retain Pod UID/envrev; the frozen incarnation must differ.
        with contextmanager(running_container)(True, pod_uid=a.binding.pod_uid) as replacement:
            replacement_sandbox = connect_bound_execd(
                "sandbox-p1",
                "127.0.0.1:" + str(replacement.get_exposed_port(44772)),
                TOKEN,
            )
            try:
                fresh_context = await read_execution_context(replacement_sandbox)
                assert fresh_context["pod_uid"] == context["pod_uid"]
                assert fresh_context["environment_revision"] == context["environment_revision"]
                assert fresh_context["execution_context_id"] != context["execution_context_id"]
                stale = SandboxExecution(replacement_sandbox, a.binding, journal)
                attempted = request("echo bad > /workspace/stale-incarnation")
                assert (await stale.run(attempted)).exit_code == 126
                with pytest.raises(UnknownAssignmentCleanup):
                    await stale.stop_assignment(failed.command_id)
                assert not admin(replacement, "sh", "-c", "test ! -e /workspace/stale-incarnation")
                assert not admin(
                    replacement, "sh", "-c", "test ! -e " + CONTROL + "/cgroup2/" + a_scope
                )
                fresh_binding = replace(
                    a.binding, execution_context_id=fresh_context["execution_context_id"]
                )
                fresh = SandboxExecution(replacement_sandbox, fresh_binding, journal)
                fresh_command = request(heartbeat("replacement", detached=False), background=True)
                await fresh.run(fresh_command)
                await read_identity(fresh, "replacement")
                with pytest.raises(UnknownAssignmentCleanup):
                    await stale.assignment_cleanup_status(failed.command_id)
                old = await fresh.files.read_file("/workspace/replacement.heartbeat")
                await asyncio.sleep(0.1)
                assert await fresh.files.read_file("/workspace/replacement.heartbeat") != old
                evidence["replacement_receipt"] = await fresh.stop_assignment(
                    fresh_command.command_id
                )
                evidence["replacement_container_id"] = replacement.get_wrapped_container().id
                evidence["checks"][
                    "same_pod_and_envrev_replacement_rejects_old_frozen_incarnation"
                ] = True
            finally:
                await replacement_sandbox.close()
        output = (
            Path(__file__).resolve().parents[3]
            / ".data/bbx-archv2-g1-20261106/combined-evidence.json"
        )
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2))
    finally:
        await sandbox.close()
        journal.close()
