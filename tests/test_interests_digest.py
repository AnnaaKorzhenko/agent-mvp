"""The pipeline's digest of users' topics and interests (side "interests", since 2026-10-31), as each user sees it,
and a stated interest reaching the pipeline's record the same day."""
import copy
import itertools
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from brain.db.repositories import Repos
from brain.domain import digests, interests
from brain.agents import learner as auto
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS
from tests.test_agents import cloud

_dbs = itertools.count()
_ids = itertools.count(980_000_001)
NOW = datetime.now(timezone.utc)


@pytest.fixture
async def own(test_db):
    db = test_db.client[f"{test_db.name}_i{next(_dbs)}"]
    yield db
    await test_db.client.drop_database(db.name)


def item(n, text):
    return {"n": n, "text": text, "source_urls": [f"https://t.me/c/{n}"], "source_post_ids": []}


async def interests_digest(db, sections, **fields):
    """As the pipeline writes it: side interests, one section per search, a story repeated under several."""
    await db.digests.insert_one({"_id": ObjectId(), "period": "day", "side": "interests", "trigger": "scheduled",
                                 "interest": "User interests", "created_at": NOW - timedelta(hours=1),
                                 "sections": [{"category": c, "title": f"🔎 {c}", "items": its} for c, its in sections],
                                 "sensitivity": "internal", **fields})


async def add_user(db, **fields):
    doc = copy.deepcopy(USERS[0])
    doc.pop("learned", None)
    doc.update({"_id": next(_ids), "goals": [], "feedback": [], "insights": [],
                "interests": {"category_scores": {}, "tag_scores": {}}, **fields})
    await db.users.insert_one(doc)
    return doc["_id"]


BELARUS = ["Павло Ткач заявив, що російська супутникова система зв'язку «Рассвет», яку тестує Білорусь для МНС, "
           "забезпечить зв'язок у віддалених районах.",
           "За даними першого заступника міністра зв'язку Білорусі Павла Ткача, у Білорусі тестують російську "
           "систему «Рассвет» для МНС."]
BURNED = "Російський супутник «Рассвет-3» № 17 згорів в атмосфері 30 жовтня через зниження орбіти."


async def test_each_user_sees_their_topics_and_interests_once_each_story(own):
    topic = {"id": ObjectId(), "name": "Рассвет", "norm": "рассвет", "status": "active", "specific": ["Рассвет"],
             "expires_at": NOW + timedelta(days=5)}
    follows = await add_user(own, topics=[topic])
    learned = {"sensitivity": "internal", "updated_at": NOW, "interests": interests.merge(
        [], [{"topic": "Наземні роботи: «Мураха», «Тарантул»", "weight": 5, "signals": ["comment"]}], NOW)}
    likes = await add_user(own, learned=learned)
    other = await add_user(own)
    await interests_digest(own, [("Рассвет-3", [item(1, BELARUS[0]), item(2, BURNED)]),
                                 ("Бюро 1440", [item(3, BELARUS[1])]),
                                 ("НРК", [item(4, "Бригада отримала НРК «Мураха» для евакуації поранених.")])])
    res = await digests.get_digests(Repos(own), cloud(follows))
    [view] = [v for v in res.digests if v.side == digests.TOPICS]
    assert [(s.title, [i.n for i in items]) for s, items in view.sections] == [("⭐ Рассвет", [1, 2])]   # 3: same story
    assert view.why(view.sections[0][1][0]).why == ["тема, яку ви відстежуєте: Рассвет"]
    res = await digests.get_digests(Repos(own), cloud(likes))
    [view] = [v for v in res.digests if v.side == digests.TOPICS]
    assert [(s.title, [i.n for i in items]) for s, items in view.sections] == [("⭐ Мураха", [4])]   # by the name inside
    res = await digests.get_digests(Repos(own), cloud(other))
    assert not [v for v in res.digests if v.side == digests.TOPICS]             # nothing of theirs: not shown
    assert all(v.side != "interests" for v in res.digests)                     # never as a side of its own


async def test_the_weekly_interests_digest_is_not_the_morning_one(own):
    topic = {"id": ObjectId(), "name": "Рассвет", "norm": "рассвет", "status": "active", "specific": ["Рассвет"],
             "expires_at": NOW + timedelta(days=5)}
    me = await add_user(own, topics=[topic])
    await interests_digest(own, [("Рассвет-3", [item(1, BURNED)])], period="week")
    res = await digests.get_digests(Repos(own), cloud(me))
    assert not [v for v in res.digests if v.side == digests.TOPICS]


def test_reworded_repeats_are_one_story_but_two_stories_on_one_topic_stay_two():
    own = {"расс", "рассв"}
    story = lambda t: (digests._words(t), digests._names(t))
    assert digests._same_story(story(BELARUS[0]), story(BELARUS[1]), own)
    assert not digests._same_story(story(BELARUS[0]), story(BURNED), own)


async def test_a_stated_interest_starts_a_learning_run_and_a_detected_one_waits(own):
    uid = ObjectId()
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": uid, "identities": [{"provider": "p", "subject": str(uid)}], "goals": [], "feedback": [],
                "learned": {"sensitivity": "internal", "processed_until": NOW - timedelta(hours=1),
                            "updated_at": NOW - timedelta(hours=1), "goal_keywords": []},
                "insights": [{"id": ObjectId(), "kind": "interest", "origin": "detected", "text": "дрони",
                              "created_at": NOW - timedelta(minutes=10)}]})
    await own.users.insert_one(doc)
    s = make_settings(LEARNING_AUTO=True)
    assert uid not in {d.user_id for d in await auto.due_users(own, s, NOW)}  # detected: waits for the others
    await own.users.update_one({"_id": uid}, {"$push": {"insights": {
        "id": ObjectId(), "kind": "interest", "origin": "explicit", "text": "мене цікавлять морські дрони",
        "created_at": NOW - timedelta(minutes=10)}}})
    assert {d.user_id: d.reason for d in await auto.due_users(own, s, NOW)}[uid] == "interest"
    learner = auto.Learner(own, s, None)
    nudged = []
    learner.nudge = lambda u, delay_seconds=None: nudged.append(u)
    await learner.on_change({"operationType": "update", "documentKey": {"_id": uid},
                             "updateDescription": {"updatedFields": {"insights.1": {"kind": "interest"}}}})
    assert nudged == [uid]
