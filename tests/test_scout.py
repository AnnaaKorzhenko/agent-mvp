"""The scout (docs/AGENT_PLAN.md phase 2): rising names in our posts, reviewed by the model, proposed to the users
they fit."""
import copy
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from brain.agents import scout
from brain.agents.runs import GLOBAL, AgentRuns
from brain.agents.runtime import Runtime
from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import proposals, trends
from tests.fixtures.sample_data import USERS, load_samples
from tests.test_learning import FakeBackend

_dbs = itertools.count()
_ids = itertools.count(940_000_001)


@pytest.fixture
async def own(test_db):
    db = test_db.client[f"{test_db.name}_s{next(_dbs)}"]
    await load_samples(db)
    yield db
    await test_db.client.drop_database(db.name)


def post(summary, side="ua", hours_ago=5, channel=None):
    at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"_id": ObjectId(), "summary": summary, "source_side": side, "channel_id": channel or ObjectId(),
            "post_url": f"https://t.me/c/{ObjectId()}", "published_at": at.isoformat(), "sensitivity": "internal",
            "is_ad": False, "is_duplicate": False, "processed": True, "category": "other", "tags": [], "importance": 3}


ROBOT = ["Компанія ARX Robotics показала наземного робота «Мураха» для евакуації поранених.",
         "Наземний робот «Мураха» від ARX Robotics пройшов випробування на фронті.",
         "ARX Robotics передасть ЗСУ партію НРК «Мураха».",
         "Бригада отримала НРК «Мураха», які виготовляє ARX Robotics.",
         "«Мураха» від ARX Robotics: роботизована евакуація."]


async def seed(db, texts, side="ua", channels=None, hours_ago=5):
    channels = channels or [ObjectId() for _ in texts]
    await db.posts.insert_many([post(t, side, hours_ago, c) for t, c in zip(texts, channels)])


def test_names_not_words_and_not_places():
    terms = trends.terms_of("У Литві та в Алешках тривога. Компанія ARX Robotics показала «Мураха» і Су-34, "
                            "а Іван Петренко з-за кордону сказав про чат-бот.")
    assert {"ARX Robotics", "Мураха", "Су-34", "Іван Петренко"} <= terms
    assert not terms & {"Литві", "Алешках", "з-за", "чат-бот", "У Литві"}


def test_a_name_rising_across_channels_is_a_candidate_and_one_loud_channel_isnt():
    now = datetime.now(timezone.utc)
    spread = [post(t) for t in ROBOT]
    one = ObjectId()
    loud = [post(f"Канал знову про «Тритон-{n}» і «Тритон»", channel=one) for n in range(8)]
    usual = [post("Новини про «Brave1» гранти", hours_ago=24 * d + 60) for d in range(14) for _ in range(2)]
    today_usual = [post("«Brave1» оголосив новий етап") for _ in range(4)]
    found = trends.detect(spread + loud + today_usual, usual, now)
    names = [set(c.terms) for c in found]
    assert any({"ARX Robotics", "Мураха"} <= n for n in names)                  # merged: they travel together
    assert not any("Тритон" in n for n in names)                                # one channel only
    assert not any("Brave1" in n for n in names)                                # 2 a day before, 2 a day now


REVIEW = {"keep": [{"id": 0, "name": "Мураха", "what": "Наземний робот ARX Robotics для евакуації поранених.",
                    "kind": "model", "specific": ["Мураха", "Муравей", "Muraha"],
                    "web_queries": ["робот «Мураха» евакуація", "«Мураха»"], "tags": ["наземні роботи"]}]}


async def add_user(db, **fields):
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": next(_ids), "goals": [], "feedback": [], "interests": {"category_scores": {}, "tag_scores": {}},
                "identities": [{"provider": "p", "subject": str(ObjectId())}],
                "last_seen": datetime.now(timezone.utc), **fields})
    await db.users.insert_one(doc)
    return doc["_id"]


