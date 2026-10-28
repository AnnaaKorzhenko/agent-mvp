"""save_insight / get_insights: verbatim storage, private by default, sensitivity filtering, the cap, the
profile count, and the link to the conversation message that carried it."""
import copy
import itertools

import pytest
from mcp import Client

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import insights, profile, sensitivity
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS

_ids = itertools.count(950_000_001)


@pytest.fixture
async def user(test_db):
    uid = next(_ids)
    await test_db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": uid})
    return uid


def cloud(uid):
    return UserContext(uid, "cloud", "claude", sensitivity.max_for("cloud"))


def local(uid):
    return UserContext(uid, "local", "local", "restricted")


async def test_saved_verbatim_and_private_by_default(repos, test_db, user):
    text = "  Наш фонд не інвестує в компанії без серійного виробництва  "
    saved = await insights.save_insight(repos, cloud(user), text, area="Інвестиції", tags=["Фонд", "фонд", " "])
    assert saved.sensitivity == "confidential" and saved.visible_here is False
    [doc] = (await test_db.users.find_one({"_id": user}))["insights"]
    assert doc["text"] == text.strip() and doc["area"] == "інвестиції" and doc["tags"] == ["фонд"]
    assert doc["channel"] == "claude" and doc["id"] == saved.id


async def test_recall_respects_sensitivity(repos, user):
    await insights.save_insight(repos, cloud(user), "Приватна думка про Brave1")                  # confidential
    await insights.save_insight(repos, cloud(user), "Можна згадувати: цікавлять НРК", label="internal")
    seen = await insights.get_insights(repos, cloud(user))
    assert [i.text for i in seen.insights] == ["Можна згадувати: цікавлять НРК"] and seen.hidden == 1
    assert len((await insights.get_insights(repos, local(user))).insights) == 2         # the local client: all


async def test_recall_by_query_and_area(repos, user):
    ctx = local(user)
    await insights.save_insight(repos, ctx, "Партнер — компанія Roboneers", area="люди")
    await insights.save_insight(repos, ctx, "НРК для логістики важливіші за ударні", area="аналіз")
    await insights.save_insight(repos, ctx, "Наземні роботизовані комплекси: стежити за TerMIT", area="аналіз")
    found = await insights.get_insights(repos, ctx, query="роботизовані комплекси")
    assert found.insights[0].text.startswith("Наземні роботизовані")
    assert [i.area for i in (await insights.get_insights(repos, ctx, area="Аналіз")).insights] == ["аналіз"] * 2
    assert (await insights.get_insights(repos, ctx, query="квантові комп'ютери")).insights == []
    with pytest.raises(InvalidInput):
        await insights.get_insights(repos, ctx, query="і на")


async def test_validation_and_cap(repos, test_db, user, monkeypatch):
    with pytest.raises(InvalidInput):
        await insights.save_insight(repos, cloud(user), "   ")
    with pytest.raises(InvalidInput):
        await insights.save_insight(repos, cloud(user), "x", label="secret")
    with pytest.raises(NotFound):
        await insights.save_insight(repos, cloud(424242), "x")
    monkeypatch.setattr(insights, "MAX_INSIGHTS", 2)
    await insights.save_insight(repos, cloud(user), "один")
    await insights.save_insight(repos, cloud(user), "два")
    with pytest.raises(InvalidInput, match="nothing was saved"):
        await insights.save_insight(repos, cloud(user), "три")
    assert len((await test_db.users.find_one({"_id": user}))["insights"]) == 2


async def test_profile_counts_but_never_reads_texts(repos, user):
    await insights.save_insight(repos, cloud(user), "приватне")
    await insights.save_insight(repos, cloud(user), "можна", label="internal")
    p = await profile.get_profile(repos, cloud(user))
    assert (p.insights_saved, p.insights_visible) == (2, 1)
    assert repos.users.PROFILE_PROJECTION.get("insights.sensitivity") == 1 and "insights" not in \
        repos.users.PROFILE_PROJECTION


async def test_tools_end_to_end(test_db, user):
    svc = Services(settings=make_settings(DEV_USER_ID=user, TRUST_LEVEL="cloud"), repos=Repos(test_db),
                   audit=AuditLog(test_db), token=lambda: None)
    async with Client(create_server(svc)) as c:
        r = (await c.call_tool("save_insight", {"text": "Запам'ятай: я веду проєкт з дронами-перехоплювачами",
                                                "area": "робота"})).structured_content
        assert r["sensitivity"] == "confidential" and r["visible_here"] is False and "приватно" in r["confirmation"]
        ok = (await c.call_tool("save_insight", {"text": "Можна згадувати: люблю короткі відповіді",
                                                 "sensitivity": "internal"})).structured_content
        assert ok["visible_here"] is True
        got = (await c.call_tool("get_insights", {})).structured_content
        assert [i["text"] for i in got["insights"]] == ["Можна згадувати: люблю короткі відповіді"]
        assert got["hidden"] == 1
        prof = (await c.call_tool("get_profile", {})).structured_content
        assert prof["insights"] == {"saved": 2, "visible_here": 1}
    # The insight links to the user message that carried it in the conversation log.
    doc = await test_db.users.find_one({"_id": user})
    first = doc["insights"][0]
    conv = await test_db.conversations.find_one({"_id": first["conversation_id"]})
    [said] = [m for m in conv["messages"] if m["id"] == first["message_id"]]
    assert said["role"] == "user" and said["via"] == "insight" and said["content"] == first["text"]
