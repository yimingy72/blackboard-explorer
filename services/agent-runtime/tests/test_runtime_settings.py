import pytest
from bbx_runtime.settings import Settings
from pydantic import SecretStr, ValidationError


def test_settings(monkeypatch) -> None:
    monkeypatch.delenv("MAX_RUNNING_TASKS", raising=False)
    for key in (
        "DEEPSEEK_API_KEY",
        "MINIO_ROOT_PASSWORD",
        "SERVICE_TOKEN",
        "ENVD_TOKEN_SECRET",
    ):
        monkeypatch.setenv(key, "replace-me")
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.max_running_tasks == 2
    assert settings.exec_min_pids == 1024
    assert settings.deepseek_api_key.get_secret_value() == "replace-me"
    assert "replace-me" not in repr(settings)


@pytest.mark.parametrize("limit", [1, 2, 5])
def test_task_capacity_can_be_overridden(monkeypatch, limit: int) -> None:
    monkeypatch.setenv("MAX_RUNNING_TASKS", str(limit))
    settings = Settings(
        service_token=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        envd_token_secret=SecretStr("test"),
    )
    assert settings.max_running_tasks == limit


def test_task_capacity_must_be_positive(monkeypatch) -> None:
    monkeypatch.setenv("MAX_RUNNING_TASKS", "0")
    with pytest.raises(ValidationError, match="max_running_tasks"):
        Settings(
            service_token=SecretStr("test"),
            minio_root_password=SecretStr("test"),
            envd_token_secret=SecretStr("test"),
        )


def test_execution_pid_floor_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("EXEC_MIN_PIDS", "2048")
    settings = Settings(
        service_token=SecretStr("test"),
        minio_root_password=SecretStr("test"),
        envd_token_secret=SecretStr("test"),
    )
    assert settings.exec_min_pids == 2048


def test_settings_allow_platform_only_models_without_deepseek_key(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    for key in ("MINIO_ROOT_PASSWORD", "SERVICE_TOKEN", "ENVD_TOKEN_SECRET"):
        monkeypatch.setenv(key, "test-only-value")
    assert Settings().deepseek_api_key.get_secret_value() == ""  # pyright: ignore[reportCallIssue]