async def test_the_scout_finds_reviews_remembers_and_proposes_to_who_it_fits(own):
    await seed(own, ROBOT)
    fits = await add_user(own, goals=[{"text": "Знайти виробників наземних роботів для евакуації", "active": True}])
    other = await add_user(own, goals=[{"text": "Стежити за ринком нафти", "active": True}])
    gone = await add_user(own, goals=[{"text": "Знайти виробників наземних роботів для евакуації", "active": True}],
                          last_seen=datetime.now(timezone.utc) - timedelta(days=90))
    rt = Runtime(own)
    backend = FakeBackend(AuditLog(own), [json.dumps(REVIEW, ensure_ascii=False)])
    s = scout.Scout(own, backend, rt, timedelta(hours=6))
    assert await rt.submit_scheduled() == [scout.TRENDS]
    assert await rt.submit_scheduled() == []                                    # queued: not again
    assert (await rt.work_once()).endswith("kept; matching 2 user(s)")
    snap = await AgentRuns(own).latest_output(scout.TRENDS)
    [item] = snap["kept"]
    assert item["web_queries"] == ['робот "Мураха" евакуація']                  # a lone name is not a search
    assert "users" not in json.dumps(snap, default=str)                         # posts only, no user data
    outcomes = [await rt.work_once() for _ in range(2)]
    assert sorted(o.split(", ")[-1] for o in outcomes) == ["0 proposed", "1 proposed"]
    [p] = await Repos(own).users.get_proposals(fits)
    assert p.kind == "topic" and p.title == "Мураха" and p.action.tool == "track_topic"
    assert p.action.args["queries"] == ['робот "Мураха" евакуація'] and p.action.args["goal"].startswith("Знайти")
    assert "5 дописів, 5 каналів" in p.why and "для вас: ціль:" in p.why
    assert await Repos(own).users.get_proposals(other) == [] and await Repos(own).users.get_proposals(gone) == []
    # The next run: the same candidate isn't sent to the model again, and the user isn't offered it twice.
    await s.find(GLOBAL, "test")
    assert len(backend.sent) == 1
    await s.match(fits, "test")
    assert len(await Repos(own).users.get_proposals(fits)) == 1
    # A yes follows it like 📌 would, with the scout's names and searches.
    from tests.test_agents import cloud
    r = await proposals.answer(Repos(own), cloud(fits), p.id, True, mcp_channels=["claude"])
    assert r.done.startswith("відстежується тема «Мураха»")


async def test_what_the_user_has_already_is_not_proposed(own):
    await seed(own, ROBOT)
    rt = Runtime(own)
    s = scout.Scout(own, FakeBackend(AuditLog(own), [json.dumps(REVIEW, ensure_ascii=False)]), rt, timedelta(hours=6))
    await rt.run(scout.TRENDS, GLOBAL, "test")                     # through the runtime: it keeps the snapshot
    goal = [{"text": "Знайти виробників наземних роботів для евакуації", "active": True}]
    follows = await add_user(own, goals=goal, topics=[{
        "id": ObjectId(), "name": "Мураха", "norm": "мураха", "status": "active", "specific": ["Мураха"],
        "expires_at": datetime.now(timezone.utc) + timedelta(days=5)}])
    searched = await add_user(own, goals=goal)
    await Repos(own).sources.set_keyword_queries([{"q": '"Мураха" робот', "region": "ua"}])
    for uid in (follows, searched):
        assert (await s.match(uid, "test")).outcome.endswith("0 proposed")
    await Repos(own).sources.set_keyword_queries([])
    assert (await s.match(searched, "test")).outcome.endswith("1 proposed")


async def test_only_russian_channels_count_less_and_nothing_fresh_means_nothing(own):
    rt = Runtime(own)
    s = scout.Scout(own, FakeBackend(AuditLog(own), []), rt, timedelta(hours=6))
    uid = await add_user(own)
    assert (await s.match(uid, "test")).outcome == "no fresh trends"
    item = {"name": "X", "what": "наземний робот для евакуації", "sides": {"ru": 5}, "examples": [], "tags": []}
    from brain.domain import relevance
    prefs = relevance.Interests(goals=[relevance._topic("ціль", "наземний робот евакуація", relevance.W_GOAL, group="g")])
    ua = scout.fit({**item, "sides": {"ua": 5}}, prefs).score
    assert scout.fit(item, prefs).score == round(ua * scout.RU_ONLY, 3)
