"""The video side of the pipeline's "news" digest: sides.video (sides.youtube until 2026-11-18) holds summaries of
videos, each item with the video's title, a short summary and the link; the source post's `video` gives the channel
and length. Since 2026-11-18 it has a section of Russian channels too (video_ru, Rutube): their items are claims.
It reads as a digest of its own, "<_id>-video", after ua, ru and world, and is never reported missing; an older
document's sides.youtube reads as the video side, its id kept ("<_id>-youtube"), so earlier ratings still match."""
from __future__ import annotations

import copy
import itertools
import re

import pytest
from bson import ObjectId

from brain.db.repositories import Repos, news_sides
from brain.domain import digests
from brain.mcp_server import schemas, widgets
from brain.tools import handlers
from brain.web.app import FRONTEND
from tests.fixtures.sample_data import USERS
from tests.test_digest_news_shape import ctx, kyiv, news_doc, web

_ids = itertools.count(979_000_001)
LONG, SHORT, GONE, RUTUBE = ObjectId(), ObjectId(), ObjectId(), ObjectId()


def video_post(_id, video_id, channel, seconds, short=False, platform="youtube", side="ua"):
    """A post as the pipeline writes it for a video (News.posts, platform "youtube" or "rutube")."""
    url = (f"https://rutube.ru/video/{video_id}/" if platform == "rutube"
           else f"https://www.youtube.com/watch?v={video_id}")
    return {"_id": _id, "platform": platform, "post_url": url,
            "published_at": "2026-11-15T10:00:00+00:00", "processed": True, "summary": "Про що відео.",
            "source_side": side, "category": "defense_tech", "tags": ["дрони"], "title": "Назва",
            "video": {"video_id": video_id, "channel": channel, "channel_handle": 23640451, "duration_sec": seconds,
                      "is_short": short, "found_by": "hashtag:нрк", "topic_hits": ["нрк"],
                      "text_source": "description", "transcript_lang": None, "platform": platform}}


def video_item(n, title, text, url, post_id):
    return {"n": n, "title": title, "text": text, "source_urls": [url], "source_post_ids": [post_id]}


