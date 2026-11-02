"""The scout (docs/AGENT_PLAN.md phase 2): rising names in our posts, reviewed by the model, proposed to the users
they fit."""
import copy
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from brain.agents import scout
from brain.agents.runs import GLOBAL, AgentRuns
from brain.agents.runtime import Runtime
from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import proposals, trends
from tests.fixtures.sample_data import USERS, load_samples
from tests.test_learning import FakeBackend

_dbs = itertools.count()
_ids = itertools.count(940_000_001)


@pytest.fixture
async def own(test_db):
    db = test_db.client[f"{test_db.name}_s{next(_dbs)}"]
    await load_samples(db)
    yield db
    await test_db.client.drop_database(db.name)


def post(summary, side="ua", hours_ago=5, channel=None):
    at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"_id": ObjectId(), "summary": summary, "source_side": side, "channel_id": channel or ObjectId(),
            "post_url": f"https://t.me/c/{ObjectId()}", "published_at": at.isoformat(), "sensitivity": "internal",
            "is_ad": False, "is_duplicate": False, "processed": True, "category": "other", "tags": [], "importance": 3}


ROBOT = ["Компанія ARX Robotics показала наземного робота «Мураха» для евакуації поранених.",
         "Наземний робот «Мураха» від ARX Robotics пройшов випробування на фронті.",
         "ARX Robotics передасть ЗСУ партію НРК «Мураха».",
         "Бригада отримала НРК «Мураха», які виготовляє ARX Robotics.",
         "«Мураха» від ARX Robotics: роботизована евакуація."]


async def seed(db, texts, side="ua", channels=None, hours_ago=5):
    channels = channels or [ObjectId() for _ in texts]
    await db.posts.insert_many([post(t, side, hours_ago, c) for t, c in zip(texts, channels)])


def test_names_not_words_and_not_places():
    terms = trends.terms_of("У Литві та в Алешках тривога. Компанія ARX Robotics показала «Мураха» і Су-34, "
                            "а Іван Петренко з-за кордону сказав про чат-бот.")
    assert {"ARX Robotics", "Мураха", "Су-34", "Іван Петренко"} <= terms
    assert not terms & {"Литві", "Алешках", "з-за", "чат-бот", "У Литві"}


def test_a_name_rising_across_channels_is_a_candidate_and_one_loud_channel_isnt():
    now = datetime.now(timezone.utc)
    spread = [post(t) for t in ROBOT]
    one = ObjectId()
    loud = [post(f"Канал знову про «Тритон-{n}» і «Тритон»", channel=one) for n in range(8)]
    usual = [post("Новини про «Brave1» гранти", hours_ago=24 * d + 60) for d in range(14) for _ in range(2)]
    today_usual = [post("«Brave1» оголосив новий етап") for _ in range(4)]
    found = trends.detect(spread + loud + today_usual, usual, now)
    names = [set(c.terms) for c in found]
    assert any({"ARX Robotics", "Мураха"} <= n for n in names)                  # merged: they travel together
    assert not any("Тритон" in n for n in names)                                # one channel only
    assert not any("Brave1" in n for n in names)                                # 2 a day before, 2 a day now


REVIEW = {"keep": [{"id": 0, "name": "Мураха", "what": "Наземний робот ARX Robotics для евакуації поранених.",
                    "novelty": "new", "new_now": "показали вперше",
                    "kind": "model", "specific": ["Мураха", "Муравей", "Muraha"],
                    "web_queries": ["робот «Мураха» евакуація", "«Мураха»"], "tags": ["наземні роботи"]}]}


async def add_user(db, **fields):
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": next(_ids), "goals": [], "feedback": [], "interests": {"category_scores": {}, "tag_scores": {}},
                "identities": [{"provider": "p", "subject": str(ObjectId())}],
                "last_seen": datetime.now(timezone.utc), **fields})
    await db.users.insert_one(doc)
    return doc["_id"]


