from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bbx_envd.core import privileged_allowed, truncate_streams, valid_agent_id, workspace_path
from bbx_envd.settings import Settings
from pydantic import SecretStr


def test_agent_id_and_privileged_prefixes() -> None:
    assert valid_agent_id("agent-123456")
    assert not valid_agent_id("agent-0;id")
    assert not valid_agent_id("agent-1234567")
    prefixes = "apt-get install,apt-get update,pip install,npm install -g"
    assert privileged_allowed("apt-get install jq", prefixes)
    assert privileged_allowed("apt-get update", prefixes)
    assert not privileged_allowed("apt-get installer", prefixes)
    assert not privileged_allowed("apt-get install x; rm -rf /", prefixes)
    assert not privileged_allowed("pip install $(id)", prefixes)
    assert not privileged_allowed("id", "apt-get install,")
    assert not privileged_allowed("id", " , ")


@pytest.mark.parametrize("privileged", [False, True])
async def test_commands_do_not_inherit_control_token(monkeypatch, tmp_path, privileged) -> None:
    from bbx_envd import core

    monkeypatch.setenv("ENVD_TOKEN", "test-control-token")
    monkeypatch.setenv("ENVD_TOKEN_SECRET", "test-derivation-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://egress-proxy:8888")
    monkeypatch.setattr(core, "agent_home", lambda _: tmp_path)
    process = AsyncMock()
    process.returncode = 0
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(core.asyncio, "create_subprocess_exec", spawn)
    settings = Settings(envd_token=SecretStr("test-control-token"), privileged_prefixes="id")

    result = await core.execute_command("agent-1", "id", None, 1, privileged, settings)

    assert result["exit_code"] == 0
    env = spawn.call_args.kwargs["env"]
    assert "ENVD_TOKEN" not in env
    assert "ENVD_TOKEN_SECRET" not in env
    assert env["HTTP_PROXY"] == "http://egress-proxy:8888"


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
