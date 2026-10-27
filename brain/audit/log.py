"""Audit log: every tool call and every model or external call, in the audit_log collection and the app log.

Writing an audit entry never breaks the call being audited: failures are logged and swallowed.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

logger = logging.getLogger("brain.audit")

COLLECTION = "audit_log"
_SECRET_KEYS = re.compile(r"token|secret|password|api[_-]?key|authorization|cookie", re.I)
MAX_ARG_CHARS = 500


def redact(args: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if _SECRET_KEYS.search(str(k)):
            out[k] = "[redacted]"
        elif isinstance(v, str) and len(v) > MAX_ARG_CHARS:
            out[k] = v[:MAX_ARG_CHARS] + "…"
        else:
            out[k] = v
    return out


class AuditLog:
    def __init__(self, db: AsyncDatabase | None):
        self.col = db[COLLECTION] if db is not None else None

    async def _write(self, entry: dict) -> None:
        logger.info("audit %s", {k: v for k, v in entry.items() if k != "at"})
        if self.col is None:
            return
        try:
            await self.col.insert_one(entry)
        except Exception as e:             # noqa: BLE001 - auditing must not break the call
            logger.error(f"Audit write failed: {type(e).__name__}: {e}")

    async def tool_call(self, *, name: str, user_id: int | None, trust: str | None, channel: str | None,
                        args: dict | None, items_returned: int = 0, max_sensitivity: str | None = None,
                        outcome: str = "ok", error: str | None = None, duration_ms: int | None = None) -> None:
        await self._write({"at": datetime.now(timezone.utc), "kind": "tool", "name": name, "user_id": user_id,
                           "trust": trust, "channel": channel, "args": redact(args),
                           "items_returned": items_returned, "max_sensitivity": max_sensitivity,
                           "outcome": outcome, "error": (error or None) and error[:300], "duration_ms": duration_ms})

    async def model_call(self, *, backend: str, model: str, max_sensitivity: str, input_tokens: int = 0,
                         output_tokens: int = 0, outcome: str = "ok", error: str | None = None,
                         user_id: int | None = None) -> None:
        await self._write({"at": datetime.now(timezone.utc), "kind": "model", "name": f"{backend}:{model}",
                           "user_id": user_id, "max_sensitivity": max_sensitivity,
                           "tokens": {"input": input_tokens, "output": output_tokens},
                           "outcome": outcome, "error": (error or None) and error[:300]})


async def ensure_audit_indexes(db: AsyncDatabase, ttl_days: int) -> None:
    """Run from a setup script, not at import time."""
    col = db[COLLECTION]
    await col.create_index("at", expireAfterSeconds=int(timedelta(days=ttl_days).total_seconds()), name="ttl_at")
    await col.create_index([("user_id", 1), ("at", -1)], name="user_at")
