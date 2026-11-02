"""Only trusted pre-dispatch validation produces correctable input feedback."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bbx_envd import core
from bbx_envd.core import CommandInputError
from bbx_envd.settings import Settings
from pydantic import SecretStr
from test_ctf_registry import setup_registry


@pytest.mark.parametrize(
    ("command", "cwd_kind", "timeout", "code"),
    [
        ("echo safe", "missing", 30, "invalid_cwd"),
        ("echo safe", "file", 30, "invalid_cwd"),
        ("echo safe", "outside", 30, "invalid_cwd"),
        ("echo safe", "parent", 30, "invalid_cwd"),
        ("echo safe", "home", 0, "invalid_timeout"),
        ("echo safe", "home", 1201, "invalid_timeout"),
        ("echo\x00secret", "home", 30, "invalid_command"),
    ],
)
async def test_invalid_input_is_rejected_before_audit_or_process(
    tmp_path, monkeypatch, command, cwd_kind, timeout, code
):
    home = tmp_path / "home"
    home.mkdir()
    file = home / "file"
    file.write_text("fixture")
    paths = {
        "missing": home / "not-created",
        "file": file,
        "outside": tmp_path.parent,
        "parent": home / "..",
        "home": home,
    }
    original_path = core.workspace_path
    monkeypatch.setattr(core, "agent_home", lambda _: home)
    monkeypatch.setattr(core, "workspace_path", lambda p: original_path(p, root=tmp_path))
    spawn = AsyncMock()
    monkeypatch.setattr(core.asyncio, "create_subprocess_exec", spawn)
    audit = []
    monkeypatch.setattr(core, "audit_command", audit.append)
    with pytest.raises(CommandInputError) as caught:
        await core.execute_command(
            "agent-2",
            command,
            str(paths[cwd_kind]),
            timeout,
            False,
            Settings(envd_token=SecretStr("test")),
        )
    feedback = caught.value.result()
    assert feedback["state"] == "rejected" and feedback["dispatched"] is False
    detail = feedback["error"]
    assert isinstance(detail, dict) and detail["code"] == code
    assert "secret" not in str(feedback)
    spawn.assert_not_awaited()
    assert audit == []


async def test_async_refusal_is_known_failed_and_does_not_reserve_a_running_process(
    tmp_path, monkeypatch
):
    registry, identity = setup_registry(tmp_path)
    registry.register(identity)

    async def reject(*args, **kwargs):
        raise CommandInputError("invalid_cwd")

    monkeypatch.setattr(core, "execute_command", reject)
    request = {**identity, "command_id": str(uuid4())}
    settings = Settings(envd_token=SecretStr("test"))
    result = await registry.start("agent-2", request, "mkdir new", "/missing", 30, False, settings)
    assert result["state"] == "failed"
    detail = result["result"]
    assert isinstance(detail, dict) and detail["dispatched"] is False
    assert result == registry.command_status(request)
    assert registry.registered(identity).processes == {}
    assert result == await registry.cancel_command(request)


def test_unknown_process_errors_cannot_be_converted_to_validation_feedback():
    assert not isinstance(ValueError("untrusted process failure"), CommandInputError)
