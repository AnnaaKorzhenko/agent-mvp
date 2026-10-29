"""Automatic learning: who is due and when, the lock, the daily cap, and the loop's start and stop with the app."""
import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from starlette.applications import Starlette
from starlette.testclient import TestClient

from brain.audit.log import AuditLog
from brain.learning import auto
from brain.mcp_server.server import _start_with
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS
from tests.test_learning import ANSWER, FakeBackend

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
_dbs = iter(range(10**6))


@pytest.fixture
async def db(test_db):
    """A database of its own per test: every user in it is a candidate for the job."""
    own = test_db.client[f"{test_db.name}_a{next(_dbs)}"]
    yield own
    await test_db.client.drop_database(own.name)
IDENT = [{"provider": "https://auth.example/", "subject": "s"}]


def settings(**kw):
    return make_settings(LEARNING_AUTO=True, **kw)


async def add_user(db, **fields):
    uid = ObjectId()
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": uid, "identities": [{**IDENT[0], "subject": str(uid)}], "feedback": [], "goals": [],
                **fields})
    await db.users.insert_one(doc)
    return uid


def goal(text, minutes_ago):
    return {"text": text, "active": True, "created_at": NOW - timedelta(minutes=minutes_ago)}


def ratings(n, minutes_ago=10):
    return [{"post_id": ObjectId(), "reaction": "useful", "target_type": "post", "channel": "claude",
             "at": NOW - timedelta(minutes=minutes_ago)} for _ in range(n)]


def mapped(*goals, minutes_ago=120):
    return {"sensitivity": "internal", "processed_until": NOW - timedelta(minutes=minutes_ago),
            "updated_at": NOW - timedelta(minutes=minutes_ago),
            "goal_keywords": [{"goal": g, "specific": [], "broader": [], "tags": []} for g in goals]}


async def due(db, **kw):
    return {d.user_id: d.reason for d in await auto.due_users(db, settings(**kw), NOW)}


async def test_a_new_goal_is_due_after_a_short_delay(db):
    fresh = await add_user(db, goals=[goal("нова ціль", 1)])
    waited = await add_user(db, goals=[goal("нова ціль", 3)])
    done = await add_user(db, goals=[goal("стара ціль", 300)], learned=mapped("стара ціль"))
    d = await due(db)
    assert fresh not in d and d[waited] == "goal" and done not in d


async def test_twenty_ratings_are_due_after_the_cooldown(db):
    many = await add_user(db, feedback=ratings(20), learned=mapped())
    few = await add_user(db, feedback=ratings(19), learned=mapped())
    recent = await add_user(db, feedback=ratings(25, minutes_ago=1), learned=mapped(minutes_ago=10))
    old = await add_user(db, feedback=ratings(30, minutes_ago=200), learned=mapped(minutes_ago=100))
    d = await due(db)
    assert d[many] == "feedback" and few not in d
    assert recent not in d                                   # last run 10 min ago: cooldown of 30
    assert old not in d                                      # those ratings were already processed


async def test_only_users_the_server_serves_and_not_while_locked(db):
    telegram = 700_123_456
    await db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": telegram, "feedback": ratings(40)})
    locked = await add_user(db, goals=[goal("ціль", 10)], learning_lock=NOW + timedelta(minutes=5))
    d = await due(db)
    assert telegram not in d and locked not in d


async def test_run_once_saves_unlocks_and_respects_the_cap(db):
    a = await add_user(db, goals=[goal("Знайти напрямки для інвестицій у defense tech", 10)])
    b = await add_user(db, goals=[goal("Знайти напрямки для інвестицій у defense tech", 10)])
    answer = json.dumps(ANSWER, ensure_ascii=False)
    learner = auto.Learner(db, settings(LEARNING_MAX_RUNS_PER_DAY=1),
                           FakeBackend(AuditLog(db), [answer, answer]))
    done = await learner.run_once(NOW)
    ran = [uid for uid, _, outcome in done if outcome.startswith("saved")]
    assert len(ran) == 1                                      # the cap: one run per day here
    saved = await db.users.find_one({"_id": ran[0]})
    assert saved["learned"]["goal_keywords"] and "learning_lock" not in saved
    other = b if ran[0] == a else a
    assert "learned" not in await db.users.find_one({"_id": other})


async def test_a_failure_releases_the_lock(db):
    uid = await add_user(db, goals=[goal("Знайти напрямки для інвестицій у defense tech", 10)])
    learner = auto.Learner(db, settings(), FakeBackend(AuditLog(db), ["не JSON", "теж ні"]))
    [(who, reason, outcome)] = [x for x in await learner.run_once(NOW) if x[0] == uid]
    assert reason == "goal" and outcome.startswith("failed")
    assert "learning_lock" not in await db.users.find_one({"_id": uid})


def test_the_loop_starts_and_stops_with_the_app():
    events = []

    async def task():
        events.append("started")
        try:
            await asyncio.sleep(3600)
        finally:
            events.append("stopped")

    app = Starlette()
    _start_with(app, task)
    with TestClient(app):
        pass
    assert events == ["started", "stopped"]


