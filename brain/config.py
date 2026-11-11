"""All settings of the brain, read from env (and .env for local development). Nothing else reads os.environ."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TrustLevel = Literal["cloud", "local"]
Sensitivity = Literal["public", "internal", "confidential", "restricted"]
# The web chat's models when WEB_CHAT_MODEL / WEB_CHAT_RESEARCH_MODEL aren't set: (chat, web research)
CHAT_DEFAULTS = {"anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5"), "openai": ("gpt-5-mini", "gpt-5-mini")}


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
        default_factory=lambda: {"claude": "claude", "chatgpt": "chatgpt", "openai": "chatgpt", "local": "local",
                                 "web": "web"},
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

    # --- Our own web app (brain/web, docs/WEB_PLAN.md): its own Render service, PUBLIC_BASE_URL = its URL ---
    # Auth0 login: a Regular Web Application in the same tenant (OAUTH_ISSUER), callback PUBLIC_BASE_URL/auth/callback
    web_client_id: str | None = Field(None, alias="WEB_CLIENT_ID")
    web_client_secret: SecretStr | None = Field(None, alias="WEB_CLIENT_SECRET")
    # Signs the session cookie; 32+ random bytes, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`
    web_session_secret: SecretStr | None = Field(None, alias="WEB_SESSION_SECRET")
    web_session_days: int = Field(14, ge=1, le=90, alias="WEB_SESSION_DAYS")
    # Who sees the agents console (brain/web/admin.py): a JSON list of emails, like ALLOWED_EMAILS; empty = nobody
    admin_emails: list[str] = Field(default_factory=list, alias="ADMIN_EMAILS")
    # The UI's files; default: data-digest-web/frontend in the repo
    web_frontend_dir: str | None = Field(None, alias="WEB_FRONTEND_DIR")
    # The web chat (brain/web/chat.py). WEB_CHAT_PROVIDER: anthropic (Claude) or openai (OpenAI, or any
    # OpenAI-compatible API at WEB_CHAT_BASE_URL: OpenRouter, Groq, Mistral, DeepSeek, Together, Ollama, …). Unset:
    # anthropic with ANTHROPIC_API_KEY, else openai with OPENAI_API_KEY. The key: WEB_CHAT_API_KEY, else the
    # provider's own (ANTHROPIC_API_KEY / OPENAI_API_KEY). The models default per provider (CHAT_DEFAULTS).
    web_chat_provider: Literal["anthropic", "openai"] | None = Field(None, alias="WEB_CHAT_PROVIDER")
    web_chat_api_key: SecretStr | None = Field(None, alias="WEB_CHAT_API_KEY")
    web_chat_base_url: str | None = Field(None, alias="WEB_CHAT_BASE_URL")
    web_chat_model: str | None = Field(None, alias="WEB_CHAT_MODEL")
    web_chat_tokens_per_day: int = Field(500_000, ge=0, alias="WEB_CHAT_TOKENS_PER_DAY")     # per user, UTC day
    web_chat_max_rounds: int = Field(8, ge=1, le=20, alias="WEB_CHAT_MAX_ROUNDS")      # model calls per message
    web_chat_history: int = Field(30, ge=2, le=200, alias="WEB_CHAT_HISTORY")          # earlier messages sent
    web_chat_web_search: bool = Field(True, alias="WEB_CHAT_WEB_SEARCH")               # web_research, deep dives
    # web_research runs the searches with this (smaller) model and hands the chat a short brief
    web_chat_research_model: str | None = Field(None, alias="WEB_CHAT_RESEARCH_MODEL")
    # Web Push: a VAPID key pair from `python -m brain.scripts.vapid_keys`, the same on the web app and the worker.
    # WEB_PUSH_CONTACT: a mailto: or https: URL the push services can reach us at (the VAPID `sub` claim).
    web_push_public_key: str | None = Field(None, alias="WEB_PUSH_PUBLIC_KEY")
    web_push_private_key: SecretStr | None = Field(None, alias="WEB_PUSH_PRIVATE_KEY")
    web_push_contact: str | None = Field(None, alias="WEB_PUSH_CONTACT")
    # The worker's morning push of the digest to subscribed browsers; needs the key pair
    morning_push: bool = Field(False, alias="MORNING_PUSH")

    # --- Model backends (not used by v0 tools) ---
    model_backend: Literal["local", "anthropic"] = Field("local", alias="MODEL_BACKEND")
    anthropic_api_key: SecretStr | None = Field(None, alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(None, alias="OPENAI_API_KEY")
    anthropic_model: str = Field("claude-opus-5-5", alias="ANTHROPIC_MODEL")

    # --- Agents' backend and model (brain/agents/common.py): an agent's own setting, else these ---
    agent_backend: Literal["local", "anthropic"] | None = Field(None, alias="AGENT_BACKEND")
    agent_model: str | None = Field(None, alias="AGENT_MODEL")

    # --- Where the agents run (brain/agents/setup.py): the MCP service, or the agents' worker of their own
    # (`python -m brain.agents`, a Render Background Worker). false on the MCP service once the worker runs them:
    # the MCP service then starts no background task, and the worker's change stream sees the goals, ratings and
    # insights the MCP tools write. The LEARNING_*, SCOUT_*, ADVISOR_* settings below go to whichever runs them.
    agents_in_mcp: bool = Field(True, alias="AGENTS_IN_MCP")

    # --- Automatic learning (brain/agents/learner.py) ---
    learning_auto: bool = Field(False, alias="LEARNING_AUTO")
    learning_feedback_threshold: int = Field(20, ge=1, alias="LEARNING_FEEDBACK_THRESHOLD")
    # New chat messages since the last run that make a user due. The web service queues the nudge after a chat turn
    # when LEARNING_AUTO is on there too (the worker runs it).
    learning_chat_threshold: int = Field(10, ge=1, alias="LEARNING_CHAT_THRESHOLD")
    learning_goal_delay_minutes: int = Field(2, ge=0, alias="LEARNING_GOAL_DELAY_MINUTES")
    learning_cooldown_minutes: int = Field(30, ge=0, alias="LEARNING_COOLDOWN_MINUTES")
    learning_max_runs_per_day: int = Field(20, ge=0, alias="LEARNING_MAX_RUNS_PER_DAY")
    learning_backend: Literal["local", "anthropic"] | None = Field(None, alias="LEARNING_BACKEND")
    # A merge of a profile doesn't need the largest model (ANTHROPIC_MODEL's default).
    learning_model: str | None = Field("claude-sonnet-5-5", alias="LEARNING_MODEL")
    # Agents (docs/AGENT_PLAN.md): tokens one user's runs of one agent may use per UTC day.
    agent_user_tokens_per_day: int = Field(300_000, ge=0, alias="AGENT_USER_TOKENS_PER_DAY")
    # The scout (docs/AGENT_PLAN.md phase 2): rising names in our posts, proposed to users they fit. Its backend:
    # SCOUT_BACKEND, else AGENT_BACKEND, else LEARNING_BACKEND; SCOUT_MODEL can name a smaller one (claude-haiku-5-5).
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

    # --- Semantic search (brain/domain/semantic.py) on the pipeline's embeddings, collection post_vectors ---
    # Off without VOYAGE_API_KEY: search_posts then keeps the keyword search.
    semantic_search: bool = Field(True, alias="SEMANTIC_SEARCH")
    voyage_api_key: SecretStr | None = Field(None, alias="VOYAGE_API_KEY")
    # Must match the pipeline (scrapers-3/pipeline/embed.py): the query is embedded with the same model and size
    embed_model: str = Field("voyage-4-large", alias="EMBED_MODEL")
    embed_dim: int = Field(1024, alias="EMBED_DIM")
    # Where post_vectors lives; default MONGO_DB. Local dev on News_dev: VECTORS_DB=News (copy_to_dev skips it)
    vectors_db: str | None = Field(None, alias="VECTORS_DB")
    rerank_lite_model: str = Field("rerank-3-lite", alias="RERANK_LITE_MODEL")    # stage 1, many candidates
    rerank_model: str = Field("rerank-3", alias="RERANK_MODEL")                   # stage 2, top ones with text
    rerank_candidates: int = Field(100, ge=10, le=300, alias="RERANK_CANDIDATES")
    rerank_final: int = Field(15, ge=5, le=50, alias="RERANK_FINAL")
    dedup_sim: float = Field(0.80, ge=0.5, le=1.0, alias="DEDUP_SIM")              # same event from several sources (voyage-4-large)
    min_rerank: float = Field(0.5, ge=0.0, le=1.0, alias="MIN_RERANK")             # below: not about the query
    # Voyage limits per model and minute (no payment method: 3 and 10000); waits up to SEMANTIC_MAX_WAIT seconds
    voyage_rpm: int = Field(3, ge=1, alias="VOYAGE_RPM")
    voyage_tpm: int = Field(10_000, ge=1000, alias="VOYAGE_TPM")
    semantic_max_wait: int = Field(30, ge=0, le=300, alias="SEMANTIC_MAX_WAIT")
    # Share of the per-minute token limit one rerank request (each stage) may use: 0.45 leaves room for a second search in the
    # same minute (the chat often searches 2-3 times per question). With a payment method: 1.0
    rerank_lite_share: float = Field(0.45, gt=0.0, le=1.0, alias="RERANK_LITE_SHARE")
    # When a rerank stage would have to wait for the Voyage limit, skip that stage instead of waiting
    rerank_skip_when_limited: bool = Field(True, alias="RERANK_SKIP_WHEN_LIMITED")
    # Diverse results: among posts above MIN_RERANK, pick each next one both relevant and unlike those already
    # picked (MMR), so one big cluster (e.g. interceptors) doesn't push out other relevant angles (EW, tactics).
    # SEARCH_DIVERSITY=false: plain order by relevance. Lambda: 1.0 = relevance only, lower = more variety.
    search_diversity: bool = Field(True, alias="SEARCH_DIVERSITY")
    diversity_lambda: float = Field(0.7, ge=0.0, le=1.0, alias="DIVERSITY_LAMBDA")
    # This many best keyword hits always reach the reranker (exact names, wider matches aren't crowded out); 0 = off
    keyword_quota: int = Field(8, ge=0, le=50, alias="KEYWORD_QUOTA")

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
    def chat_provider(self) -> str | None:
        """The web chat's provider: WEB_CHAT_PROVIDER, else the one whose key is set; None: no chat."""
        if self.web_chat_provider:
            return self.web_chat_provider
        if self.anthropic_api_key:
            return "anthropic"
        if self.openai_api_key or self.web_chat_api_key:
            return "openai"
        return None

    @property
    def chat_api_key(self) -> str | None:
        own = {"anthropic": self.anthropic_api_key, "openai": self.openai_api_key}.get(self.chat_provider or "")
        key = self.web_chat_api_key or own
        return key.get_secret_value() if key else None

    def chat_model(self, research: bool = False) -> str:
        """WEB_CHAT_MODEL (or WEB_CHAT_RESEARCH_MODEL), else the provider's default. A Claude model name left over
        with the openai provider (or another name with anthropic) is the other provider's: the default is used."""
        provider = self.chat_provider or "anthropic"
        name = self.web_chat_research_model if research else self.web_chat_model
        claude = (name or "").startswith("claude")
        if name and not self.web_chat_base_url and claude != (provider == "anthropic"):
            name = None
        return name or CHAT_DEFAULTS[provider][1 if research else 0]

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
        self.admin_emails = [e.strip().lower() for e in self.admin_emails if e.strip()]
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
