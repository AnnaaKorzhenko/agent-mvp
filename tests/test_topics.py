"""Deep dives and tracked topics (users.topics): what a dive returns, saving findings, following and stopping a
topic, and the same through the MCP tools and the widget's 🔎 button."""
import copy
import itertools
from datetime import datetime, timedelta, timezone

import pytest
from mcp import Client

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import sensitivity, topics
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import DIGEST_UA, P_DRONES, P_GRANTS, USERS

_ids = itertools.count(910_000_001)
FOUND = [{"title": "Brave1: гранти на РЕБ", "url": "https://brave1.gov.ua/news/1", "source": "Brave1",
          "date": "2026-10-28", "summary": "Офіційне оголошення грантів.", "status": "confirmed"},
         {"title": "Огляд", "url": "https://example.org/review", "summary": "Огляд програми.", "status": "claim"}]


@pytest.fixture
async def user(test_db):
    doc = copy.deepcopy(USERS[0])
    doc["_id"] = next(_ids)
    await test_db.users.insert_one(doc)
    return doc["_id"]


def cloud(uid):
    return UserContext(uid, "cloud", "claude", sensitivity.max_for("cloud"))


async def test_a_dive_brings_the_item_its_sources_and_related_posts(repos, user):
    seed = await topics.deep_dive(repos, cloud(user), digest_id=str(DIGEST_UA), item_n=3, query="Brave1")
    assert seed.subject.text == "Brave1 видав гранти на РЕБ." and seed.subject.source_urls
    assert [v.post.id for v in seed.source_posts] == [str(P_GRANTS)]
    assert str(P_GRANTS) not in [v.post.id for v in seed.related_posts]        # not repeated as related
    assert seed.topic is None and seed.goals and seed.searched_for == ["Brave1"]


async def test_a_dive_needs_something_to_dig_into(repos, user):
    with pytest.raises(InvalidInput):
        await topics.deep_dive(repos, cloud(user))
    with pytest.raises(NotFound):
        await topics.deep_dive(repos, cloud(user), digest_id=str(DIGEST_UA), item_n=99)
    seed = await topics.deep_dive(repos, cloud(user), text="морські дрони «Магура»")
    assert seed.subject.text.startswith("морські") and seed.searched_for == ["Магура"]


async def test_findings_are_kept_on_an_explored_topic_without_duplicates(repos, user):
    r = await topics.save_findings(repos, cloud(user), "Brave1 РЕБ", FOUND, digest_id=str(DIGEST_UA), item_n=3)
    assert r.created and r.added_findings == 2 and r.topic.status == "explored"
    assert r.topic.origin.text == "Brave1 видав гранти на РЕБ."
    again = await topics.save_findings(repos, cloud(user), "brave1  реб", FOUND[:1] + [
        {**FOUND[0], "url": "https://brave1.gov.ua/news/2"}])
    assert not again.created and again.added_findings == 1 and again.duplicate_findings == 1
    assert len(again.topic.findings) == 3
    # The next dive into the same item brings the earlier findings back.
    seed = await topics.deep_dive(repos, cloud(user), digest_id=str(DIGEST_UA), item_n=3)
    assert seed.topic.name == "Brave1 РЕБ" and len(seed.topic.findings) == 3
    with pytest.raises(InvalidInput):
        await topics.save_findings(repos, cloud(user), "Brave1 РЕБ", [{**FOUND[0], "url": "brave1.gov.ua"}])


async def test_findings_keep_the_newest_only(repos, user, monkeypatch):
    monkeypatch.setattr(topics, "MAX_FINDINGS", 3)
    for i in range(2):
        await topics.save_findings(repos, cloud(user), "X", [{**FOUND[0], "url": f"https://a.org/{i}-{j}"}
                                                            for j in range(2)])
    [t] = await repos.users.get_topics(user, findings=True)
    assert [f.url for f in t.findings] == ["https://a.org/0-1", "https://a.org/1-0", "https://a.org/1-1"]


