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
        assert "app.sendMessage" in html and "🔎 Глибше про #" in html and "📌 Відстежувати тему з #" in html


# ---- phase C: the pipeline's keyword record and the tracked-topics digest ---------------------------------

from bson import ObjectId                                                        # noqa: E402

from brain.domain import digests                                                 # noqa: E402
from tests.fixtures.sample_data import NOW, load_samples                         # noqa: E402

_own = itertools.count()
P_SPEKTR = ObjectId("66a0000000000000000000a1")


@pytest.fixture
async def own(test_db):
    """A database of its own with the sample data: the keyword record spans all users, and the topics digest
    must not reach other tests."""
    db = test_db.client[f"{test_db.name}_t{next(_own)}"]
    await load_samples(db)
    yield db
    await test_db.client.drop_database(db.name)


async def add_user(db):
    doc = copy.deepcopy(USERS[0])
    doc["_id"] = next(_ids)
    await db.users.insert_one(doc)
    return doc["_id"]


def test_regions_by_spelling():
    assert topics.regions("Спектр-К") == ["ua", "ru"]
    assert topics.regions("відеоприймач Спектр") == ["ua"] and topics.regions("видеоприёмник") == ["ru"]
    assert topics.regions("Spektr-K") == ["world"] and topics.regions("12") == []
    assert topics.regions("супутниковий зв'язок") == ["ua"]
    assert topics.missing_regions(["Спектр-К"]) == ["world"]
    assert topics.missing_regions(["Спектр-К", "Spektr-K"]) == []


def test_a_topic_is_named_in_the_text_not_a_look_alike():
    t = topics.TopicDoc(id="1", name="Спектр-К", norm="спектр-к", specific=["Спектр-К", "Spektr-K"],
                        exclude=["Спектр-ТВ"])
    assert topics.matched_term(t, "Видеоприемники «Спектр‑К» работают выше 12 ГГц") == "Спектр-К"   # other dash
    assert topics.matched_term(t, "SPEKTR-K receivers") == "Spektr-K"
    assert topics.matched_term(t, "Спектр-КА") is None and topics.matched_term(t, "спектр частот") is None
    assert topics.matched_term(t, "Спектр-К і Спектр-ТВ") is None                    # a look-alike is mentioned


async def test_the_keyword_record_follows_every_users_active_topics(own):
    repos = Repos(own)
    a, b, c = await add_user(own), await add_user(own), await add_user(own)
    await topics.track_topic(repos, cloud(a), "Спектр-К", specific=["Spektr-K", "Spectr-K"], exclude=["Спектр-ТВ"])
    await topics.track_topic(repos, cloud(b), "спектр-к")                           # the same name, another user
    await topics.track_topic(repos, cloud(b), "Магура", specific=["Magura V5"])
    await topics.track_topic(repos, cloud(c), "Ланцет")
    await topics.stop_topic(repos, cloud(c), "Ланцет")                               # stopped: out
    await topics.save_findings(repos, cloud(c), "Герань-5", FOUND[:1])              # explored only: out
    doc = await own.sources.find_one({"platform": "search_keywords", "name": "User interests"})
    assert sorted(doc) == ["_id", "name", "platform", "queries"]                    # exactly the agreed shape
    assert doc["queries"] == [                       # «Магура» alone is an everyday word: not searched
        {"q": "Magura V5", "region": "world"}, {"q": "Spectr-K", "region": "world"},
        {"q": "Spektr-K", "region": "world"}, {"q": "Спектр-К", "region": "ru"}, {"q": "Спектр-К", "region": "ua"}]
    assert await topics.sync_queries(repos) is False                                # unchanged: no write
    # An expired topic drops out at the next sync.
    await own.users.update_one({"_id": b, "topics.norm": "магура"},
                               {"$set": {"topics.$.expires_at": datetime.now(timezone.utc) - timedelta(hours=1)}})
    assert await topics.sync_queries(repos) is True
    queries = (await own.sources.find_one({"platform": "search_keywords"}))["queries"]
    assert not any(q["q"].startswith(("Магура", "Magura")) for q in queries)


