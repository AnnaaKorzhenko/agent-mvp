"""The worker's morning push (brain/agents/morning_push.py) with a fake sender: the window, the morning delivery's
rule, once per digest and user apart from Claude's delivery, gone subscriptions, and failures kept apart."""
import copy
import itertools
import json
from datetime import timedelta

import pytest
from bson import ObjectId

from brain.agents import morning_push
from brain.agents.runs import GLOBAL
from brain.agents.runtime import Runtime
from brain.agents.setup import build_agents
from brain.db.repositories import Repos
from brain.domain import digests
from brain.web import admin
from tests.conftest import make_settings
from tests.fixtures.sample_data import DIGESTS, USERS
from tests.test_morning import ctx, kyiv

_ids = itertools.count(975_000_001)
KEYS = {"p256dh": "p", "auth": "a"}


def sub(name):
    return {"endpoint": f"https://push.example.com/{name}", "keys": KEYS}


@pytest.fixture
async def world(test_db):
    db = test_db.client[f"{test_db.name}_mp{next(_ids)}"]
    uid, other, quiet = next(_ids), next(_ids), next(_ids)
    await db.users.insert_many([
        {**copy.deepcopy(USERS[0]), "_id": uid, "push_subscriptions": [sub("a"), sub("b")]},
        {**copy.deepcopy(USERS[0]), "_id": other, "push_subscriptions": [sub("c")]},
        {**copy.deepcopy(USERS[0]), "_id": quiet},                       # never subscribed
    ])
    yield db, uid, other
    await test_db.client.drop_database(db.name)


async def add_digest(db, side, at):
    d = copy.deepcopy(next(x for x in DIGESTS if x["side"] == side))
    d.update(_id=ObjectId(), created_at=at.astimezone(digests.KYIV))
    await db.digests.insert_one(d)


class FakeSender:
    def __init__(self, status=None, fail=()):
        self.sent, self.status, self.fail = [], status or {}, set(fail)

    async def __call__(self, s, payload):
        if s["endpoint"] in self.fail:
            raise ConnectionError("push service down")
        self.sent.append((s["endpoint"], json.loads(payload)))
        return self.status.get(s["endpoint"], 201)


def agent(db, sender):
    return morning_push.MorningPush(db, Runtime(db), sender)


async def test_pushes_once_per_digest_to_every_subscription(world):
    db, uid, other = world
    await add_digest(db, "ua", kyiv(7, 40))
    await add_digest(db, "ru", kyiv(7, 41))
    sender = FakeSender()
    report = await agent(db, sender).push(GLOBAL, "schedule", now=kyiv(8, 30))
    assert sorted(e for e, _ in sender.sent) == [sub(n)["endpoint"] for n in "abc"]
    p = sender.sent[0][1]
    assert p["tag"] == "morning-digest" and p["title"] and p["body"].startswith("Ранковий дайджест готовий: ")
    assert "2 pushed: 3 sent" in report.outcome
    assert (await db.users.find_one({"_id": uid}))["morning_digest"]["web_push"]["date"] == kyiv(8).date().isoformat()

    sender.sent.clear()
    await agent(db, sender).push(GLOBAL, "schedule", now=kyiv(9))
    assert sender.sent == []                                             # the same digest: not again
    await add_digest(db, "ua", kyiv(9, 10))                              # a rebuilt digest is new
    await agent(db, sender).push(GLOBAL, "schedule", now=kyiv(9, 30))
    assert len(sender.sent) == 3


async def test_does_not_consume_claudes_delivery_nor_the_other_way(world):
    db, uid, _ = world
    repos = Repos(db)
    await add_digest(db, "ua", kyiv(7, 40))
    await add_digest(db, "ru", kyiv(7, 41))
    assert (await digests.morning_digest(repos, ctx(uid), now=kyiv(8, 10))).status == "new"
    sender = FakeSender()
    await agent(db, sender).push(GLOBAL, "schedule", now=kyiv(8, 30))
    assert len(sender.sent) == 3
    await add_digest(db, "ua", kyiv(8, 50))
    await agent(db, sender).push(GLOBAL, "schedule", now=kyiv(9))
    assert (await digests.morning_digest(repos, ctx(uid), now=kyiv(9, 10))).status == "new"
    state = (await db.users.find_one({"_id": uid}))["morning_digest"]
    assert state["channel"] == "claude" and state["web_push"]["key"] == state["key"]


