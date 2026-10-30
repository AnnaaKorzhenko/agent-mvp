"""The agents' runtime (agent_runs), proposals, and the learner's lasting interests and intents (docs/AGENT_PLAN.md
phases 0 and 1)."""
import asyncio
import copy
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest
from mcp import Client

from brain.agents.runs import AgentRuns
from brain.agents.runtime import Budget, Report, Runtime
from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import interests, proposals, relevance, sensitivity, topics
from brain.domain.errors import InvalidInput
from brain.domain.identity import UserContext
from brain.integrations.backends import ModelResult
from brain.learning import auto, profile
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS, load_samples
from tests.test_learning import ANSWER, FakeBackend

_dbs = itertools.count()
_ids = itertools.count(930_000_001)
NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
async def own(test_db):
    """A database of its own with the sample data: runs, budgets and keyword records span all users."""
    db = test_db.client[f"{test_db.name}_g{next(_dbs)}"]
    await load_samples(db)
    yield db
    await test_db.client.drop_database(db.name)


async def add_user(db, **fields):
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": next(_ids), "goals": [], "feedback": [], **fields})
    await db.users.insert_one(doc)
    return doc["_id"]


def cloud(uid):
    return UserContext(uid, "cloud", "claude", sensitivity.max_for("cloud"))


def call(tokens_in=100, tokens_out=20):
    return ModelResult("{}", tokens_in, tokens_out, "fake-model", "fake")


# ---- phase 0: the runtime ---------------------------------------------------------------------------------

async def test_one_run_at_a_time_per_agent_and_user_and_every_run_is_logged(own):
    rt = Runtime(own)
    gate = asyncio.Event()

    async def slow(scope, trigger):
        await gate.wait()
        return Report("ok", [call()])

    rt.register("demo", slow)
    first = asyncio.create_task(rt.run("demo", 1, "test"))
    await asyncio.sleep(0.3)
    assert await rt.run("demo", 1, "test") is None                   # the same work is going on
    other = asyncio.create_task(rt.run("demo", 2, "test"))           # another user is independent
    gate.set()
    assert await first == "ok" and await other == "ok"
    runs = [r async for r in own.agent_runs.find({"agent": "demo"})]
    assert sorted(r["scope"] for r in runs) == [1, 2]
    assert all(r["status"] == "done" and r["model_calls"] == 1 and r["tokens_in"] == 100 for r in runs)


async def test_a_failure_is_logged_and_a_dead_workers_run_expires(own):
    rt = Runtime(own)

    async def broken(scope, trigger):
        raise RuntimeError("model down")

    rt.register("demo", broken)
    assert (await rt.run("demo", 1, "test")).startswith("failed: RuntimeError")
    assert (await own.agent_runs.find_one({"agent": "demo"}))["status"] == "failed"
    # A worker that died holding a run: once its lease is over, the next run starts and the old one is expired.
    runs = AgentRuns(own)
    old = await runs.start("demo", 7, "test", NOW - timedelta(hours=1))
    rt.register("demo", lambda s, t: asyncio.sleep(0, Report("ok")))
    assert await rt.run("demo", 7, "test") == "ok"
    assert (await own.agent_runs.find_one({"_id": old}))["status"] == "expired"


async def test_budgets_hold_per_agent_and_per_user(own):
    rt = Runtime(own)
    rt.register("demo", lambda s, t: asyncio.sleep(0, Report("ok", [call(600, 0)])),
                Budget(runs_per_day=3, scope_tokens_per_day=1000))
    assert await rt.run("demo", 1, "a") == "ok"
    assert await rt.run("demo", 1, "b") == "ok"
    assert await rt.run("demo", 1, "c") is None                       # this user's 1000 tokens are used up
    assert await rt.run("demo", 2, "a") == "ok"                       # the third run today, another user
    assert await rt.run("demo", 3, "a") is None                       # the agent's 3 runs are used up
    # A run that didn't call the model doesn't count.
    rt.register("free", lambda s, t: asyncio.sleep(0, Report("nothing new")), Budget(runs_per_day=1))
    assert [await rt.run("free", u, "a") for u in (1, 2)] == ["nothing new", "nothing new"]


