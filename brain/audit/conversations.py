"""MCP activity per user and session, in the `conversations` collection (docs/CONVERSATIONS_PLAN.md §1).

The server never sees the user's chat or Claude's replies, only tool calls. Each call becomes a `role: "tool"`
message (sanitized arguments, a result summary with references only); arguments that are the user's own words
(USER_WORDS) also become verbatim `role: "user"` messages. Nothing reads these documents back.

Documents sit next to the Telegram bot's (one per Telegram user, no `session_key`). A unique index on
(user_id, session_key) keeps the bot's one-document-per-user rule and lets MCP sessions have many.
Logging never breaks a tool: every failure is caught and reported to audit_log.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from brain.audit.log import AuditLog, redact

logger = logging.getLogger("brain.conversations")

COLLECTION = "conversations"
UNIQUE_INDEX = "user_session_unique"      # same name, keys and options as user_store.py (the bot)
MAX_ARG_CHARS = 2000
MAX_LIST = 50
MAX_REFS = 50

# Which tool arguments are the user's own words (stored verbatim as role "user"). Nothing else is.
USER_WORDS: dict[str, dict[str, str]] = {
    "search_posts": {"query": "search_query"},
    "record_feedback": {"text": "feedback_text"},
    "set_goal": {"text": "goal"},
    "save_insight": {"text": "insight"},
}


def sanitize(value: Any) -> Any:
    """Arguments as stored: secret keys hidden, strings cut at 2000, lists cut at 50, None dropped."""
    if isinstance(value, dict):
        clean = redact({k: v for k, v in value.items() if v is not None}, max_chars=MAX_ARG_CHARS)
        return {k: v if v == "[redacted]" else sanitize(v) for k, v in clean.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in list(value)[:MAX_LIST]]
    if isinstance(value, str) and len(value) > MAX_ARG_CHARS:
        return value[:MAX_ARG_CHARS] + "…"
    return value


def user_words(tool: str, args: dict) -> list[tuple[str, str]]:
    """[(via, text)] for the arguments of `tool` that are the user's words."""
    out = []
    for arg, via in USER_WORDS.get(tool, {}).items():
        text = args.get(arg)
        if isinstance(text, str) and text.strip():
            out.append((via, text.strip()[:MAX_ARG_CHARS]))
    return out


@dataclass
class CallRecord:
    """One tool call as logged."""
    tool: str
    args: dict
    ok: bool = True
    error: str | None = None
    count: int = 0
    duration_ms: int = 0
    post_ids: list[str] = field(default_factory=list)
    digest_ids: list[str] = field(default_factory=list)
    widget: bool = False
    message_id: ObjectId = field(default_factory=ObjectId)          # id of the role "tool" message
    word_ids: dict[str, ObjectId] = field(default_factory=dict)     # via -> id of its role "user" message

    def messages(self, now: datetime) -> list[dict]:
        tool_msg = {"id": self.message_id, "role": "tool", "created_at": now, "tool": self.tool,
                    "args": sanitize(self.args),
                    "result": {"ok": self.ok, "error": self.error and self.error[:300], "count": self.count,
                               "duration_ms": self.duration_ms, "post_ids": self.post_ids[:MAX_REFS],
                               "digest_ids": self.digest_ids[:MAX_REFS], "widget": self.widget}}
        words = [{"id": self.word_ids.setdefault(via, ObjectId()), "role": "user", "created_at": now,
                  "content": text, "via": via, "tool_message_id": self.message_id}
                 for via, text in user_words(self.tool, self.args)]
        return [tool_msg, *words]


class ConversationLog:
    def __init__(self, db: AsyncDatabase | None, audit: AuditLog, idle_minutes: int = 30,
                 max_messages: int = 500):
        self.col = db[COLLECTION] if db is not None else None
        self.audit = audit
        self.idle = timedelta(minutes=idle_minutes)
        self.max_messages = max_messages

    async def log(self, *, user_id: int | str | ObjectId, channel: str, mcp_session_id: str | None,
                  client: dict | None, record: CallRecord) -> ObjectId | None:
        """Appends the call to the user's session document. Returns the document id, or None on failure."""
        if self.col is None:
            return None
        try:
            return await self._append(user_id, channel, mcp_session_id, client, record)
        except Exception as e:             # noqa: BLE001 - logging must never break a tool
            logger.error(f"Conversation log failed: {type(e).__name__}: {e}")
            await self.audit.event("conversation_log_error", user_id=_public(user_id), channel=channel,
                                   mcp_session_id=mcp_session_id, tool=record.tool,
                                   error=f"{type(e).__name__}: {e}")
            return None

    async def _append(self, user_id, channel, mcp_session_id, client, record: CallRecord) -> ObjectId:
        now = datetime.now(timezone.utc)
        msgs = record.messages(now)
        room = self.max_messages - len(msgs)
        push = {"$push": {"messages": {"$each": msgs}}, "$inc": {"message_count": len(msgs)},
                "$set": {"updated_at": now}}
        # The current document of this session (or of this user without a session id), newest part first.
        current = await self.col.find_one(
            {"user_id": user_id, "channel": channel, "mcp_session_id": mcp_session_id,
             "session_key": {"$exists": True}},
            {"_id": 1, "part": 1, "updated_at": 1}, sort=[("updated_at", -1)])
        if current:
            done = await self.col.find_one_and_update(
                {"_id": current["_id"], "message_count": {"$lte": room}, "updated_at": {"$gte": now - self.idle}},
                push, projection={"_id": 1})
            if done:
                return done["_id"]
        part = current["part"] + 1 if current and mcp_session_id else 1
        key = f"{mcp_session_id}:{part}" if mcp_session_id else f"anon:{ObjectId()}"
        new = {"channel": channel, "client": client, "mcp_session_id": mcp_session_id, "part": part,
               "started_at": now}
        for attempt in (1, 2):
            try:
                doc = await self.col.find_one_and_update(
                    {"user_id": user_id, "session_key": key}, {**push, "$setOnInsert": new},
                    upsert=True, projection={"_id": 1}, return_document=True)
                return doc["_id"]
            except DuplicateKeyError:      # a parallel first call created it a moment ago
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")


def _public(user_id):
    return str(user_id) if isinstance(user_id, ObjectId) else user_id


async def ensure_conversation_indexes(db: AsyncDatabase) -> None:
    """Run from brain.scripts.setup_indexes. The bot's old unique {user_id} index is dropped separately."""
    col = db[COLLECTION]
    await col.create_index([("user_id", 1), ("session_key", 1)], unique=True, name=UNIQUE_INDEX)
    await col.create_index([("user_id", 1), ("updated_at", -1)], name="user_updated")
    await col.create_index([("mcp_session_id", 1)], name="mcp_session")
