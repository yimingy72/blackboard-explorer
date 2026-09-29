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
from datetime import UTC, datetime
from pathlib import Path

from bbx_envd.settings import Settings

WORKSPACE = Path("/workspace")
AGENT_ID = re.compile(r"agent-[0-9]{1,6}\Z")
OUTPUT_LIMIT = 64 * 1024
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
) -> dict[str, object]:
    home = agent_home(agent_id)
    if not 1 <= timeout_sec <= settings.command_timeout_max:
        raise ValueError("invalid timeout_sec")
    directory = workspace_path(cwd) if cwd is not None else home
    if not directory.is_dir():
        raise ValueError("cwd is not a directory")
    started = time.monotonic()
    execution: dict[str, object] = {
        "command_id": str(uuid.uuid4()),
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
                    await finish_process_group(process)
                raise
            try:
                await asyncio.wait_for(process.wait(), timeout_sec)
            except TimeoutError:
                status = "timed_out"
                await finish_process_group(process)
            except asyncio.CancelledError:
                status = "cancelled"
                await finish_process_group(process)
                raise
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