async def test_queued_work_is_debounced_and_runs_when_due(own):
    rt = Runtime(own)
    done = []
    rt.register("demo", lambda s, t: asyncio.sleep(0, done.append((s, t)) or Report("ok")))
    await rt.submit("demo", 5, "first", delay=timedelta(minutes=10))
    await rt.submit("demo", 5, "second", delay=timedelta(minutes=20))   # a burst: one run, after the last
    assert await own.agent_runs.count_documents({"agent": "demo", "status": "queued"}) == 1
    assert await rt.work_once() is None                                # not due yet
    assert await rt.work_once(datetime.now(timezone.utc) + timedelta(minutes=21)) == "ok"
    assert done == [(5, "second")]
    assert await rt.work_once(datetime.now(timezone.utc) + timedelta(minutes=30)) is None


# ---- phase 0: proposals -----------------------------------------------------------------------------------

async def test_a_proposal_is_made_once_shown_a_few_at_a_time_and_goes_stale(own):
    repos = Repos(own)
    me = await add_user(own)
    for n in range(5):
        assert await proposals.propose(repos, me, kind="goal", title=f"ціль {n}", agent="test",
                                       action={"tool": "set_goal", "args": {"text": f"ціль {n}"}}) == "ok"
    assert await proposals.propose(repos, me, kind="goal", title="Ціль  0", agent="test") == "duplicate"
    shown = await proposals.open_for(repos, cloud(me))
    assert [p.title for p in shown] == ["ціль 0", "ціль 1", "ціль 2"]           # SHOW_MAX, oldest first
    assert (await proposals.open_for(repos, cloud(me), mark_shown=False))[0].shown_count == 1
    later = datetime.now(timezone.utc) + timedelta(days=proposals.OPEN_DAYS + 1)
    assert await proposals.open_for(repos, cloud(me), now=later) == []
    assert {p.status for p in await repos.users.get_proposals(me)} == {"stale"}
    with pytest.raises(ValueError):
        await proposals.propose(repos, me, kind="goal", title="x", agent="test", action={"tool": "delete_all"})


async def test_a_yes_adds_the_goal_a_no_closes_it_and_both_are_feedback(own):
    repos = Repos(own)
    me = await add_user(own)
    await proposals.propose(repos, me, kind="goal", title="знайти 3 НРК-компанії", agent="learner",
                            action={"tool": "set_goal", "args": {"text": "знайти 3 НРК-компанії"}})
    await proposals.propose(repos, me, kind="topic", title="Рассвет-3", agent="scout",
                            action={"tool": "track_topic", "args": {"name": "Рассвет-3",
                                                                    "queries": ["«Рассвет-3» спутники"]}})
    goal_p, topic_p = await proposals.open_for(repos, cloud(me))
    r = await proposals.answer(repos, cloud(me), goal_p.id, True, mcp_channels=["claude"])
    assert r.done == "додано ціль «знайти 3 НРК-компанії»"
    assert [g.text for g in (await repos.users.get_profile(me)).goals] == ["знайти 3 НРК-компанії"]
    with pytest.raises(InvalidInput):
        await proposals.answer(repos, cloud(me), goal_p.id, True, mcp_channels=["claude"])
    r = await proposals.answer(repos, cloud(me), topic_p.id, True, mcp_channels=["claude"])
    assert r.done.startswith("відстежується тема «Рассвет-3»")
    [t] = await repos.users.get_topics(me)
    assert t.status == "active" and t.queries == ['"Рассвет-3" спутники']
    fb = (await own.users.find_one({"_id": me}))["feedback"]
    assert [(f["target_type"], f["reaction"], f["about"]) for f in fb] == [
        ("suggestion", "useful", "пропозиція (goal): знайти 3 НРК-компанії"),
        ("suggestion", "useful", "пропозиція (topic): Рассвет-3")]
    await proposals.propose(repos, me, kind="goal", title="інша ціль", agent="learner")
    [p] = await proposals.open_for(repos, cloud(me))
    assert (await proposals.answer(repos, cloud(me), p.id, False, mcp_channels=["claude"])).done == "закрито"
    assert len((await repos.users.get_profile(me)).goals) == 1


