"""Signed cookie values: the web session and the login's state. HMAC-SHA256 over JSON, with an expiry.

Signed, not encrypted: they hold who the user is (Auth0 issuer, subject, email, name), never tokens or secrets.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

SESSION_COOKIE = "brain_session"
LOGIN_COOKIE = "brain_login"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: bytes, body: str) -> str:
    return _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())


def sign(data: dict, secret: bytes, max_age: int, now: float | None = None) -> str:
    body = _b64(json.dumps({**data, "exp": int((now or time.time()) + max_age)}, separators=(",", ":")).encode())
    return f"{body}.{_mac(secret, body)}"


def verify(value: str | None, secret: bytes, now: float | None = None) -> dict | None:
    """The data if the value is ours and not expired, else None."""
    if not value or value.count(".") != 1:
        return None
    body, mac = value.split(".")
    if not hmac.compare_digest(mac, _mac(secret, body)):
        return None
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < (now or time.time()):
        return None
    data.pop("exp")
    return data