def test_a_lone_everyday_word_is_not_searched():
    for term in ("Рассвет", "Rassvet", "Apex.AI", "Рассвєт", "Магура"):
        assert not topics.distinctive(term), term
    for term in ("Рассвет-3", "Бюро 1440", "Спектр-К", "Apex.AI UGV", "российский аналог Starlink", "1440.УЛЬТРА"):
        assert topics.distinctive(term), term


async def test_web_queries_replace_the_names_in_the_record(own):
    repos = Repos(own)
    me = await add_user(own)
    r = await topics.track_topic(repos, cloud(me), "Рассвет", specific=["Rassvet", "Бюро 1440"])
    assert topics.not_searched(r.topic) == ["Рассвет", "Rassvet"]               # the record keeps «Бюро 1440»
    queries = (await own.sources.find_one({"platform": "search_keywords"}))["queries"]
    assert [q["q"] for q in queries] == ["Бюро 1440", "Бюро 1440"]
    with pytest.raises(InvalidInput):
        await topics.track_topic(repos, cloud(me), "Рассвет", queries=["Рассвет"])
    wanted = ["Rassvet satellite constellation Bureau 1440", "Рассвет Бюро 1440 спутники",
              "супутниковий зв'язок Рассвет Шахед"]
    r = await topics.track_topic(repos, cloud(me), "Рассвет", queries=wanted)
    assert topics.not_searched(r.topic) == [] and r.topic.specific == ["Рассвет", "Rassvet", "Бюро 1440"]
    queries = (await own.sources.find_one({"platform": "search_keywords"}))["queries"]
    assert queries == [{"q": wanted[0], "region": "world"}, {"q": wanted[1], "region": "ru"},
                       {"q": wanted[1], "region": "ua"}, {"q": wanted[2], "region": "ua"}]
    # Matching news still uses the short names: «Рассвет» in an item is recognised.
    assert topics.matched_term(r.topic, "Росія запускає супутники «Рассвет»") == "Рассвет"


async def test_each_user_sees_their_own_topics_first(own):
    repos = Repos(own)
    await own.posts.insert_one({"_id": P_SPEKTR, "post_url": "https://t.me/ru_war_channel/9", "channel_id": None,
                                "published_at": NOW.isoformat(), "summary": "Видеоприемники «Спектр-К» для FPV.",
                                "source_side": "ru", "category": "ru_tech", "tags": [], "processed": True})
    await own.digests.insert_one({
        "_id": ObjectId(), "period": "day", "trigger": "topics", "side": "ua", "created_at": NOW,
        "sections": [{"category": "topics", "title": "Теми", "items": [
            {"text": "Нова партія приймачів для FPV.", "source_urls": ["https://x.org/1"],
             "source_post_ids": [P_SPEKTR], "n": 1},                                 # named only in its post
            {"text": "Магура V5 уразила ціль.", "source_urls": ["https://x.org/2"], "source_post_ids": [], "n": 2},
            {"text": "Brave1 оголосив новий грант.", "source_urls": ["https://x.org/3"], "source_post_ids": [],
             "n": 3}]}]})
    me, other = await add_user(own), await add_user(own)
    await topics.track_topic(repos, cloud(me), "Brave1")
    await topics.track_topic(repos, cloud(me), "Спектр-К")
    await topics.track_topic(repos, cloud(other), "Магура")

    res = await digests.get_digests(repos, cloud(me), "both")
    first, *rest = res.digests
    assert first.kind == "topics" and [s.title for s, _ in first.sections] == ["⭐ Brave1", "⭐ Спектр-К"]
    assert [[i.text for i in items] for _, items in first.sections] == [
        ["Brave1 оголосив новий грант."], ["Нова партія приймачів для FPV."]]          # not the other user's
    spektr = first.sections[1][1][0]
    assert first.claim(spektr) and not first.claim(first.sections[0][1][0])          # its source is Russian
    assert first.why(spektr).why == ["тема, яку ви відстежуєте: Спектр-К"]
    # The sides keep their own digests (the newer topics digest has side "ua", yet isn't the ua digest).
    assert [v.digest.id for v in rest] == [str(DIGEST_UA), str((await own.digests.find_one({"side": "ru"}))["_id"])]
    ua = rest[0]
    brave = next(i for s, items in ua.sections for i in items if "Brave1" in i.text)
    assert ua.why(brave).why[0] == "тема, яку ви відстежуєте: Brave1" and ua.sections[0][0].title == "⭐ Для вас"
    only = await digests.get_digests(repos, cloud(me), "topics")
    assert [v.kind for v in only.digests] == ["topics"] and only.missing_sides == []
    # Someone without topics gets the digest as before; nothing from the topics digest.
    plain = await digests.get_digests(repos, cloud(await add_user(own)), "both")
    assert [v.kind for v in plain.digests] == [None, None]
    # The morning delivery's key is the sides' digests: a topics digest doesn't make it new again.
    m = await digests.morning_digest(repos, cloud(me), datetime.now(digests.KYIV).replace(hour=13))
    assert m.status == "new" and str(DIGEST_UA) in (await own.users.find_one({"_id": me}))["morning_digest"]["key"]
    key = (await own.users.find_one({"_id": me}))["morning_digest"]["key"]
    assert "," in key and all(v.digest.id in key for v in rest) and first.digest.id not in key


