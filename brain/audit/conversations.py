"""MCP activity per user and session, in the `conversations` collection (docs/CONVERSATIONS_PLAN.md §1).

The server never sees the chat itself, only tool calls. Each call becomes a `role: "tool"` message (sanitized
arguments, a result summary with references only). Arguments that are the user's own words (USER_WORDS) also
become verbatim `role: "user"` messages: every tool carries the user's latest chat message (`user_message`,
stored once per message even when several tools carry it), plus a few tool-specific arguments. The client model's one-line
summary of its previous reply (MODEL_WORDS) becomes a `role: "assistant"` message, marked as model-written.
Nothing reads these documents back.

Documents sit next to the Telegram bot's (one per Telegram user, no `session_key`). A unique index on
(user_id, session_key) keeps the bot's one-document-per-user rule and lets MCP sessions have many.
Logging never breaks a tool: every failure is caught and reported to audit_log.
"""
from __future__ import annotations

import hashlib
import logging
import re
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
MAX_MESSAGE_CHARS = 8000          # a chat message passed by log_turn
MAX_LIST = 50
MAX_REFS = 50

# Which tool arguments are the user's own words (stored verbatim as role "user"). Nothing else is.
USER_WORDS: dict[str, dict[str, str]] = {
    "search_posts": {"query": "search_query"},
    "record_feedback": {"text": "feedback_text"},
    "set_goal": {"text": "goal"},
    "save_insight": {"text": "insight"},
}
CHAT_ARG = "user_message"         # on every tool: the user's latest chat message, verbatim
WIDGET_PREFIX = "[widget]"        # user_message sent by a widget button, not typed by the user
SCHEDULE_PREFIX = "[schedule]"    # user_message of a scheduled task (e.g. the morning digest), not typed
# Text the client model wrote about its own replies (stored as role "assistant", never as the user's words).
MODEL_WORDS: dict[str, dict[str, str]] = {
    "log_turn": {"previous_reply": "reply_summary"},
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


def _texts(mapping: dict[str, dict[str, str]], tool: str, args: dict) -> list[tuple[str, str]]:
    out = []
    pairs = list(mapping.get(tool, {}).items())
    if mapping is USER_WORDS:
        text = args.get(CHAT_ARG)
        via = "chat"
        if isinstance(text, str):
            via = "widget" if text.startswith(WIDGET_PREFIX) else "schedule" if text.startswith(SCHEDULE_PREFIX) \
                else "chat"
        pairs.insert(0, (CHAT_ARG, via))
    for arg, via in pairs:
        text = args.get(arg)
        if isinstance(text, str) and text.strip():
            out.append((via, text.strip()[:MAX_MESSAGE_CHARS if via == "chat" else MAX_ARG_CHARS]))
    return out


def user_words(tool: str, args: dict) -> list[tuple[str, str]]:
    """[(via, text)] for the arguments of `tool` that are the user's words."""
    return _texts(USER_WORDS, tool, args)


def model_words(tool: str, args: dict) -> list[tuple[str, str]]:
    """[(via, text)] for the arguments of `tool` that the client model wrote about its own replies."""
    return _texts(MODEL_WORDS, tool, args)


# Arguments stored only as their own messages (long chat text is not repeated inside the tool message).
MOVED_ARGS = {CHAT_ARG, "previous_reply"}


def chat_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:24]


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

    def chat(self) -> str | None:
        """The user's chat message this call carries (typed, not a widget button), if any."""
        return next((text for via, text in user_words(self.tool, self.args) if via == "chat"), None)

    def messages(self, now: datetime, skip_chat: bool = False) -> list[dict]:
        """skip_chat: the chat message is already in the document (an earlier tool of the same turn)."""
        tool_msg = {"id": self.message_id, "role": "tool", "created_at": now, "tool": self.tool,
                    "args": sanitize({k: v for k, v in self.args.items() if k not in MOVED_ARGS}),
                    "result": {"ok": self.ok, "error": self.error and self.error[:300], "count": self.count,
                               "duration_ms": self.duration_ms, "post_ids": self.post_ids[:MAX_REFS],
                               "digest_ids": self.digest_ids[:MAX_REFS], "widget": self.widget}}
        # The model's summary of its previous reply comes first: it was said before this user message.
        replies = [{"id": ObjectId(), "role": "assistant", "created_at": now, "content": text, "via": via,
                    "written_by": "model", "tool_message_id": self.message_id}
                   for via, text in model_words(self.tool, self.args)]
        words = [{"id": self.word_ids.setdefault(via, ObjectId()), "role": "user", "created_at": now,
                  "content": text, "via": via, "tool_message_id": self.message_id}
                 for via, text in user_words(self.tool, self.args) if not (skip_chat and via == "chat")]
        return [tool_msg, *replies, *words]


