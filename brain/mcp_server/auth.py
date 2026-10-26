"""Identity of the caller.

Phase 3: no OAuth. In dev mode (DEV_USER_ID, local URL only) there is no token and every call is the dev user.
TODO(phase 4): a TokenVerifier for the chosen authorization server + AuthSettings on MCPServer; then this
provider returns VerifiedToken(issuer, subject, client_id, client_name) from
mcp.server.auth.middleware.auth_context.get_access_token().
"""
from __future__ import annotations

from brain.config import Settings
from brain.domain.identity import VerifiedToken


def token_provider(settings: Settings):
    def no_token() -> VerifiedToken | None:
        return None
    return no_token
