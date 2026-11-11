"""The web chat on OpenAI (WEB_CHAT_PROVIDER=openai, or any OpenAI-compatible API at WEB_CHAT_BASE_URL): the
provider comes from the env, the turns are translated to Chat Completions and the answers back, and the chat's loop
runs as it does on Claude. OpenAI is a scripted fake client: no network, no cost."""
import json
from types import SimpleNamespace

import pytest

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.integrations.backends import (AnthropicBackend, OpenAIBackend, chat_backend, to_openai_messages,
                                         to_openai_tools)
from brain.tools.base import Services
from brain.web.app import create_app
from tests.conftest import make_settings
from tests.test_web_chat import DEV_UID, db, events, web  # noqa: F401 - db is a fixture


# ---- which provider, key and model ------------------------------------------------------------------------

@pytest.mark.parametrize("env, provider, key, model, research", [
    ({}, None, None, "claude-sonnet-5-5", "claude-haiku-5-5"),
    ({"ANTHROPIC_API_KEY": "sk-ant"}, "anthropic", "sk-ant", "claude-sonnet-5-5", "claude-haiku-5-5"),
    ({"OPENAI_API_KEY": "sk-oa"}, "openai", "sk-oa", "gpt-5-mini", "gpt-5-mini"),
    # both keys: Claude unless the provider is named
    ({"ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-oa"}, "anthropic", "sk-ant", "claude-sonnet-5-5",
     "claude-haiku-5-5"),
    ({"ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-oa", "WEB_CHAT_PROVIDER": "openai"}, "openai", "sk-oa",
     "gpt-5-mini", "gpt-5-mini"),
    # Claude model names left over from before: the openai provider ignores them
    ({"WEB_CHAT_PROVIDER": "openai", "OPENAI_API_KEY": "sk-oa", "WEB_CHAT_MODEL": "claude-sonnet-5-5",
      "WEB_CHAT_RESEARCH_MODEL": "claude-haiku-5-5"}, "openai", "sk-oa", "gpt-5-mini", "gpt-5-mini"),
    ({"WEB_CHAT_PROVIDER": "openai", "OPENAI_API_KEY": "sk-oa", "WEB_CHAT_MODEL": "gpt-5",
      "WEB_CHAT_RESEARCH_MODEL": "gpt-5-nano"}, "openai", "sk-oa", "gpt-5", "gpt-5-nano"),
    ({"WEB_CHAT_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant", "WEB_CHAT_MODEL": "gpt-5"}, "anthropic",
     "sk-ant", "claude-sonnet-5-5", "claude-haiku-5-5"),
    # one key for whichever provider; another OpenAI-compatible API takes any model name
    ({"WEB_CHAT_PROVIDER": "openai", "WEB_CHAT_API_KEY": "sk-or", "WEB_CHAT_BASE_URL": "https://openrouter.ai/api/v1",
      "WEB_CHAT_MODEL": "anthropic/claude-sonnet-5-5"}, "openai", "sk-or", "anthropic/claude-sonnet-5-5", "gpt-5-mini"),
    ({"WEB_CHAT_API_KEY": "sk-x"}, "openai", "sk-x", "gpt-5-mini", "gpt-5-mini"),
])
def test_the_provider_key_and_models_come_from_the_env(env, provider, key, model, research):
    s = make_settings(**env)
    assert (s.chat_provider, s.chat_api_key, s.chat_model(), s.chat_model(research=True)) == \
           (provider, key, model, research)


def test_chat_backend_builds_the_providers_client():
    audit = SimpleNamespace()
    assert chat_backend(make_settings(), audit) is None
    claude = chat_backend(make_settings(ANTHROPIC_API_KEY="sk-ant"), audit)
    assert isinstance(claude, AnthropicBackend) and claude.model == "claude-sonnet-5-5"
    assert claude.client.api_key == "sk-ant"
    oa = chat_backend(make_settings(WEB_CHAT_PROVIDER="openai", OPENAI_API_KEY="sk-oa",
                                    WEB_CHAT_BASE_URL="https://api.groq.com/openai/v1", WEB_CHAT_MODEL="llama-x"), audit)
    assert isinstance(oa, OpenAIBackend) and oa.model == "llama-x" and oa.client.api_key == "sk-oa"
    assert str(oa.client.base_url).startswith("https://api.groq.com/openai/v1")
    research = oa.with_model("other")
    assert research.client is oa.client and research.model == "other" and research.base_url == oa.base_url
    assert oa._limit(100) == {"max_tokens": 100}
    assert OpenAIBackend(audit, "gpt-5-mini", api_key="k")._limit(100) == {"max_completion_tokens": 100}


