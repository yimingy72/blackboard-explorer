"""Agent runtime environment configuration."""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    deepseek_api_key: SecretStr
    deepseek_base_url: str = "https://api.deepseek.com"
    blackboard_url: str = "http://blackboard:8000"
    minio_root_user: str = "blackboard"
    minio_root_password: SecretStr
    minio_endpoint: str = "http://minio:9000"
    minio_bucket: str = "blackboard"
    service_token: SecretStr
    envd_token_secret: SecretStr
    exec_network: str = "blackboard-explorer_exec"
    exec_access_mode: Literal["network", "relay"] = "network"
    egress_proxy_url: str = "http://egress-proxy:8888"
    max_running_tasks: int = Field(default=1, ge=1)
    otel_enabled: bool = False
