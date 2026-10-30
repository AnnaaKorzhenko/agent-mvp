"""The advisor (docs/AGENT_PLAN.md phase 4): signals of what changed, counted per digest day; advice with one next
step, citing only the news behind it; quiet topics and goals said by code."""
import copy
import itertools
import json
from datetime import datetime, time, timedelta, timezone

import pytest
from bson import ObjectId
from mcp import Client

from brain.agents import advisor
from brain.agents.runs import GLOBAL
from brain.agents.runtime import Runtime
from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import advice, digests, proposals
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS
from tests.test_agents import cloud
from tests.test_learning import FakeBackend

_dbs = itertools.count()
_ids = itertools.count(960_000_001)
TODAY = datetime.now(timezone.utc).astimezone(digests.KYIV).date()


@pytest.fixture
async def own(test_db):
    """No sample digests: every test builds its own days."""
    db = test_db.client[f"{test_db.name}_v{next(_dbs)}"]
    yield db
    await test_db.client.drop_database(db.name)


async def day(db, days_ago: int, texts: list[str]):
    at = datetime.combine(TODAY - timedelta(days=days_ago), time(9), digests.KYIV).astimezone(timezone.utc)
    items = [{"n": n + 1, "text": t, "source_urls": [f"https://t.me/c/{days_ago}{n}"], "source_post_ids": []}
             for n, t in enumerate(texts)]
    await db.digests.insert_one({"_id": ObjectId(), "period": "day", "side": "ua", "trigger": "scheduled",
                                 "created_at": at, "sections": [{"category": "tech", "title": "Т", "items": items}],
                                 "sensitivity": "internal"})


def topic(name, days_ago=20):
    now = datetime.now(timezone.utc)
    return {"id": ObjectId(), "name": name, "norm": name.lower(), "status": "active", "specific": [name],
            "created_at": now - timedelta(days=days_ago), "expires_at": now + timedelta(days=10), "findings": []}


async def add_user(db, **fields):
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": next(_ids), "goals": [], "feedback": [], "insights": [],
                "interests": {"category_scores": {}, "tag_scores": {}},
                "identities": [{"provider": "p", "subject": str(ObjectId())}],
                "last_seen": datetime.now(timezone.utc), **fields})
    doc.pop("learned", None)
    await db.users.insert_one(doc)
    return doc["_id"]


async def test_a_busier_topic_is_counted_per_digest_day(own):
    me = await add_user(own, topics=[topic("Рассвет")])
    for d in range(7, 11):                                                     # the week before: 4 days, quiet
        await day(own, d, ["Інша новина"])
    for d in range(0, 3):                                                      # this week: 3 days, 4 mentions
        await day(own, d, ["Супутники «Рассвет» вийшли на орбіту"] + (["Ще про Рассвет"] if d == 0 else []))
    [s] = await advice.signals(Repos(own), cloud(me))
    assert s.kind == "spike" and s.facts["this_week"] == 4 and s.facts["digest_days_before"] == 4
    assert s.steps == ["deep_dive", "analysis"] and len(s.urls) == 4


async def test_no_rise_without_enough_days_before(own):
    me = await add_user(own, topics=[topic("Рассвет")])
    await day(own, 8, ["Інша новина"])                                         # one day of baseline only
    for d in range(0, 3):
        await day(own, d, ["Про «Рассвет»", "І ще «Рассвет»"])
    assert await advice.signals(Repos(own), cloud(me)) == []


async def test_quiet_needs_two_weeks_of_digests(own):
    learned = {"sensitivity": "internal", "updated_at": datetime.now(timezone.utc),
               "goal_keywords": [{"goal": "Стежити за Барак", "specific": ["Barak MX"], "broader": [], "tags": []}]}
    me = await add_user(own, topics=[topic("Спектр-К")], learned=learned,
                        goals=[{"text": "Стежити за Барак", "active": True}])
    for d in range(0, 5):
        await day(own, d, ["Інша новина"])
    assert await advice.signals(Repos(own), cloud(me)) == []                   # 5 days of data aren't «quiet»
    for d in range(5, 14):
        await day(own, d, ["Інша новина"])
    kinds = sorted(s.kind for s in await advice.signals(Repos(own), cloud(me)))
    assert kinds == ["goal_quiet", "quiet"]


