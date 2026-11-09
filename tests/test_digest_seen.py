"""Which of today's digest items the user was shown (users.digest_seen), in any channel."""
import copy
import itertools
from datetime import datetime, timedelta

import httpx2
import pytest
from bson import ObjectId
from mcp import Client

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import digests, sensitivity
from brain.domain.identity import UserContext
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from brain.web.app import create_app
from tests.conftest import make_settings
from tests.fixtures.sample_data import DIGESTS, USERS

_ids = itertools.count(975_000_001)


@pytest.fixture
async def world(mongo_uri, test_db):
    db = test_db.client[f"{test_db.name}_s{next(_ids)}"]
    uid = next(_ids)
    await db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": uid})
    yield db, uid
    await test_db.client.drop_database(db.name)


def ctx(uid, channel="claude"):
    return UserContext(uid, "cloud", channel, sensitivity.max_for("cloud"))


def kyiv(hour, days=0):
    return datetime.now(digests.KYIV).replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)


async def add_digest(db, side, at):
    d = copy.deepcopy(next(x for x in DIGESTS if x["side"] == side))
    d.update(_id=ObjectId(), created_at=at.astimezone(digests.KYIV))
    await db.digests.insert_one(d)
    return str(d["_id"])


def keys_of(res):
    return {digests.seen_key(v.digest.id, v.number(i)) for v in res.digests for _, items in v.sections for i in items}


async def seen(db, uid):
    return (await db.users.find_one({"_id": uid})).get("digest_seen")


async def test_todays_items_are_marked_once(world):
    db, uid = world
    repos = Repos(db)
    await add_digest(db, "ua", kyiv(0))
    res = await digests.get_digests(repos, ctx(uid), "both", kyiv(0).date().isoformat())
    assert await digests.mark_seen(repos, ctx(uid), res) > 0
    await digests.mark_seen(repos, ctx(uid), res)
    state = await seen(db, uid)
    assert state["date"] == kyiv(0).date().isoformat()
    assert sorted(state["items"]) == sorted(keys_of(res))              # no duplicates
    assert await repos.users.digest_seen(uid, state["date"]) == keys_of(res)


async def test_a_new_day_replaces_the_list(world):
    db, uid = world
    repos = Repos(db)
    await db.users.update_one({"_id": uid}, {"$set": {"digest_seen": {"date": "2020-01-01", "items": ["old:1"]}}})
    await add_digest(db, "ua", kyiv(0))
    res = await digests.get_digests(repos, ctx(uid), "both", kyiv(0).date().isoformat())
    await digests.mark_seen(repos, ctx(uid), res)
    state = await seen(db, uid)
    assert state["date"] == kyiv(0).date().isoformat() and "old:1" not in state["items"] and state["items"]


async def test_at_most_the_cap(world):
    db, uid = world
    repos = Repos(db)
    day = kyiv(0).date().isoformat()
    await repos.users.mark_digest_seen(uid, day, [f"d:{n}" for n in range(digests.SEEN_MAX - 1)], digests.SEEN_MAX)
    await repos.users.mark_digest_seen(uid, day, ["d:0", "x:1", "x:2"], digests.SEEN_MAX)
    items = (await seen(db, uid))["items"]
    assert len(items) == digests.SEEN_MAX and items[-1] == "x:1"


async def test_a_past_dates_digest_marks_nothing(world):
    db, uid = world
    repos = Repos(db)
    await add_digest(db, "ua", kyiv(8, days=-1))
    res = await digests.get_digests(repos, ctx(uid), "both", kyiv(8, days=-1).date().isoformat())
    assert res.digests and await digests.mark_seen(repos, ctx(uid), res) == 0
    assert await seen(db, uid) is None


async def test_claude_and_the_web_both_mark(world):
    db, uid = world
    ua = await add_digest(db, "ua", kyiv(0))
    await add_digest(db, "ru", kyiv(0))
    settings = make_settings(DEV_USER_ID=uid, TRUST_LEVEL="cloud")
    svc = Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None)
    async with Client(create_server(svc)) as c:
        morning = await c.call_tool("get_morning_digest", {"user_message": "[schedule] ранкове зведення"})
        assert morning.structured_content["status"] == "new"
    after_claude = set((await seen(db, uid))["items"])
    assert after_claude and all(k.split(":")[0] in {d["digest_id"] for d in morning.structured_content["digests"]}
                                for k in after_claude)
    await db.users.update_one({"_id": uid}, {"$unset": {"digest_seen": 1}})
    app = create_app(settings, svc)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://app.example.com") as w:
        r = await w.get("/api/digest", params={"side": "ua"})
        assert r.status_code == 200
    after_web = (await seen(db, uid))["items"]
    assert after_web and all(k.startswith(f"{ua}:") for k in after_web)


async def test_a_failure_to_mark_never_fails_the_call(world, monkeypatch):
    db, uid = world
    await add_digest(db, "ua", kyiv(0))

    async def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("brain.db.repositories.UsersRepo.mark_digest_seen", broken)
    svc = Services(settings=make_settings(DEV_USER_ID=uid, TRUST_LEVEL="cloud"), repos=Repos(db),
                   audit=AuditLog(db), token=lambda: None)
    async with Client(create_server(svc)) as c:
        r = await c.call_tool("get_digest", {"user_message": "дайджест", "date": kyiv(0).date().isoformat()})
        assert not r.is_error and r.structured_content["digests"]
