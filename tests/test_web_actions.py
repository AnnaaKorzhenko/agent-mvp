"""The UI's buttons (docs/WEB_PLAN.md step 5.3): proposals answered, goals and topics changed, interests corrected,
suggestion settings, ratings of news and of the analyst's replies. Each button runs the same brain tool as
Claude's cards, as the signed-in user, and is logged as a button press."""
import copy
import itertools

import httpx2
import pytest

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import proposals as proposals_domain
from brain.integrations.backends import AnthropicBackend
from brain.tools.base import Services
from brain.web.app import create_app
from tests.conftest import make_settings
from tests.fixtures.sample_data import DIGESTS, POSTS, USERS, load_samples
from tests.test_web_chat import FakeClaude, block, events, message

DEV_UID = 820_000_003
_dbs = itertools.count()


@pytest.fixture
async def db(test_db):
    d = test_db.client[f"{test_db.name}_a{next(_dbs)}"]
    await load_samples(d)
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": DEV_UID, "goals": [{"text": "стежити за НРК", "active": True, "source": "user"}],
                "feedback": [], "insights": []})
    await d.users.insert_one(doc)
    yield d
    await test_db.client.drop_database(d.name)


def app_with(db, fake=None):
    settings = make_settings(DEV_USER_ID=DEV_UID)
    svc = Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None)
    backend = AnthropicBackend(svc.audit, settings, client=fake) if fake else None
    return create_app(settings, svc, backend=backend)


def web(app, csrf=True):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8001",
                              headers={"X-Brain": "1"} if csrf else {})


async def user(db):
    return await db.users.find_one({"_id": DEV_UID})


async def test_yes_on_a_topic_card_follows_it_without_a_chat(db):
    await proposals_domain.propose(Repos(db), DEV_UID, kind="topic", title="Спектр-К", agent="scout",
                                   why="згадується в постах про РЕБ",
                                   action={"tool": "track_topic", "args": {"name": "Спектр-К",
                                                                           "specific": ["Спектр-К", "Spektr-K"]}})
    await proposals_domain.propose(Repos(db), DEV_UID, kind="goal", title="стежити за дронами-перехоплювачами",
                                   agent="learner", action={"tool": "set_goal",
                                                            "args": {"text": "стежити за дронами-перехоплювачами"}})
    async with web(app_with(db)) as c:
        cards = (await c.get("/api/proposals")).json()["proposals"]
        assert {p["kind"] for p in cards} == {"topic", "goal"}
        topic = next(p for p in cards if p["kind"] == "topic")
        r = (await c.post(f"/api/proposals/{topic['proposal_id']}", json={"answer": "accept"})).json()
        assert r["accepted"] and "Спектр-К" in r["done"]
        goal = next(p for p in cards if p["kind"] == "goal")
        r = await c.post(f"/api/proposals/{goal['proposal_id']}", json={"answer": "dismiss", "reason": "не моє"})
        assert r.json()["accepted"] is False
        again = await c.post(f"/api/proposals/{goal['proposal_id']}", json={"answer": "accept"})
        assert again.status_code == 400                                  # already answered
        assert (await web(app_with(db), csrf=False).post(f"/api/proposals/{goal['proposal_id']}",
                                                         json={"answer": "accept"})).status_code == 403
    u = await user(db)
    assert [t["name"] for t in u["topics"] if t["status"] == "active"] == ["Спектр-К"]
    assert [g["text"] for g in u["goals"] if g["active"]] == ["стежити за НРК"]
    dismissed = next(f for f in u["feedback"] if f["target_type"] == "suggestion" and f["reaction"] == "not_useful")
    assert dismissed["text"] == "не моє"
    [conv] = await db.conversations.find({"user_id": DEV_UID}).to_list(None)
    presses = [m["content"] for m in conv["messages"] if m["role"] == "user"]
    assert all(p.startswith("[widget]") for p in presses) and any("так: пропозиція" in p for p in presses)


