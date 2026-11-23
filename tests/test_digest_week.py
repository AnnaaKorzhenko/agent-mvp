"""The weekly digest: the pipeline's Monday "news" document with period "week" (sides ua, ru, world, video, each
with the week's period_start / period_end), next to the daily ones. get_digest / GET /api/digest read it with
period=week; the default stays the daily digest even when the weekly one is newer. «Лише нове за сьогодні» is about
today's daily digest only, and a weekly digest marks nothing seen."""
from __future__ import annotations

import re

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.tools import catalog
from brain.tools.base import Services
from brain.web.app import FRONTEND
from tests.conftest import make_settings
from tests.test_digest_news_shape import kyiv, web
from tests.test_digest_video import video_doc, world  # noqa: F401 - the fixture

WEEK_START, WEEK_END = "2026-11-16T03:02:27+00:00", "2026-11-23T03:02:27+00:00"


def week_doc(at):
    doc = video_doc(at)
    doc["period"] = "week"
    for part in doc["sides"].values():
        part["period_start"] = WEEK_START
        for s in part["sections"]:
            for i in s["items"]:
                i["text"] = "Тиждень: " + i["text"]
    doc["period_end"] = WEEK_END
    return doc


async def test_the_weekly_digest_by_period_and_the_daily_one_by_default(world):
    db, uid = world
    day, week = video_doc(kyiv(6)), week_doc(kyiv(9))           # the weekly one is the newer
    await db.digests.insert_many([day, week])
    async with web(db, uid) as w:
        body = (await w.get("/api/digest")).json()
        assert body["period"] == "day" and [d["digest_id"] for d in body["digests"]] == \
            [f"{day['_id']}-{s}" for s in ("ua", "ru", "world", "video")]
        body = (await w.get("/api/digest", params={"period": "week"})).json()
        assert body["period"] == "week" and body["missing_sides"] == []
        assert [d["digest_id"] for d in body["digests"]] == [f"{week['_id']}-{s}" for s in ("ua", "ru", "world", "video")]
        first = body["digests"][0]
        assert (first["period_start"], first["period_end"]) == (WEEK_START, WEEK_END)
        assert first["sections"][0]["items"][0]["text"].startswith("Тиждень: ")
        assert body["digests"][-1]["sections"][1]["items"][0]["is_russian_source_claim"]     # video_ru: claims
        ua = (await w.get("/api/digest", params={"period": "week", "side": "ua"})).json()
        assert [d["digest_id"] for d in ua["digests"]] == [f"{week['_id']}-ua"]
        assert (await w.get("/api/digest", params={"period": "month"})).status_code == 422


async def test_no_weekly_digest_yet_and_no_unseen_for_a_week(world):
    db, uid = world
    await db.digests.insert_one(video_doc(kyiv(6)))
    async with web(db, uid) as w:
        body = (await w.get("/api/digest", params={"period": "week"})).json()
        assert body["digests"] == [] and body["missing_sides"] == ["ua", "ru"]
        assert body["note"].startswith("No weekly digest found")
        r = await w.get("/api/digest", params={"period": "week", "unseen_only": "1"})
        assert r.status_code == 400 and "daily digest only" in r.text


async def test_a_weekly_digest_marks_nothing_seen_so_todays_new_items_stay_new(world):
    db, uid = world
    await db.digests.insert_many([video_doc(kyiv(6)), week_doc(kyiv(9))])
    async with web(db, uid) as w:
        await w.get("/api/digest", params={"period": "week"})
        assert not ((await db.users.find_one({"_id": uid})).get("digest_seen") or {}).get("items")
        new = (await w.get("/api/digest", params={"unseen_only": "1"})).json()
        assert sum(len(s["items"]) for d in new["digests"] for s in d["sections"]) == 9      # all of today's


async def test_the_weekly_topics_digest_for_the_week(world):
    db, uid = world
    daily = {"side": "interests", "period": "day", "trigger": "scheduled", "created_at": kyiv(7),
             "sections": [{"category": "x", "title": "Рассвет", "items": [{"n": 1, "text": "День."}]}]}
    weekly = {**daily, "period": "week", "created_at": kyiv(6),
              "sections": [{"category": "x", "title": "Рассвет", "items": [{"n": 1, "text": "Тиждень."}]}]}
    await db.digests.insert_many([daily, weekly])
    repos = Repos(db)
    assert (await repos.digests.latest_topics()).sections[0].items[0].text == "День."
    assert (await repos.digests.latest_topics(period="week")).sections[0].items[0].text == "Тиждень."


def test_claude_and_the_web_chat_can_ask_for_the_weekly_digest(world):
    db, uid = world
    settings = make_settings(DEV_USER_ID=uid)
    spec = catalog.build(Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None))["get_digest"]
    period = spec.input_schema["properties"]["period"]
    assert period["enum"] == ["day", "week"] and period["default"] == "day" and "тиждень" in period["description"]


def test_the_web_app_has_a_week_button_next_to_only_new():
    js = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert re.search(r"Лише нове за сьогодні</button>\s*<button[^>]*data-week-digest[^>]*>За тиждень</button>", js)
    assert "state.digest.week ? 'week' : 'day'" in js and "Тижневого дайджесту ще немає." in js
    assert "p.set('period', 'week')" in (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")
