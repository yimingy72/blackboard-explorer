"""Filesystem and command primitives used by envd."""

import asyncio
import itertools
import os
import pwd
import re
import shlex
import signal
import tempfile
from pathlib import Path

from bbx_envd.settings import Settings

WORKSPACE = Path("/workspace")
AGENT_ID = re.compile(r"agent-[0-9]{1,6}\Z")
OUTPUT_LIMIT = 64 * 1024
_sequence = itertools.count(1)


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


def privileged_allowed(command: str, prefixes: str) -> bool:
    if not command or re.search(r"[;&|`$<>\\\n\r'\"(){}]", command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return any(
        prefix and words[: len(prefix)] == prefix
        for prefix in (p.split() for p in prefixes.split(","))
    )


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
    if privileged and not privileged_allowed(command, settings.privileged_prefixes):
        raise ValueError("privileged command is not allowed")
    directory = workspace_path(cwd) if cwd is not None else home
    if not directory.is_dir():
        raise ValueError("cwd is not a directory")
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        args = ["bash", "-lc", command]
        if not privileged:
            args = ["runuser", "-u", agent_id, "--", *args]
        process = await asyncio.create_subprocess_exec(
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
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout_sec)
        except TimeoutError:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        out.seek(0)
        err.seek(0)
        stdout = out.read()
        stderr = err.read()
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
        "exit_code": 124 if timed_out else process.returncode,
        "stdout": wrap(shown_out),
        "stderr": wrap(shown_err),
        "truncated": truncated,
        "full_output_path": full_output_path,
    }