async def test_a_goal_the_user_added_meanwhile_closes_its_proposal(own):
    repos = Repos(own)
    me = await add_user(own, goals=[{"text": "Стежити за РЕБ", "active": True}])
    await proposals.propose(repos, me, kind="goal", title="стежити за РЕБ", agent="learner")
    assert await proposals.open_for(repos, cloud(me)) == []
    assert (await repos.users.get_proposals(me))[0].status == "accepted"


async def test_proposals_come_with_the_digest_and_are_answered_through_the_tool(own):
    me = await add_user(own)
    svc = Services(settings=make_settings(DEV_USER_ID=me, TRUST_LEVEL="cloud"), repos=Repos(own),
                   audit=AuditLog(own), token=lambda: None)
    await proposals.propose(svc.repos, me, kind="goal", title="знайти 3 НРК-компанії", agent="learner",
                            why="ви про це говорили", action={"tool": "set_goal", "args": {"text": "знайти 3 НРК-компанії"}})
    async with Client(create_server(svc)) as c:
        r = await c.call_tool("get_digest", {"user_message": "дайджест"})
        [p] = r.structured_content["proposals"]
        assert p["yes"] == "додати ціль" and p["why"] == "ви про це говорили"
        assert p["proposal_id"] in r.content[0].text                            # the model can answer it too
        r = await c.call_tool("answer_proposal", {"user_message": "так, додай", "proposal_id": p["proposal_id"],
                                                  "answer": "accept"})
        assert not r.is_error and r.structured_content["done"] == "додано ціль «знайти 3 НРК-компанії»"
        r = await c.call_tool("get_digest", {"user_message": "дайджест"})
        assert r.structured_content["proposals"] == []


def test_the_digest_widget_shows_proposals():
    from brain.mcp_server import widgets
    html = widgets.widget_html("digest.html")
    assert "answer_proposal" in html and "Не цікаво" in html and "data.proposals" in html


# ---- phase 1: lasting interests and intents ---------------------------------------------------------------

def test_an_inferred_interest_counts_only_once_confirmed():
    day1, day2 = NOW, NOW + timedelta(days=1)
    asked = [{"topic": "НРК", "weight": 3, "origin": "inferred", "signals": ["search"]}]
    [i] = interests.merge([], asked, day1)
    assert not i["confirmed"] and interests.weight(i, day1) is None             # one search: not yet
    [i] = interests.merge([i], [{**asked[0], "signals": ["search", "chat"]}], day1)
    assert not i["confirmed"]                                                    # three signals, one day
    [i] = interests.merge([i], [{**asked[0], "signals": ["rating"]}], day2)
    assert i["confirmed"] and i["evidence"] == {"search": 2, "chat": 1, "rating": 1} and len(i["days"]) == 2
    # A strong signal, or the user's own words, confirm at once.
    [c] = interests.merge([], [{"topic": "РЕБ", "weight": 4, "signals": ["comment"]}], day1)
    [s] = interests.merge([], [{"topic": "ППО", "weight": 4, "origin": "stated"}], day1)
    assert c["confirmed"] and s["confirmed"] and s["evidence"] == {"other": 1}


