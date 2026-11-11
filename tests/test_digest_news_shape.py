"""The pipeline's digest document since 2026-11-11: one per run, side "news", every side under sides.<side>. Each
side reads as a digest of its own, id "<_id>-<side>"; the old one-document-per-side digests read as before."""
from __future__ import annotations

import copy
import itertools
from datetime import datetime, timedelta, timezone

import httpx2
import pytest
from bson import ObjectId

from brain.agents.learner_profile import _rated_texts
from brain.agents.scout_web import in_our_news
from brain.audit.log import AuditLog
from brain.db.repositories import Repos, news_sides
from brain.domain import digests, sensitivity
from brain.domain.feedback import record_feedback
from brain.domain.identity import UserContext
from brain.tools.base import Services
from brain.web.app import create_app
from tests.conftest import make_settings
from tests.fixtures.sample_data import POSTS, USERS

_ids = itertools.count(977_000_001)


def section(category, *texts, start=1):
    return {"category": category, "title": category.title(), "items": [
        {"n": start + i, "text": t, "source_urls": [f"https://example.com/{category}/{i}"], "source_post_ids": []}
        for i, t in enumerate(texts)]}


def news_doc(at, sides=("ua", "ru", "world"), trigger="scheduled"):
    parts = {"ua": {"period_start": "x", "posts_considered": 120, "items_count": 2, "parts_html": ["…"],
                    "sections": [section("front", "Бої під Покровськом."), section("tech", "Дрон-перехоплювач.", start=2)]},
             "ru": {"period_start": "x", "items_count": 1, "sections": [section("front", "Заява МО РФ.")]},
             "world": {"period_start": "x", "items_count": 2,
                       "sections": [section("world_tech", "Anduril raised $2.5B.", "NATO drone wall.")]}}
    return {"_id": ObjectId(), "side": "news", "period": "day", "trigger": trigger, "created_at": at,
            "period_end": "y", "model": "gemma4:e4b", "sides_done": list(sides),
            "sides": {s: parts[s] for s in sides}}


def kyiv(hour, days=0):
    return datetime.now(digests.KYIV).replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)


def ctx(uid):
    return UserContext(uid, "cloud", "web", sensitivity.max_for("cloud"))


@pytest.fixture
async def world(test_db):
    db = test_db.client[f"{test_db.name}_n{next(_ids)}"]
    uid = next(_ids)
    await db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": uid, "goals": [], "feedback": [], "interests": {}})
    await db.posts.insert_many(copy.deepcopy(POSTS))
    yield db, uid
    await test_db.client.drop_database(db.name)


def web(db, uid):
    settings = make_settings(DEV_USER_ID=uid, TRUST_LEVEL="cloud")
    app = create_app(settings, Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None))
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://app.example.com",
                              headers={"x-brain": "1"})


def test_news_sides_splits_a_run_and_leaves_old_documents_alone():
    doc = news_doc(kyiv(6))
    parts = news_sides(doc)
    assert [p["side"] for p in parts] == ["ua", "ru", "world"]
    assert [p["_id"] for p in parts] == [f"{doc['_id']}-{s}" for s in ("ua", "ru", "world")]
    assert all(p["created_at"] == doc["created_at"] and p["trigger"] == "scheduled" for p in parts)
    assert parts[0]["posts_considered"] == 120 and "sides" not in parts[0] and "sides_done" not in parts[0]
    old = {"_id": ObjectId(), "side": "ua", "sections": []}
    assert news_sides(old) == [old]


async def test_the_web_digest_shows_every_side_of_a_run(world):
    db, uid = world
    doc = news_doc(kyiv(6))
    await db.digests.insert_one(doc)
    async with web(db, uid) as w:
        body = (await w.get("/api/digest")).json()
        assert [d["side"] for d in body["digests"]] == ["ua", "ru", "world"]
        assert [d["digest_id"] for d in body["digests"]] == [f"{doc['_id']}-{s}" for s in ("ua", "ru", "world")]
        assert body["missing_sides"] == []
        ru = body["digests"][1]
        assert ru["side_note"] and all(i["is_russian_source_claim"] for s in ru["sections"] for i in s["items"])
        wd = body["digests"][2]
        assert wd["side_note"] is None and [s["category"] for s in wd["sections"]] == ["world_tech"]
        assert not any(i["is_russian_source_claim"] for s in wd["sections"] for i in s["items"])
        only = (await w.get("/api/digest", params={"side": "world"})).json()
        assert [d["side"] for d in only["digests"]] == ["world"]


async def test_world_is_never_missing_but_ua_and_ru_are(world):
    db, uid = world
    await db.digests.insert_one(news_doc(kyiv(6), sides=("ua",)))
    res = await digests.get_digests(Repos(db), ctx(uid), "both", kyiv(6).date().isoformat())
    assert [v.side for v in res.digests] == ["ua"] and res.missing_sides == ["ru"]
    res = await digests.get_digests(Repos(db), ctx(uid), "world", kyiv(6).date().isoformat())
    assert res.digests == [] and res.missing_sides == ["world"]


