"""Security log: the audit_log collection keeps security events only, with no content.

Events (docs/CONVERSATIONS_PLAN.md §2): auth_token_accepted, auth_failed, access_denied, sensitivity_filtered,
model_call, conversation_log_error. Tool activity goes to `conversations` (brain/audit/conversations.py).
audit_log stays separate because it must survive if a user's conversations are summarized or deleted, it is
the evidence that nothing sensitive left the system, and it is small.

Writing an entry never breaks the call being logged: failures are logged and swallowed.
Entries written before 2026-10-28 have the older shape {kind: "tool", name, args, …}; they are left as they are.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

logger = logging.getLogger("brain.audit")

COLLECTION = "audit_log"
EVENTS = ("auth_token_accepted", "auth_failed", "access_denied", "sensitivity_filtered", "model_call",
          "conversation_log_error")
_SECRET_KEYS = re.compile(r"token|secret|password|api[_-]?key|authorization|cookie", re.I)
MAX_DETAIL_CHARS = 300


def redact(values: dict[str, Any] | None, max_chars: int = MAX_DETAIL_CHARS) -> dict[str, Any]:
    """Secret-looking keys hidden, long strings cut."""
    out: dict[str, Any] = {}
    for k, v in (values or {}).items():
        if _SECRET_KEYS.search(str(k)):
            out[k] = "[redacted]"
        elif isinstance(v, str) and len(v) > max_chars:
            out[k] = v[:max_chars] + "…"
        else:
            out[k] = v
    return out


class AuditLog:
    def __init__(self, db: AsyncDatabase | None):
        self.col = db[COLLECTION] if db is not None else None

    async def event(self, event: str, *, user_id: int | str | None = None, channel: str | None = None,
                    mcp_session_id: str | None = None, **details: Any) -> None:
        if event not in EVENTS:
            raise ValueError(f"unknown security event {event!r}")
        entry = {"at": datetime.now(timezone.utc), "event": event, "user_id": user_id, "channel": channel,
                 "mcp_session_id": mcp_session_id, "details": redact(details)}
        logger.info("security %s", {k: v for k, v in entry.items() if k != "at" and v is not None})
        if self.col is None:
            return
        try:
            await self.col.insert_one(entry)
        except Exception as e:             # noqa: BLE001 - logging must not break the call
            logger.error(f"Audit write failed: {type(e).__name__}: {e}")

    async def model_call(self, *, backend: str, model: str, max_sensitivity: str, input_tokens: int = 0,
                         output_tokens: int = 0, outcome: str = "ok", error: str | None = None,
                         user_id: int | str | None = None) -> None:
        await self.event("model_call", user_id=user_id, backend=backend, model=model,
                         max_sensitivity=max_sensitivity, input_tokens=input_tokens, output_tokens=output_tokens,
                         outcome=outcome, error=error)


async def ensure_audit_indexes(db: AsyncDatabase, ttl_days: int) -> None:
    """Run from a setup script, not at import time."""
    col = db[COLLECTION]
    await col.create_index("at", expireAfterSeconds=int(timedelta(days=ttl_days).total_seconds()), name="ttl_at")
    await col.create_index([("user_id", 1), ("at", -1)], name="user_at")
