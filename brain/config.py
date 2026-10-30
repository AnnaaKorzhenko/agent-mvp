"""All settings of the brain, read from env (and .env for local development). Nothing else reads os.environ."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TrustLevel = Literal["cloud", "local"]
Sensitivity = Literal["public", "internal", "confidential", "restricted"]


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error must never print env values (keys, URIs) into logs
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      hide_input_in_errors=True)

    # --- MongoDB ---
    mongo_uri: SecretStr = Field(alias="MONGO_URI")
    mongo_db: str = Field("News", alias="MONGO_DB")

    # --- Who is calling ---
    # Trust level of THIS deployment, never taken from the client: Render = cloud, own machine = local.
    trust_level: TrustLevel = Field("cloud", alias="TRUST_LEVEL")
    # Local development without OAuth: every call acts as this users._id (a Telegram id, or a users _id hex).
    dev_user_id: int | str | None = Field(None, alias="DEV_USER_ID")
    max_sensitivity_local: Sensitivity = Field("restricted", alias="MAX_SENSITIVITY_LOCAL")
    # OAuth client_id / client_name (lower-case) -> feedback channel
    channel_by_client: dict[str, str] = Field(
        default_factory=lambda: {"claude": "claude", "chatgpt": "chatgpt", "openai": "chatgpt", "local": "local"},
        alias="CHANNEL_BY_CLIENT")

    # --- HTTP ---
    public_base_url: str | None = Field(None, alias="PUBLIC_BASE_URL")   # e.g. https://mcp.example.com
    allowed_hosts: list[str] = Field(default_factory=list, alias="ALLOWED_HOSTS")

    # --- OAuth (Auth0) ---
    # Issuer exactly as in the token's `iss`, e.g. https://YOUR-TENANT.eu.auth0.com/ (with the trailing slash)
    oauth_issuer: str | None = Field(None, alias="OAUTH_ISSUER")
    # The Auth0 API identifier = this server's MCP URL; defaults to PUBLIC_BASE_URL + /mcp
    oauth_audience: str | None = Field(None, alias="OAUTH_AUDIENCE")
    oauth_required_scopes: list[str] = Field(default_factory=list, alias="OAUTH_REQUIRED_SCOPES")
    # Namespace of the custom claims the Auth0 post-login Action adds (client_name, email)
    oauth_claims_namespace: str = Field("https://news-brain/", alias="OAUTH_CLAIMS_NAMESPACE")
    # Signed-in people get their own users record on first use (Telegram users are left as they are)
    auto_create_users: bool = Field(True, alias="AUTO_CREATE_USERS")
    # If set, only these emails (from the Action's email claim) may use the server
    allowed_emails: list[str] = Field(default_factory=list, alias="ALLOWED_EMAILS")

    # --- Model backends (not used by v0 tools) ---
    model_backend: Literal["local", "anthropic"] = Field("local", alias="MODEL_BACKEND")
    anthropic_api_key: SecretStr | None = Field(None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-opus-5-5", alias="ANTHROPIC_MODEL")

    # --- Automatic learning (brain/learning/auto.py): runs inside the MCP service ---
    learning_auto: bool = Field(False, alias="LEARNING_AUTO")
    learning_feedback_threshold: int = Field(20, ge=1, alias="LEARNING_FEEDBACK_THRESHOLD")
    learning_goal_delay_minutes: int = Field(2, ge=0, alias="LEARNING_GOAL_DELAY_MINUTES")
    learning_cooldown_minutes: int = Field(30, ge=0, alias="LEARNING_COOLDOWN_MINUTES")
    learning_max_runs_per_day: int = Field(20, ge=0, alias="LEARNING_MAX_RUNS_PER_DAY")
    learning_backend: Literal["local", "anthropic"] | None = Field(None, alias="LEARNING_BACKEND")  # default: MODEL_BACKEND
    # Agents (docs/AGENT_PLAN.md): tokens one user's runs of one agent may use per UTC day.
    agent_user_tokens_per_day: int = Field(300_000, ge=0, alias="AGENT_USER_TOKENS_PER_DAY")
    # Write the pipeline's second keyword record («Profile interests») from users' lasting interests. Off until the
    # pipeline reads it (agreed with the pipeline's owner).
    profile_keywords: bool = Field(False, alias="PROFILE_KEYWORDS")
    # The scout (docs/AGENT_PLAN.md phase 2): rising names in our posts, proposed to users they fit. Its backend
    # and model default to the learning job's; SCOUT_MODEL can name a smaller one (e.g. claude-haiku-5-5).
    scout_auto: bool = Field(False, alias="SCOUT_AUTO")
    scout_every_hours: int = Field(6, ge=1, le=48, alias="SCOUT_EVERY_HOURS")
    scout_backend: Literal["local", "anthropic"] | None = Field(None, alias="SCOUT_BACKEND")
    scout_model: str | None = Field(None, alias="SCOUT_MODEL")
    # The web scout (phase 3): searches the web around users' goals and interests, once a day by default, within
    # a daily search budget (searches are billed per search). Anthropic backend only.
    scout_web: bool = Field(False, alias="SCOUT_WEB")
    scout_web_every_hours: int = Field(24, ge=6, le=168, alias="SCOUT_WEB_EVERY_HOURS")
    scout_web_model: str | None = Field(None, alias="SCOUT_WEB_MODEL")       # default: SCOUT_MODEL
    scout_web_searches_per_day: int = Field(40, ge=0, alias="SCOUT_WEB_SEARCHES_PER_DAY")
    scout_web_areas: int = Field(8, ge=1, le=50, alias="SCOUT_WEB_AREAS")
    # The advisor (phase 4): once a day, what changed for each user and one next step, as proposals. Backend as the
    # scout's; ADVISOR_MODEL, else SCOUT_MODEL.
    advisor_auto: bool = Field(False, alias="ADVISOR_AUTO")
    advisor_every_hours: int = Field(24, ge=6, le=168, alias="ADVISOR_EVERY_HOURS")
    advisor_model: str | None = Field(None, alias="ADVISOR_MODEL")

    # --- Audit and conversation log ---
    audit_ttl_days: int = Field(180, alias="AUDIT_TTL_DAYS")
    # A new conversations document after this much inactivity, or once one holds this many messages
    conversation_idle_minutes: int = Field(30, ge=1, alias="CONVERSATION_IDLE_MINUTES")
    conversation_max_messages: int = Field(500, ge=10, alias="CONVERSATION_MAX_MESSAGES")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    @field_validator("dev_user_id", mode="before")
    @classmethod
    def _telegram_ids_are_ints(cls, v):
        """Env values are text: "111000111" is a Telegram id (int); a 24-hex value stays a string."""
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            return int(v.strip())
        return v or None

    @property
    def dev_mode(self) -> bool:
        return self.dev_user_id is not None and not self.oauth_issuer

    @property
    def resource_url(self) -> str | None:
        """The MCP endpoint's public URL: the OAuth resource / audience Claude asks a token for."""
        if self.oauth_audience:
            return self.oauth_audience
        return f"{self.public_base_url.rstrip('/')}/mcp" if self.public_base_url else None

    @model_validator(mode="after")
    def _dev_mode_only_locally(self) -> "Settings":
        url = (self.public_base_url or "").lower()
        is_local = not url or any(h in url for h in ("://localhost", "://127.0.0.1", "://[::1]"))
        if self.dev_mode and not is_local:
            raise ValueError("DEV_USER_ID without OAuth is allowed only on a local URL "
                             f"(PUBLIC_BASE_URL={self.public_base_url})")
        if self.oauth_issuer and not self.resource_url:
            raise ValueError("OAUTH_ISSUER needs PUBLIC_BASE_URL (or OAUTH_AUDIENCE): the MCP URL tokens are for")
        self.allowed_emails = [e.strip().lower() for e in self.allowed_emails if e.strip()]
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
