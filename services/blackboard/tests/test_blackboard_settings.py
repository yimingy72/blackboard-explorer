from bbx_blackboard.settings import Settings


def test_settings(monkeypatch) -> None:
    for key in (
        "POSTGRES_PASSWORD",
        "MINIO_ROOT_PASSWORD",
        "SERVICE_TOKEN",
        "AGENT_TOKEN_SECRET",
        "ADMIN_USERS",
    ):
        monkeypatch.setenv(key, "replace-me")
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.embed_dim == 512
    assert settings.postgres_password.get_secret_value() == "replace-me"
    assert "replace-me" not in repr(settings)
