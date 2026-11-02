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
