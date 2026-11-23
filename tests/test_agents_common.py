"""The agents' shared helpers: which backend and model each agent uses, and asking for JSON."""
import pytest

from brain.agents import common
from brain.integrations.backends import ModelResult
from tests.conftest import make_settings


def chosen(agent, **env):
    s = common.backend_settings(make_settings(**env), agent)
    return s.model_backend, s.anthropic_model


def test_each_agent_gets_its_own_setting_else_the_shared_one_else_the_old_fallbacks():
    # What is set on Render today keeps working: LEARNING_BACKEND drives the scout and the advisor too.
    assert chosen("scout", LEARNING_BACKEND="anthropic", SCOUT_MODEL="claude-haiku-5-5") == \
        ("anthropic", "claude-haiku-5-5")
    assert chosen("advisor", LEARNING_BACKEND="anthropic", SCOUT_MODEL="claude-haiku-5-5")[1] == "claude-haiku-5-5"
    # The learner no longer runs on the largest model by default.
    assert chosen("learner", LEARNING_BACKEND="anthropic") == ("anthropic", "claude-sonnet-5-5")
    # AGENT_BACKEND / AGENT_MODEL for all that don't say otherwise.
    assert chosen("advisor", AGENT_BACKEND="anthropic", AGENT_MODEL="m") == ("anthropic", "m")
    assert chosen("advisor", AGENT_BACKEND="anthropic", AGENT_MODEL="m", ADVISOR_MODEL="a")[1] == "a"
    assert chosen("scout") == ("local", "claude-opus-5-5")                       # nothing set: MODEL_BACKEND
    assert chosen("scout_web")[0] == "anthropic"                                 # only it can search


def test_model_provider_moves_every_agent_and_swaps_claude_names_for_the_providers_own():
    # Render today: LEARNING_BACKEND / SCOUT_BACKEND = anthropic with Claude models; MODEL_PROVIDER=openai moves them
    env = dict(MODEL_PROVIDER="openai", LEARNING_BACKEND="anthropic", SCOUT_BACKEND="anthropic",
               SCOUT_MODEL="claude-haiku-5-5")
    for agent in ("learner", "scout", "scout_web", "advisor"):
        s = common.backend_settings(make_settings(**env), agent)
        assert (s.model_backend, s.openai_model) == ("openai", "gpt-5-mini"), agent
    s = common.backend_settings(make_settings(**env, ADVISOR_MODEL="gpt-5"), "advisor")
    assert (s.model_backend, s.openai_model) == ("openai", "gpt-5")                 # an OpenAI name is kept
    s = common.backend_settings(make_settings(MODEL_PROVIDER="anthropic", SCOUT_BACKEND="openai"), "scout")
    assert (s.model_backend, s.anthropic_model) == ("anthropic", "claude-opus-5-5")  # the switch back
    # The local model stays local; without MODEL_PROVIDER the named backend is used
    assert chosen("scout", MODEL_PROVIDER="openai", SCOUT_BACKEND="local")[0] == "local"
    assert chosen("scout", SCOUT_BACKEND="openai")[0] == "openai"
    assert chosen("scout", MODEL_PROVIDER="openai")[0] == "openai"                   # nothing else set


def test_the_provider_drives_the_chat_too_unless_web_chat_provider_says_otherwise():
    s = make_settings(MODEL_PROVIDER="openai", ANTHROPIC_API_KEY="sk-ant", OPENAI_API_KEY="sk-oa")
    assert (s.chat_provider, s.chat_api_key, s.openai_key) == ("openai", "sk-oa", "sk-oa")
    s = make_settings(MODEL_PROVIDER="openai", WEB_CHAT_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant")
    assert s.chat_provider == "anthropic"
    # The chat's own OpenAI key serves the agents when OPENAI_API_KEY isn't set; another API's key doesn't
    assert make_settings(MODEL_PROVIDER="openai", WEB_CHAT_API_KEY="sk-chat").openai_key == "sk-chat"
    assert make_settings(MODEL_PROVIDER="openai", WEB_CHAT_API_KEY="k",
                         WEB_CHAT_BASE_URL="https://openrouter.ai/api/v1").openai_key is None


class Scripted:
    max_label = "internal"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    async def complete(self, messages, system=None, max_tokens=1024, sensitivity="internal", user_id=None):
        self.sent.append(list(messages))
        return ModelResult(self.answers.pop(0), 10, 5, "m", "fake", "end_turn")


async def test_one_retry_with_the_error_then_a_bad_answer_that_still_counts():
    from pydantic import BaseModel

    class Shape(BaseModel):
        x: int

    got, calls = await common.ask_json(Scripted("```json\n{\"x\": 1}\n```"), [], Shape, system="s", max_tokens=9)
    assert got.x == 1 and len(calls) == 1
    backend = Scripted("not json", '{"x": 2}')
    got, calls = await common.ask_json(backend, [], Shape, system="s", max_tokens=9)
    assert got.x == 2 and len(calls) == 2 and "wasn't valid JSON" in backend.sent[1][-1]["content"]
    with pytest.raises(common.BadAnswer) as e:
        await common.ask_json(Scripted("no", "still no"), [], Shape, system="s", max_tokens=9, what="the test")
    assert len(e.value.calls) == 2 and "the test didn't match" in str(e.value)
    assert common.extract_json('blah {"a": 1} text {"finds": [2]}', start='{"finds"') == {"finds": [2]}


async def test_an_agents_json_question_on_openai_with_the_retry():
    from types import SimpleNamespace

    from pydantic import BaseModel

    from brain.integrations.backends import OpenAIBackend

    class Shape(BaseModel):
        x: int

    sent, answers = [], ["not json", '{"x": 3}']

    async def create(**kw):
        sent.append(kw)
        return SimpleNamespace(model="gpt-5-mini-1", usage=SimpleNamespace(prompt_tokens=20, completion_tokens=4),
                               choices=[SimpleNamespace(finish_reason="stop",
                                                        message=SimpleNamespace(content=answers.pop(0)))])

    class Audit:
        async def model_call(self, **kw):
            pass

    backend = OpenAIBackend(Audit(), "gpt-5-mini",
                            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    got, calls = await common.ask_json(backend, [{"role": "user", "content": "q"}], Shape, system="s", max_tokens=99)
    assert got.x == 3 and [c.backend for c in calls] == ["openai"] * 2
    first, second = sent
    assert first["messages"] == [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    assert first["max_completion_tokens"] == 99 and "temperature" not in first
    assert second["messages"][2] == {"role": "assistant", "content": "not json"}
    assert "wasn't valid JSON" in second["messages"][3]["content"]
