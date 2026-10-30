"""The agents' suggestions in plain conversation (docs/AGENT_PLAN.md, after phase 4): a suggestion mentioned outside
the digest, listing and answering them in any chat, a «no» with a reason, no nagging, and correcting what was
inferred."""
import copy
import itertools
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from mcp import Client

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import corrections, interests, proposals
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS, load_samples
from tests.test_agents import cloud

_dbs = itertools.count()
_ids = itertools.count(970_000_001)
NOW = datetime.now(timezone.utc)


@pytest.fixture
async def own(test_db):
    db = test_db.client[f"{test_db.name}_p{next(_dbs)}"]
    await load_samples(db)
    yield db
    await test_db.client.drop_database(db.name)


async def add_user(db, **fields):
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": next(_ids), "goals": [], "feedback": [], "insights": [], **fields})
    await db.users.insert_one(doc)
    return doc["_id"]


def services(db, uid):
    return Services(settings=make_settings(DEV_USER_ID=uid, TRUST_LEVEL="cloud"), repos=Repos(db),
                    audit=AuditLog(db), token=lambda: None)


async def goal_proposal(db, uid, title="знайти 3 НРК-компанії"):
    await proposals.propose(Repos(db), uid, kind="goal", title=title, agent="learner", why="ви про це говорили",
                            action={"tool": "set_goal", "args": {"text": title}})


async def test_a_suggestion_is_mentioned_outside_the_digest_once_a_day(own):
    me = await add_user(own)
    await goal_proposal(own, me)
    async with Client(create_server(services(own, me))) as c:
        r = await c.call_tool("log_turn", {"user_message": "дякую"})
        p = r.structured_content["proposal_to_mention"]
        assert p["title"] == "знайти 3 НРК-компанії" and p["yes"] == "додати ціль"
        assert "знайти 3 НРК-компанії" in r.content[0].text                          # the model reads it too
        r = await c.call_tool("get_profile", {"user_message": "мій профіль"})
        assert r.structured_content.get("proposal_to_mention") is None                # once a day
        r = await c.call_tool("get_digest", {"user_message": "дайджест"})
        assert r.structured_content["proposals"][0]["title"] == "знайти 3 НРК-компанії"   # the digest still has it
        assert r.structured_content.get("proposal_to_mention") is None


async def test_a_card_stops_showing_after_three_times_unanswered(own):
    me = await add_user(own)
    await goal_proposal(own, me)
    for _ in range(proposals.MAX_SHOWN):
        assert len(await proposals.open_for(Repos(own), cloud(me))) == 1
    assert await proposals.open_for(Repos(own), cloud(me)) == []
    assert (await Repos(own).users.get_proposals(me))[0].status == "stale"


async def test_suggestions_are_listed_and_answered_from_any_chat_with_a_reason(own):
    me = await add_user(own)
    await goal_proposal(own, me, "перша ціль")
    await proposals.propose(Repos(own), me, kind="topic", title="CONTSHIP JET", agent="scout", why="контейнеровоз",
                            action={"tool": "track_topic", "args": {"name": "CONTSHIP JET"}})
    async with Client(create_server(services(own, me))) as c:
        r = await c.call_tool("get_proposals", {"user_message": "що ти там пропонував?"})
        listed = r.structured_content["proposals"]
        assert [p["title"] for p in listed] == ["CONTSHIP JET", "перша ціль"]           # newest first
        assert r.structured_content.get("proposal_to_mention") is None
        ship = listed[0]["proposal_id"]
        r = await c.call_tool("answer_proposal", {"user_message": "ні, не пропонуй мені про кораблі",
                                                  "proposal_id": ship, "answer": "dismiss",
                                                  "reason": "не пропонуй мені про кораблі"})
        assert r.structured_content["done"] == "закрито"
    [fb] = (await own.users.find_one({"_id": me}))["feedback"]
    assert fb["target_type"] == "suggestion" and fb["text"] == "не пропонуй мені про кораблі"
    assert [p.status for p in await proposals.list_open(Repos(own), cloud(me))] == ["open"]   # listing isn't showing
    from brain.learning import profile
    assert "goes to avoid" in profile.SYSTEM                                         # the learner is told what to do


async def test_correcting_an_inference_takes_effect_at_once(own):
    learned = {"sensitivity": "internal", "updated_at": NOW, "avoid": [],
               "interests": interests.merge([], [{"topic": "космічні запуски", "weight": 5, "signals": ["comment"]},
                                                 {"topic": "морські дрони", "weight": 4, "signals": ["comment"]}], NOW),
               "intents": interests.merge_intents([{"text": "готує огляд про космос", "terms": ["космос"],
                                                    "days": 7}], NOW)}
    me = await add_user(own, learned=learned, topics=[{
        "id": ObjectId(), "name": "Космос-1", "norm": "космос-1", "status": "active", "specific": ["Космос-1"],
        "expires_at": NOW + timedelta(days=5)}])
    await proposals.propose(Repos(own), me, kind="topic", title="Космічна програма Х", agent="scout")
    await goal_proposal(own, me)
    async with Client(create_server(services(own, me))) as c:
        r = await c.call_tool("correct_profile", {"user_message": "мене космос не цікавить",
                                                  "not_interested": "космос"})
    out = r.structured_content
    assert out["dropped_interests"] == ["космічні запуски"] and out["dropped_intents"] == ["готує огляд про космос"]
    assert out["closed_proposals"] == ["Космічна програма Х"] and out["followed_matching"] == ["Космос-1"]
    doc = await own.users.find_one({"_id": me})
    assert [i["topic"] for i in doc["learned"]["interests"]] == ["морські дрони"] and doc["learned"]["avoid"] == ["космос"]
    assert doc["topics"][0]["status"] == "active"                                    # his own choice: not stopped
    assert any(i["text"] == "не цікавить: космос" and i["origin"] == "explicit" for i in doc["insights"])
    from brain.domain import relevance
    prefs = await relevance.load(Repos(own), cloud(me))
    assert prefs.score(side="ua", category="other", text="Новий запуск у космос").score < 0


def test_word_starts_match_across_endings_only_for_the_same_thing():
    assert corrections.matches("космос", "космічні запуски") and corrections.matches("кораблі", "корабельний флот")
    assert not corrections.matches("космос", "морські дрони")
