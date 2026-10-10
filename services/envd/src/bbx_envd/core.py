"""Filesystem and command primitives used by envd."""

import asyncio
import itertools
import json
import os
import pwd
import re
import signal
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bbx_envd.settings import Settings

WORKSPACE = Path("/workspace")
AGENT_ID = re.compile(r"agent-[0-9]{1,6}\Z")
OUTPUT_LIMIT = 64 * 1024
CTF_DRAIN_WAIT_SECONDS = 2.0
_sequence = itertools.count(1)
AUDIT_DIR = Path("/var/log/bbx")
COMMAND_LOG = AUDIT_DIR / "commands.jsonl"
SUDO_LOG = AUDIT_DIR / "sudo.log"


def valid_agent_id(value: str) -> bool:
    return AGENT_ID.fullmatch(value) is not None


def workspace_path(value: str, *, root: Path = WORKSPACE, strict: bool = True) -> Path:
    raw = Path(value)
    if not raw.is_absolute() or ".." in raw.parts:
        raise ValueError("path must be absolute and cannot contain '..'")
    resolved = raw.resolve(strict=strict)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError("path is outside workspace")
    return resolved


def prepare_audit_logs() -> None:
    AUDIT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(AUDIT_DIR, 0, 0)
    AUDIT_DIR.chmod(0o700)
    for path in (COMMAND_LOG, SUDO_LOG):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)


