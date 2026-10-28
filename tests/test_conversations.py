"""The conversation log: session rules, sanitizing, the user-words mapping, failure isolation, and coexistence
with the Telegram bot's documents in the same collection."""
import asyncio
import copy
import itertools
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from mcp import Client
from pymongo.errors import DuplicateKeyError

from brain.audit.conversations import (USER_WORDS, CallRecord, ConversationLog, ensure_conversation_indexes,
                                       sanitize, user_words)
from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import P_DRONES, USERS

_users = itertools.count(1)


@pytest.fixture
async def log(test_db):
    await ensure_conversation_indexes(test_db)
    return ConversationLog(test_db, AuditLog(test_db), idle_minutes=30, max_messages=10)


def uid():
    return ObjectId()


async def docs(db, user_id):
    return await db.conversations.find({"user_id": user_id}).sort("started_at", 1).to_list(None)


def call(tool="get_post", **args):
    return CallRecord(tool=tool, args=args or {"post_id": "x"})


# ---- sanitizing and user words ---------------------------------------------------

def test_sanitize():
    out = sanitize({"query": "q" * 2500, "api_key": "sk-123", "nested": {"Authorization": "Bearer x", "n": None},
                    "ids": list(range(80)), "none": None, "ok": 3})
    assert out["query"] == "q" * 2000 + "…" and out["api_key"] == "[redacted]"
    assert out["nested"] == {"Authorization": "[redacted]"} and len(out["ids"]) == 50
    assert "none" not in out and out["ok"] == 3


def test_user_words_mapping():
    assert USER_WORDS["search_posts"] == {"query": "search_query"}
    assert user_words("search_posts", {"query": "  Brave1 гранти ", "side": "ua"}) == [("search_query", "Brave1 гранти")]
    # Claude's own fields are never the user's words; empty text is skipped.
    assert user_words("record_feedback", {"about": "аналіз НРК", "text": "  ", "reasons": ["inaccurate"]}) == []
    assert user_words("record_feedback", {"text": "вже знав"}) == [("feedback_text", "вже знав")]
    assert user_words("get_post", {"post_id": "x"}) == []


def test_messages_link_user_words_to_the_tool_call():
    rec = call("set_goal", action="add", text="стежити за НРК")
    tool_msg, word = rec.messages(datetime.now(timezone.utc))
    assert tool_msg["role"] == "tool" and tool_msg["args"] == {"action": "add", "text": "стежити за НРК"}
    assert word == {**word, "role": "user", "content": "стежити за НРК", "via": "goal",
                    "tool_message_id": tool_msg["id"]}
    assert rec.word_ids["goal"] == word["id"]


# ---- session rules ------------------------------------------------------------------

async def test_same_session_one_document_new_session_new_document(log, test_db):
    u = uid()
    for _ in range(2):
        await log.log(user_id=u, channel="claude", mcp_session_id="s1", client={"name": "c", "version": "1"},
                      record=call())
    await log.log(user_id=u, channel="claude", mcp_session_id="s2", client=None, record=call())
    d = await docs(test_db, u)
    assert [(x["mcp_session_id"], x["part"], x["message_count"], len(x["messages"])) for x in d] == [
        ("s1", 1, 2, 2), ("s2", 1, 1, 1)]
    assert d[0]["session_key"] == "s1:1" and d[0]["client"] == {"name": "c", "version": "1"}


async def test_idle_session_continues_in_a_new_part(log, test_db):
    u = uid()
    await log.log(user_id=u, channel="claude", mcp_session_id="s1", client=None, record=call())
    await test_db.conversations.update_one({"user_id": u}, {"$set": {
        "updated_at": datetime.now(timezone.utc) - timedelta(minutes=31)}})
    await log.log(user_id=u, channel="claude", mcp_session_id="s1", client=None, record=call())
    assert [(x["part"], x["session_key"]) for x in await docs(test_db, u)] == [(1, "s1:1"), (2, "s1:2")]


async def test_without_session_id_the_idle_window_decides(log, test_db):
    u = uid()
    await log.log(user_id=u, channel="claude", mcp_session_id=None, client=None, record=call())
    await log.log(user_id=u, channel="claude", mcp_session_id=None, client=None, record=call())
    assert len(await docs(test_db, u)) == 1
    await test_db.conversations.update_many({"user_id": u}, {"$set": {
        "updated_at": datetime.now(timezone.utc) - timedelta(minutes=45)}})
    await log.log(user_id=u, channel="claude", mcp_session_id=None, client=None, record=call())
    d = await docs(test_db, u)
    assert len(d) == 2 and all(x["session_key"].startswith("anon:") and x["part"] == 1 for x in d)


