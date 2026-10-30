"""Runs the agents: every run goes through agent_runs (one at a time per agent and scope, logged, budgeted).

An agent is a handler `async (scope, trigger) -> Report` with a Budget. Two ways in:
- run(): now, from the caller's task (the learner's change-stream triggers use this);
- submit() + work(): queued in agent_runs, picked up by any worker when due. A worker sleeps until the next
  queued run is due (at most IDLE), or until submit() in this process wakes it;
- schedule(): the worker queues it again whenever its last run is older than its interval (agent_runs is the
  clock, so restarts and several workers don't run it twice).
A run over budget doesn't start: it waits for the next day (queued runs are marked skipped). A handler's
exception marks the run failed; it never stops the worker.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from pymongo.asynchronous.database import AsyncDatabase

from brain.agents.runs import AgentRuns, ensure_indexes
from brain.integrations.backends import ModelResult

logger = logging.getLogger("brain.agents")

IDLE = timedelta(seconds=60)


@dataclass
class Report:
    outcome: str
    calls: list[ModelResult] = field(default_factory=list)
    output: dict | None = None          # kept on the run for later runs (global agents only: no user data)


@dataclass
class Budget:
    runs_per_day: int | None = None             # runs of this agent that call a model, all scopes
    tokens_per_day: int | None = None           # this agent, all scopes
    scope_tokens_per_day: int | None = None     # this agent, one user
    web_searches_per_day: int | None = None     # this agent, all scopes (billed per search)


Handler = Callable[[Any, str], Awaitable[Report]]


@dataclass
class _Agent:
    handler: Handler
    budget: Budget


@dataclass
class _Schedule:
    agent: str
    scope: Any
    every: timedelta


class Runtime:
    def __init__(self, db: AsyncDatabase):
        self.db = db
        self.runs = AgentRuns(db)
        self.agents: dict[str, _Agent] = {}
        self._wake = asyncio.Event()
        self._indexed = False
        self.schedules: list[_Schedule] = []

    def schedule(self, name: str, scope: Any, every: timedelta) -> None:
        """Queues `name` for `scope` whenever its last run (in any status) was queued over `every` ago."""
        self.schedules.append(_Schedule(name, scope, every))

    async def submit_scheduled(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        due = []
        for s in self.schedules:
            last = await self.runs.last_created(s.agent, s.scope)
            if last is None or now - last >= s.every:
                await self.runs.submit(s.agent, s.scope, "schedule", now)
                due.append(s.agent)
        return due

    async def _ready(self) -> None:
        """The indexes that make «one run at a time» hold, once per process (creating them is idempotent)."""
        if not self._indexed:
            await ensure_indexes(self.db)
            self._indexed = True

    def register(self, name: str, handler: Handler, budget: Budget | None = None) -> None:
        self.agents[name] = _Agent(handler, budget or Budget())

    async def over_budget(self, name: str, scope: Any, now: datetime) -> str | None:
        """Why this run may not start today, or None."""
        b = self.agents[name].budget
        if b.runs_per_day is not None or b.tokens_per_day is not None or b.web_searches_per_day is not None:
            used = await self.runs.usage(now, agent=name)
            if b.runs_per_day is not None and used.runs >= b.runs_per_day:
                return f"daily cap of {b.runs_per_day} runs reached"
            if b.tokens_per_day is not None and used.tokens >= b.tokens_per_day:
                return f"daily budget of {b.tokens_per_day} tokens reached"
            if b.web_searches_per_day is not None and used.web_searches >= b.web_searches_per_day:
                return f"daily budget of {b.web_searches_per_day} web searches reached"
        if b.scope_tokens_per_day is not None:
            used = await self.runs.usage(now, agent=name, scope=scope)
            if used.tokens >= b.scope_tokens_per_day:
                return f"this user's daily budget of {b.scope_tokens_per_day} tokens reached"
        return None

    async def _execute(self, name: str, run_id, scope: Any, trigger: str) -> str:
        try:
            report = await self.agents[name].handler(scope, trigger)
        except Exception as e:                 # noqa: BLE001 - one run's failure never stops the others
            outcome = f"failed: {type(e).__name__}: {e}"
            await self.runs.finish(run_id, "failed", outcome, error=f"{type(e).__name__}: {e}")
            return outcome
        calls = report.calls
        await self.runs.finish(run_id, "done", report.outcome, model=calls[-1].model if calls else None,
                               model_calls=len(calls), tokens_in=sum(c.input_tokens for c in calls),
                               tokens_out=sum(c.output_tokens for c in calls), output=report.output,
                               web_searches=sum(c.web_searches for c in calls))
        return report.outcome

    async def capped(self, name: str, scope: Any = None, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if why := await self.over_budget(name, scope, now):
            logger.warning(f"{name}: {why}")
            return True
        return False

    async def run(self, name: str, scope: Any, trigger: str, now: datetime | None = None) -> str | None:
        """Runs now. None if over budget or already running for this scope."""
        now = now or datetime.now(timezone.utc)
        await self._ready()
        if await self.capped(name, scope, now):
            return None
        run_id = await self.runs.start(name, scope, trigger, now)
        if run_id is None:
            return None
        outcome = await self._execute(name, run_id, scope, trigger)
        logger.info(f"{name} {scope} ({trigger}): {outcome}")
        return outcome

    async def submit(self, name: str, scope: Any, trigger: str, delay: timedelta = timedelta()) -> None:
        await self._ready()
        await self.runs.submit(name, scope, trigger, datetime.now(timezone.utc) + delay)
        self._wake.set()

    async def work_once(self, now: datetime | None = None) -> str | None:
        """Takes one due queued run and runs it. None if nothing is due."""
        now = now or datetime.now(timezone.utc)
        await self._ready()
        doc = await self.runs.take(list(self.agents), now)
        if doc is None:
            return None
        name, scope, trigger = doc["agent"], doc["scope"], doc.get("trigger") or "queued"
        if why := await self.over_budget(name, scope, now):
            await self.runs.finish(doc["_id"], "skipped", why)
            return f"skipped: {why}"
        outcome = await self._execute(name, doc["_id"], scope, trigger)
        logger.info(f"{name} {scope} ({trigger}, queued): {outcome}")
        return outcome

    async def work(self) -> None:
        """For the service's lifetime: runs queued work as it becomes due."""
        while True:
            try:
                await self._ready()
                await self.submit_scheduled()
                while await self.work_once() is not None:
                    pass
                due = await self.runs.next_due(list(self.agents))
                wait = IDLE.total_seconds() if due is None else \
                    max(0.0, min(IDLE.total_seconds(), (due - datetime.now(timezone.utc)).total_seconds()))
            except asyncio.CancelledError:
                raise
            except Exception as e:             # noqa: BLE001 - the worker keeps going
                logger.error(f"agents: worker error: {type(e).__name__}: {e}")
                wait = IDLE.total_seconds()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
