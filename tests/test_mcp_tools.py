"""Starts the MCP server in memory and calls every tool through a real MCP client."""
import copy
import json

import pytest
from mcp import Client

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.mcp_server.server import INSTRUCTIONS, create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import DIGEST_UA, P_DRONES, P_SECRET, P_TOP, USERS

UID = 800_000_001
TOOLS = {"get_digest", "search_posts", "get_post", "get_profile", "record_feedback", "set_goal"}
WRITE_TOOLS = {"record_feedback", "set_goal"}
SECRET_TEXTS = ("Оцінка постачання дронів", "Внутрішня оцінка", "Закритий звіт")


@pytest.fixture(scope="module")
async def svc(test_db):
    doc = copy.deepcopy(USERS[0])
    doc["_id"] = UID
    await test_db.users.insert_one(doc)
    settings = make_settings(DEV_USER_ID=UID, TRUST_LEVEL="cloud")
    return Services(settings=settings, repos=Repos(test_db),
                    audit=AuditLog(test_db), token=lambda: None)


class ConnectPerCall:
    """Opens a fresh in-memory MCP connection for every request: an `async with Client` must start and end in
    the same task, which a pytest fixture's setup and teardown do not."""

    def __init__(self, server):
        self.server = server

    async def list_tools(self):
        async with Client(self.server) as c:
            return await c.list_tools()

    async def call_tool(self, name, args):
        async with Client(self.server) as c:
            return await c.call_tool(name, args)

    async def get_instructions(self):
        async with Client(self.server) as c:
            return c.instructions


@pytest.fixture
def client(svc):
    return ConnectPerCall(create_server(svc))


