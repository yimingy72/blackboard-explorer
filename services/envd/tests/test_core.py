import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast
from unittest.mock import AsyncMock

import pytest
from bbx_envd.core import truncate_streams, valid_agent_id, workspace_path
from bbx_envd.settings import Settings
from pydantic import SecretStr


def test_agent_id() -> None:
    assert valid_agent_id("agent-123456")
    assert not valid_agent_id("agent-0;id")
    assert not valid_agent_id("agent-1234567")


@pytest.mark.parametrize("privileged", [False, True])
async def test_commands_do_not_inherit_control_token(monkeypatch, tmp_path, privileged) -> None:
    from bbx_envd import core

    monkeypatch.setenv("ENVD_TOKEN", "test-control-token")
    monkeypatch.setenv("ENVD_TOKEN_SECRET", "test-derivation-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://egress-proxy:8888")
    monkeypatch.setattr(core, "agent_home", lambda _: tmp_path)
    monkeypatch.setattr(core, "COMMAND_LOG", tmp_path / "commands.jsonl")
    (tmp_path / "commands.jsonl").touch()
    process = AsyncMock()
    process.returncode = 0
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(core.asyncio, "create_subprocess_exec", spawn)
    settings = Settings(envd_token=SecretStr("test-control-token"))

    result = await core.execute_command(
        "agent-1", "id -u; echo done", None, 1, privileged, settings
    )

    assert result["exit_code"] == 0
    env = spawn.call_args.kwargs["env"]
    assert "ENVD_TOKEN" not in env
    assert "ENVD_TOKEN_SECRET" not in env
    assert env["HTTP_PROXY"] == "http://egress-proxy:8888"
    assert spawn.call_args.args[:4] == ("runuser", "-u", "agent-1", "--")
    assert spawn.call_args.args[4:] == (
        ("sudo", "-n", "--", "bash", "-lc", "id -u; echo done")
        if privileged
        else ("bash", "-lc", "id -u; echo done")
    )
    records = [json.loads(line) for line in (tmp_path / "commands.jsonl").read_text().splitlines()]
    assert [record["status"] for record in records] == ["started", "completed"]
    execution = cast(dict[str, object], result["execution"])
    assert records[0]["command_id"] == records[1]["command_id"] == execution["command_id"]


async def test_command_audit_concurrent_failure_and_cancel(monkeypatch, tmp_path) -> None:
    from bbx_envd import core

    monkeypatch.setattr(core, "agent_home", lambda _: tmp_path)
    monkeypatch.setattr(core, "COMMAND_LOG", tmp_path / "commands.jsonl")
    (tmp_path / "commands.jsonl").touch()
    settings = Settings(envd_token=SecretStr("test"))

    async def wait_forever():
        await asyncio.sleep(30)

    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    spawn_completed = asyncio.Event()

    async def spawn(*args, **kwargs):
        if args[-1] == "fail":
            raise OSError("spawn failed")
        if args[-1] == "cancel-spawn":
            spawn_started.set()
            await release_spawn.wait()
            spawn_completed.set()
        process = AsyncMock()
        process.returncode = 0
        if args[-1] in {"cancel", "timeout"}:
            process.wait = AsyncMock(side_effect=wait_forever)
        return process

    monkeypatch.setattr(core.asyncio, "create_subprocess_exec", spawn)
    stop = AsyncMock()
    monkeypatch.setattr(core, "stop_process_group", stop)
    completed = await asyncio.gather(
        core.execute_command("agent-1", "one", None, 1, False, settings),
        core.execute_command("agent-1", "two", None, 1, True, settings),
    )
    with pytest.raises(OSError):
        await core.execute_command("agent-1", "fail", None, 1, False, settings)
    timed_out = await core.execute_command("agent-1", "timeout", None, 1, True, settings)
    assert timed_out["exit_code"] == 124
    task = asyncio.create_task(core.execute_command("agent-1", "cancel", None, 10, True, settings))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    spawning = asyncio.create_task(
        core.execute_command("agent-1", "cancel-spawn", None, 10, True, settings)
    )
    await spawn_started.wait()
    spawning.cancel()
    await asyncio.sleep(0)
    assert not spawning.done()
    spawning.cancel()
    await asyncio.sleep(0)
    assert not spawning.done()
    release_spawn.set()
    with pytest.raises(asyncio.CancelledError):
        await spawning
    assert spawn_completed.is_set()
    assert stop.await_count == 3
    records = [json.loads(line) for line in (tmp_path / "commands.jsonl").read_text().splitlines()]
    assert len(records) == 12
    assert len({record["command_id"] for record in records}) == 6
    assert {record["status"] for record in records} == {
        "started",
        "completed",
        "start_failed",
        "timed_out",
        "cancelled",
    }
    assert all("ENVD_TOKEN" not in str(record) for record in records)
    assert {cast(dict[str, object], item["execution"])["status"] for item in completed} == {
        "completed"
    }


