"""The web scout (docs/AGENT_PLAN.md phase 3): areas from profiles, searches within budget, only verified
sources, and the finds proposed like trends."""
import copy
import itertools
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId

from brain.agents import scout, scout_web
from brain.agents.runs import GLOBAL, AgentRuns
from brain.agents.runtime import Runtime
from brain.db.repositories import Repos
from brain.domain import interests
from brain.integrations.backends import AnthropicBackend, ModelResult
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS, load_samples
from tests.test_backends import FakeAudit

_dbs = itertools.count()
_ids = itertools.count(950_000_001)
TODAY = datetime.now(timezone.utc)
D = (TODAY - timedelta(days=2)).date().isoformat()
OLD = (TODAY - timedelta(days=40)).date().isoformat()


@pytest.fixture
async def own(test_db):
    db = test_db.client[f"{test_db.name}_w{next(_dbs)}"]
    await load_samples(db)
    yield db
    await test_db.client.drop_database(db.name)


GOAL = "Знайти виробників наземних роботів для евакуації"


def learned(**kw):
    return {"sensitivity": "internal", "updated_at": TODAY,
            "goal_keywords": [{"goal": GOAL, "specific": ["Мураха"], "broader": ["наземні роботи евакуація",
                                                                          "евакуаційні НРК"], "tags": []}],
            "interests": interests.merge([], [{"topic": "автономні НРК", "weight": 4, "signals": ["comment"]}], TODAY),
            **kw}


async def add_user(db, **fields):
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": next(_ids), "goals": [{"text": GOAL, "active": True}], "feedback": [],
                "interests": {"category_scores": {}, "tag_scores": {}},
                "identities": [{"provider": "p", "subject": str(ObjectId())}], "last_seen": TODAY,
                "learned": learned(), **fields})
    await db.users.insert_one(doc)
    return doc["_id"]


def test_areas_are_phrases_never_the_goal_text_and_shared_areas_merge():
    a = scout_web.areas_of(learned(), [GOAL], TODAY)
    assert a == [["наземні роботи евакуація", "евакуаційні НРК"], ["автономні НРК"]]
    assert all(GOAL not in p for area in a for p in area)
    b = [["евакуаційні НРК", "медична евакуація дронами"]]
    merged = scout_web.merge_areas([a, b, [["ринок нафти"]]], 10)
    assert merged[0] == {"phrases": ["наземні роботи евакуація", "евакуаційні НРК", "медична евакуація дронами"],
                         "users": 2}
    assert [m["phrases"] for m in scout_web.merge_areas([a, b, [["ринок нафти"]]], 2)][1:] == [["автономні НРК"]]
    # An old goal mapping (the goal was removed) is not an area.
    assert scout_web.areas_of(learned(), [], TODAY) == [["автономні НРК"]]


RESULTS = ({"url": "https://defence-ua.com/news/robot-x.html", "title": "Robot X", "page_age": None},
           {"url": "https://mil.in.ua/uk/news/robot-x/", "title": "Robot X", "page_age": None},
           {"url": "https://robotx.ua/news", "title": "RobotX", "page_age": None},
           {"url": "https://old.example/x", "title": "old", "page_age": None})


def find(**kw):
    base = {"name": "Robot X", "what": "Новий НРК для евакуації поранених.", "kind": "model",
            "specific": ["Robot X", "Робот Х"], "web_queries": ["НРК «Robot X» евакуація", "«Robot X»"],
            "tags": [], "sources": []}
    return scout_web.Find.model_validate({**base, **kw})


def test_only_real_recent_sources_count():
    two_sites = find(sources=[{"url": "https://defence-ua.com/news/robot-x.html", "date": D},
                              {"url": "https://mil.in.ua/uk/news/robot-x", "date": D}])          # (slash differs)
    assert [s["domain"] for s in scout_web.verify(two_sites, RESULTS, TODAY)] == ["defence-ua.com", "mil.in.ua"]
    invented = find(sources=[{"url": "https://defence-ua.com/news/robot-x.html", "date": D},
                             {"url": "https://made-up.example/robot-x", "date": D}])
    assert scout_web.verify(invented, RESULTS, TODAY) is None                   # one real site is not enough
    maker = find(sources=[{"url": "https://robotx.ua/news", "date": D}])
    assert scout_web.verify(maker, RESULTS, TODAY)                              # the maker's own site
    old = find(sources=[{"url": "https://defence-ua.com/news/robot-x.html", "date": OLD},
                        {"url": "https://mil.in.ua/uk/news/robot-x/", "date": D}])
    assert scout_web.verify(old, RESULTS, TODAY) is None
    undated = find(sources=[{"url": "https://defence-ua.com/news/robot-x.html"},
                            {"url": "https://mil.in.ua/uk/news/robot-x/"}])
    assert scout_web.verify(undated, RESULTS, TODAY) is None


class SearchBackend:
    """Answers each search from a script; records what was sent."""
    name = "fake"
    max_label = "internal"

    def __init__(self, answers, searches=2):
        self.answers, self.sent, self.searches = list(answers), [], searches

    async def search(self, messages, system=None, max_tokens=4096, max_uses=5, sensitivity="internal", user_id=None):
        self.sent.append({"messages": messages, "max_uses": max_uses})
        text = self.answers.pop(0) if self.answers else '{"finds": []}'
        return ModelResult(text, 500, 100, "fake-model", "fake", "end_turn", self.searches, RESULTS)


async def test_the_web_scout_searches_checks_and_its_finds_are_proposed_with_links(own):
    me = await add_user(own)
    await add_user(own, learned=learned(sensitivity="confidential"),
                   goals=[{"text": "Таємна ціль", "active": True}])
    good = {"finds": [find(sources=[{"url": RESULTS[0]["url"], "date": D},
                                    {"url": RESULTS[1]["url"], "date": D}]).model_dump(),
                      find(name="Мураха", specific=["Мураха"], sources=[{"url": RESULTS[0]["url"], "date": D},
                                                                       {"url": RESULTS[1]["url"], "date": D}]).model_dump(),
                      find(name="Fake", specific=["Fake"],
                           sources=[{"url": "https://nowhere.example/a", "date": D}]).model_dump()]}
    backend = SearchBackend([json.dumps(good, ensure_ascii=False)])
    rt = Runtime(own)
    scout.Scout(own, backend, rt, timedelta(hours=6), trends_on=False)
    scout_web.WebScout(own, backend, rt, timedelta(hours=24), searches_per_day=40, max_areas=8)
    await Repos(own).sources.set_keyword_queries([{"q": '"Мураха" робот', "region": "ua"}])
    assert await rt.submit_scheduled() == [scout_web.AGENT]                      # trends off: only the web scout
    out = await rt.work_once()
    assert out.startswith("2 area(s), 2 searched, 1 found, 2 dropped")          # Мураха known, Fake unverified
    sent = [json.loads(x["messages"][0]["content"].split("\n", 1)[1]) for x in backend.sent]
    assert sorted(x["area"][0] for x in sent) == ["автономні НРК", "наземні роботи евакуація"]
    assert all("Мураха робот" in x["known_names"] for x in sent)
    assert "Таємна" not in json.dumps([s["messages"] for s in backend.sent], ensure_ascii=False)
    run = await own.agent_runs.find_one({"agent": scout_web.AGENT})
    assert run["web_searches"] == 4 and run["output"]["kept"][0]["origin"] == "web"
    await rt.work_once()                                                         # scout_match for the users
    await rt.work_once()
    [p] = await Repos(own).users.get_proposals(me)
    assert p.title == "Robot X" and p.links == [RESULTS[0]["url"], RESULTS[1]["url"]]
    assert "Знайдено в інтернеті, не в наших каналах: defence-ua.com, mil.in.ua" in p.why
    assert p.action.args["queries"] == ['НРК "Robot X" евакуація', '"Robot X"']


async def test_the_search_budget_holds_across_runs(own):
    await add_user(own)
    backend = SearchBackend([], searches=5)
    rt = Runtime(own)
    w = scout_web.WebScout(own, backend, rt, timedelta(hours=24), searches_per_day=7, max_areas=8)
    assert (await rt.run(scout_web.AGENT, GLOBAL, "a")).startswith("2 area(s), 2 searched")
    assert backend.sent[1]["max_uses"] == 2                                     # 7 - 5 left for the second area
    assert await rt.run(scout_web.AGENT, GLOBAL, "b") is None                     # today's 7 (10) are used up
    assert (await AgentRuns(own).usage(agent=scout_web.AGENT)).web_searches == 10
    assert w.searches_per_day == 7


