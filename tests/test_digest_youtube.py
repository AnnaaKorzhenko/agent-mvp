"""The youtube side of the pipeline's "news" digest (since 2026-11-11): sides.youtube holds summaries of videos, each
item with the video's title, a short summary and the link; the source post's `video` gives the channel and length.
It reads as a digest of its own, id "<_id>-youtube", after ua, ru and world, and is never reported missing."""
from __future__ import annotations

import copy
import itertools

import pytest
from bson import ObjectId

from brain.db.repositories import Repos, news_sides
from brain.domain import digests
from tests.fixtures.sample_data import USERS
from tests.test_digest_news_shape import ctx, kyiv, news_doc, web

_ids = itertools.count(979_000_001)
LONG, SHORT, GONE = ObjectId(), ObjectId(), ObjectId()


def video_post(_id, video_id, channel, seconds, short=False):
    """A post as the pipeline writes it for a video (News.posts, platform "youtube")."""
    return {"_id": _id, "platform": "youtube", "post_url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": "2026-11-15T10:00:00+00:00", "processed": True, "summary": "Про що відео.",
            "source_side": "ua", "category": "defense_tech", "tags": ["дрони"], "title": "Назва",
            "video": {"video_id": video_id, "channel": channel, "channel_handle": "@" + channel.replace(" ", ""),
                      "youtube_channel_id": "UC123", "duration_sec": seconds, "is_short": short,
                      "found_by": "hashtag:нрк", "topic_hits": ["нрк"], "text_source": "description",
                      "transcript_lang": None}}


def video_item(n, title, text, video_id, post_id):
    return {"n": n, "title": title, "text": text, "source_urls": [f"https://www.youtube.com/watch?v={video_id}"],
            "source_post_ids": [post_id]}


def youtube_doc(at, sides=("ua", "ru", "world", "youtube")):
    doc = news_doc(at, sides=tuple(s for s in sides if s != "youtube"))
    if "youtube" in sides:
        doc["sides"]["youtube"] = {
            "period_start": "x", "posts_considered": 8, "items_count": 3, "parts_html": ["🎬 Відео…"],
            "sections": [{"category": "video_ua", "title": "🇺🇦🌍 Українські та світові канали", "items": [
                video_item(1, "Tsar Rockets Are Coming", "Аналітик про оновлення російських ракет.", "0Eo9GFgyS0o", LONG),
                video_item(2, "Самоскид без водія", "Українці створили вантажівку з дистанційним керуванням.",
                           "CCO3V7Xgjws", SHORT),
                video_item(3, "Відео без поста", "Пост уже прибрали з бази.", "DOf64Lg3RS8", GONE)]}]}
        doc["sides_done"] = list(sides)
    return doc


@pytest.fixture
async def world(test_db):
    db = test_db.client[f"{test_db.name}_yt{next(_ids)}"]
    uid = next(_ids)
    await db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": uid, "goals": [], "feedback": [], "interests": {}})
    await db.posts.insert_many([video_post(LONG, "0Eo9GFgyS0o", "NDTV World", 1436),
                                video_post(SHORT, "CCO3V7Xgjws", "Триглав News", 16, short=True)])
    yield db, uid
    await test_db.client.drop_database(db.name)


def test_a_bad_video_length_never_breaks_the_post():
    from brain.db.models import PostDoc
    base = {k: v for k, v in video_post(LONG, "0Eo9GFgyS0o", "NDTV World", 1436).items() if k != "video"}
    for raw, want in ((1436.4, 1436), ("90", 90), ("?", None), (None, None)):
        post = PostDoc.model_validate({**base, "video": {"video_id": "x", "channel": "C", "duration_sec": raw}})
        assert post.video.duration_sec == want


def test_a_run_gives_youtube_as_a_side_of_its_own():
    doc = youtube_doc(kyiv(6))
    part = news_sides(doc)[-1]
    assert (part["side"], part["_id"]) == ("youtube", f"{doc['_id']}-youtube")
    assert part["items_count"] == 3 and part["sections"][0]["items"][0]["title"] == "Tsar Rockets Are Coming"


async def test_the_web_digest_shows_videos_last_with_title_channel_and_length(world):
    db, uid = world
    doc = youtube_doc(kyiv(6))
    await db.digests.insert_one(doc)
    async with web(db, uid) as w:
        body = (await w.get("/api/digest")).json()
        assert [d["side"] for d in body["digests"]] == ["ua", "ru", "world", "youtube"] and body["missing_sides"] == []
        yt = body["digests"][-1]
        assert yt["digest_id"] == f"{doc['_id']}-youtube" and yt["side_note"] is None
        assert [s["category"] for s in yt["sections"]] == ["video_ua"]
        first, short, gone = yt["sections"][0]["items"]
        assert (first["title"], first["channel"], first["duration_sec"]) == ("Tsar Rockets Are Coming", "NDTV World", 1436)
        assert first["text"] == "Аналітик про оновлення російських ракет." and not first["is_russian_source_claim"]
        assert first["source_urls"] == ["https://www.youtube.com/watch?v=0Eo9GFgyS0o"]
        assert (short["channel"], short["duration_sec"]) == ("Триглав News", 16)
        assert (gone["title"], gone["channel"], gone["duration_sec"]) == ("Відео без поста", None, None)
        assert [i["ref"] for i in yt["sections"][0]["items"]] == [6, 7, 8]          # after ua 2, ru 1, world 2
        only = (await w.get("/api/digest", params={"side": "youtube"})).json()
        assert [d["side"] for d in only["digests"]] == ["youtube"] and only["missing_sides"] == []
        assert (await w.get("/api/digest", params={"side": "video"})).status_code == 422
        # a news item has none of the video's fields
        assert {body["digests"][0]["sections"][0]["items"][0][k] for k in ("title", "channel", "duration_sec")} == {None}


async def test_youtube_is_never_missing_in_the_whole_digest_and_the_morning_does_not_wait_for_it(world):
    db, uid = world
    await db.digests.insert_one(youtube_doc(kyiv(6), sides=("ua", "ru")))
    repos, day = Repos(db), kyiv(6).date().isoformat()
    res = await digests.get_digests(repos, ctx(uid), "both", day)
    assert [v.side for v in res.digests] == ["ua", "ru"] and res.missing_sides == []
    res = await digests.get_digests(repos, ctx(uid), "youtube", day)
    assert res.digests == [] and res.missing_sides == ["youtube"]
    _, key = await digests.morning_ready(repos, ctx(uid), kyiv(9))
    assert key is not None


async def test_a_video_can_be_rated_and_found_by_its_item_id(world):
    db, uid = world
    doc = youtube_doc(kyiv(6))
    await db.digests.insert_one(doc)
    found = await digests.find_item(Repos(db), ctx(uid), f"{doc['_id']}-youtube", 2)
    assert found and found[0].side == "youtube" and found[2].title == "Самоскид без водія"
    async with web(db, uid) as w:
        r = await w.post("/api/feedback", json={"target_type": "digest_item", "digest_id": f"{doc['_id']}-youtube",
                                                "item_n": 2, "reaction": "useful"})
        assert r.status_code == 200, r.text
        yt = (await w.get("/api/digest", params={"side": "youtube"})).json()["digests"][0]
        assert [i.get("my_reaction") for i in yt["sections"][0]["items"] if i["n"] == 2] == ["useful"]
