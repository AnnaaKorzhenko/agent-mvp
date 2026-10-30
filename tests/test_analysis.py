"""Period analysis: the period, the pack (which digests, exact counts, the selection), stored reports, and the
tools with the morning delivery's analysis_due."""
import copy
import itertools
from datetime import date, datetime, timedelta, timezone

import pytest
from bson import ObjectId
from mcp import Client

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import analysis, digests, sensitivity, topics
from brain.domain.errors import InvalidInput
from brain.domain.identity import UserContext
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS, load_samples

_ids = itertools.count(920_000_001)
_dbs = itertools.count()
REPORT = "## Тиждень\n" + "Аналіз розвитку подій щодо НРК і РЕБ, з посиланнями. " * 10


def cloud(uid):
    return UserContext(uid, "cloud", "claude", sensitivity.max_for("cloud"))


@pytest.fixture
async def own(test_db):
    db = test_db.client[f"{test_db.name}_an{next(_dbs)}"]
    await load_samples(db)
    yield db
    await test_db.client.drop_database(db.name)


async def add_user(db):
    doc = copy.deepcopy(USERS[0])
    doc["_id"] = next(_ids)
    await db.users.insert_one(doc)
    return doc["_id"]


def kyiv_noon(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, tzinfo=digests.KYIV).astimezone(timezone.utc)


def digest(day: date, side="ua", trigger="scheduled", items=(), category="defense_tech"):
    return {"_id": ObjectId(), "period": "day", "trigger": trigger, "side": side, "created_at": kyiv_noon(day),
            "sections": [{"category": category, "title": category, "items": [
                {"text": t, "source_urls": [f"https://t.me/x/{i}"], "source_post_ids": [], "n": i}
                for i, t in enumerate(items, 1)]}]}


def test_periods():
    today = date(2026, 10, 30)
    w = analysis.period_for("week", today=today)
    assert (w.start, w.end, w.days) == (date(2026, 10, 24), today, 7)
    assert analysis.period_for("month", today=today).days == 30
    assert w.previous().end == date(2026, 10, 23) and w.previous().days == 7
    assert analysis.period_for("week", "2026-10-01", "2026-10-31").days == 31
    with pytest.raises(InvalidInput):
        analysis.period_for("week", "2026-09-01", "2026-10-31")
    assert analysis.due(date(2026, 11, 2)) == ("week", analysis.Period(date(2026, 10, 26), date(2026, 11, 1)))
    kind, m = analysis.due(date(2026, 11, 1))
    assert kind == "month" and (m.start, m.end) == (date(2026, 10, 1), date(2026, 10, 31))
    assert analysis.month_name(m) == "жовтень 2026" and analysis.due(date(2026, 10, 30)) is None


async def test_the_pack_counts_everything_and_selects_what_matters(own):
    repos = Repos(own)
    today = digests.kyiv_today()
    d1, d2, old = today - timedelta(days=1), today - timedelta(days=2), today - timedelta(days=9)
    await own.digests.insert_many([
        digest(d1, items=["Brave1 видав грант на НРК.", "Новини про погоду."]),
        digest(d1, trigger="on_demand", items=["Не цей: є запланований за той день."]),   # same side and day
        digest(d2, items=["Brave1 видав грант на НРК.", "НРК «Фродо» пройшов випробування."]),  # a repeat
        digest(d2, side="ru", items=["Росіяни заявили про новий НРК."], category="ru_tech"),
        digest(old, items=["НРК у минулому періоді."]),                                      # previous week
        digest(d1, side="ua", trigger="topics", items=["«Спектр-К» знову на фронті.", "Магура V5 уразила ціль."]),
    ])
    me = await add_user(own)
    await own.users.update_one({"_id": me}, {"$set": {"goals": [{"text": "стежити за НРК", "active": True}]}})
    await topics.track_topic(repos, cloud(me), "Спектр-К", specific=["Spektr-K"])
    await topics.save_findings(repos, cloud(me), "Спектр-К", [{"title": "Огляд", "url": "https://a.org/1",
                                                              "summary": "Що відомо.", "status": "claim"}])
    pack = await analysis.build(repos, cloud(me), analysis.period_for("week"))
    texts = [i.text for i in pack.items]
    assert "Не цей: є запланований за той день." not in texts and "Магура V5 уразила ціль." not in texts
    assert texts.count("Brave1 видав грант на НРК.") == 1                         # repeated text: once
    c = pack.counts
    goal = next(g for g in c["goals"] if g["goal"] == "стежити за НРК")
    assert goal["named_items"] == 4 and goal["previous_named_items"] == 1           # counts every item, not the pick
    assert goal["related_items"] >= goal["named_items"]
    assert c["topics"] == [{"topic": "Спектр-К", "status": "active", "items": 1, "previous_items": 0}]
    assert c["by_side"]["ru"] == 2 and c["by_side"]["topics"] == 1          # + the sample ru digest and c["previous_items"] == 1
    assert sum(d["items"] for d in c["per_day"]) == c["items"] and len(c["per_day"]) == 7
    first = pack.items[0] if pack.items[0].topics else next(i for i in pack.items if i.topics)
    assert first.topics == ["Спектр-К"] and all(i.claim for i in pack.items if i.side == "ru")
    assert pack.findings[0]["url"] == "https://a.org/1" and pack.tracked == ["Спектр-К"]
    focused = await analysis.build(repos, cloud(me), analysis.period_for("week"), focus="Фродо")
    assert [i.text for i in focused.items] == ["НРК «Фродо» пройшов випробування."]


