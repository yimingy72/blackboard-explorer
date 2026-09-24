"""Blackboard environment configuration."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    postgres_user: str = Field(default="blackboard")
    postgres_password: SecretStr
    postgres_db: str = Field(default="blackboard")
    postgres_host: str = Field(default="postgres")
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    minio_root_user: str = Field(default="blackboard")
    minio_root_password: SecretStr
    minio_endpoint: str = Field(default="http://minio:9000")
    minio_bucket: str = Field(default="blackboard")
    embed_model: str = Field(default="BAAI/bge-small-zh-v1.5")
    embed_dim: int = Field(default=512, ge=1)
    service_token: SecretStr
    agent_token_secret: SecretStr
    admin_users: SecretStr
    otel_enabled: bool = False
