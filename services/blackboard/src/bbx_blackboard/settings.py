"""Blackboard environment configuration."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


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
    service_token: SecretStr
    agent_token_secret: SecretStr
    admin_users: SecretStr
    otel_enabled: bool = False
    profiles_dir: Path = Path("profiles/default")

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )
