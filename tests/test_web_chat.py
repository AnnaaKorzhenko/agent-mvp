"""The web chat (docs/WEB_PLAN.md step 5.2): the tool loop over the brain's handlers, streamed as server-sent
events, and every message in `conversations`. Claude is a scripted fake client: no network, no cost."""
import copy
import itertools
import json
from types import SimpleNamespace

import httpx2
import pytest

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.integrations.backends import AnthropicBackend
from brain.tools.base import Services
from brain.web.app import create_app
from brain.web.chat import SESSION_PREFIX, as_turns, echo
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS, load_samples

DEV_UID = 820_000_002
_dbs = itertools.count()


def block(type, **kw):
    return SimpleNamespace(type=type, **kw)


def message(*content, stop="end_turn", tokens=(100, 20)):
    usage = SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1], cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, server_tool_use=None)
    return SimpleNamespace(content=list(content), stop_reason=stop, usage=usage, model="claude-sonnet-5-5")


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for b in self.msg.content:
            if b.type == "text":
                yield SimpleNamespace(type="text", text=b.text)

    async def get_final_message(self):
        return self.msg


class FakeClaude:
    """Answers each round with the next scripted message; keeps every request."""

    def __init__(self, *rounds, searches=()):
        self.rounds = list(rounds)
        self.searches = list(searches)            # the research model's answers (messages.create)
        self.requests: list[dict] = []
        self.created: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream, create=self.create))

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return self.searches.pop(0)

    def stream(self, **kwargs):
        self.requests.append(copy.deepcopy({k: v for k, v in kwargs.items() if k != "messages"})
                             | {"messages": list(kwargs["messages"])})
        return FakeStream(self.rounds.pop(0))


def search_then_answer():
    return [message(block("text", text="Шукаю в постах."),
                    block("tool_use", id="tu_1", name="search_posts", input={"query": "дрони", "days": 30}),
                    stop="tool_use"),
            message(block("text", text="Ось що є: [пост](https://t.me/x/1)."))]


@pytest.fixture
async def db(test_db):
    d = test_db.client[f"{test_db.name}_c{next(_dbs)}"]
    await load_samples(d)
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": DEV_UID, "goals": [{"text": "стежити за НРК", "active": True, "source": "user"}],
                "feedback": [], "insights": []})
    await d.users.insert_one(doc)
    yield d
    await test_db.client.drop_database(d.name)


def app_with(db, fake, **overrides):
    settings = make_settings(DEV_USER_ID=DEV_UID, **overrides)
    svc = Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None)
    backend = AnthropicBackend(svc.audit, settings, client=fake, model=settings.web_chat_model) if fake else None
    return create_app(settings, svc, backend=backend)


def web(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8001",
                              headers={"X-Brain": "1"})


def events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