async def test_old_and_new_documents_mix_newest_first(world):
    db, uid = world
    old_ua = {"_id": ObjectId(), "side": "ua", "period": "day", "trigger": "scheduled", "created_at": kyiv(5, -1),
              "sections": [section("front", "Вчорашнє.")]}
    await db.digests.insert_one(old_ua)
    repos = Repos(db)
    assert (await repos.digests.latest("ua")).id == str(old_ua["_id"])
    doc = news_doc(kyiv(6))
    await db.digests.insert_one(doc)
    assert (await repos.digests.latest("ua")).id == f"{doc['_id']}-ua"
    assert (await repos.digests.latest("world")).sections[0].category == "world_tech"
    # An on-demand run counts only when there is no scheduled one
    await db.digests.insert_one(news_doc(kyiv(7), trigger="on_demand"))
    assert (await repos.digests.latest("ua")).id == f"{doc['_id']}-ua"
    found = await repos.digests.in_range(kyiv(0, -1), kyiv(0, 1))
    assert [d.side for d in found] == ["ua", "ua", "ru", "world", "ua", "ru", "world"]


async def test_get_by_side_id(world):
    db, _ = world
    doc = news_doc(kyiv(6))
    await db.digests.insert_one(doc)
    repos = Repos(db)
    got = await repos.digests.get(f"{doc['_id']}-world")
    assert got.side == "world" and got.sections[0].items[1].text == "NATO drone wall."
    assert await repos.digests.get(str(doc["_id"])) is None              # the run itself is no digest
    assert await repos.digests.get(f"{doc['_id']}-interests") is None
    assert await repos.digests.get(f"{ObjectId()}-ua") is None
    assert await repos.digests.get("nonsense-ua") is None


async def test_rating_a_world_item_and_seeing_it_back(world):
    db, uid = world
    doc = news_doc(kyiv(6))
    await db.digests.insert_one(doc)
    did = f"{doc['_id']}-world"
    async with web(db, uid) as w:
        r = await w.post("/api/feedback", json={"reaction": "useful", "target_type": "digest_item",
                                                 "digest_id": did, "item_n": 2})
        assert r.status_code == 200, r.text
        body = (await w.get("/api/digest", params={"side": "world"})).json()
    items = {i["n"]: i.get("my_reaction") for s in body["digests"][0]["sections"] for i in s["items"]}
    assert items == {1: None, 2: "useful"}
    [fb] = (await db.users.find_one({"_id": uid}))["feedback"]
    assert fb["digest_id"] == did and fb["side"] == "world" and fb["category"] == "world_tech"
    # The same item in ua, numbered 2 as well, is another item
    fb_ua = await record_feedback(Repos(db), ctx(uid), mcp_channels=["web"], target_type="digest_item",
                                  reaction="not_useful", digest_id=f"{doc['_id']}-ua", item_n=2)
    assert fb_ua
    saved = (await db.users.find_one({"_id": uid}))["feedback"]
    assert [(f["digest_id"], f["reaction"]) for f in saved] == [(did, "useful"), (f"{doc['_id']}-ua", "not_useful")]
    # The learner finds what each rating was about, for ratings without a copy
    texts = await _rated_texts(db, [{**f, "about": None, "post_id": None} for f in saved])
    assert texts == {0: "NATO drone wall.", 1: "Дрон-перехоплювач."}


async def test_the_chat_takes_a_side_item_ref(world):
    from brain.web.app import ItemRef
    assert ItemRef(digest_id=f"{ObjectId()}-world", item_n=1)
    assert ItemRef(digest_id=str(ObjectId()), item_n=1)
    with pytest.raises(ValueError):
        ItemRef(digest_id=f"{ObjectId()}-world; drop", item_n=1)


async def test_unseen_only_with_a_run(world):
    db, uid = world
    doc = news_doc(kyiv(0))
    await db.digests.insert_one(doc)
    async with web(db, uid) as w:
        first = (await w.get("/api/digest", params={"unseen_only": "1"})).json()
        assert [d["side"] for d in first["digests"]] == ["ua", "ru", "world"]
        again = (await w.get("/api/digest", params={"unseen_only": "1"})).json()
        assert again["digests"] == [] and again["note"] == digests.ALL_SEEN_NOTE
    seen = (await db.users.find_one({"_id": uid}))["digest_seen"]["items"]
    assert f"{doc['_id']}-world:2" in seen and f"{doc['_id']}-ua:2" in seen


async def test_the_web_scout_knows_a_story_from_a_run(world):
    db, _ = world
    await db.digests.insert_one(news_doc(datetime.now(timezone.utc)))
    assert await in_our_news(db, ["Anduril"], datetime.now(timezone.utc))
    assert not await in_our_news(db, ["Helsing"], datetime.now(timezone.utc))