# ---- the backend's search loop ------------------------------------------------------------------------

class SearchClient:
    """Pauses once, then ends; the second response has the search results."""
    def __init__(self, reject_callers=False):
        self.requests, self.reject = [], reject_callers
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        import anthropic
        self.requests.append(kw)
        tool = kw["tools"][0]
        if self.reject and "allowed_callers" not in tool:
            raise anthropic.BadRequestError("set allowed_callers to direct", response=_response(), body=None)
        usage = SimpleNamespace(input_tokens=100, output_tokens=10, server_tool_use=SimpleNamespace(web_search_requests=1))
        result = SimpleNamespace(type="web_search_tool_result", content=[
            SimpleNamespace(type="web_search_result", url="https://a.example/1", title="A", page_age="October 28, 2026")])
        if len([r for r in self.requests if "allowed_callers" in r["tools"][0] or not self.reject]) == 1:
            return SimpleNamespace(stop_reason="pause_turn", content=[SimpleNamespace(type="text", text="шукаю "), result],
                                   model=kw["model"], usage=usage)
        return SimpleNamespace(stop_reason="end_turn", content=[result, SimpleNamespace(type="text", text='{"finds": []}')],
                               model=kw["model"], usage=usage)


def _response():
    import httpx2                                   # (the SDK's HTTP client)
    return httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


async def test_the_backend_resumes_a_paused_search_and_returns_the_real_results():
    audit, client = FakeAudit(), SearchClient()
    backend = AnthropicBackend(audit, make_settings(ANTHROPIC_API_KEY="test", ANTHROPIC_MODEL="m"), client=client)
    r = await backend.search([{"role": "user", "content": "x"}], system="s", max_uses=3)
    assert len(client.requests) == 2 and client.requests[1]["messages"][-1]["role"] == "assistant"
    assert client.requests[0]["tools"] == [{"type": "web_search_20260318", "name": "web_search", "max_uses": 3}]
    assert r.text == 'шукаю {"finds": []}' and r.web_searches == 2 and r.input_tokens == 200
    assert r.sources == ({"url": "https://a.example/1", "title": "A", "page_age": "October 28, 2026"},)
    assert audit.calls[-1]["web_searches"] == 2


async def test_a_model_without_tool_calling_from_code_gets_the_direct_search():
    client = SearchClient(reject_callers=True)
    backend = AnthropicBackend(FakeAudit(), make_settings(ANTHROPIC_API_KEY="test"), client=client)
    await backend.search([{"role": "user", "content": "x"}])
    assert client.requests[-1]["tools"][0]["allowed_callers"] == ["direct"]


async def test_the_local_backend_cant_search():
    from brain.integrations.backends import LocalPipelineBackend
    with pytest.raises(NotImplementedError):
        await LocalPipelineBackend(FakeAudit()).search([{"role": "user", "content": "x"}])


async def test_a_find_our_own_news_already_has_is_dropped(own):
    """2026-10-30: the web scout offered Helsing AD-1 Inferno, which our digests had carried three days before."""
    await add_user(own)
    good = {"finds": [find(name="Helsing AD-1 Inferno", specific=["Хельсінг AD-1"],
                           sources=[{"url": RESULTS[0]["url"], "date": D},
                                    {"url": RESULTS[1]["url"], "date": D}]).model_dump()]}
    rt = Runtime(own)
    w = scout_web.WebScout(own, SearchBackend([json.dumps(good, ensure_ascii=False)] * 2), rt, timedelta(hours=24),
                           searches_per_day=40, max_areas=1)
    areas = await w.areas(TODAY)
    assert (await w.search_areas(areas, TODAY, 10))[0][0]["name"] == "Helsing AD-1 Inferno"     # nothing has it yet
    from tests.test_scout import post
    await own.posts.insert_one(post("Німецька компанія Helsing представила реактивний перехоплювач.", hours_ago=72))
    kept, dropped, _, _ = await w.search_areas(areas, TODAY, 10)
    assert kept == [] and dropped == 1
    assert scout_web.name_patterns(["Unmanned Ground Vehicle"]) == ["Unmanned\\ Ground\\ Vehicle"]   # no generic parts