async def test_the_profile_buttons_change_goals_topics_interests_and_settings(db):
    async with web(app_with(db)) as c:
        r = (await c.post("/api/goals", json={"action": "add", "text": "розуміти ринок РЕБ"})).json()
        assert r["goals"] == ["стежити за НРК", "розуміти ринок РЕБ"]
        r = (await c.post("/api/goals", json={"action": "remove", "text": "стежити за НРК"})).json()
        assert r["goals"] == ["розуміти ринок РЕБ"]
        assert (await c.post("/api/goals", json={"action": "add", "text": ""})).status_code == 422

        await c.post("/api/topics", json={"name": "Спектр-К", "action": "follow"})
        r = (await c.post("/api/topics", json={"name": "Спектр-К", "action": "stop"})).json()
        assert r["topic"]["status"] == "stopped"

        r = (await c.post("/api/profile/not-interested", json={"text": "кораблі"})).json()
        assert r["not_interested"] == "кораблі"

        r = (await c.post("/api/prefs", json={"cards": 1, "turn_off": ["advice"], "mentions": False})).json()
        assert (r["cards"], r["off"], r["mentions"]) == (1, ["advice"], False)
        profile = (await c.get("/api/profile")).json()
        assert profile["agent_prefs"] == {"cards": 1, "off": ["advice"], "mentions": False}
        assert profile["goals"] == ["розуміти ринок РЕБ"]
        assert (await c.post("/api/prefs", json={"cards": 7})).status_code == 422


async def test_a_news_item_is_rated_and_commented(db):
    d = DIGESTS[0]
    digest_id, item_n = str(d["_id"]), 1
    async with web(app_with(db)) as c:
        r = (await c.post("/api/feedback", json={"reaction": "not_useful", "target_type": "digest_item",
                                                 "digest_id": digest_id, "item_n": item_n})).json()
        assert r["reaction"] == "not_useful" and r["confirmation"]
        r = await c.post("/api/feedback", json={"target_type": "digest_item", "digest_id": digest_id,
                                                "item_n": item_n, "text": "це я вже знаю"})
        assert r.status_code == 200
        assert (await c.post("/api/feedback", json={"reaction": "useful", "target_type": "post",
                                                    "post_id": "nope"})).status_code == 400
        r = await c.post("/api/feedback", json={"reaction": "useful", "target_type": "post",
                                                "post_id": str(POSTS[0]["_id"])})
        assert r.status_code == 200 and r.json()["target_type"] == "post"
    [rating] = [f for f in (await user(db))["feedback"] if f["target_type"] == "digest_item"]
    assert rating["reaction"] == "not_useful" and rating["text"] == "це я вже знаю"


async def test_a_reply_of_the_analyst_is_rated(db):
    fake = FakeClaude(message(block("text", text="Відповідь.")))
    async with web(app_with(db, fake)) as c:
        chat_id = (await c.post("/api/chats")).json()["id"]
        done = events((await c.post(f"/api/chats/{chat_id}/messages", json={"content": "Що з НРК?"})).text)[-1]
        url = f"/api/chats/{chat_id}/messages/{done['message_id']}/rating"
        r = (await c.post(url, json={"reaction": "useful"})).json()
        assert r["target_type"] == "answer" and r["reaction"] == "useful"
        thread = (await c.get(f"/api/chats/{chat_id}")).json()["messages"]
        assert thread[-1]["rating"] == "useful"
        await c.post(url, json={"reaction": "not_useful"})                       # changed their mind
        await c.post(url, json={"reaction": "not_useful", "text": "забагато води"})   # and said why
        assert (await c.post(f"/api/chats/{chat_id}/messages/{'0' * 24}/rating",
                             json={"reaction": "useful"})).status_code == 404
    answers = [f for f in (await user(db))["feedback"] if f["target_type"] == "answer"]
    assert [a["reaction"] for a in answers] == ["useful", "not_useful"]     # the comment didn't add a third
    assert "НРК" in (answers[0].get("about") or "") and answers[1]["text"] == "забагато води"
    [conv] = await db.conversations.find({"user_id": DEV_UID, "mcp_session_id": {"$regex": "^web:chat:"}}).to_list(None)
    [said] = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert said["rating"]["reaction"] == "not_useful" and said["rating"]["text"] == "забагато води"