async def test_a_question_runs_a_tool_streams_the_answer_and_is_logged(db):
    fake = FakeClaude(*search_then_answer())
    async with web(app_with(db, fake)) as c:
        chat_id = (await c.post("/api/chats")).json()["id"]
        r = await c.post(f"/api/chats/{chat_id}/messages", json={"content": "Що нового про дрони?"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        ev = events(r.text)
        assert [e["type"] for e in ev] == ["text", "tool", "text", "text", "done"]
        assert ev[1]["name"] == "search_posts" and ev[-1]["stop_reason"] == "end_turn"
        assert "".join(e["text"] for e in ev if e["type"] == "text") == \
            "Шукаю в постах.\n\nОсь що є: [пост](https://t.me/x/1)."

        listed = (await c.get("/api/chats")).json()["chats"]
        assert listed == [{"id": chat_id, "title": "Що нового про дрони?", "updated_at": listed[0]["updated_at"]}]
        thread = (await c.get(f"/api/chats/{chat_id}")).json()["messages"]
        assert [(m["role"], m["content"][:10]) for m in thread] == [("user", "Що нового "), ("assistant", "Шукаю в по")]
        assert (await c.get(f"/api/chats/{'0' * 24}")).status_code == 404
        assert (await c.get("/api/chats/not-an-id")).status_code == 404

    first = fake.requests[0]
    names = {t["name"] for t in first["tools"]}
    assert "search_posts" in names and "log_turn" not in names and "web_research" in names
    assert not any(t.get("type", "").startswith("web_search") for t in first["tools"])   # searches: web_research
    schemas = json.dumps([t["input_schema"] for t in first["tools"]])
    assert '"title": "' not in schemas and '{"type": "null"}' not in schemas and "user_message" not in schemas
    tools = {t["name"]: t for t in first["tools"]}
    finding = tools["save_findings"]["input_schema"]["$defs"]["FindingIn"]["properties"]
    assert {"title", "url", "summary"} <= set(finding)                       # an argument named title stays
    assert "title" in tools["save_report"]["input_schema"]["properties"]
    assert first["model"] == "claude-sonnet-5-5" and first["thinking"] == {"type": "adaptive"}
    static, personal = first["system"]
    assert static["cache_control"] == {"type": "ephemeral", "ttl": "1h"} and "news brain" in static["text"]
    assert "Today is" in personal["text"] and "cache_control" not in personal     # per user: after the shared prefix
    second = fake.requests[1]["messages"]
    assert second[-1]["role"] == "user" and second[-1]["content"][0]["tool_use_id"] == "tu_1"

    [conv] = await db.conversations.find({"user_id": DEV_UID}).to_list(None)
    assert conv["mcp_session_id"] == SESSION_PREFIX + chat_id
    msgs = conv["messages"]
    users = [m for m in msgs if m["role"] == "user" and m["via"] == "chat"]
    assert len(users) == 1                                     # the tool's copy links to it, no duplicate
    assert {m["via"] for m in msgs if m["role"] == "user"} == {"chat", "search_query"}
    [tool] = [m for m in msgs if m["role"] == "tool"]
    assert tool["tool"] == "search_posts"
    [reply] = [m for m in msgs if m["role"] == "assistant"]
    assert reply["written_by"] == "model" and reply["rounds"] == 2 and reply["tools"] == ["search_posts"]
    assert reply["input_tokens"] == 200 and reply["output_tokens"] == 40 and reply["model"] == "claude-sonnet-5-5"
    assert await db.audit_log.count_documents({"event": "model_call", "details.outcome": "ok"}) == 2


async def test_the_next_message_carries_the_thread_and_the_budget_stops_it(db):
    fake = FakeClaude(*search_then_answer(), message(block("text", text="Друга відповідь.")))
    async with web(app_with(db, fake, WEB_CHAT_TOKENS_PER_DAY=300)) as c:
        chat_id = (await c.post("/api/chats")).json()["id"]
        await c.post(f"/api/chats/{chat_id}/messages", json={"content": "Перше питання"})
        await c.post(f"/api/chats/{chat_id}/messages", json={"content": "А друге?"})
        sent = fake.requests[-1]["messages"]
        assert [m["role"] for m in sent] == ["user", "assistant", "user"]
        assert sent[0]["content"] == "Перше питання" and sent[-1]["content"] == "А друге?"
        r = await c.post(f"/api/chats/{chat_id}/messages", json={"content": "Третє"})   # 360 tokens used of 300
        assert events(r.text) == [{"type": "error", "detail": "Денний ліміт чату вичерпано. Спробуйте завтра."}]
    assert len(fake.requests) == 3


async def test_a_bad_tool_input_goes_back_to_the_model_and_the_rounds_are_capped(db):
    bad = message(block("tool_use", id="tu_1", name="search_posts", input={"days": "lots"}), stop="tool_use")
    again = message(block("tool_use", id="tu_2", name="get_profile", input={}), stop="tool_use")
    fake = FakeClaude(bad, again)
    async with web(app_with(db, fake, WEB_CHAT_MAX_ROUNDS=2)) as c:
        chat_id = (await c.post("/api/chats")).json()["id"]
        ev = events((await c.post(f"/api/chats/{chat_id}/messages", json={"content": "?"})).text)
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and result["content"].startswith("Invalid arguments")
    assert ev[-1] == {"type": "done", "message_id": ev[-1]["message_id"], "stop_reason": "max_rounds"}
    assert "забагато кроків" in "".join(e.get("text", "") for e in ev)


async def test_without_a_key_the_chat_is_off(db):
    async with web(app_with(db, None)) as c:
        assert (await c.post("/api/chats")).status_code == 503
        assert (await c.get("/api/chats")).status_code == 503


def test_turns_alternate_and_a_fallback_drops_the_declined_attempt():
    logged = [{"role": "assistant", "content": "привіт"}, {"role": "user", "content": "a"},
              {"role": "user", "content": "b"}, {"role": "assistant", "content": ""},
              {"role": "assistant", "content": "c"}]
    assert as_turns(logged, 10) == [{"role": "user", "content": "a\n\nb"}, {"role": "assistant", "content": "c"}]
    content = [block("thinking"), block("text", text="part"), block("tool_use"), block("fallback"),
               block("text", text="rest"), block("tool_use")]
    assert [b.type for b in echo(content)] == ["text", "fallback", "text", "tool_use"]


async def test_a_stopped_answer_is_logged_as_cancelled_with_what_was_said(db):
    from brain.domain.identity import resolve_user
    from brain.tools import catalog
    from brain.web.chat import Chat
    settings = make_settings(DEV_USER_ID=DEV_UID)
    svc = Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None)
    fake = FakeClaude(*search_then_answer())
    chat = Chat(svc, catalog.build(svc), AnthropicBackend(svc.audit, settings, client=fake), {"name": "web"})
    ctx = await resolve_user(settings, None, svc.repos.users)
    turn = chat.turn(svc, ctx, "6ae8b6632c980be2f0c234e7", "Що нового?")
    async for event in turn:
        if event["type"] == "tool":           # the user presses stop while the tool runs
            break
    await turn.aclose()
    [reply] = await db.conversations.find({"messages.role": "assistant"}, {"messages": 1}).to_list(None)
    [said] = [m for m in reply["messages"] if m["role"] == "assistant"]
    assert said["stop_reason"] == "cancelled" and said["content"] == "Шукаю в постах."
    assert len(fake.requests) == 1



def research_answer():
    results = [SimpleNamespace(url="https://example.org/a", title="A", page_age="2026-10-30"),
               SimpleNamespace(url="https://example.org/b", title="B", page_age=None)]
    usage = SimpleNamespace(input_tokens=900, output_tokens=150, server_tool_use=SimpleNamespace(web_search_requests=2))
    return SimpleNamespace(stop_reason="end_turn", model="claude-haiku-5-5", usage=usage, content=[
        block("web_search_tool_result", content=results),
        block("text", text="Знайдено [A](https://example.org/a) і [вигадане](https://made.up/x).")])


async def test_web_research_hands_the_chat_a_brief_and_the_item_ids_reach_the_model(db):
    fake = FakeClaude(
        message(block("tool_use", id="tu_1", name="web_research", input={"query": "Спектр-К РЕБ"}), stop="tool_use"),
        message(block("text", text="Досьє.")), searches=[research_answer()])
    async with web(app_with(db, fake)) as c:
        chat_id = (await c.post("/api/chats")).json()["id"]
        item = {"digest_id": "67b000000000000000000001", "item_n": 2}
        ev = events((await c.post(f"/api/chats/{chat_id}/messages",
                                  json={"content": "Глибше про #2: Спектр-К", "item": item})).text)
        assert ev[-1]["type"] == "done"
        thread = (await c.get(f"/api/chats/{chat_id}")).json()["messages"]
    assert fake.requests[0]["messages"][-1]["content"] == \
        "Глибше про #2: Спектр-К\n\n[item: digest_id 67b000000000000000000001, item_n 2]"
    assert thread[0]["content"] == "Глибше про #2: Спектр-К"             # the user sees and the log keeps the text
    search = fake.created[0]
    assert search["model"] == "claude-haiku-5-5" and search["tools"][0]["max_uses"] == 5
    result = json.loads(fake.requests[1]["messages"][-1]["content"][0]["content"])
    assert result["brief"] == "Знайдено [A](https://example.org/a) і вигадане."       # an unknown link is dropped
    assert [x["url"] for x in result["sources"]] == ["https://example.org/a", "https://example.org/b"]
    [conv] = await db.conversations.find({"user_id": DEV_UID}).to_list(None)
    [tool] = [m for m in conv["messages"] if m["role"] == "tool"]
    assert tool["tool"] == "web_research" and tool["result"]["count"] == 2
    [reply] = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert (reply["research_input_tokens"], reply["research_output_tokens"], reply["web_searches"]) == (900, 150, 2)
    [user] = [m for m in conv["messages"] if m["role"] == "user" and m["via"] == "chat"]
    assert user["item"] == item
    assert await db.audit_log.count_documents({"event": "model_call", "details.model": "claude-haiku-5-5"}) == 1
    # the budget counts the research too: 2 rounds × 120 + 900 + 150 = 1290 tokens used today
    async with web(app_with(db, FakeClaude(), WEB_CHAT_TOKENS_PER_DAY=1290)) as c:
        r = await c.post(f"/api/chats/{chat_id}/messages", json={"content": "ще"})
    assert events(r.text)[0]["type"] == "error" and "ліміт" in events(r.text)[0]["detail"]


def test_the_schemas_are_compacted_without_losing_what_the_model_needs():
    from brain.web.chat import compact
    schema = {"title": "X", "type": "object", "required": ["a"], "properties": {
        "title": {"title": "Title", "type": "string"},
        "a": {"title": "A", "type": "string", "description": "the a"},
        "b": {"anyOf": [{"type": "integer", "maximum": 3}, {"type": "null"}], "default": None, "title": "B",
              "description": "the b"},
        "c": {"anyOf": [{"type": "string"}, {"type": "integer"}], "default": "x"}}}
    assert compact(schema) == {"type": "object", "required": ["a"], "properties": {
        "title": {"type": "string"},
        "a": {"type": "string", "description": "the a"},
        "b": {"type": "integer", "maximum": 3, "description": "the b"},
        "c": {"anyOf": [{"type": "string"}, {"type": "integer"}], "default": "x"}}}


def test_every_web_description_is_of_a_real_tool():
    from brain.tools import catalog
    from brain.web.chat import WEB_DESCRIPTIONS
    settings = make_settings()
    specs = catalog.build(Services(settings=settings, repos=SimpleNamespace(db=None), audit=AuditLog(None),
                                   token=lambda: None))
    assert set(WEB_DESCRIPTIONS) <= set(specs)