@dataclass
class Said:
    """A message of our own chat (brain/web/chat.py), logged as it is: the user's words, or the model's whole
    reply with its model, tokens and timing (`meta`). The chat sees the messages itself, so nothing is guessed."""
    role: str                         # "user" | "assistant"
    content: str
    meta: dict = field(default_factory=dict)
    tool: str | None = None           # for log()'s error report: not a tool call
    new_message: bool = True          # a user message is always a new one, even with the same words as the last
    message_id: ObjectId = field(default_factory=ObjectId)
    word_ids: dict[str, ObjectId] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.role == "user":       # the tools of this turn carry the same text: they link to this message
            self.word_ids["chat"] = self.message_id

    def chat(self) -> str | None:
        return self.content if self.role == "user" else None

    def messages(self, now: datetime, skip_chat: bool = False) -> list[dict]:
        msg = {"id": self.message_id, "role": self.role, "created_at": now,
               "content": self.content[:MAX_MESSAGE_CHARS * 4], "via": "chat", **self.meta}
        if self.role == "assistant":
            msg["written_by"] = "model"
        return [msg]


class ConversationLog:
    def __init__(self, db: AsyncDatabase | None, audit: AuditLog, idle_minutes: int = 30,
                 max_messages: int = 500):
        self.col = db[COLLECTION] if db is not None else None
        self.audit = audit
        self.idle = timedelta(minutes=idle_minutes)
        self.max_messages = max_messages

    async def log(self, *, user_id: int | str | ObjectId, channel: str, mcp_session_id: str | None,
                  client: dict | None, record: CallRecord | Said) -> ObjectId | None:
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

    async def active(self, user_id, channel: str, mcp_session_id: str | None) -> bool:
        """Whether this user already has a conversation in progress (activity within the idle window)."""
        if self.col is None:
            return False
        try:
            return bool(await self.col.find_one(
                {"user_id": user_id, "channel": channel, "mcp_session_id": mcp_session_id,
                 "session_key": {"$exists": True},
                 "updated_at": {"$gte": datetime.now(timezone.utc) - self.idle}}, {"_id": 1}))
        except Exception:                  # noqa: BLE001 - only decides whether to send the profile again
            return False

    async def _append(self, user_id, channel, mcp_session_id, client, record: CallRecord) -> ObjectId:
        now = datetime.now(timezone.utc)
        # The current document of this session (or of this user without a session id), newest part first.
        current = await self.col.find_one(
            {"user_id": user_id, "channel": channel, "mcp_session_id": mcp_session_id,
             "session_key": {"$exists": True}},
            {"_id": 1, "part": 1, "updated_at": 1, "last_chat": 1}, sort=[("updated_at", -1)])
        chat = record.chat()
        h = chat_hash(chat) if chat else None
        last = (current or {}).get("last_chat") or {}
        repeat = bool(h and not getattr(record, "new_message", False) and last.get("hash") == h and current
                      and current["updated_at"] >= now - self.idle)
        if repeat:                          # another tool for the same message: link to the stored one
            record.word_ids["chat"] = last["id"]
        msgs = record.messages(now, skip_chat=repeat)
        room = self.max_messages - len(msgs)
        updates: dict = {"updated_at": now}
        if h and not repeat:
            updates["last_chat"] = {"hash": h, "id": record.word_ids["chat"]}
        push = {"$push": {"messages": {"$each": msgs}}, "$inc": {"message_count": len(msgs)}, "$set": updates}
        if current:
            done = await self.col.find_one_and_update(
                {"_id": current["_id"], "message_count": {"$lte": room}, "updated_at": {"$gte": now - self.idle}},
                push, projection={"_id": 1})
            if done:
                return done["_id"]
        if repeat:                          # a new part starts: it gets the chat message itself
            msgs = record.messages(now)
            updates["last_chat"] = {"hash": h, "id": record.word_ids["chat"]}
            push = {"$push": {"messages": {"$each": msgs}}, "$inc": {"message_count": len(msgs)},
                    "$set": updates}
        part = current["part"] + 1 if current and mcp_session_id else 1
        # Deterministic keys: calls racing to start the next document compute the same key, and the unique
        # (user_id, session_key) index makes them share it. Without a session id, the key names the document
        # it follows.
        if mcp_session_id:
            key = f"{mcp_session_id}:{part}"
        else:
            key = f"anon:{channel}:after:{current['_id']}" if current else f"anon:{channel}:first"
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


    # ---- reading our own chat back (brain/web/chat.py): only its user and assistant messages ----

    async def thread(self, user_id, channel: str, mcp_session_id: str) -> list[dict]:
        """The session's chat messages, oldest first, across its parts."""
        if self.col is None:
            return []
        out = []
        async for doc in self.col.find({"user_id": user_id, "channel": channel, "mcp_session_id": mcp_session_id,
                                        "session_key": {"$exists": True}, "deleted_at": None},
                                       {"messages": 1}).sort("part", 1):
            out += [m for m in doc.get("messages", []) if m.get("role") in ("user", "assistant")
                    and m.get("via") == "chat"]
        return out

    async def sessions(self, user_id, channel: str, prefix: str, limit: int = 30) -> list[dict]:
        """The user's sessions whose id starts with `prefix`, newest first: {session, started_at, updated_at,
        first} (the first user message)."""
        if self.col is None:
            return []
        found: dict[str, dict] = {}
        cursor = self.col.find(
            {"user_id": user_id, "channel": channel, "mcp_session_id": {"$regex": f"^{re.escape(prefix)}"},
             "deleted_at": None},
            {"mcp_session_id": 1, "part": 1, "started_at": 1, "updated_at": 1,
             "messages": {"$elemMatch": {"role": "user", "via": "chat"}}}).sort("updated_at", -1).limit(limit * 4)
        async for doc in cursor:
            first = (doc.get("messages") or [{}])[0].get("content")
            s = found.setdefault(doc["mcp_session_id"], {"session": doc["mcp_session_id"],
                                                        "updated_at": doc["updated_at"], "part": doc.get("part", 1)})
            if doc.get("part", 1) <= s["part"]:
                s.update(part=doc.get("part", 1), started_at=doc.get("started_at"), first=first or s.get("first"))
        return list(found.values())[:limit]

    async def rate(self, user_id, channel: str, mcp_session_id: str, message_id: ObjectId, rating: dict) -> bool:
        """Puts the user's rating on one of the session's replies (the latest rating replaces an earlier one)."""
        if self.col is None:
            return False
        r = await self.col.update_one(
            {"user_id": user_id, "channel": channel, "mcp_session_id": mcp_session_id, "deleted_at": None,
             "messages": {"$elemMatch": {"id": message_id, "role": "assistant"}}},
            {"$set": {"messages.$.rating": rating}})
        return r.modified_count == 1

    async def delete(self, user_id, channel: str, mcp_session_id: str) -> bool:
        """Hides the session from its user (every part gets `deleted_at`); nothing is removed: the learner, the
        console and the daily budget still count it. False if the user has no such session left."""
        if self.col is None:
            return False
        r = await self.col.update_many(
            {"user_id": user_id, "channel": channel, "mcp_session_id": mcp_session_id,
             "session_key": {"$exists": True}, "deleted_at": None},
            {"$set": {"deleted_at": datetime.now(timezone.utc)}})
        return r.modified_count > 0

    async def deleted(self, user_id, channel: str, mcp_session_id: str) -> bool:
        """Whether the user deleted this session: it can't be continued."""
        if self.col is None:
            return False
        return bool(await self.col.find_one(
            {"user_id": user_id, "channel": channel, "mcp_session_id": mcp_session_id,
             "deleted_at": {"$ne": None}}, {"_id": 1}))

    async def assistant_tokens(self, user_id, channel: str, since: datetime) -> int:
        """Tokens of the replies logged since `since` (input + output, the web research's too), for a daily
        budget."""
        if self.col is None:
            return 0
        rows = await (await self.col.aggregate([
            {"$match": {"user_id": user_id, "channel": channel, "updated_at": {"$gte": since}}},
            {"$unwind": "$messages"},
            {"$match": {"messages.role": "assistant", "messages.via": "chat", "messages.created_at": {"$gte": since}}},
            {"$group": {"_id": None, "n": {"$sum": {"$add": [
                {"$ifNull": [f"$messages.{k}", 0]} for k in ("input_tokens", "output_tokens", "research_input_tokens",
                                                              "research_output_tokens")]}}}},
        ])).to_list(1)
        return rows[0]["n"] if rows else 0


def _public(user_id):
    return str(user_id) if isinstance(user_id, ObjectId) else user_id


async def ensure_conversation_indexes(db: AsyncDatabase) -> None:
    """Run from brain.scripts.setup_indexes. The bot's old unique {user_id} index is dropped separately."""
    col = db[COLLECTION]
    await col.create_index([("user_id", 1), ("session_key", 1)], unique=True, name=UNIQUE_INDEX)
    await col.create_index([("user_id", 1), ("updated_at", -1)], name="user_updated")
    await col.create_index([("mcp_session_id", 1)], name="mcp_session")