def audit_command(record: dict[str, object]) -> None:
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    fd = os.open(COMMAND_LOG, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    try:
        if os.write(fd, line) != len(line):
            raise OSError("incomplete command audit write")
    finally:
        os.close(fd)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


async def stop_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        pass
    # The parent can exit before its children; always kill the remaining group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def finish_process_group(process: asyncio.subprocess.Process) -> None:
    cleanup = asyncio.create_task(stop_process_group(process))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    await cleanup
    if cancelled:
        raise asyncio.CancelledError


def truncate_streams(stdout: bytes, stderr: bytes) -> tuple[bytes, bytes, bool]:
    total = len(stdout) + len(stderr)
    if total <= OUTPUT_LIMIT:
        return stdout, stderr, False
    head = OUTPUT_LIMIT // 2
    tail = OUTPUT_LIMIT // 2
    stdout_head = stdout[:head]
    stderr_head = stderr[: max(0, head - len(stdout))]
    stderr_tail = stderr[-tail:]
    stdout_tail = stdout[-max(0, tail - len(stderr)) :] if len(stderr) < tail else b""
    return stdout_head + stdout_tail, stderr_head + stderr_tail, True


def prepare_workspace() -> None:
    WORKSPACE.mkdir(mode=0o755, exist_ok=True)
    WORKSPACE.chmod(0o755)
    agents = WORKSPACE / "agents"
    agents.mkdir(mode=0o755, exist_ok=True)
    agents.chmod(0o755)
    shared = WORKSPACE / "shared"
    shared.mkdir(mode=0o1777, exist_ok=True)
    shared.chmod(0o1777)


def agent_home(agent_id: str) -> Path:
    if not valid_agent_id(agent_id):
        raise ValueError("invalid agent id")
    home = WORKSPACE / "agents" / agent_id
    if not home.is_dir():
        raise ValueError("unknown agent")
    return home


async def execute_command(
    agent_id: str,
    command: str,
    cwd: str | None,
    timeout_sec: int,
    privileged: bool,
    settings: Settings,
    *,
    command_id: str | None = None,
    process_started: Callable[[asyncio.subprocess.Process], None] | None = None,
) -> dict[str, object]:
    home = agent_home(agent_id)
    if not 1 <= timeout_sec <= settings.command_timeout_max:
        raise ValueError("invalid timeout_sec")
    directory = workspace_path(cwd) if cwd is not None else home
    if not directory.is_dir():
        raise ValueError("cwd is not a directory")
    started = time.monotonic()
    execution: dict[str, object] = {
        "command_id": command_id or str(uuid.uuid4()),
        "agent_id": agent_id,
        "command": command,
        "cwd": str(directory),
        "privileged_requested": privileged,
        "started_at": utc_now(),
    }
    audit_command({**execution, "status": "started"})
    status = "completed"
    exit_code: int | None = None
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            args = ["runuser", "-u", agent_id, "--"]
            if privileged:
                args += ["sudo", "-n", "--"]
            args += ["bash", "-lc", command]
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *args,
                    cwd=directory,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if key not in {"ENVD_TOKEN", "ENVD_TOKEN_SECRET"}
                    },
                )
            )
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                status = "cancelled"
                while not spawn.done():
                    try:
                        await asyncio.shield(spawn)
                    except asyncio.CancelledError:
                        pass
                try:
                    process = spawn.result()
                except Exception:
                    pass
                else:
                    if process_started is not None:
                        process_started(process)
                    await finish_process_group(process)
                raise
            if process_started is not None:
                process_started(process)
            try:
                await asyncio.wait_for(process.wait(), timeout_sec)
            except TimeoutError:
                status = "timed_out"
                await finish_process_group(process)
            except asyncio.CancelledError:
                status = "cancelled"
                await finish_process_group(process)
                raise
            if process_started is not None:
                await finish_process_group(process)
            exit_code = 124 if status == "timed_out" else process.returncode
            out.seek(0)
            err.seek(0)
            stdout = out.read()
            stderr = err.read()
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception:
        status = "start_failed"
        raise
    finally:
        execution.update(
            status=status,
            ended_at=utc_now(),
            duration_ms=round((time.monotonic() - started) * 1000),
            exit_code=exit_code,
        )
        audit_command(execution)
    shown_out, shown_err, truncated = truncate_streams(stdout, stderr)
    full_output_path: str | None = None
    if truncated:
        output_dir = home / ".outputs"
        output_dir.mkdir(mode=0o755, exist_ok=True)
        output = output_dir / f"{next(_sequence)}.txt"
        owner = pwd.getpwnam(agent_id)
        directory_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchown(directory_fd, owner.pw_uid, owner.pw_gid)
            os.fchmod(directory_fd, 0o755)
            file_fd = os.open(
                output.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(file_fd, "wb") as target:
                os.fchown(target.fileno(), owner.pw_uid, owner.pw_gid)
                target.write(stdout)
                target.write(stderr)
        finally:
            os.close(directory_fd)
        full_output_path = str(output)

    def wrap(value: bytes) -> str:
        return f"<command_output>{value.decode('utf-8', errors='replace')}</command_output>"

    return {
        "exit_code": exit_code,
        "stdout": wrap(shown_out),
        "stderr": wrap(shown_err),
        "truncated": truncated,
        "full_output_path": full_output_path,
        "execution": {key: value for key, value in execution.items() if key != "command"},
    }


@dataclass
class CtfRegistration:
    member_id: str
    generation: int
    stopping: bool = False
    commands: dict[str, tuple[str, asyncio.Task[dict[str, object]]]] = field(default_factory=dict)
    processes: dict[str, asyncio.subprocess.Process] = field(default_factory=dict)


class CtfCommandRegistry:
    """Fence one daemon boot and retain command outcomes until container removal."""

    def __init__(self, task_id: str, marker: Path) -> None:
        self.task_id = str(uuid.UUID(task_id))
        self.boot_id = str(uuid.uuid4())
        self.registrations: dict[str, CtfRegistration] = {}
        self.commands: dict[tuple[str, str], tuple[int, str, asyncio.Task[dict[str, object]]]] = {}
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            self.unknown = True
        else:
            self.unknown = False
            with os.fdopen(fd, "w") as stream:
                stream.write(self.boot_id)
                stream.flush()
                os.fsync(stream.fileno())

    def identity(self, body: dict[str, object]) -> tuple[str, str, int]:
        if self.unknown:
            raise ValueError("Previous boot process state unknown; replace the container")
        if body.get("boot_id") != self.boot_id or body.get("task_id") != self.task_id:
            raise ValueError("Stale boot or wrong task")
        agent_id = body.get("agent_id")
        member_id = body.get("member_id")
        generation = body.get("generation")
        if not isinstance(agent_id, str) or not valid_agent_id(agent_id):
            raise ValueError("Invalid execution agent")
        if not isinstance(member_id, str) or not member_id:
            raise ValueError("Missing semantic member")
        if type(generation) is not int or generation < 0:
            raise ValueError("Invalid generation")
        return agent_id, member_id, generation

    def register(self, body: dict[str, object]) -> dict[str, object]:
        agent_id, member_id, generation = self.identity(body)
        previous = self.registrations.get(agent_id)
        if previous is not None:
            if previous.member_id != member_id or generation < previous.generation:
                raise ValueError("Stale generation or changed member")
            if generation == previous.generation:
                if previous.stopping:
                    raise ValueError("Generation stopped")
                return self.status(agent_id)
            if not previous.stopping or not self._drained(previous):
                raise ValueError("Previous generation is not drained")
        self.registrations[agent_id] = CtfRegistration(member_id, generation)
        return self.status(agent_id)

    @staticmethod
    def _drained(registration: CtfRegistration) -> bool:
        if any(not task.done() for _, task in registration.commands.values()):
            return False
        for process in registration.processes.values():
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                return False
            return False
        return True

    def status(self, agent_id: str | None = None) -> dict[str, object]:
        base: dict[str, object] = {"boot_id": self.boot_id, "task_id": self.task_id}
        if self.unknown:
            return {**base, "state": "unknown", "drained": False}
        if agent_id is None:
            return {**base, "state": "ready", "drained": False}
        registration = self.registrations.get(agent_id)
        if registration is None:
            return {**base, "agent_id": agent_id, "state": "unregistered", "drained": False}
        drained = registration.stopping and self._drained(registration)
        running = any(not task.done() for _, task in registration.commands.values())
        state = (
            "drained"
            if drained
            else "stopping"
            if registration.stopping
            else "running"
            if running
            else "registered"
        )
        return {
            **base,
            "agent_id": agent_id,
            "member_id": registration.member_id,
            "generation": registration.generation,
            "state": state,
            "drained": drained,
        }

    def registered(self, body: dict[str, object]) -> CtfRegistration:
        agent_id, member_id, generation = self.identity(body)
        registration = self.registrations.get(agent_id)
        if registration is None or (registration.member_id, registration.generation) != (
            member_id,
            generation,
        ):
            raise ValueError("Unregistered generation")
        return registration

    async def stop(self, body: dict[str, object]) -> dict[str, object]:
        registration = self.registered(body)
        registration.stopping = True
        tasks = [task for _, task in registration.commands.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Container PID 1 reaps orphaned children asynchronously after SIGKILL.
        deadline = asyncio.get_running_loop().time() + CTF_DRAIN_WAIT_SECONDS
        while not self._drained(registration):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.02, remaining))
        return self.status(str(body["agent_id"]))

    async def execute(
        self,
        agent_id: str,
        metadata: dict[str, object],
        command: str,
        cwd: str | None,
        timeout_sec: int,
        privileged: bool,
        settings: Settings,
    ) -> dict[str, object]:
        body = {**metadata, "agent_id": agent_id}
        registration = self.registered(body)
        if registration.stopping:
            raise ValueError("Generation stopping")
        if registration.generation == 0:
            raise ValueError("Generation zero cannot execute commands")
        command_id = str(uuid.UUID(str(metadata.get("command_id", ""))))
        digest = json.dumps([command, cwd, timeout_sec, privileged], separators=(",", ":"))
        previous = self.commands.get((agent_id, command_id))
        if previous:
            if previous[1] != digest:
                raise ValueError("Command ID reused with different arguments")
            if previous[0] != registration.generation:
                raise ValueError("Command belongs to a previous generation; reconcile its outcome")
            task = previous[2]
        else:
            task = asyncio.create_task(
                execute_command(
                    agent_id,
                    command,
                    cwd,
                    timeout_sec,
                    privileged,
                    settings,
                    command_id=command_id,
                    process_started=lambda process: registration.processes.__setitem__(
                        command_id, process
                    ),
                )
            )
            registration.commands[command_id] = (digest, task)
            self.commands[(agent_id, command_id)] = (registration.generation, digest, task)
            # Consume errors even when the original MCP client disconnects.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(task)
