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


@pytest.mark.parametrize("in_mcp", [True, False])
async def test_the_mcp_service_leaves_the_agents_to_the_worker_when_told(test_db, monkeypatch, in_mcp):
    settings = make_settings(DEV_USER_ID="1", LEARNING_AUTO="true", SCOUT_AUTO="true", ADVISOR_AUTO="true",
                             AGENTS_IN_MCP=str(in_mcp).lower())
    monkeypatch.setattr(server, "get_db", lambda _: test_db)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    started = []
    monkeypatch.setattr(server, "_start_with", lambda app, task: started.append(task.__qualname__))
    svc = server.build_services(settings)
    server.create_app()

    if in_mcp:                                  # the default: as before, both loops start with the MCP service
        assert svc.agents is not None and svc.learner is not None
        assert started == ["Learner.watch", "Runtime.work"]
    else:                                       # the worker runs them: nothing here, not even a learner to nudge
        assert svc.agents is None and svc.learner is None
        assert started == []


def test_the_agents_run_in_the_mcp_service_by_default():
    assert make_settings().agents_in_mcp is True