async def test_track_topic_reports_missing_spellings(test_db, user):
    svc = Services(settings=make_settings(DEV_USER_ID=user, TRUST_LEVEL="cloud"), repos=Repos(test_db),
                   audit=AuditLog(test_db), token=lambda: None)
    async with Client(create_server(svc)) as c:
        r = await c.call_tool("track_topic", {"user_message": "стеж", "name": "Аюдаг-6"})
        assert r.structured_content["missing_regions"] == ["world"]
        r = await c.call_tool("track_topic", {"user_message": "стеж", "name": "Аюдаг-6",
                                              "specific": ["Ayudag-6"]})
        assert r.structured_content["missing_regions"] == []


async def test_broader_phrases_lift_news_that_doesnt_name_the_topic(own):
    from brain.domain import relevance
    repos = Repos(own)
    me = await add_user(own)
    await topics.track_topic(repos, cloud(me), "Спектр-К", specific=["Spektr-K"],
                             broader=["відеоприймач FPV", "захист відеоканалу від РЕБ"], exclude=["Спектр-ТВ"])
    prefs = await relevance.load(repos, cloud(me))

    def why(text):
        return prefs.score(side="ua", category="other", text=text)

    named, near = why("Нові приймачі «Спектр-К» на фронті"), why("Росіяни отримали нові відеоприймачі для FPV")
    assert named.why[0] == "тема, яку ви відстежуєте: Спектр-К" and named.score >= relevance.W_TRACKED
    assert near.why[0] == "близько до теми «Спектр-К»" and relevance.W_TRACKED_NEAR <= near.score < named.score
    assert near.score >= digests.FOR_YOU_MIN                                      # still into «Для вас»
    assert why("Відеоприймач FPV від Спектр-ТВ").why == [] and why("Новини про погоду").why == []
    # Broader phrases never go to the pipeline's keyword record.
    queries = (await own.sources.find_one({"platform": "search_keywords"}))["queries"]
    assert [q["q"] for q in queries] == ["Spektr-K", "Спектр-К", "Спектр-К"]


