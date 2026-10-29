"""The morning delivery: today's digest once per user, complete (both sides) before noon, and the tool that a
scheduled task calls."""
import asyncio
import copy
import itertools
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from mcp import Client

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import digests, sensitivity
from brain.domain.identity import UserContext
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import DIGESTS, USERS

_ids = itertools.count(970_000_001)


@pytest.fixture
async def world(mongo_uri, test_db):
    """A database of its own: today's digests are created per test."""
    db = test_db.client[f"{test_db.name}_m{next(_ids)}"]
    uid = next(_ids)
    await db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": uid})
    yield db, uid
    await test_db.client.drop_database(db.name)


def ctx(uid):
    return UserContext(uid, "cloud", "claude", sensitivity.max_for("cloud"))


async def add_digest(db, side, at):
    d = copy.deepcopy(next(x for x in DIGESTS if x["side"] == side))
    d.update(_id=ObjectId(), created_at=at.astimezone(digests.KYIV))
    await db.digests.insert_one(d)
    return d["_id"]


def kyiv(hour, minute=0):
    return datetime.now(digests.KYIV).replace(hour=hour, minute=minute, second=0, microsecond=0)


async def test_nothing_today_is_not_ready(world):
    db, uid = world
    await add_digest(db, "ua", kyiv(8) - timedelta(days=1))                 # yesterday's
    assert (await digests.morning_digest(Repos(db), ctx(uid), now=kyiv(9))).status == "not_ready"


async def test_waits_for_both_sides_before_noon_then_delivers_once(world):
    db, uid = world
    repos = Repos(db)
    await add_digest(db, "ua", kyiv(8, 40))
    assert (await digests.morning_digest(repos, ctx(uid), now=kyiv(9))).status == "not_ready"
    await add_digest(db, "ru", kyiv(8, 41))
    first = await digests.morning_digest(repos, ctx(uid), now=kyiv(9, 30))
    assert first.status == "new" and len(first.result.digests) == 2
    assert (await digests.morning_digest(repos, ctx(uid), now=kyiv(10))).status == "already_sent"
    state = (await db.users.find_one({"_id": uid}))["morning_digest"]
    assert state["date"] == kyiv(10).date().isoformat() and state["channel"] == "claude"


async def test_after_noon_one_side_is_enough(world):
    db, uid = world
    await add_digest(db, "ua", kyiv(8, 40))
    assert (await digests.morning_digest(Repos(db), ctx(uid), now=kyiv(12, 5))).status == "new"


async def test_parallel_runs_deliver_once(world):
    db, uid = world
    await add_digest(db, "ua", kyiv(8, 40))
    await add_digest(db, "ru", kyiv(8, 41))
    results = await asyncio.gather(*(digests.morning_digest(Repos(db), ctx(uid), now=kyiv(9, 30))
                                     for _ in range(4)))
    assert sorted(r.status for r in results) == ["already_sent"] * 3 + ["new"]


async def test_a_new_digest_later_is_new_again(world):
    db, uid = world
    repos = Repos(db)
    await add_digest(db, "ua", kyiv(8, 40))
    await add_digest(db, "ru", kyiv(8, 41))
    assert (await digests.morning_digest(repos, ctx(uid), now=kyiv(9))).status == "new"
    await add_digest(db, "ua", kyiv(9, 30))                                  # a rebuilt digest
    assert (await digests.morning_digest(repos, ctx(uid), now=kyiv(10))).status == "new"


async def test_the_tool(world):
    db, uid = world
    await add_digest(db, "ua", kyiv(0, 1))
    await add_digest(db, "ru", kyiv(0, 2))
    svc = Services(settings=make_settings(DEV_USER_ID=uid, TRUST_LEVEL="cloud"), repos=Repos(db),
                   audit=AuditLog(db), token=lambda: None)
    async with Client(create_server(svc)) as c:
        tool = {t.name: t for t in (await c.list_tools()).tools}["get_morning_digest"]
        assert tool.meta["ui"]["resourceUri"].startswith("ui://news-brain/digest-")   # the same cards
        first = (await c.call_tool("get_morning_digest", {"user_message": "[schedule] ранкове зведення"}))
        assert first.structured_content["status"] == "new" and first.structured_content["digests"]
        again = (await c.call_tool("get_morning_digest", {"user_message": "[schedule] ранкове зведення"}))
        assert again.structured_content["status"] == "already_sent"
        assert again.structured_content["digests"] == [] and "Don't present" in again.structured_content["note"]
    [conv] = await db.conversations.find({"user_id": uid}).to_list(None)
    vias = [m["via"] for m in conv["messages"] if m["role"] == "user"]
    assert vias == ["schedule", "schedule"]         # two scheduled runs, not the user's words