def test_truncate_keeps_combined_head_and_tail() -> None:
    out, err, cut = truncate_streams(b"a" * 40000, b"b" * 40000)
    assert cut
    assert out == b"a" * 32768
    assert err == b"b" * 32768
    assert truncate_streams(b"a", b"b") == (b"a", b"b", False)


@pytest.mark.parametrize("sizes", [(80000, 80000), (1, 80000), (80000, 1), (65536, 0), (0, 1)])
def test_output_uses_bounded_reads_and_keeps_full_file(tmp_path, monkeypatch, sizes):
    from bbx_envd import core

    owner = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    monkeypatch.setattr(core.pwd, "getpwnam", lambda _: owner)
    output, error = b"a" * sizes[0], b"b" * sizes[1]

    class BoundedReader:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, name):
            return getattr(self.file, name)

        def read(self, length=-1):
            assert 0 <= length <= core.OUTPUT_LIMIT
            return self.file.read(length)

    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        out.write(output)
        err.write(error)
        shown_out, shown_err, truncated, full = core.command_output(
            cast(BinaryIO, BoundedReader(out)),
            cast(BinaryIO, BoundedReader(err)),
            tmp_path,
            "agent-1",
            None,
        )
    assert (shown_out, shown_err, truncated) == truncate_streams(output, error)
    assert len(shown_out) + len(shown_err) <= core.OUTPUT_LIMIT
    if truncated:
        assert full is not None
        assert Path(full).read_bytes() == output + error
    else:
        assert full is None


async def test_output_processing_runs_off_loop_and_joins_before_cancelled_files_close(
    tmp_path, monkeypatch
):
    from bbx_envd import core

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_output = core.command_output
    monkeypatch.setattr(core, "agent_home", lambda _: tmp_path)
    monkeypatch.setattr(core, "audit_command", lambda _: None)
    process = AsyncMock()
    process.returncode = 0
    monkeypatch.setattr(core.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))

    def output(out, err, *args):
        assert threading.current_thread() is not threading.main_thread()
        started.set()
        try:
            assert release.wait(5)
            assert not out.closed and not err.closed
            return original_output(out, err, *args)
        finally:
            finished.set()

    monkeypatch.setattr(core, "command_output", output)
    pending = asyncio.create_task(
        core.execute_command("agent-1", "test", None, 1, False, Settings(envd_token=SecretStr("t")))
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        done, _ = await asyncio.wait({pending}, timeout=0.02)
        assert not done
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert finished.is_set()


def test_workspace_path_rejects_traversal_and_external_symlink(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "inside").write_text("ok")
    (root / "outside").symlink_to(tmp_path)
    assert workspace_path(str(root / "inside"), root=root) == root / "inside"
    with pytest.raises(ValueError):
        workspace_path(str(root / ".." / "other"), root=root)
    with pytest.raises(ValueError):
        workspace_path(str(root / "outside"), root=root)
