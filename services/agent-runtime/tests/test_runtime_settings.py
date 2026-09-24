from bbx_runtime.settings import Settings


def test_settings(monkeypatch) -> None:
    for key in (
        "DEEPSEEK_API_KEY",
        "MINIO_ROOT_PASSWORD",
        "SERVICE_TOKEN",
        "ENVD_TOKEN_SECRET",
    ):
        monkeypatch.setenv(key, "replace-me")
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.max_running_tasks == 1
    assert settings.deepseek_api_key.get_secret_value() == "replace-me"
    assert "replace-me" not in repr(settings)
