"""Login with Auth0 for the web app: the authorization code flow of a Regular Web Application.

The browser is sent to Auth0's login page; Auth0 sends it back to /auth/callback with a code; the backend
exchanges the code (with the client secret) for an ID token and verifies it against the tenant's keys: issuer,
audience (our client id), expiry, nonce. The tokens are not kept: the session cookie holds only who the user is.

The same Auth0 user has the same `sub` in Claude's connector and here, so both find the same users record (its
`identities`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlencode

import anyio
import httpx2
import jwt

from brain.config import Settings
from brain.domain.identity import VerifiedToken

logger = logging.getLogger("brain.web.auth")

ALGORITHMS = ["RS256"]
CLIENT_NAME = "web"            # its channel (CHANNEL_BY_CLIENT): conversations and feedback say "web"


class LoginError(Exception):
    """The login didn't complete (Auth0 refused, the code or the ID token was bad)."""


@dataclass(frozen=True)
class WebUser:
    issuer: str
    subject: str
    email: str | None = None
    name: str | None = None
    picture: str | None = None
    sid: str = ""              # this browser's session: the conversations log's session

    def token(self, client_id: str | None) -> VerifiedToken:
        return VerifiedToken(issuer=self.issuer, subject=self.subject, client_id=client_id,
                             client_name=CLIENT_NAME, email=self.email)


class Auth0Login:
    def __init__(self, settings: Settings, keys=None, http: httpx2.AsyncClient | None = None):
        self.issuer = settings.oauth_issuer or ""
        self.base = self.issuer.rstrip("/")
        self.client_id = settings.web_client_id or ""
        self.secret = settings.web_client_secret.get_secret_value() if settings.web_client_secret else ""
        self.keys = keys or jwt.PyJWKClient(f"{self.base}/.well-known/jwks.json", cache_keys=True, lifespan=3600,
                                            timeout=10)
        self.http = http

    def authorize_url(self, redirect_uri: str, state: str, nonce: str) -> str:
        # audience=/userinfo: no API token. Otherwise the tenant's Default Audience (the MCP API, for Claude's
        # connector) applies, and this app isn't, and needn't be, authorized for it.
        return f"{self.base}/authorize?" + urlencode({
            "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
            "scope": "openid profile email", "audience": f"{self.base}/userinfo", "state": state, "nonce": nonce})

    def logout_url(self, return_to: str) -> str:
        return f"{self.base}/v2/logout?" + urlencode({"client_id": self.client_id, "returnTo": return_to})

    async def _exchange(self, code: str, redirect_uri: str) -> str:
        data = {"grant_type": "authorization_code", "client_id": self.client_id, "client_secret": self.secret,
                "code": code, "redirect_uri": redirect_uri}
        async with (httpx2.AsyncClient(timeout=15) if self.http is None else _keep(self.http)) as http:
            r = await http.post(f"{self.base}/oauth/token", data=data)
        if r.status_code != 200:
            logger.info(f"Auth0 token exchange refused: {r.status_code} {r.text[:200]}")
            raise LoginError("Auth0 refused the login code")
        token = r.json().get("id_token")
        if not token:
            raise LoginError("Auth0 sent no ID token")
        return token

    def _decode(self, token: str) -> dict:
        key = self.keys.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=ALGORITHMS, audience=self.client_id, issuer=self.issuer,
                          leeway=30, options={"require": ["exp", "iss", "aud", "sub"]})

    async def identity(self, code: str, redirect_uri: str, nonce: str) -> dict:
        """The verified ID token's claims for a callback's code."""
        token = await self._exchange(code, redirect_uri)
        try:
            claims = await anyio.to_thread.run_sync(self._decode, token)      # the JWKS fetch is blocking
        except (jwt.PyJWTError, ValueError) as e:
            raise LoginError(f"bad ID token: {type(e).__name__}") from None
        if claims.get("nonce") != nonce:
            raise LoginError("bad ID token: nonce")
        return claims


class _keep:
    """An injected client (tests) used as a context manager without closing it."""

    def __init__(self, http):
        self.http = http

    async def __aenter__(self):
        return self.http

    async def __aexit__(self, *exc):
        return False
