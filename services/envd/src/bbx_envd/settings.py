"""Execution environment daemon configuration."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    envd_token: SecretStr
    command_timeout_max: int = 1200
    evidence_max_bytes: int = 50 * 1024 * 1024
    archive_max_bytes: int = 2 * 1024 * 1024 * 1024
    privileged_prefixes: str = "apt-get install,apt-get update,pip install,npm install -g"
    archive_exclude: str = "**/.git/objects/,**/node_modules/,**/__pycache__/,**/target/,**/*.o"
