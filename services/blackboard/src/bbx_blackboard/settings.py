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
    pi_site_id: str = Field(default="", pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$|^$")
    pi_model_authority: str = ""
    pi_model_primary_site: str = ""
    pi_model_primary_url: str = ""
    pi_model_operator: str = ""
    pi_model_allowed_origins: list[str] = Field(default_factory=list)
    pi_model_authority_token: SecretStr = SecretStr("")
    pi_model_runtime_token: SecretStr = SecretStr("")
    pi_review_peer_url: str = ""
    pi_review_peer_site: str = ""
    otel_enabled: bool = False
    profiles_dir: Path = Path("profiles/default")
    native_gateway_accounting_source_id: str = ""
    native_gateway_admin_url: str = ""
    native_gateway_data_url: str = ""
    native_gateway_admin_token: SecretStr = SecretStr("")
    native_gateway_catalog_revision: str = ""
    gateway_catalog_read_key: SecretStr = SecretStr("")
    gateway_review_key_owner: bool = False
    gateway_model_management_url: str = ""
    gateway_site_id: str = Field(default="", pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$|^$")
    gateway_review_key: SecretStr = SecretStr("")
    gateway_review_key_id: str = ""
    gateway_runtime_data_url: str = ""
    gateway_maintenance_key: SecretStr = SecretStr("")
    gateway_maintenance_peers: dict[str, str] = Field(default_factory=dict)
    runner_mcp_signing_key: SecretStr = SecretStr("")
    runner_mcp_backend_token: SecretStr = SecretStr("")
    runner_mcp_direct_v2: bool = False
    native_mcp_gateway_url: str = ""
    native_mcp_backend_url: str = ""
    native_mcp_jwks_file: str = ""
    native_mcp_backend_token_file: str = ""
    sandbox_tenant_resolver_token: SecretStr = SecretStr("")
    native_ctf_publication_file: Path | None = None
    legacy_network_policy_file: Path | None = None

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
