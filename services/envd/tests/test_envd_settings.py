from bbx_envd.settings import Settings


def test_settings(monkeypatch) -> None:
    monkeypatch.setenv("ENVD_TOKEN", "replace-me")
    monkeypatch.setenv("COMMAND_TIMEOUT_MAX", "90")
    monkeypatch.setenv("EVIDENCE_MAX_BYTES", "123")
    monkeypatch.setenv("PRIVILEGED_PREFIXES", "id,pip install")
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.command_timeout_max == 90
    assert settings.evidence_max_bytes == 123
    assert settings.privileged_prefixes == "id,pip install"
    assert "replace-me" not in repr(settings)