def test_an_interest_fades_without_fresh_evidence():
    [i] = interests.merge([], [{"topic": "РЕБ", "weight": 4, "signals": ["comment"]}], NOW)
    assert interests.weight(i, NOW) == 4.0
    assert interests.weight(i, NOW + timedelta(days=30)) == 2.0                   # half-life
    assert interests.weight(i, NOW + timedelta(days=90)) is None                  # faded out
    [i] = interests.merge([i], [{"topic": "РЕБ", "weight": 4}], NOW + timedelta(days=30))
    assert interests.weight(i, NOW + timedelta(days=30)) == 2.0                   # no new signal: still fading
    [i] = interests.merge([i], [{"topic": "РЕБ", "weight": 4, "signals": ["rating"]}], NOW + timedelta(days=30))
    assert interests.weight(i, NOW + timedelta(days=30)) == 4.0                   # fresh evidence
    # Interests written before this bookkeeping count, from the time the profile was written.
    legacy = {"interests": [{"topic": "НРК", "weight": 5}], "updated_at": NOW - timedelta(days=30)}
    assert interests.counted(legacy, NOW) == [({"topic": "НРК", "weight": 5}, 2.5)]
    [kept] = interests.merge(legacy["interests"], [{"topic": "нрк", "weight": 5}], NOW, legacy["updated_at"])
    assert kept["confirmed"] and kept["first_seen"] == legacy["updated_at"]


async def test_intents_and_faded_interests_change_the_order(own):
    repos = Repos(own)
    me = await add_user(own, interests={"category_scores": {}, "tag_scores": {}})
    now = datetime.now(timezone.utc)
    learned = {"sensitivity": "internal", "updated_at": now,
               "interests": interests.merge([], [{"topic": "морські дрони", "weight": 5, "signals": ["comment"]},
                                                 {"topic": "космос", "weight": 5, "signals": ["chat"]}], now),
               "intents": interests.merge_intents([{"text": "готує доповідь про РЕБ", "terms": ["РЕБ", "радіоелектронна боротьба"],
                                                    "days": 7}], now)}
    await own.users.update_one({"_id": me}, {"$set": {"learned": learned}})
    prefs = await relevance.load(repos, cloud(me))
    reb = prefs.score(side="ua", category="other", text="Нова система РЕБ на фронті")
    assert reb.why == ["ваш поточний запит: готує доповідь про РЕБ"] and reb.score == relevance.W_INTENT
    assert prefs.score(side="ua", category="other", text="Морські дрони атакували").score > 0
    assert prefs.score(side="ua", category="other", text="Запуск у космос").score == 0     # not confirmed yet
    expired = {**learned, "intents": [{**learned["intents"][0], "until": now - timedelta(hours=1)}]}
    await own.users.update_one({"_id": me}, {"$set": {"learned": expired}})
    prefs = await relevance.load(repos, cloud(me))
    assert prefs.score(side="ua", category="other", text="Нова система РЕБ на фронті").score == 0