# ---- translation ------------------------------------------------------------------------------------------

def test_turns_and_tools_are_translated():
    system = [{"type": "text", "text": "Rules.", "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": "Today."}]
    messages = [
        {"role": "user", "content": "Що нового?"},
        {"role": "assistant", "content": [SimpleNamespace(type="text", text="Шукаю."),
                                          SimpleNamespace(type="tool_use", id="c1", name="search_posts",
                                                          input={"query": "дрони"})]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "[]"}]},
        {"role": "assistant", "content": [SimpleNamespace(type="tool_use", id="c2", name="get_digest", input={})]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c2", "is_error": True, "content": "boom"}]},
    ]
    assert to_openai_messages(messages, system) == [
        {"role": "system", "content": "Rules.\nToday."},
        {"role": "user", "content": "Що нового?"},
        {"role": "assistant", "content": "Шукаю.", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "search_posts", "arguments": '{"query": "дрони"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "[]"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "get_digest", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "Error: boom"},
    ]
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object"}, "eager_input_streaming": True}]
    assert to_openai_tools(tools) == [{"type": "function", "function": {
        "name": "t", "description": "d", "parameters": {"type": "object"}}}]


# ---- a whole turn on the fake OpenAI ----------------------------------------------------------------------

def chunk(content=None, tool=None, finish=None, usage=None):
    calls = None
    if tool:
        index, id, name, args = tool
        calls = [SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=args))]
    choices = [] if usage else [SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=calls),
                                                finish_reason=finish)]
    return SimpleNamespace(model="gpt-5-mini-2026", choices=choices, usage=usage)


def usage(prompt=100, completion=20, cached=0):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                           prompt_tokens_details=SimpleNamespace(cached_tokens=cached))


class FakeOpenAI:
    """Streams each round's scripted chunks; keeps every request."""

    def __init__(self, *rounds, searches=()):
        self.rounds, self.searches = list(rounds), list(searches)      # searches: the Responses API's answers
        self.requests: list[dict] = []
        self.created: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.responses = SimpleNamespace(create=self.respond)

    async def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))

        async def stream(chunks):
            for c in chunks:
                yield c
        return stream(self.rounds.pop(0))

    async def respond(self, **kwargs):
        self.created.append(kwargs)
        return self.searches.pop(0)


def openai_app(db, fake, **env):
    settings = make_settings(DEV_USER_ID=DEV_UID, WEB_CHAT_PROVIDER="openai", OPENAI_API_KEY="sk-oa", **env)
    svc = Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None)
    backend = OpenAIBackend(svc.audit, settings.chat_model(), client=fake)
    return create_app(settings, svc, backend=backend)


async def test_a_tool_round_then_the_answer(db):
    fake = FakeOpenAI(
        [chunk("Шукаю "), chunk("в постах."),
         chunk(tool=(0, "call_1", "search_posts", '{"query": "дро')), chunk(tool=(0, None, None, 'ни", "days": 30}')),
         chunk(finish="tool_calls"), chunk(usage=usage(1000, 50, cached=800))],
        [chunk("Ось що є: [пост](https://t.me/x/1)."), chunk(finish="stop"), chunk(usage=usage(1200, 30))])
    async with web(openai_app(db, fake)) as c:
        chat_id = (await c.post("/api/chats")).json()["id"]
        ev = events((await c.post(f"/api/chats/{chat_id}/messages", json={"content": "Що нового про дрони?"})).text)
    assert [e["type"] for e in ev] == ["text", "text", "tool", "text", "text", "done"]
    assert ev[2]["name"] == "search_posts" and ev[-1]["stop_reason"] == "end_turn"
    assert "".join(e["text"] for e in ev if e["type"] == "text") == \
        "Шукаю в постах.\n\nОсь що є: [пост](https://t.me/x/1)."

    first, second = fake.requests
    assert first["model"] == "gpt-5-mini" and first["stream"] and first["stream_options"] == {"include_usage": True}
    assert first["max_completion_tokens"] == 16000 and "max_tokens" not in first
    assert first["messages"][0]["role"] == "system" and "analyst" in first["messages"][0]["content"]
    assert first["messages"][-1] == {"role": "user", "content": "Що нового про дрони?"}
    names = {t["function"]["name"] for t in first["tools"]}
    assert "search_posts" in names and "web_research" in names and "log_turn" not in names
    call, result = second["messages"][-2:]
    assert call["tool_calls"][0]["function"] == {"name": "search_posts", "arguments": '{"query": "дрони", "days": 30}'}
    assert result["role"] == "tool" and result["tool_call_id"] == "call_1" and not result["content"].startswith("Error")

    [conv] = await db.conversations.find({"user_id": DEV_UID}).to_list(None)
    [tool] = [m for m in conv["messages"] if m["role"] == "tool"]
    assert tool["tool"] == "search_posts" and not tool.get("error")
    [reply] = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert reply["model"] == "gpt-5-mini-2026" and reply["rounds"] == 2
    assert (reply["input_tokens"], reply["output_tokens"], reply["cache_read_tokens"]) == (200 + 1200, 80, 800)
    calls = await db.audit_log.find({"event": "model_call"}).to_list(None)
    assert [(c["details"]["backend"], c["details"]["outcome"]) for c in calls] == [("openai", "ok")] * 2


