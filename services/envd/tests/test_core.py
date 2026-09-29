import asyncio
import json
from pathlib import Path
from typing import cast
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
