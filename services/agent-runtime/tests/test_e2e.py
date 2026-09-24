"""The explicit checkpoint isolates services and does not persist secret values."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from bbx_runtime.e2e import (
    ComposeCheckpoint,
    checkpoint_environment,
    compose_document,
    container_proxy,
    redact,
)

ROOT = Path(__file__).resolve().parents[3]


def test_checkpoint_overrides_host_credentials_and_keeps_only_loopback_random_port():
    config = compose_document(ROOT)
    assert "ports" not in config["services"]["postgres"]
    assert "ports" not in config["services"]["minio"]
    assert config["services"]["blackboard"]["ports"] == [
        {"target": 8000, "published": "0", "host_ip": "127.0.0.1", "protocol": "tcp"}
    ]
    assert config["services"]["eval-targets"]["volumes"][0].startswith(str(ROOT))
    env = checkpoint_environment(
        {"DEEPSEEK_API_KEY": "private-test-key", "POSTGRES_PASSWORD": "host-password"},
        "bbx-e2e-test",
    )
    assert env["POSTGRES_PASSWORD"] != "host-password"
    assert env["EXEC_NETWORK"] == "bbx-e2e-test_exec"
    serialized = yaml.safe_dump(config)
    assert "private-test-key" not in serialized and "host-password" not in serialized
    assert "private-test-key" not in redact("HTTP error for private-test-key", env)


def test_checkpoint_requires_explicit_key_and_translates_only_local_proxy():
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        checkpoint_environment({}, "bbx-e2e-test")
    assert container_proxy("http://127.0.0.1:7897") == "http://host.docker.internal:7897"
    assert container_proxy("https://proxy.example:443") == "https://proxy.example:443"
    assert container_proxy("") == ""
    proxy = "http://alice:test-password@127.0.0.1:7897"
    assert container_proxy(proxy) == "http://alice:test-password@host.docker.internal:7897"
    assert "test-password" not in redact(f"Proxy failed: {proxy}", {"http_proxy": proxy})


def test_cleanup_removes_only_containers_owned_by_the_checkpoint_task(monkeypatch):
    container = Mock()
    client = SimpleNamespace(containers=Mock(), close=Mock())
    client.containers.list.return_value = [container]
    monkeypatch.setattr("bbx_runtime.e2e.docker.from_env", lambda: client)
    checkpoint = ComposeCheckpoint.__new__(ComposeCheckpoint)
    checkpoint.task_id = "checkpoint-task"
    checkpoint._remove_task_containers()
    client.containers.list.assert_called_once_with(
        all=True,
        filters={"label": ["bbx.managed=agent-runtime", "bbx.task-id=checkpoint-task"]},
    )
    container.remove.assert_called_once_with(force=True)
    client.close.assert_called_once()


async def test_cleanup_continues_after_compose_stop_fails(monkeypatch):
    checkpoint = ComposeCheckpoint.__new__(ComposeCheckpoint)
    calls = []

    async def command(*arguments):
        calls.append(arguments)
        if arguments[0] == "stop":
            raise RuntimeError("temporary stop error")
        return ""

    monkeypatch.setattr(checkpoint, "docker", AsyncMock(side_effect=command))
    remove = Mock()
    monkeypatch.setattr(checkpoint, "_remove_task_containers", remove)
    with pytest.raises(RuntimeError, match="stop error"):
        await checkpoint._cleanup()
    remove.assert_called_once()
    assert calls == [("stop", "agent-runtime"), ("down", "-v", "--remove-orphans")]
