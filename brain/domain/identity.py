"""Who is calling. user_id never comes from the model or tool arguments.

- Dev mode (DEV_USER_ID set, no OAuth): every call acts as the configured user.
- OAuth (phase 4): the verified token's (issuer, subject) is matched against users.identities.
The trust level comes from the deployment's settings, never from the client.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from brain.config import Settings
from brain.domain import sensitivity


class IdentityError(Exception):
    """The caller could not be resolved to a known user."""


@dataclass(frozen=True)
class UserContext:
    user_id: int
    trust: str             # "cloud" | "local"
    channel: str           # "claude" | "chatgpt" | "local" | "dev" | …
    max_sensitivity: str

    @property
    def allowed(self) -> list[str]:
        return sensitivity.allowed_labels(self.max_sensitivity)


@dataclass(frozen=True)
class VerifiedToken:
    """What phase 4's TokenVerifier hands over (a subset of the SDK's AccessToken)."""
    issuer: str
    subject: str
    client_id: str | None = None
    client_name: str | None = None


class IdentityLookup(Protocol):
    async def find_by_identity(self, provider: str, subject: str) -> int | None: ...


def channel_for(token: VerifiedToken | None, settings: Settings) -> str:
    if token is None:
        return "dev"
    for name in (token.client_name, token.client_id):
        if name and name.lower() in settings.channel_by_client:
            return settings.channel_by_client[name.lower()]
    return "unknown"


async def resolve_user(settings: Settings, token: VerifiedToken | None, users: IdentityLookup) -> UserContext:
    trust = settings.trust_level
    max_label = sensitivity.max_for(trust, settings.max_sensitivity_local)
    if token is None:
        if settings.dev_mode:
            return UserContext(settings.dev_user_id, trust, "dev", max_label)
        raise IdentityError("Not signed in")
    user_id = await users.find_by_identity(token.issuer, token.subject)
    if user_id is None:
        raise IdentityError("This account is not linked to a user yet. Ask the administrator to link it.")
    return UserContext(user_id, trust, channel_for(token, settings), max_label)
