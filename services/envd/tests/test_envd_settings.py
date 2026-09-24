from bbx_envd.settings import Settings


def test_settings(monkeypatch) -> None:
    monkeypatch.setenv("ENVD_TOKEN_SECRET", "replace-me")
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.otel_enabled is False
    assert "replace-me" not in repr(settings)