async def test_follow_renew_and_stop(repos, test_db, user):
    r = await topics.track_topic(repos, cloud(user), "Спектр-К", specific=["Spektr-K", "Спектр-К"],
                                 broader=["захист відеоканалу від РЕБ"], exclude=["Спектр-ТВ"],
                                 tags=["мілтех", "не з переліку"], goal="Track new technology", days=10)
    t = r.topic
    assert r.created and topics.status_of(t) == "active" and t.specific == ["Спектр-К", "Spektr-K"]
    assert t.tags == ["мілтех"] and r.unknown_tags == ["не з переліку"] and t.goal == "Track new technology"
    assert timedelta(days=9) < topics.utc(t.expires_at) - datetime.now(timezone.utc) <= timedelta(days=10)
    # Light reads leave the findings out; the user record holds one topic per name.
    await topics.save_findings(repos, cloud(user), "спектр-к", FOUND[:1])
    [light] = await repos.users.get_topics(user)
    assert light.findings == [] and len((await repos.users.get_topics(user, findings=True))[0].findings) == 1
    # Expired reads as expired; following again renews it and keeps the terms not passed.
    await test_db.users.update_one({"_id": user}, {"$set": {"topics.0.expires_at": datetime.now(timezone.utc)
                                                            - timedelta(days=1)}})
    assert topics.status_of((await repos.users.get_topics(user))[0]) == "expired"
    renewed = (await topics.track_topic(repos, cloud(user), "Спектр-К")).topic
    assert topics.status_of(renewed) == "active" and renewed.exclude == ["Спектр-ТВ"]
    stopped = (await topics.stop_topic(repos, cloud(user), "СПЕКТР-К")).topic
    assert stopped.status == "stopped" and len((await repos.users.get_topics(user, findings=True))[0].findings) == 1
    with pytest.raises(NotFound):
        await topics.stop_topic(repos, cloud(user), "немає такої")


def test_wants_more():
    assert topics.wants_more("Надо максимально больше информации по єтому пристрою")
    assert topics.wants_more("хочу детальніше") and topics.wants_more("", ["want_more"])
    assert not topics.wants_more("не моя тема") and not topics.wants_more(None)


async def test_the_tools_end_to_end(test_db, user):
    svc = Services(settings=make_settings(DEV_USER_ID=user, TRUST_LEVEL="cloud"), repos=Repos(test_db),
                   audit=AuditLog(test_db), token=lambda: None)
    msg = "🔎 Глибше про #3 «Brave1 видав гранти на РЕБ.»"
    async with Client(create_server(svc)) as c:
        r = await c.call_tool("deep_dive", {"user_message": msg, "digest_id": str(DIGEST_UA), "item_n": 3})
        assert not r.is_error and "web" in r.structured_content["next_steps"]
        r = await c.call_tool("save_findings", {"user_message": msg, "topic": "Brave1 РЕБ", "findings": FOUND,
                                                "digest_id": str(DIGEST_UA), "item_n": 3})
        assert r.structured_content["added_findings"] == 2 and "Brave1 РЕБ" in r.structured_content["confirmation"]
        r = await c.call_tool("track_topic", {"user_message": "так, відстежуй", "name": "Brave1 РЕБ",
                                              "specific": ["Brave1"], "broader": ["гранти на РЕБ"]})
        assert r.structured_content["topic"]["status"] == "active" and "до" in r.structured_content["confirmation"]
        r = await c.call_tool("get_profile", {"user_message": "мої теми"})
        [t] = r.structured_content["topics"]
        assert t["name"] == "Brave1 РЕБ" and t["findings_count"] == 2 and t["findings"] == []
        r = await c.call_tool("record_feedback", {"user_message": "більше про це", "post_id": str(P_DRONES),
                                                  "reaction": "useful", "text": "хочу більше деталей"})
        assert r.structured_content["offer_deep_dive"] is True
        r = await c.call_tool("track_topic", {"user_message": "досить", "name": "brave1 реб", "action": "stop"})
        assert r.structured_content["topic"]["status"] == "stopped"
    doc = await test_db.conversations.find_one({"user_id": user, "messages.tool": "deep_dive"})
    assert any(m.get("content") == msg for m in doc["messages"])                 # the 🔎 message is logged


def test_the_cards_have_the_deeper_button():
    from brain.mcp_server import widgets
    for name in ("digest.html", "search.html"):
        html = widgets.widget_html(name)
        assert "app.sendMessage" in html and "🔎 Глибше про #" in html
