"""OAuth for the MCP server, with Auth0 as the authorization server.

The server is only a resource server: it never shows a login page or issues tokens. It
- publishes RFC 9728 metadata naming Auth0 (the SDK does this from AuthSettings),
- answers unauthenticated requests with 401 + WWW-Authenticate (the SDK),
- verifies Auth0's RS256 JWT access tokens against the tenant's JWKS (Auth0TokenVerifier below):
  issuer, audience (= this server's MCP URL), expiry, subject.
Custom claims `<namespace>client_name` and `<namespace>email` come from the Auth0 post-login Action
described in docs/CONNECT_CLAUDE.md; both are optional.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

import anyio
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from brain.config import Settings
from brain.domain.identity import VerifiedToken

logger = logging.getLogger("brain.auth")

ALGORITHMS = ["RS256"]


class SigningKeySource(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class Auth0TokenVerifier:
    """The SDK's TokenVerifier for Auth0 access tokens (JWT, RS256)."""

    def __init__(self, issuer: str, audience: str, keys: SigningKeySource | None = None, leeway: int = 30):
        self.issuer, self.audience, self.leeway = issuer, audience, leeway
        self.keys = keys or jwt.PyJWKClient(f"{issuer.rstrip('/')}/.well-known/jwks.json", cache_keys=True,
                                            lifespan=3600, timeout=10)

    def _decode(self, token: str) -> dict:
        key = self.keys.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=ALGORITHMS, audience=self.audience, issuer=self.issuer,
                          leeway=self.leeway, options={"require": ["exp", "iss", "aud", "sub"]})

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await anyio.to_thread.run_sync(self._decode, token)     # JWKS fetch is blocking
        except (jwt.PyJWTError, ValueError) as e:
            logger.info(f"Rejected access token: {type(e).__name__}: {e}")
            return None
        return AccessToken(token=token, client_id=str(claims.get("azp") or claims.get("client_id") or ""),
                           scopes=str(claims.get("scope") or "").split(), expires_at=claims.get("exp"),
                           resource=self.audience, subject=claims["sub"], claims=claims)


def auth_settings(settings: Settings) -> AuthSettings:
    return AuthSettings(issuer_url=settings.oauth_issuer, resource_server_url=settings.resource_url,
                        required_scopes=settings.oauth_required_scopes or None, validate_token_resource=True)


def token_verifier(settings: Settings) -> Auth0TokenVerifier:
    return Auth0TokenVerifier(settings.oauth_issuer, settings.resource_url)


def token_provider(settings: Settings):
    """The caller's verified identity for the current request (None in dev mode / outside a request)."""
    ns = settings.oauth_claims_namespace

    def current() -> VerifiedToken | None:
        if not settings.oauth_issuer:
            return None
        token = get_access_token()
        if token is None or not token.subject:
            return None
        claims = token.claims or {}
        return VerifiedToken(issuer=str(claims.get("iss") or settings.oauth_issuer), subject=token.subject,
                             client_id=token.client_id or None, client_name=claims.get(f"{ns}client_name"),
                             email=(claims.get(f"{ns}email") or None))
    return current