ADVICE = {"advice": [{"signal": None, "title": "«Рассвет» на підйомі", "text": "4 новини за 3 дні замість жодної.",
                      "step": "deep_dive", "source_urls": ["https://t.me/c/00"]},
                     {"signal": None, "title": "друга", "text": "x", "step": "analysis",
                      "source_urls": ["https://made.up/x"]}]}


async def test_the_advisor_advises_with_a_next_step_and_cites_only_the_news(own):
    me = await add_user(own, topics=[topic("Рассвет"), topic("Спектр-К", days_ago=30)])
    for d in range(7, 14):
        await day(own, d, ["Інша новина"])
    for d in range(0, 3):
        await day(own, d, ["Супутники «Рассвет» вийшли на орбіту"] + (["Ще про Рассвет"] if d == 0 else []))
    key = next(s.key for s in await advice.signals(Repos(own), cloud(me)) if s.kind == "spike")
    answer = {"advice": [{**a, "signal": key} for a in ADVICE["advice"]]}
    backend = FakeBackend(AuditLog(own), [json.dumps(answer, ensure_ascii=False)])
    rt = Runtime(own)
    advisor.Advisor(own, backend, rt, timedelta(hours=24))
    assert await rt.submit_scheduled() == [advisor.DAILY]
    assert (await rt.work_once()).startswith("queued")
    assert (await rt.work_once()) == "2 signal(s), 2 advice"
    sent = json.loads(backend.sent[0][0]["content"].split("\n", 1)[1])
    assert [s["kind"] for s in sent["signals"]] == ["spike"]                    # the quiet one is said by code
    ps = {p.action.tool: p for p in await Repos(own).users.get_proposals(me)}
    chat, stop = ps["chat"], ps["stop_topic"]
    assert chat.kind == "advice" and chat.links == ["https://t.me/c/00"]         # the made-up URL: dropped advice
    assert chat.action.args == {"label": "глибше", "message": "Глибше про «Рассвет»: що нового за тиждень"}
    assert stop.title == "«Спектр-К»: тиша" and "14 днів" in stop.why
    # The yes: a request is handed back; stopping stops.
    r = await proposals.answer(Repos(own), cloud(me), chat.id, True, mcp_channels=["claude"])
    assert r.ask == "Глибше про «Рассвет»: що нового за тиждень"
    await proposals.answer(Repos(own), cloud(me), stop.id, True, mcp_channels=["claude"])
    assert {t.name: t.status for t in await Repos(own).users.get_topics(me)}["Спектр-К"] == "stopped"
    # The next day: the same signals aren't advised again.
    assert await advisor.Advisor(own, backend, Runtime(own), timedelta(hours=24)).advise(me, "again") \
        == advisor.Report("nothing new to say")


async def test_answering_advice_through_the_tool_returns_the_request(own):
    me = await add_user(own)
    await proposals.propose(Repos(own), me, kind="advice", title="«Рассвет» на підйомі", why="…", agent="advisor",
                            action={"tool": "chat", "args": {"label": "аналіз за тиждень",
                                                             "message": "Аналіз за тиждень, фокус: «Рассвет»"}})
    [p] = await proposals.open_for(Repos(own), cloud(me), mark_shown=False)
    svc = Services(settings=make_settings(DEV_USER_ID=me, TRUST_LEVEL="cloud"), repos=Repos(own),
                   audit=AuditLog(own), token=lambda: None)
    async with Client(create_server(svc)) as c:
        r = await c.call_tool("answer_proposal", {"user_message": "так", "proposal_id": p.id, "answer": "accept"})
    assert r.structured_content["ask"] == "Аналіз за тиждень, фокус: «Рассвет»"


def test_the_widget_hands_an_advice_request_to_the_chat():
    from brain.mcp_server import widgets
    html = widgets.widget_html("digest.html")
    assert "out.ask" in html and "app.sendMessage" in html
