"""Agent runtime environment configuration."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ControlSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    blackboard_url: str = "http://blackboard:8000"
    service_token: SecretStr
    pi_runtime_sender_token: SecretStr | None = None
    pi_provider_allowed_origins: tuple[str, ...] = ()


class Settings(ControlSettings):
    minio_root_user: str = "blackboard"
    minio_root_password: SecretStr
    minio_endpoint: str = "http://minio:9000"
    minio_bucket: str = "blackboard"
    envd_token_secret: SecretStr
    exec_network: str = "blackboard-explorer_exec"
    exec_access_mode: Literal["network", "relay"] = "network"
    exec_egress_mode: Literal["direct", "proxy"] = "direct"
    legacy_network_policy_file: Path | None = None
    legacy_control_network: str | None = None
    legacy_envd_image: str = "bbx-exec-env:latest"
    exec_min_pids: int = Field(default=1024, ge=64)
    exec_apparmor_profile: str | None = Field(
        default=None, pattern=r"^bbx-[a-z0-9][a-z0-9_.-]{0,62}$"
    )
    egress_proxy_url: str = "http://egress-proxy:8888"
    max_running_tasks: int = Field(default=2, ge=1)
    runner_proxy_host: str = "127.0.0.1"
    runner_proxy_port: int | None = Field(default=None, ge=1, le=65535)
    runner_proxy_max_inflight: int = Field(default=8, ge=1, le=128)
    otel_enabled: bool = False
    traffic_enabled: bool = True
    traffic_poll_seconds: float = Field(default=1, ge=0.1, le=60)
    traffic_max_bytes: int = Field(default=2 * 1024**3, ge=1024**2)


class SchedulerSettings(Settings):
    postgres_host: str = "postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = "blackboard"
    postgres_password: SecretStr
    postgres_db: str = "blackboard"


class NativeRunnerSettings(ControlSettings):
    postgres_host: str = "postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = "blackboard"
    postgres_password: SecretStr
    postgres_db: str = "blackboard"
    minio_root_user: str = "blackboard"
    minio_root_password: SecretStr | None = None
    minio_endpoint: str = "http://minio:9000"
    minio_bucket: str = "blackboard"
    runner_resource_config_file: Path
    board_resource_config_file: Path | None = None
    runner_blackboard_url: str
    runner_model_proxy_url: str
    runner_proxy_host: str = "0.0.0.0"
    runner_proxy_port: int = Field(default=8081, ge=1, le=65535)
    runner_proxy_max_inflight: int = Field(default=8, ge=1, le=128)
    max_running_tasks: int = Field(default=2, ge=1, le=20)