def yt(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


def video_part(with_ru=True):
    sections = [{"category": "video_ua", "title": "🇺🇦🌍 Українські та світові канали", "items": [
        video_item(1, "Tsar Rockets Are Coming", "Аналітик про оновлення російських ракет.", yt("0Eo9GFgyS0o"), LONG),
        video_item(2, "Самоскид без водія", "Українці створили вантажівку з дистанційним керуванням.",
                   yt("CCO3V7Xgjws"), SHORT),
        video_item(3, "Відео без поста", "Пост уже прибрали з бази.", yt("DOf64Lg3RS8"), GONE)]}]
    if with_ru:
        sections.append({"category": "video_ru", "title": "🇷🇺 Российские каналы — утверждения, не факты", "items": [
            video_item(4, "Россия строит беспилотники", "Канал показує важкий безпілотник Р-75.",
                       "https://rutube.ru/video/2fe1cadf6ea911d950d50f28c739f915/", RUTUBE)]})
    return {"period_start": "x", "posts_considered": 50, "items_count": 4, "parts_html": ["🎬 Відео…"],
            "sections": sections}


def video_doc(at, sides=("ua", "ru", "world", "video"), key="video", with_ru=True):
    doc = news_doc(at, sides=tuple(s for s in sides if s != "video"))
    if "video" in sides:
        doc["sides"][key] = video_part(with_ru)
        doc["sides_done"] = [*(s for s in sides if s != "video"), key]
    return doc


@pytest.fixture
async def world(test_db):
    db = test_db.client[f"{test_db.name}_vd{next(_ids)}"]
    uid = next(_ids)
    await db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": uid, "goals": [], "feedback": [], "interests": {}})
    await db.posts.insert_many([video_post(LONG, "0Eo9GFgyS0o", "NDTV World", 1436),
                                video_post(SHORT, "CCO3V7Xgjws", "Триглав News", 16, short=True),
                                video_post(RUTUBE, "2fe1cadf6ea911d950d50f28c739f915", "Хорошие новости", 84,
                                           short=True, platform="rutube", side="ru")])
    yield db, uid
    await test_db.client.drop_database(db.name)


def test_a_bad_video_length_never_breaks_the_post():
    from brain.db.models import PostDoc
    base = {k: v for k, v in video_post(LONG, "0Eo9GFgyS0o", "NDTV World", 1436).items() if k != "video"}
    for raw, want in ((1436.4, 1436), ("90", 90), ("?", None), (None, None)):
        post = PostDoc.model_validate({**base, "video": {"video_id": "x", "channel": "C", "duration_sec": raw}})
        assert post.video.duration_sec == want


def test_a_run_gives_video_as_a_side_of_its_own_and_an_old_youtube_part_reads_as_video():
    doc = video_doc(kyiv(6))
    part = news_sides(doc)[-1]
    assert (part["side"], part["_id"]) == ("video", f"{doc['_id']}-video")
    assert part["sections"][0]["items"][0]["title"] == "Tsar Rockets Are Coming"
    old = video_doc(kyiv(6), key="youtube", with_ru=False)
    assert [(p["side"], p["_id"]) for p in news_sides(old)][-1] == ("video", f"{old['_id']}-youtube")


async def test_the_web_digest_shows_videos_last_with_title_channel_length_and_russian_claims(world):
    db, uid = world
    doc = video_doc(kyiv(6))
    await db.digests.insert_one(doc)
    async with web(db, uid) as w:
        body = (await w.get("/api/digest")).json()
        assert [d["side"] for d in body["digests"]] == ["ua", "ru", "world", "video"] and body["missing_sides"] == []
        vd = body["digests"][-1]
        assert vd["digest_id"] == f"{doc['_id']}-video" and vd["side_note"] is None
        assert [s["category"] for s in vd["sections"]] == ["video_ua", "video_ru"]
        first, short, gone = vd["sections"][0]["items"]
        assert (first["title"], first["channel"], first["duration_sec"]) == ("Tsar Rockets Are Coming", "NDTV World", 1436)
        assert first["text"] == "Аналітик про оновлення російських ракет." and not first["is_russian_source_claim"]
        assert first["source_urls"] == [yt("0Eo9GFgyS0o")]
        assert (short["channel"], short["duration_sec"]) == ("Триглав News", 16)
        assert (gone["title"], gone["channel"], gone["duration_sec"]) == ("Відео без поста", None, None)
        [ru] = vd["sections"][1]["items"]
        assert ru["is_russian_source_claim"] and (ru["channel"], ru["duration_sec"]) == ("Хорошие новости", 84)
        assert not any(i["is_russian_source_claim"] for i in vd["sections"][0]["items"])
        assert [i["ref"] for s in vd["sections"] for i in s["items"]] == [6, 7, 8, 9]   # after ua 2, ru 1, world 2
        only = (await w.get("/api/digest", params={"side": "video"})).json()
        assert [d["side"] for d in only["digests"]] == ["video"] and only["missing_sides"] == []
        old_name = (await w.get("/api/digest", params={"side": "youtube"})).json()          # the old name still works
        assert [d["digest_id"] for d in old_name["digests"]] == [f"{doc['_id']}-video"]
        assert (await w.get("/api/digest", params={"side": "videos"})).status_code == 422
        # a news item has none of the video's fields
        assert {body["digests"][0]["sections"][0]["items"][0][k] for k in ("title", "channel", "duration_sec")} == {None}


async def test_a_russian_video_is_a_claim_by_its_post_even_outside_video_ru(world):
    db, uid = world
    doc = video_doc(kyiv(6), with_ru=False)
    doc["sides"]["video"]["sections"][0]["items"].append(
        video_item(4, "Россия строит", "Заява.", "https://rutube.ru/video/2fe1cadf6ea911d950d50f28c739f915/", RUTUBE))
    await db.digests.insert_one(doc)
    res = await digests.get_digests(Repos(db), ctx(uid), "video", kyiv(6).date().isoformat())
    view = res.digests[0]
    assert [view.claim(i) for _, items in view.sections for i in items if i.n == 4] == [True]
    assert not any(view.claim(i) for _, items in view.sections for i in items if i.n != 4)


async def test_an_older_youtube_digest_still_shows_as_video_and_keeps_its_ratings(world):
    db, uid = world
    doc = video_doc(kyiv(6), key="youtube", with_ru=False)
    await db.digests.insert_one(doc)
    did = f"{doc['_id']}-youtube"
    found = await digests.find_item(Repos(db), ctx(uid), did, 2)
    assert found and found[0].side == "video" and found[2].title == "Самоскид без водія"
    assert await digests.find_item(Repos(db), ctx(uid), f"{doc['_id']}-video", 2) is None
    async with web(db, uid) as w:
        r = await w.post("/api/feedback", json={"target_type": "digest_item", "digest_id": did, "item_n": 2,
                                                "reaction": "useful"})
        assert r.status_code == 200, r.text
        vd = (await w.get("/api/digest", params={"side": "video"})).json()["digests"][0]
        assert vd["side"] == "video" and vd["digest_id"] == did
        assert {i["n"]: i.get("my_reaction") for s in vd["sections"] for i in s["items"]} == {
            1: None, 2: "useful", 3: None}


async def test_video_is_never_missing_in_the_whole_digest_and_the_morning_does_not_wait_for_it(world):
    db, uid = world
    await db.digests.insert_one(video_doc(kyiv(6), sides=("ua", "ru")))
    repos, day = Repos(db), kyiv(6).date().isoformat()
    res = await digests.get_digests(repos, ctx(uid), "both", day)
    assert [v.side for v in res.digests] == ["ua", "ru"] and res.missing_sides == []
    res = await digests.get_digests(repos, ctx(uid), "video", day)
    assert res.digests == [] and res.missing_sides == ["video"]
    _, key = await digests.morning_ready(repos, ctx(uid), kyiv(9))
    assert key is not None


async def test_a_video_can_be_rated_and_found_by_its_item_id(world):
    db, uid = world
    doc = video_doc(kyiv(6))
    await db.digests.insert_one(doc)
    found = await digests.find_item(Repos(db), ctx(uid), f"{doc['_id']}-video", 4)
    assert found and found[0].side == "video" and found[2].title == "Россия строит беспилотники"
    async with web(db, uid) as w:
        r = await w.post("/api/feedback", json={"target_type": "digest_item", "digest_id": f"{doc['_id']}-video",
                                                "item_n": 2, "reaction": "useful"})
        assert r.status_code == 200, r.text
        vd = (await w.get("/api/digest", params={"side": "video"})).json()["digests"][0]
        assert [i.get("my_reaction") for s in vd["sections"] for i in s["items"] if i["n"] == 2] == ["useful"]


async def test_claude_reads_videos_as_video_with_their_titles(world):
    db, uid = world
    await db.digests.insert_one(video_doc(kyiv(6)))
    res = await digests.get_digests(Repos(db), ctx(uid), "video", kyiv(6).date().isoformat())
    result = handlers._digest_out(res)
    assert result.digests[0].side == "video" and "video" in str(schemas.DigestOut.model_fields["side"].annotation)
    text = widgets.digest_index(result)
    assert " VIDEO UA · video_ua · " in text and "«Tsar Rockets Are Coming: Аналітик" in text
    assert " VIDEO RU · video_ru · " in text


def test_claudes_digest_cards_show_the_video_title_channel_and_claims():
    html = widgets.widget_html("digest.html")
    assert 'video: "Відео, які можуть зацікавити"' in html and "youtube:" not in html
    card = html[html.index("function itemCard("):html.index("function render(")]
    assert 'el("b", {}, item.title)' in card and "item.channel" in card and "duration(item.duration_sec)" in card
    assert 'digest.side === "video") && item.is_russian_source_claim' in card


def test_the_web_app_has_a_video_tab_and_draws_a_video_card():
    js = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert "['video', 'Відео']" in js and "video: 'Відео, які можуть зацікавити'" in js and "'youtube'" not in js
    item = js[js.index("function itemHtml("):js.index("function digestHtml(")]
    assert "side === 'video'" in item and "videoHeadHtml(it)" in item
    assert "!video && (side === 'ru' || it.is_russian_source_claim)" in item       # no «Повний текст» on a video
    head = js[js.index("function videoHeadHtml("):js.index("function sourcesHtml(")]
    assert "esc(it.title)" in head and "it.channel" in head and "i.ytimg.com/vi/${esc(id)}" in head
    assert "'▶ Rutube'" in js
    assert "\\p{Regional_Indicator}" in js         # the 🇺🇦 of the section title isn't drawn as two letters
    assert re.search(r"\.video-thumb\s*\{", (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8"))
