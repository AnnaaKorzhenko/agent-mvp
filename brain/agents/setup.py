"""The agents built from settings, in one place: the MCP service and the agents' worker run the same set."""
from __future__ import annotations

from dataclasses import dataclass

from pymongo.asynchronous.database import AsyncDatabase

from brain.agents.advisor import advisor
from brain.agents import morning_push
from brain.agents.learner import Learner, learner
from brain.agents.runtime import Runtime
from brain.agents.scout import scout
from brain.agents.scout_web import web_scout
from brain.config import Settings


@dataclass
class Agents:
    runtime: Runtime | None         # None: no agent is on, nothing to work
    learner: Learner | None         # its own watch loop (new messages), besides the runtime's queue


def build_agents(db: AsyncDatabase, settings: Settings) -> Agents:
    """Registers every agent the settings turn on, on one runtime."""
    agents_on = settings.scout_auto or settings.scout_web or settings.advisor_auto or morning_push.enabled(settings)
    runtime = Runtime(db) if settings.learning_auto or agents_on else None
    if settings.scout_auto or settings.scout_web:
        scout(db, settings, runtime)            # scout_match proposes the web finds too
    if settings.scout_web:
        web_scout(db, settings, runtime)
    if settings.advisor_auto:
        advisor(db, settings, runtime)
    if morning_push.enabled(settings):
        morning_push.morning_push(db, settings, runtime)
    return Agents(runtime=runtime, learner=learner(db, settings, runtime) if settings.learning_auto else None)