async def test_bad_tool_json_and_a_cut_off_answer(db):
    fake = FakeOpenAI(
        [chunk(tool=(0, "call_1", "get_digest", '{"side": ')), chunk(finish="tool_calls"), chunk(usage=usage())],
        [chunk("Дуже довга від"), chunk(finish="length"), chunk(usage=usage())])
    async with web(openai_app(db, fake)) as c:
        chat_id = (await c.post("/api/chats")).json()["id"]
        ev = events((await c.post(f"/api/chats/{chat_id}/messages", json={"content": "Дайджест"})).text)
    assert ev[-1] == {**ev[-1], "type": "done", "stop_reason": "max_tokens"}
    result = fake.requests[1]["messages"][-1]
    assert result["role"] == "tool" and result["content"].startswith("Error: ") and "INVALID_JSON" in result["content"]


async def test_a_provider_error_is_the_chats_error_and_is_audited(db):
    class Broken(FakeOpenAI):
        async def create(self, **kwargs):
            raise RuntimeError("401 invalid api key")
    async with web(openai_app(db, Broken())) as c:
        chat_id = (await c.post("/api/chats")).json()["id"]
        ev = events((await c.post(f"/api/chats/{chat_id}/messages", json={"content": "Привіт"})).text)
    assert ev == [{"type": "error", "detail": "Не вдалося отримати відповідь. Спробуйте ще раз."}]
    [call] = await db.audit_log.find({"event": "model_call"}).to_list(None)
    assert call["details"]["outcome"] == "error" and "invalid api key" in call["details"]["error"]


async def test_web_research_on_openai(db):
    msg = SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", annotations=[
        SimpleNamespace(type="url_citation", url="https://example.org/a", title="A"),
        SimpleNamespace(type="url_citation", url="https://example.org/a", title="A")])])
    found = SimpleNamespace(output=[SimpleNamespace(type="web_search_call"), SimpleNamespace(type="web_search_call"), msg],
                            output_text="Знайдено [A](https://example.org/a) і [вигадане](https://made.up/x).",
                            usage=SimpleNamespace(input_tokens=900, output_tokens=150), model="gpt-5-mini",
                            status="completed")
    fake = FakeOpenAI(
        [chunk(tool=(0, "call_1", "web_research", '{"query": "Спектр-К РЕБ"}')), chunk(finish="tool_calls"),
         chunk(usage=usage())],
        [chunk("Досьє."), chunk(finish="stop"), chunk(usage=usage())], searches=[found])
    async with web(openai_app(db, fake, WEB_CHAT_RESEARCH_MODEL="gpt-5-nano")) as c:
        chat_id = (await c.post("/api/chats")).json()["id"]
        ev = events((await c.post(f"/api/chats/{chat_id}/messages", json={"content": "Глибше про Спектр-К"})).text)
    assert ev[-1]["type"] == "done"
    [search] = fake.created
    assert search["model"] == "gpt-5-nano" and search["tools"] == [{"type": "web_search"}]
    assert search["input"][0]["content"].startswith("Спектр-К РЕБ")
    result = json.loads(fake.requests[1]["messages"][-1]["content"])
    assert result["brief"] == "Знайдено [A](https://example.org/a) і вигадане." and result["searches"] == 2
    assert [x["url"] for x in result["sources"]] == ["https://example.org/a"]
