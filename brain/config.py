"""All settings of the brain, read from env (and .env for local development). Nothing else reads os.environ."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TrustLevel = Literal["cloud", "local"]
Sensitivity = Literal["public", "internal", "confidential", "restricted"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- MongoDB ---
    mongo_uri: SecretStr = Field(alias="MONGO_URI")
    mongo_db: str = Field("News", alias="MONGO_DB")

    # --- Who is calling ---
    # Trust level of THIS deployment, never taken from the client: Railway = cloud, own machine = local.
    trust_level: TrustLevel = Field("cloud", alias="TRUST_LEVEL")
    # Local development without OAuth: every call acts as this users._id (Telegram id).
    dev_user_id: int | None = Field(None, alias="DEV_USER_ID")
    max_sensitivity_local: Sensitivity = Field("restricted", alias="MAX_SENSITIVITY_LOCAL")
    # OAuth client_id / client_name (lower-case) -> feedback channel
    channel_by_client: dict[str, str] = Field(
        default_factory=lambda: {"claude": "claude", "chatgpt": "chatgpt", "openai": "chatgpt", "local": "local"},
        alias="CHANNEL_BY_CLIENT")

    # --- HTTP ---
    public_base_url: str | None = Field(None, alias="PUBLIC_BASE_URL")   # e.g. https://mcp.example.com
    allowed_hosts: list[str] = Field(default_factory=list, alias="ALLOWED_HOSTS")

    # --- OAuth (phase 4) ---
    oauth_issuer: str | None = Field(None, alias="OAUTH_ISSUER")
    oauth_required_scopes: list[str] = Field(default_factory=list, alias="OAUTH_REQUIRED_SCOPES")

    # --- Model backends (not used by v0 tools) ---
    model_backend: Literal["local", "anthropic"] = Field("local", alias="MODEL_BACKEND")
    anthropic_api_key: SecretStr | None = Field(None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-opus-5-5", alias="ANTHROPIC_MODEL")

    # --- Audit ---
    audit_ttl_days: int = Field(180, alias="AUDIT_TTL_DAYS")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    @property
    def dev_mode(self) -> bool:
        return self.dev_user_id is not None and not self.oauth_issuer

    @model_validator(mode="after")
    def _dev_mode_only_locally(self) -> "Settings":
        url = (self.public_base_url or "").lower()
        is_local = not url or any(h in url for h in ("://localhost", "://127.0.0.1", "://[::1]"))
        if self.dev_mode and not is_local:
            raise ValueError("DEV_USER_ID without OAuth is allowed only on a local URL "
                             f"(PUBLIC_BASE_URL={self.public_base_url})")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