async def test_set_goal_in_claude_starts_the_run(db):
    from mcp import Client
    from brain.db.repositories import Repos
    from brain.mcp_server.server import create_server
    from brain.mcp_server.tools import Services
    uid = await add_user(db, goals=[])
    s = make_settings(DEV_USER_ID=str(uid), LEARNING_AUTO=True)
    backend = FakeBackend(AuditLog(db), [json.dumps({**ANSWER, "goal_keywords": [
        {"goal": "стежити за НРК", "specific": ["НРК"], "broader": [], "tags": []}]}, ensure_ascii=False)])
    learner = auto.Learner(db, s, backend)
    svc = Services(settings=s, repos=Repos(db), audit=AuditLog(db), token=lambda: None, learner=learner)
    async with Client(create_server(svc)) as c:
        for text in ("стежити за НРК", "друга ціль", "третя ціль"):            # three in a row: one run
            r = await c.call_tool("set_goal", {"action": "add", "text": text, "user_message": text})
            assert not r.is_error
        learner.settings = s.model_copy(update={"learning_goal_delay_minutes": 0})
        learner.nudge(uid, delay_seconds=0)                                    # (skip the 2-minute wait)
        await learner.pending[uid]
    assert len(backend.sent) == 1                                              # debounced into one run
    learned = (await db.users.find_one({"_id": uid}))["learned"]
    assert learned["goal_keywords"][0]["goal"] == "стежити за НРК"


async def test_set_goal_without_the_learner_works_as_before(db):
    from mcp import Client
    from brain.db.repositories import Repos
    from brain.mcp_server.server import create_server
    from brain.mcp_server.tools import Services
    uid = await add_user(db, goals=[])
    svc = Services(settings=make_settings(DEV_USER_ID=str(uid)), repos=Repos(db), audit=AuditLog(db),
                   token=lambda: None)
    async with Client(create_server(svc)) as c:
        r = await c.call_tool("set_goal", {"action": "add", "text": "ціль", "user_message": "ціль"})
    assert not r.is_error and svc.learner is None


async def test_changes_trigger_only_what_they_should(db):
    s = settings(LEARNING_GOAL_DELAY_MINUTES=0)
    learner = auto.Learner(db, s, FakeBackend(AuditLog(db), []))
    nudged = []
    learner.nudge = lambda uid, delay_seconds=None: nudged.append(uid)
    goal_change = {"operationType": "update", "documentKey": {"_id": 1},
                   "updateDescription": {"updatedFields": {"goals.3": {"text": "x"}}}}
    own_write = {"operationType": "update", "documentKey": {"_id": 2},
                 "updateDescription": {"updatedFields": {"learned": {}, "learning_lock": 0, "last_seen": 0}}}
    await learner.on_change(goal_change)
    await learner.on_change(own_write)
    assert nudged == [1]
    # Ratings: below the threshold nothing runs; at the threshold a run starts.
    uid = await add_user(db, feedback=ratings(19), learned=mapped())
    runs = []
    learner.run_user = lambda u, reason, now=None: runs.append((u, reason)) or asyncio.sleep(0)
    rating = {"operationType": "update", "documentKey": {"_id": uid},
              "updateDescription": {"updatedFields": {"feedback": []}}}
    await learner.on_change(rating)
    await db.users.update_one({"_id": uid}, {"$push": {"feedback": ratings(1)[0]}})
    await learner.on_change(rating)
    await asyncio.sleep(0)
    assert runs == [(uid, "feedback")]


async def test_a_goal_written_to_the_database_starts_a_run_through_the_stream(db):
    """End to end on the real change stream: the goal is pushed straight into the database, as the bot does."""
    uid = await add_user(db, goals=[])
    s = settings(LEARNING_GOAL_DELAY_MINUTES=0)
    backend = FakeBackend(AuditLog(db), [json.dumps({**ANSWER, "goal_keywords": [
        {"goal": "стежити за НРК", "specific": ["НРК"], "broader": [], "tags": []}]}, ensure_ascii=False)])
    learner = auto.Learner(db, s, backend)
    task = asyncio.create_task(learner.watch())
    try:
        await asyncio.sleep(3)                                   # the stream is open
        await db.users.update_one({"_id": uid}, {"$push": {"goals": {
            "text": "стежити за НРК", "active": True, "source": "user", "created_at": datetime.now(timezone.utc)}}})
        for _ in range(60):
            if (await db.users.find_one({"_id": uid}, {"learned": 1})).get("learned"):
                break
            await asyncio.sleep(0.5)
    finally:
        task.cancel()
    learned = (await db.users.find_one({"_id": uid}))["learned"]
    assert learned["goal_keywords"][0]["goal"] == "стежити за НРК" and len(backend.sent) == 1


def commented(minutes_ago, text="більше про цей пристрій"):
    return {"post_id": ObjectId(), "reaction": "useful", "target_type": "digest_item", "channel": "claude",
            "at": NOW - timedelta(minutes=minutes_ago), "text": text, "text_at": NOW - timedelta(minutes=minutes_ago)}


async def test_a_comment_is_due_after_the_short_delay_without_the_threshold(db):
    waited = await add_user(db, feedback=[commented(3)], learned=mapped(minutes_ago=10))   # cooldown doesn't apply
    fresh = await add_user(db, feedback=[commented(1)], learned=mapped())
    silent = await add_user(db, feedback=[commented(3, text="")], learned=mapped())
    old = await add_user(db, feedback=[commented(200)], learned=mapped(minutes_ago=100))   # already processed
    d = await due(db)
    assert d[waited] == "comment" and fresh not in d and silent not in d and old not in d


async def test_a_comment_from_the_stream_is_debounced_like_a_goal(db):
    learner = auto.Learner(db, settings(LEARNING_GOAL_DELAY_MINUTES=0), FakeBackend(AuditLog(db), []))
    nudged = []
    learner.nudge = lambda uid, delay_seconds=None: nudged.append(uid)
    uid = await add_user(db, feedback=[commented(1)], learned=mapped())
    await learner.on_change({"operationType": "update", "documentKey": {"_id": uid},
                             "updateDescription": {"updatedFields": {"feedback.0.text": "більше"}}})
    assert nudged == [uid]
