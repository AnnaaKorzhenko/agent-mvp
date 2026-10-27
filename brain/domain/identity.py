"""Who is calling. user_id never comes from the model or tool arguments.

- Dev mode (DEV_USER_ID set, no OAuth, local URL only): every call acts as the configured user.
- OAuth: the verified token's (issuer, subject) is looked up in users.identities. A login seen for the
  first time gets its own new users record (AUTO_CREATE_USERS); existing Telegram users are not touched
  or linked. ALLOWED_EMAILS, if set, is checked on every call.
The trust level comes from the deployment's settings, never from the client.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from brain.config import Settings
from brain.domain import sensitivity


class IdentityError(Exception):
    """The caller could not be resolved to an allowed user."""


@dataclass(frozen=True)
class UserContext:
    user_id: int | str     # Telegram id, or the hex _id of a users record created for an OAuth login
    trust: str             # "cloud" | "local"
    channel: str           # "claude" | "chatgpt" | "local" | "dev" | "unknown"
    max_sensitivity: str

    @property
    def allowed(self) -> list[str]:
        return sensitivity.allowed_labels(self.max_sensitivity)


@dataclass(frozen=True)
class VerifiedToken:
    """What the token verifier established about the caller."""
    issuer: str
    subject: str
    client_id: str | None = None
    client_name: str | None = None
    email: str | None = None


class UserDirectory(Protocol):
    async def find_by_identity(self, provider: str, subject: str) -> int | str | None: ...

    async def create_identity_user(self, provider: str, subject: str, channel: str,
                                   email: str | None = None) -> int | str: ...


def channel_for(token: VerifiedToken | None, settings: Settings) -> str:
    if token is None:
        return "dev"
    for name in (token.client_name, token.client_id):
        if name and name.lower() in settings.channel_by_client:
            return settings.channel_by_client[name.lower()]
    return "unknown"


async def resolve_user(settings: Settings, token: VerifiedToken | None, users: UserDirectory) -> UserContext:
    trust = settings.trust_level
    max_label = sensitivity.max_for(trust, settings.max_sensitivity_local)
    if token is None:
        if settings.dev_mode:
            return UserContext(settings.dev_user_id, trust, "dev", max_label)
        raise IdentityError("Not signed in")
    if settings.allowed_emails and (token.email or "").lower() not in settings.allowed_emails:
        raise IdentityError("This account is not allowed to use this server. Ask the administrator.")
    channel = channel_for(token, settings)
    user_id = await users.find_by_identity(token.issuer, token.subject)
    if user_id is None:
        if not settings.auto_create_users:
            raise IdentityError("This account has no profile here yet. Ask the administrator.")
        user_id = await users.create_identity_user(token.issuer, token.subject, channel, token.email)
    return UserContext(user_id, trust, channel, max_label)