@pytest.mark.parametrize("hour,minute", [(7, 59), (11, 30), (13, 0)])
async def test_only_inside_the_morning_window(world, hour, minute):
    db, *_ = world
    await add_digest(db, "ua", kyiv(0, 10))
    await add_digest(db, "ru", kyiv(0, 11))
    sender = FakeSender()
    report = await agent(db, sender).push(GLOBAL, "schedule", now=kyiv(hour, minute))
    assert sender.sent == [] and report.outcome == "outside the morning window"


async def test_waits_for_both_sides_like_the_morning_delivery(world):
    db, uid, _ = world
    await add_digest(db, "ua", kyiv(7, 40))
    sender = FakeSender()
    await agent(db, sender).push(GLOBAL, "schedule", now=kyiv(9))
    assert sender.sent == [] and "web_push" not in ((await db.users.find_one({"_id": uid})).get("morning_digest") or {})
    await add_digest(db, "ua", kyiv(7, 40) - timedelta(days=1))          # yesterday's doesn't count
    await add_digest(db, "ru", kyiv(9, 5))
    await agent(db, sender).push(GLOBAL, "schedule", now=kyiv(9, 30))
    assert len(sender.sent) == 3


async def test_gone_subscriptions_are_deleted_and_failures_stay_apart(world):
    db, uid, other = world
    await add_digest(db, "ua", kyiv(7, 40))
    await add_digest(db, "ru", kyiv(7, 41))
    sender = FakeSender(status={sub("a")["endpoint"]: 410, sub("c")["endpoint"]: 404},
                        fail={sub("b")["endpoint"]})
    report = await agent(db, sender).push(GLOBAL, "schedule", now=kyiv(8, 30))
    assert "0 sent, 2 gone, 1 failed" in report.outcome
    assert [s["endpoint"] for s in (await db.users.find_one({"_id": uid}))["push_subscriptions"]] == \
        [sub("b")["endpoint"]]
    assert (await db.users.find_one({"_id": other}))["push_subscriptions"] == []


async def test_one_users_failure_does_not_stop_the_others(world, monkeypatch):
    db, uid, other = world
    await add_digest(db, "ua", kyiv(7, 40))
    await add_digest(db, "ru", kyiv(7, 41))
    real = digests.morning_ready

    async def broken_for_one(repos, c, now):
        if c.user_id == uid:
            raise RuntimeError("bad profile")
        return await real(repos, c, now)

    monkeypatch.setattr(digests, "morning_ready", broken_for_one)
    sender = FakeSender()
    report = await agent(db, sender).push(GLOBAL, "schedule", now=kyiv(8, 30))
    assert [e for e, _ in sender.sent] == [sub("c")["endpoint"]] and "1 failed" in report.outcome


@pytest.mark.parametrize("env,on", [
    ({"MORNING_PUSH": "true", "WEB_PUSH_PUBLIC_KEY": "pub", "WEB_PUSH_PRIVATE_KEY": "priv"}, True),
    ({"MORNING_PUSH": "true", "WEB_PUSH_PUBLIC_KEY": "pub"}, False),                 # no private key
    ({"WEB_PUSH_PUBLIC_KEY": "pub", "WEB_PUSH_PRIVATE_KEY": "priv"}, False),          # MORNING_PUSH off
])
async def test_registered_only_when_switched_on_with_the_keys(test_db, env, on):
    runtime = build_agents(test_db, make_settings(**env)).runtime
    if not on:
        assert runtime is None
        return
    assert set(runtime.agents) == {morning_push.AGENT}
    assert [(s.agent, s.scope, s.every) for s in runtime.schedules] == [(morning_push.AGENT, GLOBAL,
                                                                          timedelta(minutes=30))]


def test_the_console_lists_it():
    assert admin.BY_NAME[morning_push.AGENT].enabled_by == "MORNING_PUSH"
    assert admin.BY_NAME[morning_push.AGENT].per_user is False


@pytest.mark.parametrize("n,word", [(1, "пункт"), (4, "пункти"), (5, "пунктів"), (11, "пунктів"), (22, "пункти")])
def test_item_counts_in_ukrainian(n, word):
    assert morning_push._items(n) == f"{n} {word}"
