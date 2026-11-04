"""The agents console (docs/WEB_PLAN.md step 5.4): ADMIN_EMAILS only; runs, suggestions and chat usage counted;
pause and run now through agent_runs, so the agents' worker in the MCP service obeys them."""
import copy
import itertools
from datetime import datetime, timedelta, timezone

import httpx2
import pytest
from bson import ObjectId

from brain.agents.runs import GLOBAL, AgentRuns
from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import proposals as proposals_domain
from brain.tools.base import Services
from brain.web.app import create_app
from brain.web.auth import Auth0Login
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS, load_samples
from tests.test_web import BASE, CLIENT, ISS, FakeAuth0, LocalKeys, sign_in, web

_dbs = itertools.count()
BOSS = ObjectId("6ae0d78217f7db881103fd01")


@pytest.fixture
async def db(test_db):
    d = test_db.client[f"{test_db.name}_adm{next(_dbs)}"]
    await load_samples(d)
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": BOSS, "goals": [], "feedback": [], "insights": [],
                "identities": [{"provider": ISS, "subject": "google-oauth2|director"}]})
    await d.users.insert_one(doc)
    yield d
    await test_db.client.drop_database(d.name)


def app_for(db, admins):
    settings = make_settings(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL=BASE, WEB_CLIENT_ID=CLIENT, WEB_CLIENT_SECRET="s3cret",
                             WEB_SESSION_SECRET="x" * 48, TRUST_LEVEL="cloud", ADMIN_EMAILS=admins)
    svc = Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None)
    auth0 = FakeAuth0()
    login = Auth0Login(settings, keys=LocalKeys(), http=httpx2.AsyncClient(transport=httpx2.MockTransport(auth0.handler)))
    return create_app(settings, svc, login), auth0


async def signed_in(c, auth0):
    r = await sign_in(c, auth0)
    c.cookies.set("brain_session", r.cookies["brain_session"])
    c.headers["X-Brain"] = "1"


async def test_only_admins_see_the_console(db):
    app, auth0 = app_for(db, [])
    async with web(app) as c:
        await signed_in(c, auth0)
        assert (await c.get("/api/me")).json()["admin"] is False
        assert (await c.get("/api/admin/agents")).status_code == 403
        assert (await c.post("/api/admin/agents/scout_trends/pause", json={"paused": True})).status_code == 403
    assert not await AgentRuns(db).paused("scout_trends")


async def test_the_console_counts_runs_suggestions_and_chat_and_controls_the_agents(db):
    now = datetime.now(timezone.utc)
    runs = db.agent_runs
    await runs.insert_many([
        {"agent": "scout_trends", "scope": GLOBAL, "status": "done", "outcome": "3 тренди", "model_calls": 1,
         "tokens_in": 1000, "tokens_out": 200, "created_at": now - timedelta(hours=2), "started_at": now - timedelta(hours=2),
         "finished_at": now - timedelta(hours=2)},
        {"agent": "scout_trends", "scope": GLOBAL, "status": "failed", "error": "BadAnswer: not JSON", "model_calls": 1,
         "tokens_in": 500, "tokens_out": 50, "created_at": now - timedelta(hours=1), "started_at": now - timedelta(hours=1),
         "finished_at": now - timedelta(hours=1)},
        {"agent": "advisor", "scope": BOSS, "status": "done", "created_at": now - timedelta(days=30),
         "finished_at": now - timedelta(days=30)},                                             # outside the period
    ])
    repos = Repos(db)
    for title in ("а", "б"):
        await proposals_domain.propose(repos, BOSS, kind="topic", title=title, agent="scout_match")
    [p] = [p for p in await repos.users.get_proposals(BOSS) if p.title == "а"]
    assert await repos.users.update_proposal(BOSS, ObjectId(p.id), {"status": "accepted"}, status="open")
    await db.conversations.insert_one({"user_id": BOSS, "channel": "web", "session_key": "web:chat:x:1",
                                       "updated_at": now, "messages": [
        {"role": "assistant", "via": "chat", "created_at": now, "input_tokens": 900, "output_tokens": 100,
         "research_input_tokens": 50, "web_searches": 2}]})

    app, auth0 = app_for(db, ["Director@Example.com"])
    async with web(app) as c:
        await signed_in(c, auth0)
        assert (await c.get("/api/me")).json()["admin"] is True
        o = (await c.get("/api/admin/agents", params={"days": 7})).json()
        trends = next(a for a in o["agents"] if a["name"] == "scout_trends")
        assert trends["runs"] == {"done": 1, "failed": 1} and trends["tokens_in"] == 1500
        assert trends["last"]["status"] == "failed" and trends["failures"][0]["error"].startswith("BadAnswer")
        assert trends["today"]["runs"] == 2 and trends["paused"] is None
        assert next(a for a in o["agents"] if a["name"] == "advisor")["runs"] == {}
        [match] = o["proposals"]
        assert (match["agent"], match["made"], match["accepted"], match["open"]) == ("scout_match", 2, 1, 1)
        assert match["accept_rate"] == 1.0
        assert o["chat"] == {"replies": 1, "users": 1, "input_tokens": 900, "output_tokens": 100,
                             "cache_read_tokens": 0, "research_input_tokens": 50, "research_output_tokens": 0,
                             "web_searches": 2}

        assert (await c.post("/api/admin/agents/scout_trends/pause", json={"paused": True})).json()["paused"]
        o = (await c.get("/api/admin/agents")).json()
        trends = next(a for a in o["agents"] if a["name"] == "scout_trends")
        assert trends["paused"]["by"] == "director@example.com" and trends["runs"] == {"done": 1, "failed": 1}
        await c.post("/api/admin/agents/scout_trends/pause", json={"paused": False})

        assert (await c.post("/api/admin/agents/scout_web/run")).json()["for"] == "усі"
        assert (await c.post("/api/admin/agents/learner/run")).json()["for"] == "вас"
        queue = (await c.get("/api/admin/agents")).json()["queue"]
        assert {(q["agent"], q["scope"]) for q in queue} == {("scout_web", "усі"), ("learner", "користувач")}
        assert (await c.post("/api/admin/agents/nobody/run")).status_code == 404
        del c.headers["X-Brain"]
        assert (await c.post("/api/admin/agents/learner/run")).status_code == 403              # CSRF header
    assert not await AgentRuns(db).paused("scout_trends")
    assert await runs.count_documents({"agent": "learner", "scope": BOSS, "status": "queued", "trigger": "console"}) == 1
    actions = [a["details"]["action"] async for a in db.audit_log.find({"event": "admin_action"})]
    assert actions == ["pause", "unpause", "run_now", "run_now"]
