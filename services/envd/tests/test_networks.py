"""Network identity is explicit and command execution fails closed."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bbx_envd.networks import AgentNetworks


def test_network_prefix_drops_only_added_capability_and_requires_registered_agent(tmp_path):
    networks = AgentNetworks(tmp_path / "network", tmp_path / "shared")
    with pytest.raises(ValueError, match="not executed"):
        networks.prefix("agent-1")
    networks.agents["agent-1"] = {
        "namespace": "bbx-safe",
        "ipv4": "198.18.0.10",
        "ipv6": "fd42::10",
        "agent_id": "member-1",
    }
    networks.dns = SimpleNamespace(returncode=None)  # pyright: ignore[reportAttributeAccessIssue]
    assert networks.prefix("agent-1") == [
        "ip",
        "netns",
        "exec",
        "bbx-safe",
        "setpriv",
        "--bounding-set=-sys_admin",
        "--",
    ]
    assert networks.environment("agent-1")["BBX_AGENT_IP"] == "198.18.0.10"
    assert networks.environment("agent-1")["BBX_TASK_GATEWAY"] == "198.18.0.1"
    networks.dns = SimpleNamespace(returncode=1)  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValueError, match="not executed"):
        networks.prefix("agent-1")


async def test_network_registration_does_not_rebind_an_existing_agent(tmp_path, monkeypatch):
    networks = AgentNetworks(tmp_path / "network", tmp_path / "shared")
    networks.task_id = "task-1"
    networks.agents["agent-1"] = {"agent_id": "member-1"}
    run = AsyncMock()
    monkeypatch.setattr(networks, "run", run)
    assert await networks.register("task-1", "agent-1", "member-1") == {"agent_id": "member-1"}
    run.assert_not_called()
    with pytest.raises(ValueError, match="another Agent"):
        await networks.register("task-1", "agent-1", "member-2")
    with pytest.raises(ValueError, match="another task"):
        await networks.register("task-2", "agent-1", "member-1")


async def test_network_restart_and_subnet_conflict_require_replacement(tmp_path, monkeypatch):
    root = tmp_path / "network"
    root.mkdir()
    (root / "identity.json").write_text('{"task_id":"previous"}')
    networks = AgentNetworks(root, tmp_path / "shared")
    with pytest.raises(ValueError, match="replace the container"):
        await networks.initialize("task-1")
    (root / "identity.json").unlink()
    monkeypatch.setattr(networks, "run", AsyncMock(return_value='[{"dst":"198.18.0.0/16"}]'))
    with pytest.raises(ValueError, match="conflicts"):
        await networks.initialize("task-1")