async def test_reports_are_stored_apart_from_the_chat_log(own):
    repos = Repos(own)
    me = await add_user(own)
    p = analysis.period_for("week")
    assert await analysis.stored(repos, cloud(me), p, None) is None
    await analysis.save_report(repos, cloud(me), p, None, "Тиждень", ["РЕБ", "НРК"], REPORT)
    r = await analysis.save_report(repos, cloud(me), p, None, "Тиждень, новий", ["РЕБ"], REPORT + " ще")
    got = await analysis.stored(repos, cloud(me), p, None)
    assert got.title == "Тиждень, новий" and got.text.endswith("ще") and got.conversation_id == r.conversation_id
    assert await analysis.stored(repos, cloud(me), p, "НРК") is None               # another focus, another report
    doc = await own.conversations.find_one({"user_id": me, "kind": "analysis"})
    assert doc["message_count"] == 2 and "channel" not in doc and "mcp_session_id" not in doc
    with pytest.raises(InvalidInput):
        await analysis.save_report(repos, cloud(me), p, None, "x", ["y"], "коротко")


async def test_the_tools_and_the_monday_delivery(own, monkeypatch):
    me = await add_user(own)
    svc = Services(settings=make_settings(DEV_USER_ID=me, TRUST_LEVEL="cloud"), repos=Repos(own),
                   audit=AuditLog(own), token=lambda: None)
    week = analysis.period_for("week")
    monkeypatch.setattr(analysis, "due", lambda today=None: ("week", week))
    async with Client(create_server(svc)) as c:
        r = await c.call_tool("get_morning_digest", {"user_message": "[schedule] ранкове зведення"})
        due = r.structured_content["analysis_due"]
        assert due["start_date"] == week.start.isoformat() and "get_analysis" in r.content[0].text
        r = await c.call_tool("get_analysis", {"user_message": "аналіз", "start_date": due["start_date"],
                                               "end_date": due["end_date"]})
        assert r.structured_content["status"] == "to_write" and r.structured_content["items"]
        assert "save_report" in r.structured_content["next_steps"]
        r = await c.call_tool("save_report", {"user_message": "аналіз", "start_date": due["start_date"],
                                              "end_date": due["end_date"], "title": "Тиждень",
                                              "highlights": ["НРК"], "report": REPORT})
        assert "збережено" in r.structured_content["confirmation"]
        r = await c.call_tool("get_analysis", {"user_message": "аналіз за тиждень", "start_date": due["start_date"],
                                               "end_date": due["end_date"]})
        assert r.structured_content["status"] == "stored" and r.structured_content["report"] == REPORT.strip()
        r = await c.call_tool("get_analysis", {"user_message": "аналіз за 2020", "start_date": "2020-01-01",
                                               "end_date": "2020-01-07"})
        assert r.structured_content["status"] == "empty"
    # Written: the next morning doesn't ask again.
    assert await analysis.stored(Repos(own), cloud(me), week, None) is not None