async def test_lists_typed_tools(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == TOOLS
    for t in tools.values():
        assert t.output_schema and t.description
    assert all(tools[n].annotations.read_only_hint for n in TOOLS - WRITE_TOOLS)
    assert all(tools[n].annotations.read_only_hint is False for n in WRITE_TOOLS)
    assert "user_id" not in json.dumps([t.input_schema for t in tools.values()])     # never an argument


async def test_instructions(client):
    instructions = await client.get_instructions()
    assert instructions == INSTRUCTIONS
    assert "I don't have that in the data" in instructions and "post_url" in instructions


async def test_get_digest(client, test_db):
    r = await client.call_tool("get_digest", {"side": "both"})
    assert not r.is_error
    d = r.structured_content
    ua, ru = d["digests"]
    assert ua["side"] == "ua" and ua["hidden_items"] == 1
    assert all(i["is_russian_source_claim"] for s in ru["sections"] for i in s["items"])
    assert ru["side_note"] and not ua["side_note"]
    assert all(i["source_urls"] and i["source_post_ids"] for s in ua["sections"] for i in s["items"])
    items = [i for dg in d["digests"] for s in dg["sections"] for i in s["items"]]
    assert [i["ref"] for i in items] == list(range(1, len(items) + 1))       # one numbering across both sides
    assert [i["n"] for i in items] == [1, 3, 1]                              # each digest's own numbers
    assert not any(t in json.dumps(d, ensure_ascii=False) for t in SECRET_TEXTS)
    entry = await test_db.audit_log.find_one({"name": "get_digest", "user_id": UID}, sort=[("at", -1)])
    assert entry["kind"] == "tool" and entry["trust"] == "cloud" and entry["items_returned"] == 3
    assert entry["outcome"] == "ok" and entry["args"] == {"side": "both", "date": None}


async def test_search_posts(client):
    r = await client.call_tool("search_posts", {"query": "дрони РЕБ", "days": 30})
    res = r.structured_content
    assert res["search_words"] == ["дрон", "реб"] and res["results"]
    ids = {p["post_id"] for p in res["results"]}
    assert str(P_DRONES) in ids and not {str(P_SECRET), str(P_TOP)} & ids
    assert all(p["post_url"].startswith("https://t.me/") and p["channel"] for p in res["results"])
    assert [p["ref"] for p in res["results"]] == list(range(1, len(res["results"]) + 1))
    empty = (await client.call_tool("search_posts", {"query": "квантовий комп'ютер"})).structured_content
    assert empty["results"] == [] and "don't have" in empty["note"]


async def test_search_posts_validates_arguments(client):
    r = await client.call_tool("search_posts", {"query": "дрони", "days": 90})
    assert r.is_error


async def test_get_post(client, test_db):
    ok = await client.call_tool("get_post", {"post_id": str(P_DRONES)})
    assert ok.structured_content["channel"]["username"] == "ua_defense_news"
    for bad in (str(P_SECRET), "nope"):
        r = await client.call_tool("get_post", {"post_id": bad})
        assert r.is_error and "Post not found" in r.content[0].text
    entry = await test_db.audit_log.find_one({"name": "get_post", "outcome": "error"})
    assert entry and entry["error"] == "Post not found"


async def test_get_profile(client):
    p = (await client.call_tool("get_profile", {})).structured_content
    assert p["goals"] == ["Знайти напрямки для інвестицій у defense tech"]
    assert p["top_categories"][0] == {"side": "ua", "category": "defense_tech", "score": 3}
    assert "feedback" not in json.dumps(p)


async def test_record_feedback(client, test_db):
    r = await client.call_tool("record_feedback", {"post_id": str(P_DRONES), "reaction": "like",
                                                   "text": "корисно"})
    assert r.structured_content["reaction"] == "useful" and r.structured_content["confirmation"]
    entry = (await test_db.users.find_one({"_id": UID}))["feedback"][-1]
    assert entry["channel"] == "dev" and entry["text"] == "корисно" and entry["reaction"] == "useful"
    for args in ({"post_id": "", "reaction": "useful"}, {"post_id": str(P_SECRET), "reaction": "useful"},
                 {"post_id": str(P_DRONES), "reaction": "love"},
                 {"target_type": "answer", "reaction": "useful"}):                    # answer without about
        assert (await client.call_tool("record_feedback", args)).is_error


async def test_record_feedback_every_target_type(client, test_db):
    calls = {
        "post": {"post_id": str(P_DRONES), "reaction": "neutral"},
        "digest_item": {"digest_id": str(DIGEST_UA), "item_n": 1, "reaction": "useful"},
        "answer": {"about": "огляд морських дронів", "cited_post_ids": [str(P_DRONES)], "reaction": "not_useful",
                   "reasons": ["inaccurate"], "text": "не той катер"},
        "suggestion": {"about": "порада стежити за НРК", "reaction": "useful"},
    }
    for target, args in calls.items():
        r = await client.call_tool("record_feedback", {"target_type": target, **args})
        assert not r.is_error, (target, r.content)
        out = r.structured_content
        assert out["target_type"] == target and out["feedback_id"]
        if target in ("answer", "suggestion"):
            assert out["score_changes"] == {}                                # never scored
    follow = await client.call_tool("record_feedback", {"feedback_id": out["feedback_id"], "text": "дякую"})
    assert not follow.is_error and follow.structured_content["score_changes"] == {}
    entry = (await test_db.users.find_one({"_id": UID}))["feedback"][-1]
    assert entry["target_type"] == "suggestion" and entry["text"] == "дякую"


async def test_set_goal(client, test_db):
    r = (await client.call_tool("set_goal", {"action": "add", "text": "стежити за НРК"})).structured_content
    assert r["added"] == "стежити за НРК" and "стежити за НРК" in r["goals"]
    assert (await test_db.users.find_one({"_id": UID}))["goals"][-1]["channel"] == "dev"
    assert (await client.call_tool("set_goal", {"action": "add", "text": "Стежити за НРК"})).is_error
    r = (await client.call_tool("set_goal", {"action": "remove", "text": "стежити за НРК"})).structured_content
    assert r["removed"] == "стежити за НРК" and "стежити за НРК" not in r["goals"]
    err = await client.call_tool("set_goal", {"action": "remove", "text": "немає такої"})
    assert err.is_error and "Current goals" in err.content[0].text
    assert (await client.call_tool("set_goal", {"action": "rename", "text": "x"})).is_error


async def test_not_signed_in_is_refused(test_db):
    svc = Services(settings=make_settings(OAUTH_ISSUER="https://auth.example.com/",
                                          PUBLIC_BASE_URL="https://brain.example.com"),
                   repos=Repos(test_db),
                   audit=AuditLog(test_db), token=lambda: None)
    async with Client(create_server(svc)) as c:
        r = await c.call_tool("get_profile", {})
        assert r.is_error and "Not signed in" in r.content[0].text