async def test_the_learner_keeps_the_counts_proposes_goals_and_sends_no_bookkeeping(own):
    me = await add_user(own, identities=[{"provider": "p", "subject": "s1"}],
                        goals=[{"text": "Знайти напрямки для інвестицій у defense tech", "active": True}],
                        feedback=[{"post_id": None, "reaction": "useful", "target_type": "post", "channel": "claude",
                                   "text": "більше про НРК", "at": datetime.now(timezone.utc)}])
    answer = {**ANSWER, "interests": [{"topic": "НРК", "weight": 5, "origin": "inferred", "signals": ["comment"],
                                       "web_queries": ["«НРК» штурмові групи"]}],
              "intents": [{"text": "вибирає НРК для закупівлі", "terms": ["НРК"], "days": 14}]}
    backend = FakeBackend(AuditLog(own), [json.dumps(answer, ensure_ascii=False)] * 2)
    learner = auto.Learner(own, make_settings(LEARNING_AUTO=True), backend)
    assert (await learner.run_user(me, "comment")).startswith("saved")
    learned = (await own.users.find_one({"_id": me}))["learned"]
    [i] = learned["interests"]
    assert i["confirmed"] and i["evidence"] == {"comment": 1} and i["web_queries"] == ["«НРК» штурмові групи"]
    assert learned["intents"][0]["text"] == "вибирає НРК для закупівлі" and learned["intents"][0]["until"]
    [p] = await Repos(own).users.get_proposals(me)                                # ANSWER's goal candidate
    assert p.kind == "goal" and p.title == "знайти 3 НРК-компанії" and p.action.tool == "set_goal"
    # The next run's payload: the previous interests without the counts, the intents with their end date.
    await own.users.update_one({"_id": me}, {"$push": {"feedback": {
        "post_id": None, "reaction": "useful", "target_type": "post", "channel": "claude", "text": "ще",
        "at": datetime.now(timezone.utc) + timedelta(seconds=1)}}})
    act = await profile.collect(own, me, "internal")
    prev = act.payload()["previous_profile"]
    assert prev["interests"] == [{"topic": "НРК", "weight": 5, "origin": "inferred",
                                  "web_queries": ["«НРК» штурмові групи"]}]
    assert prev["intents"][0]["text"] == "вибирає НРК для закупівлі" and len(prev["intents"][0]["until"]) == 10
    assert (await learner.run_user(me, "comment")).startswith("saved")
    [i] = (await own.users.find_one({"_id": me}))["learned"]["interests"]
    assert i["evidence"] == {"comment": 2}                                        # counted across runs
    assert len(await Repos(own).users.get_proposals(me)) == 1                     # proposed once


def _profile(*items, written=None, label="internal"):
    now = datetime.now(timezone.utc)
    return {"sensitivity": label, "updated_at": written or now, "interests": interests.merge(
        [], [{"topic": t, "weight": w, "signals": ["comment"], "web_queries": q} for t, w, q in items], now)}


def test_the_profile_record_is_shared_ranked_and_quoted():
    a = _profile(("НРК", 5, ["«НРК» штурмові групи", "unmanned ground vehicle Ukraine"]), ("РЕБ", 1, ["РЕБ окопний"]))
    b = _profile(("наземні роботи", 4, ["НРК штурмові «групи»", "\"НРК\" штурмові групи"]))
    queries = interests.profile_queries([a, b])
    assert queries == [{"q": '"НРК" штурмові групи', "region": "ua"},          # both users: one search
                       {"q": "unmanned ground vehicle Ukraine", "region": "world"}]
    # (the same words, quoted differently: one search; «РЕБ» weight 1 is too weak to search)
    old = _profile(("НРК", 5, ["«НРК» штурмові групи"]), written=datetime.now(timezone.utc) - timedelta(days=120))
    for i in old["interests"]:
        i["last_seen"] = datetime.now(timezone.utc) - timedelta(days=120)
    assert interests.profile_queries([old]) == []                                  # faded out
    lone = _profile(("Рассвет", 5, ["Рассвет"]))
    assert interests.profile_queries([lone]) == []                                 # a lone word is never searched


async def test_the_profile_record_has_the_agreed_shape_and_leaves_the_topics_record_alone(own):
    repos = Repos(own)
    await repos.sources.set_keyword_queries([{"q": "Спектр-К", "region": "ua"}])
    me = await add_user(own)
    await own.users.update_one({"_id": me}, {"$set": {"learned": _profile(("НРК", 5, ["«НРК» штурмові групи"]))}})
    secret = await add_user(own)
    await own.users.update_one({"_id": secret}, {"$set": {"learned": _profile(
        ("закрита тема", 5, ["закритий проєкт «Х-1»"]), label="confidential")}})
    assert await topics.sync_profile_queries(repos) is True
    doc = await own.sources.find_one({"platform": "search_keywords", "name": "Profile interests"})
    assert sorted(doc) == ["_id", "name", "platform", "queries"]
    assert doc["queries"] == [{"q": '"НРК" штурмові групи', "region": "ua"}]   # nothing from a confidential profile
    assert await repos.sources.get_keyword_queries() == [{"q": "Спектр-К", "region": "ua"}]
    assert await topics.sync_profile_queries(repos) is False
