"""The advisor (docs/AGENT_PLAN.md §4 ③, phase 4): what changed for the user, what it means for their goals, and
one next step, as proposals of kind advice.

Agents on the runtime:
- «advisor_daily», scope global, every ADVISOR_EVERY_HOURS: queues «advisor» for every user served in the last
  scout.ACTIVE_DAYS.
- «advisor», per user: the signals (brain/domain/advice.py, code). Quiet topics and goals are said by code
  («nothing on «X» in 14 days»). Signals with news (a busier topic or goal, new findings) go to the model in one
  call: for each it writes what changed and why it matters for which goal, and picks one of the signal's steps.
  Checked by code: a signal it didn't get, a step not offered for it, or a URL that isn't one of the signal's
  items' is dropped; at most MAX_ADVICE. Claims from Russian channels stay marked as claims.
- Steps: deep_dive and analysis are requests for the client model (the yes hands the message back: «Глибше про
  …», «Аналіз за тиждень, фокус …»); stop_topic stops following; refine_goal asks the client to help reword the
  goal. A signal is advised once (its key holds the week for news).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, ValidationError
from pymongo.asynchronous.database import AsyncDatabase

from brain.agents import scout
from brain.agents.runs import GLOBAL
from brain.agents.runtime import Budget, Report, Runtime
from brain.db.repositories import Repos, _public_id
from brain.domain import advice, proposals
from brain.domain.identity import UserContext
from brain.integrations.backends import ModelBackend

logger = logging.getLogger("brain.agents.advisor")

DAILY = "advisor_daily"
AGENT = "advisor"
MAX_ADVICE = 3
OPEN_DAYS = 5
MAX_OUTPUT = 3000

SYSTEM = """You advise one user of a news service about the war in Ukraine and the defense sector: an analyst, \
not a news printer. You get their goals and a few signals of what changed in their news this week (a followed \
topic or a goal that suddenly has more news, new web findings), each with the news items behind it and the \
steps the service can take next.

