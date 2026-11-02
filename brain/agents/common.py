"""What every agent shares: the model backend each agent uses, asking a model for JSON (with one retry), the users
the agents serve, and the context an agent acts in for a user.

Backend and model per agent, the first one set:
- backend: the agent's own (LEARNING_BACKEND for the learner, SCOUT_BACKEND for the scout and the advisor), else
  AGENT_BACKEND, else LEARNING_BACKEND (what the scout and the advisor used before AGENT_BACKEND), else
  MODEL_BACKEND. The web scout is always anthropic (only it can search).
- model (anthropic): the agent's own (LEARNING_MODEL, SCOUT_MODEL, SCOUT_WEB_MODEL, ADVISOR_MODEL; the web scout
  and the advisor fall back to SCOUT_MODEL), else AGENT_MODEL, else ANTHROPIC_MODEL.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError
from pymongo.asynchronous.database import AsyncDatabase

from brain.db.repositories import _public_id
from brain.domain.identity import UserContext
from brain.integrations.backends import ModelBackend, ModelResult

ACTIVE_DAYS = 30
M = TypeVar("M", bound=BaseModel)

# agent -> (its backend setting, its model settings in order)
AGENT_SETTINGS = {
    "learner": ("learning_backend", ("learning_model",)),
    "scout": ("scout_backend", ("scout_model",)),
    "scout_web": (None, ("scout_web_model", "scout_model")),
    "advisor": ("scout_backend", ("advisor_model", "scout_model")),
}


def backend_settings(settings, agent: str):
    """The settings with model_backend / anthropic_model set as this agent's (see the module docstring)."""
    own_backend, own_models = AGENT_SETTINGS[agent]
    backend = "anthropic" if own_backend is None else (
        getattr(settings, own_backend) or settings.agent_backend or settings.learning_backend
        or settings.model_backend)
    model = next((m for m in (getattr(settings, n) for n in own_models) if m), None) or settings.agent_model
    update = {"model_backend": backend, **({"anthropic_model": model} if model else {})}
    return settings.model_copy(update=update)


def backend_for(db: AsyncDatabase, settings, agent: str) -> ModelBackend:
    from brain.audit.log import AuditLog
    from brain.integrations.backends import get_backend
    return get_backend(backend_settings(settings, agent), AuditLog(db))


def extract_json(text: str, start: str | None = None) -> dict:
    """The JSON object in a model's answer: tolerates ```json fences and text around it. start: where the object
    begins (the last occurrence), for answers that write other JSON before the final one."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    begin = cleaned.rfind(start) if start else -1
    begin = begin if begin >= 0 else cleaned.find("{")
    end = cleaned.rfind("}")
    if begin < 0 or end <= begin:
        raise ValueError("no JSON object in the answer")
    return json.loads(cleaned[begin:end + 1])


def parse(model: type[M], text: str, start: str | None = None) -> M:
    return model.model_validate(extract_json(text, start))


class BadAnswer(ValueError):
    """Two answers that didn't parse; carries the calls, so the run still counts their tokens."""

    def __init__(self, message: str, calls: list[ModelResult]):
        super().__init__(message)
        self.calls = calls


RETRY = ("That wasn't valid JSON for the format ({problem}). {hint} Answer again with only the JSON object.")
DEFAULT_HINT = "Check that every string is closed and contains no \" character (use «…»)."


async def ask_json(backend: ModelBackend, messages: list[dict], model: type[M], *, system: str, max_tokens: int,
                   sensitivity: str = "internal", user_id=None, what: str = "the answer",
                   on_bad: Callable[[int, ModelResult], None] | None = None) -> tuple[M, list[ModelResult]]:
    """Asks for one JSON object of `model`'s shape; once more, with the error, if it doesn't parse. Returns it and
    the calls (for the run's token count). Raises BadAnswer (a ValueError, with the calls) after two bad answers.
    on_bad(attempt, call): e.g. to keep the bad answer for a look later."""
    messages = list(messages)
    calls: list[ModelResult] = []
    for attempt in (1, 2):
        call = await backend.complete(messages, system=system, max_tokens=max_tokens, sensitivity=sensitivity,
                                      user_id=user_id)
        calls.append(call)
        try:
            return parse(model, call.text), calls
        except (ValueError, ValidationError) as e:
            if on_bad:
                on_bad(attempt, call)
            cut = call.stop_reason == "max_tokens"
            problem = f"the answer was cut off at {max_tokens} tokens" if cut else str(e)[:300]
            if attempt == 2:
                raise BadAnswer(f"{what} didn't match the format twice ({problem})", calls) from e
            hint = ("It was too long: keep every list within its limit and the texts short." if cut else DEFAULT_HINT)
            messages += [{"role": "assistant", "content": call.text},
                         {"role": "user", "content": RETRY.format(problem=problem, hint=hint)}]
    raise RuntimeError("unreachable")


async def active_users(db: AsyncDatabase, now: datetime, days: int = ACTIVE_DAYS) -> list:
    """Users the MCP server served in the last `days` (records with an OAuth login)."""
    cursor = db.users.find({"identities.0": {"$exists": True},
                            "last_seen": {"$gte": now - timedelta(days=days)}}, {"_id": 1})
    return [d["_id"] async for d in cursor]


def agent_ctx(user_key) -> UserContext:
    """The context an agent acts in for a user: as a cloud client (what the user's Claude may see: internal)."""
    return UserContext(_public_id(user_key), "cloud", "agent", "internal")