async def test_size_cap_starts_the_next_part(log, test_db):
    u = uid()
    for _ in range(4):                                     # 4 × (tool + user words) = 8 messages
        await log.log(user_id=u, channel="claude", mcp_session_id="s1", client=None,
                      record=call("search_posts", query="дрони"))
    await log.log(user_id=u, channel="claude", mcp_session_id="s1", client=None,
                  record=call("search_posts", query="РЕБ"))            # 8 + 2 = 10: still fits
    await log.log(user_id=u, channel="claude", mcp_session_id="s1", client=None,
                  record=call("search_posts", query="Brave1"))         # would be 12 > 10: next part
    d = await docs(test_db, u)
    assert [(x["part"], x["message_count"]) for x in d] == [(1, 10), (2, 2)]
    assert all(x["message_count"] == len(x["messages"]) <= 10 for x in d)


async def test_parallel_first_calls_share_one_document(log, test_db):
    u = uid()
    await asyncio.gather(*(log.log(user_id=u, channel="claude", mcp_session_id="p1", client=None, record=call())
                           for _ in range(5)))
    [d] = await docs(test_db, u)
    assert d["message_count"] == 5


async def test_channels_are_kept_apart(log, test_db):
    u = uid()
    await log.log(user_id=u, channel="claude", mcp_session_id=None, client=None, record=call())
    await log.log(user_id=u, channel="chatgpt", mcp_session_id=None, client=None, record=call())
    assert sorted(x["channel"] for x in await docs(test_db, u)) == ["chatgpt", "claude"]


# ---- coexistence with the bot's Telegram documents ---------------------------------------

def test_the_bots_index_call_matches_brains():
    """user_store.ensure_indexes runs at every bot start; a different spec under the same name would fail."""
    import inspect

    import user_store
    src = inspect.getsource(user_store.ensure_indexes)
    assert 'create_index([("user_id", 1), ("session_key", 1)], unique=True,' in src
    assert 'name="user_session_unique"' in src and 'create_index([("user_id", 1)], unique=True)' not in src


async def test_telegram_documents_stay_one_per_user(log, test_db):
    tg = 900_000_000 + next(_users)
    await test_db.conversations.insert_one({"user_id": tg, "messages": [], "created_at": datetime.now(timezone.utc)})
    with pytest.raises(DuplicateKeyError):          # a second Telegram document for the same user is refused
        await test_db.conversations.insert_one({"user_id": tg, "messages": []})
    # A Telegram id used in dev mode gets its own session document; the Telegram one is untouched.
    await log.log(user_id=tg, channel="dev", mcp_session_id=None, client=None, record=call())
    d = await docs(test_db, tg)
    assert len(d) == 2 and sum("session_key" in x for x in d) == 1
    assert next(x for x in d if "session_key" not in x)["messages"] == []


# ---- failures never break a tool; security events only in audit_log ----------------------

class BrokenCollection:
    def __getattr__(self, name):
        async def fail(*a, **k):
            raise RuntimeError("database down")
        return fail


async def test_a_logging_failure_is_reported_and_returns_none(test_db):
    log = ConversationLog(test_db, AuditLog(test_db))
    log.col = BrokenCollection()
    u = uid()
    assert await log.log(user_id=u, channel="claude", mcp_session_id="s9", client=None, record=call()) is None
    event = await test_db.audit_log.find_one({"event": "conversation_log_error", "user_id": str(u)})
    assert event["mcp_session_id"] == "s9" and event["details"]["tool"] == "get_post"
    assert "database down" in event["details"]["error"]


async def test_tools_still_work_when_logging_fails(test_db):
    user = 800_100_000 + next(_users)
    await test_db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": user})
    svc = Services(settings=make_settings(DEV_USER_ID=user), repos=Repos(test_db), audit=AuditLog(test_db),
                   token=lambda: None)
    svc.conversations.col = BrokenCollection()
    async with Client(create_server(svc)) as c:
        r = await c.call_tool("get_post", {"post_id": str(P_DRONES)})
    assert not r.is_error and r.structured_content["post_id"] == str(P_DRONES)
    assert await test_db.audit_log.find_one({"event": "conversation_log_error", "user_id": user})


async def test_security_events_only(test_db):
    audit = AuditLog(test_db)
    with pytest.raises(ValueError):
        await audit.event("tool_call", user_id=1)          # tool activity is not a security event
    await audit.event("access_denied", tool="get_post", reason="Not signed in", password="x")
    e = await test_db.audit_log.find_one({"event": "access_denied"}, sort=[("at", -1)])
    assert set(e) == {"_id", "at", "event", "user_id", "channel", "mcp_session_id", "details"}
    assert e["details"] == {"tool": "get_post", "reason": "Not signed in", "password": "[redacted]"}


async def test_successful_logging_writes_nothing_to_audit_log(log, test_db):
    before = await test_db.audit_log.count_documents({})
    await log.log(user_id=uid(), channel="claude", mcp_session_id=None, client=None, record=call())
    assert await test_db.audit_log.count_documents({}) == before