For each signal worth telling (at most 3; skip the weak ones; one per topic or goal: several signals on the same \
one make one piece of advice):
- title: a few words;
- text: 2-3 sentences in Ukrainian: what changed (with the numbers given), what it means for which of their \
goals, what to watch. Only what the items say; items with claim=true come from Russian channels: write «за \
даними російських каналів»;
- step: one of that signal's steps;
- source_urls: the URLs (from that signal's items only) of the items you rely on.
Answer with one JSON object only, no other text. Inside strings never use the " character: write quotes as «…». \
Exactly: {"advice": [{"signal": str, "title": str, "text": str, "step": str, "source_urls": [str]}]}"""


class Advice(BaseModel):
    signal: str
    title: str = Field(max_length=100)
    text: str = Field(max_length=600)
    step: str
    source_urls: list[str] = Field(default_factory=list, max_length=6)


class Answer(BaseModel):
    advice: list[Advice] = Field(default_factory=list, max_length=6)


def _parse(text: str) -> Answer:
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the answer")
    return Answer.model_validate(json.loads(cleaned[start:end + 1]))


def action(step: str, sig: advice.Signal) -> dict:
    """The proposal's action for a step: what the yes does, and its button's words."""
    s = sig.subject
    if step == "deep_dive":
        return {"tool": "chat", "args": {"label": "глибше", "message": f"Глибше про «{s}»: що нового за тиждень"}}
    if step == "analysis":
        return {"tool": "chat", "args": {"label": "аналіз за тиждень",
                                         "message": f"Аналіз за тиждень, фокус: «{s}»"}}
    if step == "stop_topic":
        return {"tool": "stop_topic", "args": {"name": s, "label": "припинити відстежувати"}}
    return {"tool": "chat", "args": {"label": "уточнити ціль",
                                     "message": f"Допоможи уточнити ціль «{s}»: за 2 тижні про неї не було новин"}}


def said_by_code(sig: advice.Signal) -> tuple[str, str]:
    if sig.kind == "quiet":
        return (f"«{sig.subject}»: тиша", f"За {sig.facts['days']} днів про тему «{sig.subject}» не було жодної "
                f"новини й жодної знахідки. Можливо, її варто більше не відстежувати.")
    return (f"Ціль без новин", f"За {sig.facts['days']} днів жодна новина не назвала ціль «{sig.subject}». "
            "Можливо, її варто сформулювати конкретніше: назви систем, компаній, програм.")


class Advisor:
    def __init__(self, db: AsyncDatabase, backend: ModelBackend, runtime: Runtime, every: timedelta,
                 user_tokens_per_day: int | None = None):
        self.db, self.backend, self.runtime = db, backend, runtime
        self.repos = Repos(db)
        runtime.register(DAILY, self.daily)
        runtime.register(AGENT, self.advise, Budget(scope_tokens_per_day=user_tokens_per_day))
        runtime.schedule(DAILY, GLOBAL, every)

    async def daily(self, scope, trigger: str) -> Report:
        users = await scout.active_users(self.db, datetime.now(timezone.utc))
        for uid in users:
            await self.runtime.submit(AGENT, uid, "daily")
        return Report(f"queued {len(users)} user(s)")

    async def advise(self, user_key, trigger: str) -> Report:
        ctx = UserContext(_public_id(user_key), "cloud", "agent", "internal")
        sigs = await advice.signals(self.repos, ctx)
        existing = {p.key for p in await self.repos.users.get_proposals(ctx.user_id)}
        sigs = [s for s in sigs if f"advice:{s.key}" not in existing]
        if not sigs:
            return Report("nothing new to say")
        made, calls = 0, []
        for sig in [s for s in sigs if s.kind not in advice.NEEDS_MODEL]:
            title, text = said_by_code(sig)
            made += await self._propose(user_key, sig, title, text, sig.steps[0], [])
        news = [s for s in sigs if s.kind in advice.NEEDS_MODEL]
        if news and made < MAX_ADVICE:
            profile = await self.repos.users.get_profile(ctx.user_id)
            payload = {"goals": [g.text for g in (profile.goals if profile else []) if g.active],
                       "signals": [{"signal": s.key, "kind": s.kind, "about": s.subject, "facts": s.facts,
                                    "steps": s.steps, "items": s.items} for s in news]}
            messages = [{"role": "user", "content": "Data (JSON):\n" + json.dumps(payload, ensure_ascii=False)}]
            call = await self.backend.complete(messages, system=SYSTEM, max_tokens=MAX_OUTPUT, sensitivity="internal",
                                               user_id=str(ctx.user_id))
            calls.append(call)
            try:
                answer = _parse(call.text)
            except (ValueError, ValidationError) as e:
                return Report(f"{made} said by code; the model's advice didn't parse ({str(e)[:100]})", calls)
            by_key = {s.key: s for s in news}
            for a in answer.advice:
                sig = by_key.get(a.signal)
                if sig is None or a.step not in sig.steps or made >= MAX_ADVICE:
                    continue
                urls = [u for u in a.source_urls if u in sig.urls]
                if sig.items and not urls:
                    continue                                   # advice on news must cite the news
                made += await self._propose(user_key, sig, a.title, a.text, a.step, urls)
                by_key.pop(a.signal)
        return Report(f"{len(sigs)} signal(s), {made} advice", calls)

    async def _propose(self, user_key, sig: advice.Signal, title: str, text: str, step: str, urls: list[str]) -> int:
        status = await proposals.propose(self.repos, user_key, kind="advice", title=title, why=text, agent=AGENT,
                                         key=f"advice:{sig.key}", days=OPEN_DAYS, links=urls,
                                         action=action(step, sig))
        return int(status == "ok")


def advisor(db: AsyncDatabase, settings, runtime: Runtime) -> Advisor:
    from brain.audit.log import AuditLog
    from brain.integrations.backends import get_backend
    update = {"model_backend": settings.scout_backend or settings.learning_backend or settings.model_backend}
    model = settings.advisor_model or settings.scout_model
    if model:
        update["anthropic_model"] = model
    backend = get_backend(settings.model_copy(update=update), AuditLog(db))
    return Advisor(db, backend, runtime, timedelta(hours=settings.advisor_every_hours),
                   user_tokens_per_day=settings.agent_user_tokens_per_day)