async def test_the_scout_finds_reviews_remembers_and_proposes_to_who_it_fits(own):
    await seed(own, ROBOT)
    fits = await add_user(own, goals=[{"text": "Знайти виробників наземних роботів для евакуації", "active": True}])
    other = await add_user(own, goals=[{"text": "Стежити за ринком нафти", "active": True}])
    gone = await add_user(own, goals=[{"text": "Знайти виробників наземних роботів для евакуації", "active": True}],
                          last_seen=datetime.now(timezone.utc) - timedelta(days=90))
    rt = Runtime(own)
    backend = FakeBackend(AuditLog(own), [json.dumps(REVIEW, ensure_ascii=False), '{"fits": []}'])   # review, then the check for «other»
    s = scout.Scout(own, backend, rt, timedelta(hours=6))
    assert await rt.submit_scheduled() == [scout.TRENDS]
    assert await rt.submit_scheduled() == []                                    # queued: not again
    assert (await rt.work_once()).endswith("kept; matching 2 user(s)")
    snap = await AgentRuns(own).latest_output(scout.TRENDS)
    [item] = snap["kept"]
    assert item["web_queries"] == ['робот "Мураха" евакуація']                  # a lone name is not a search
    assert "users" not in json.dumps(snap, default=str)                         # posts only, no user data
    outcomes = [await rt.work_once() for _ in range(2)]
    assert sorted(o.split(", ")[-1] for o in outcomes) == ["0 proposed", "1 proposed"]
    [p] = await Repos(own).users.get_proposals(fits)
    assert p.kind == "topic" and p.title == "Мураха" and p.action.tool == "track_topic"
    assert p.action.args["queries"] == ['робот "Мураха" евакуація'] and p.action.args["goal"].startswith("Знайти")
    assert "5 дописів, 5 каналів" in p.why and "для вас: ціль:" in p.why
    assert await Repos(own).users.get_proposals(other) == [] and await Repos(own).users.get_proposals(gone) == []
    # The next run: the same candidate isn't sent to the model again, and the user isn't offered it twice.
    await s.find(GLOBAL, "test")
    assert sum('"tag_list"' in m[0]["content"] for m in backend.sent) == 1         # one review, not two
    await s.match(fits, "test")
    assert len(await Repos(own).users.get_proposals(fits)) == 1
    # A yes follows it like 📌 would, with the scout's names and searches.
    from tests.test_agents import cloud
    r = await proposals.answer(Repos(own), cloud(fits), p.id, True, mcp_channels=["claude"])
    assert r.done.startswith("відстежується тема «Мураха»")


async def test_what_the_user_has_already_is_not_proposed(own):
    await seed(own, ROBOT)
    rt = Runtime(own)
    s = scout.Scout(own, FakeBackend(AuditLog(own), [json.dumps(REVIEW, ensure_ascii=False)]), rt, timedelta(hours=6))
    await rt.run(scout.TRENDS, GLOBAL, "test")                     # through the runtime: it keeps the snapshot
    goal = [{"text": "Знайти виробників наземних роботів для евакуації", "active": True}]
    follows = await add_user(own, goals=goal, topics=[{
        "id": ObjectId(), "name": "Мураха", "norm": "мураха", "status": "active", "specific": ["Мураха"],
        "expires_at": datetime.now(timezone.utc) + timedelta(days=5)}])
    searched = await add_user(own, goals=goal)
    await Repos(own).sources.set_keyword_queries([{"q": '"Мураха" робот', "region": "ua"}])
    for uid in (follows, searched):
        assert (await s.match(uid, "test")).outcome.endswith("0 proposed")
    await Repos(own).sources.set_keyword_queries([])
    assert (await s.match(searched, "test")).outcome.endswith("1 proposed")


