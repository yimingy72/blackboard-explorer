"""Execution environment daemon configuration."""

from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    envd_token: SecretStr
    command_timeout_max: int = 1200
    evidence_max_bytes: int = 50 * 1024 * 1024
    archive_max_bytes: int = 2 * 1024 * 1024 * 1024
    privileged_prefixes: str = "apt-get install,apt-get update,pip install,npm install -g"
    archive_exclude: str = "**/.git/objects/,**/node_modules/,**/__pycache__/,**/target/,**/*.o"
    envd_mode: Literal["blackboard", "ctf"] = "blackboard"
    envd_task_id: UUID | None = None
    envd_traffic_enabled: bool = False
    envd_traffic_max_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=65536)
    envd_traffic_max_records: int = Field(default=1000000, ge=1)

    @model_validator(mode="after")
    def ctf_task_identity(self) -> "Settings":
        if self.envd_mode == "ctf" and self.envd_task_id is None:
            raise ValueError("CTF envd requires a fixed task ID")
        return self
