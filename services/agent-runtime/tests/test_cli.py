"""Read-only control commands need no model configuration."""

from __future__ import annotations

import sys
from uuid import uuid4

import pytest
from bbx_runtime import cli
from bbx_runtime.settings import Settings
from pydantic import SecretStr


class FakeBlackboard:
    calls: list[tuple[str, str, str]] = []

    def __init__(self, _base_url: str, _token: str) -> None:
        pass

    async def __aenter__(self) -> FakeBlackboard:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def state(self, task_id: str) -> dict:
        self.calls.append(("state", task_id, ""))
        return {"task": {"status": "running"}, "board_empty": False}

    async def conclude(self, task_id: str, agent_id: str, reason: str) -> list:
        self.calls.append(("conclude", task_id, f"{agent_id}:{reason}"))
        return []


def test_state_and_conclude_use_only_control_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = str(uuid4())
    FakeBlackboard.calls = []
    monkeypatch.setenv("SERVICE_TOKEN", "test-service-token")
    for key in ("DEEPSEEK_API_KEY", "MINIO_ROOT_PASSWORD", "ENVD_TOKEN_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cli, "BlackboardClient", FakeBlackboard)
    commands = (
        ["state", "--task", task_id],
        ["conclude", "--task", task_id, "--agent", "agent-1"],
    )
    for args in commands:
        monkeypatch.setattr(sys, "argv", ["bbx-runtime", *args])
        with pytest.raises(SystemExit) as finished:
            cli.main()
        assert finished.value.code == 0
    assert FakeBlackboard.calls == [
        ("state", task_id, ""),
        ("conclude", task_id, "agent-1:manual"),
    ]
    assert "已请求结束" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_final_close_requires_closing_before_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = str(uuid4())
    FakeBlackboard.calls = []
    monkeypatch.setattr(cli, "BlackboardClient", FakeBlackboard)

    def no_resources(_settings):
        raise AssertionError("A rejected final close must not allocate resources")

    monkeypatch.setattr(cli, "object_store", no_resources)
    args = cli.parser().parse_args(
        ["run-agent", "--task", task_id, "--type", "close", "--mode", "final"]
    )
    settings = Settings.model_construct(service_token=SecretStr("test-service-token"))
    with pytest.raises(ValueError, match="requires a closing task"):
        await cli.run_command(args, settings)
    assert FakeBlackboard.calls == [("state", task_id, "")]