async def test_only_russian_channels_count_less_and_nothing_fresh_means_nothing(own):
    rt = Runtime(own)
    s = scout.Scout(own, FakeBackend(AuditLog(own), []), rt, timedelta(hours=6))
    uid = await add_user(own)
    assert (await s.match(uid, "test")).outcome == "no fresh trends"
    item = {"name": "X", "what": "наземний робот для евакуації", "sides": {"ru": 5}, "examples": [], "tags": []}
    from brain.domain import relevance
    prefs = relevance.Interests(goals=[relevance._topic("ціль", "наземний робот евакуація", relevance.W_GOAL, group="g")])
    ua = scout.fit({**item, "sides": {"ua": 5}}, prefs).score
    assert scout.fit(item, prefs).score == round(ua * scout.RU_ONLY, 3)


async def test_only_new_things_and_the_users_answers_raise_the_bar_for_a_kind(own):
    """2026-11-02: the director got 15 cards in a week (F-16, Мі-8, a refinery, an alert app) and accepted none."""
    texts = {"FP-7": "Fire Point вперше застосувала балістичну ракету «FP-7» по цілі в Росії.",
             "Нептун": "ЗСУ застосували «Нептун» проти комбінату в Ростовській області.",
             "Lynx": "Rheinmetall підписала контракт на виробництво «Lynx» в Україні."}
    for name, text in texts.items():
        await seed(own, [text.replace(name, name) + f" Новина {n}." for n in range(5)])
    goal = [{"text": "Стежити за новими ракетами, балістичними ракетами та контрактами на бронетехніку", "active": True}]
    me = await add_user(own, goals=goal)
    review = {"keep": [
        {"id": n, "name": name, "what": text, "novelty": nov, "new_now": new, "kind": kind, "specific": [name],
         "web_queries": [f"«{name}» ракета"], "tags": []}
        for n, (name, text, nov, new, kind) in enumerate([
            ("FP-7", texts["FP-7"], "development", "перше бойове застосування балістичної ракети", "weapon"),
            ("Нептун", texts["Нептун"], "known", "", "weapon"),
            ("Lynx", texts["Lynx"], "development", "контракт на виробництво бронетехніки", "deal")])]}
    rt = Runtime(own)
    s = scout.Scout(own, FakeBackend(AuditLog(own), [json.dumps(review, ensure_ascii=False)]), rt, timedelta(hours=6))
    from brain.domain import trends as trends_domain
    found = await trends_domain.scan(own)
    for k in review["keep"]:                       # the model answers by the candidates' ids
        k["id"] = next(n for n, c in enumerate(found) if k["name"] in c.terms)
    s.backend = FakeBackend(AuditLog(own), [json.dumps(review, ensure_ascii=False)])
    await rt.run(scout.TRENDS, GLOBAL, "test")
    kept = {i["name"]: i for i in (await AgentRuns(own).latest_output(scout.TRENDS))["kept"]}
    assert "Нептун" not in kept and kept["FP-7"]["new_now"].startswith("перше бойове")       # «known»: dropped
    out = await s.match(me, "test")
    titles = {p.title: p for p in await Repos(own).users.get_proposals(me)}
    assert set(titles) == set(kept) and "нове: перше бойове застосування" in titles["FP-7"].why   # no count cap
    # Two «no» to deals and none accepted: the next deal needs FIT_MIN + 1.0.
    from brain.domain.proposals import answer
    from tests.test_agents import cloud
    await answer(Repos(own), cloud(me), titles["Lynx"].id, False, mcp_channels=["claude"])
    found_props = await Repos(own).users.get_proposals(me)
    assert scout.kind_penalties(found_props) == {"deal": scout.KIND_PENALTY}
    await answer(Repos(own), cloud(me), titles["FP-7"].id, True, mcp_channels=["claude"])
    assert "weapon" not in scout.kind_penalties(await Repos(own).users.get_proposals(me))     # a yes: no penalty


