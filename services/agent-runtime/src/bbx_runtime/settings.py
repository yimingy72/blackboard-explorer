"""Agent runtime environment configuration."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    deepseek_api_key: SecretStr
    deepseek_base_url: str = "https://api.deepseek.com"
    minio_root_user: str = "blackboard"
    minio_root_password: SecretStr
    minio_endpoint: str = "http://minio:9000"
    minio_bucket: str = "blackboard"
    service_token: SecretStr
    agent_token_secret: SecretStr
    envd_token_secret: SecretStr
    max_running_tasks: int = Field(default=1, ge=1)
    otel_enabled: bool = False
