"""The agents built from settings in one place (brain/agents/setup.py): the MCP service and the worker share it."""
import pytest

from brain.agents.setup import build_agents
from brain.mcp_server import server
from tests.conftest import make_settings

CASES = [
    ({}, set(), False),
    ({"LEARNING_AUTO": "true"}, {"learner"}, True),
    ({"SCOUT_AUTO": "true"}, {"scout_trends", "scout_match"}, False),
    ({"SCOUT_WEB": "true"}, {"scout_trends", "scout_match", "scout_web"}, False),
    ({"ADVISOR_AUTO": "true"}, {"advisor_daily", "advisor"}, False),
    ({"LEARNING_AUTO": "true", "SCOUT_AUTO": "true", "SCOUT_WEB": "true", "ADVISOR_AUTO": "true"},
     {"learner", "scout_trends", "scout_match", "scout_web", "advisor_daily", "advisor"}, True),
]


def registered(runtime):
    if runtime is None:
        return set(), []
    return set(runtime.agents), sorted((s.agent, str(s.scope), s.every) for s in runtime.schedules)


@pytest.mark.parametrize("env,names,has_learner", CASES)
async def test_the_mcp_service_registers_the_same_agents_as_the_shared_function(test_db, monkeypatch, env, names,
                                                                                has_learner):
    settings = make_settings(DEV_USER_ID="1", **env)
    agents = build_agents(test_db, settings)
    monkeypatch.setattr(server, "get_db", lambda _: test_db)
    svc = server.build_services(settings)

    assert registered(agents.runtime)[0] == names
    assert registered(svc.agents) == registered(agents.runtime)
    assert (agents.learner is not None) == has_learner == (svc.learner is not None)
    assert (agents.runtime is None) == (not names)