def test_an_item_reviewed_before_the_novelty_check_is_reviewed_again():
    old = {"name": "F-16", "what": "x", "terms": ["F-16"], "specific": ["F-16"]}                 # no novelty
    c = trends.Candidate(terms=["F-16"], posts=5, channels=5, baseline_per_day=0, sides={"ua": 5}, post_ids=[])
    import asyncio

    class NoModel:
        max_label = "internal"
        sent = 0

        async def complete(self, *a, **k):
            NoModel.sent += 1
            from brain.integrations.backends import ModelResult
            return ModelResult('{"keep": []}', 1, 1, "m", "fake", "end_turn")
    kept, dropped, calls = asyncio.run(scout.review(NoModel(), [c], {"kept": [old], "dropped": []}))
    assert NoModel.sent == 1 and kept == [] and dropped                                      # asked again, dropped


async def snapshot(db, kept):
    runs = AgentRuns(db)
    run = await runs.start(scout.TRENDS, GLOBAL, "test")
    await runs.finish(run, "done", "test", output={"at": datetime.now(timezone.utc), "kept": kept, "dropped": []})


FP7 = {"name": "FP-7", "what": "Балістична ракета FP-7 виробництва Fire Point.", "novelty": "development",
       "new_now": "пройшла бойові випробування", "kind": "weapon", "specific": ["FP-7"], "terms": ["FP-7"],
       "web_queries": ['"FP-7" ракета'], "tags": [], "posts": 9, "channels": 5, "sides": {"ua": 9}, "label": "internal"}
DF26 = {**FP7, "name": "DF-26B", "what": "Китайська протикорабельна балістична ракета.", "specific": ["DF-26B"],
        "terms": ["DF-26B"], "new_now": "стрільби по макету авіаносця"}


async def test_a_new_thing_that_misses_on_words_is_checked_by_meaning_once(own):
    """2026-11-02: FP-7 («пройшла бойові випробування») scored 0 for a goal about «нова технологія» that it serves."""
    goal = "Track new technology used by Russians or Ukrainians that creates an advantage"
    me = await add_user(own, goals=[{"text": goal, "active": True}])
    await snapshot(own, [FP7, DF26])
    answer = {"fits": [{"id": 0, "serves": goal, "why": "нова українська балістична ракета"},
                       {"id": 1, "serves": "a goal the user doesn't have", "why": "x"}]}
    backend = FakeBackend(AuditLog(own), [json.dumps(answer, ensure_ascii=False)])
    rt = Runtime(own)
    s = scout.Scout(own, backend, rt, timedelta(hours=6))
    assert (await rt.run(scout.MATCH, me, "test")).endswith("2 trends, 1 fit (1 by meaning), 1 proposed")
    sent = json.loads(backend.sent[0][0]["content"].split("\n", 1)[1])
    assert sent["goals"] == [goal] and [t["name"] for t in sent["things"]] == ["FP-7", "DF-26B"]
    [p] = await Repos(own).users.get_proposals(me)
    assert p.title == "FP-7" and "нова українська балістична ракета" in p.why and p.action.args["goal"] == goal
    run = await own.agent_runs.find_one({"agent": scout.MATCH, "scope": me})
    assert run["model_calls"] == 1 and sorted(run["output"]["checked"]) == sorted(
        scout.item_key(i) for i in (FP7, DF26))                                       # keys only
    # The next run: neither is asked about again (one a yes, already proposed; one a no).
    assert (await rt.run(scout.MATCH, me, "again")).endswith("0 by meaning), 0 proposed")
    assert len(backend.sent) == 1


async def test_no_goals_or_interests_means_no_check(own):
    me = await add_user(own)
    await snapshot(own, [FP7])
    backend = FakeBackend(AuditLog(own), [])
    rt = Runtime(own)
    scout.Scout(own, backend, rt, timedelta(hours=6))
    assert (await rt.run(scout.MATCH, me, "test")).endswith("0 proposed") and backend.sent == []
    run = await own.agent_runs.find_one({"agent": scout.MATCH, "scope": me})
    assert "output" not in run                                                        # nothing asked, nothing kept