async def test_renewal_is_offered_before_expiry_once_a_day(own):
    repos = Repos(own)
    me = await add_user(own)
    for name, days in (("Скоро", 2), ("Нещодавно", 1), ("Далеко", 20), ("Давно", 1), ("Зупинена", 1)):
        await topics.track_topic(repos, cloud(me), name, days=days)
    await topics.stop_topic(repos, cloud(me), "Зупинена")
    now = datetime.now(timezone.utc)
    await own.users.update_one({"_id": me, "topics.norm": "нещодавно"},       # expired yesterday: still offered
                               {"$set": {"topics.$.expires_at": now - timedelta(days=1)}})
    await own.users.update_one({"_id": me, "topics.norm": "давно"},           # long expired: not any more
                               {"$set": {"topics.$.expires_at": now - timedelta(days=10)}})
    offered = {r.topic.name: r.days_left for r in await topics.to_renew(repos, cloud(me), now)}
    assert set(offered) == {"Скоро", "Нещодавно"} and offered["Нещодавно"] < 0 <= offered["Скоро"]
    assert await topics.to_renew(repos, cloud(me), now) == []                  # once a day
    assert [r.topic.name for r in await topics.to_renew(repos, cloud(me), now + timedelta(days=1))] == ["Скоро"]
    await topics.track_topic(repos, cloud(me), "Скоро")                         # renewed: 30 more days
    assert await topics.to_renew(repos, cloud(me), now + timedelta(days=2)) == []


async def test_the_digest_asks_to_renew(test_db, user):
    svc = Services(settings=make_settings(DEV_USER_ID=user, TRUST_LEVEL="cloud"), repos=Repos(test_db),
                   audit=AuditLog(test_db), token=lambda: None)
    await topics.track_topic(Repos(test_db), cloud(user), "Аюдаг-6", specific=["Ayudag-6"], days=2)
    async with Client(create_server(svc)) as c:
        r = await c.call_tool("get_digest", {"user_message": "що сьогодні"})
        [renew] = r.structured_content["topics_to_renew"]
        assert renew["name"] == "Аюдаг-6" and not renew["expired"] and "Аюдаг-6" in r.content[0].text
        r = await c.call_tool("get_digest", {"user_message": "ще раз"})
        assert r.structured_content["topics_to_renew"] == []                    # asked already today


async def test_followed_topics_reach_about_user_and_the_learning_job(own):
    from brain.domain import insights
    from brain.learning import profile
    repos = Repos(own)
    me = await add_user(own)
    await topics.track_topic(repos, cloud(me), "Спектр-К", specific=["Spektr-K"], goal="Track new technology",
                             digest_id=str(DIGEST_UA), item_n=3)
    await topics.track_topic(repos, cloud(me), "Ланцет")
    await topics.stop_topic(repos, cloud(me), "Ланцет")                          # stopped: neither
    await topics.save_findings(repos, cloud(me), "Герань-5", FOUND[:1])         # explored only: neither
    about = await insights.about_user(repos, cloud(me))
    [line] = about["tracked"]
    assert line.startswith("Спектр-К (до ") and "Track new technology" in line
    act = await profile.collect(own, me, "internal")
    assert act.followed_topics == [{"name": "Спектр-К", "since": act.followed_topics[0]["since"],
                                    "goal": "Track new technology", "started_from": "Brave1 видав гранти на РЕБ."}]
    assert act.payload()["followed_topics"] == act.followed_topics
    await own.users.update_one({"_id": me}, {"$set": {"feedback": [], "goals": []}})
    assert (await profile.collect(own, me, "internal")).empty   # context only: following doesn't start a run


def test_the_same_search_is_written_once():
    t1 = topics.TopicDoc(id="1", name="НРК Apex.AI", norm="нрк apex.ai", specific=["НРК Apex.AI", "Apex AI НРК"])
    t2 = topics.TopicDoc(id="2", name="X", norm="x", queries=["apex ai нрк", "Бюро 1440"])
    q = topics.keyword_queries([t1, t2])
    assert q == [{"q": "Бюро 1440", "region": "ru"}, {"q": "Бюро 1440", "region": "ua"},
                 {"q": "НРК Apex.AI", "region": "ru"}, {"q": "НРК Apex.AI", "region": "ua"}]
    assert len({(x["q"].lower(), x["region"]) for x in q}) == len(q)


async def test_web_queries_are_deduplicated_on_write(own):
    me = await add_user(own)
    r = await topics.track_topic(Repos(own), cloud(me), "Рассвет",
                                 queries=["Рассвет Бюро 1440", "бюро 1440 рассвет", "Rassvet Bureau 1440"])
    assert r.topic.queries == ["Рассвет Бюро 1440", "Rassvet Bureau 1440"]
