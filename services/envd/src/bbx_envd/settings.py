"""Execution environment daemon configuration."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    envd_token_secret: SecretStr
    otel_enabled: bool = False
