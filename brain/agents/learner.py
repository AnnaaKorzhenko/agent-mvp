"""The learner agent: runs the learning job (brain/agents/learner_profile.py) by itself, inside the MCP service,
triggered by the changes themselves.

MongoDB pushes every change of users.goals, users.feedback and users.insights to an open change stream (watch()),
whoever wrote it
(Claude's set_goal and record_feedback, the bot's /goal and buttons). For the users the MCP server serves (records
with an OAuth login):
- a goal change queues a run LEARNING_GOAL_DELAY_MINUTES after the last one (several goals in a row: one run; in
  agent_runs, run by the agents' worker, so a restart doesn't lose it), if an active goal then has no keywords
  (removing a goal doesn't cost a model call);
- a comment on a rating (a new rating with text, or text added later) starts a run after the same delay: it is
  the strongest signal there is («more on this device»), so it doesn't wait for the threshold;
- an interest, goal or preference the user stated themselves (an explicit insight: «мене цікавлять морські
  дрони») starts a run after the same delay, so its web searches reach the pipeline's keyword record the same day
  (detected ones wait for the other triggers);
- a rating counts toward LEARNING_FEEDBACK_THRESHOLD ratings since the last run; at the threshold, and at least
  LEARNING_COOLDOWN_MINUTES after the last run, it starts a run.
The job's own writes (learned, proposals, last_seen) don't trigger anything. Runs go in the background, so the
stream keeps reading; after a dropped connection the stream resumes from the last event. One scan at startup
(catch_up) runs whoever became due while the service was down. Every run goes through the agents' runtime
(brain/agents, agent «learner»): agent_runs keeps two runs of one user from overlapping (across processes too),
logs each run with its tokens, and holds the budgets: LEARNING_MAX_RUNS_PER_DAY runs that call the model, all
users, and AGENT_USER_TOKENS_PER_DAY tokens per user. After a run: the goals it found become proposals
(brain/domain/proposals.py) and the pipeline's keyword record is rebuilt with the lasting interests' searches.
Off unless LEARNING_AUTO is true. Every model call is in audit_log (model_call).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pymongo.asynchronous.database import AsyncDatabase

from brain.agents import common
from brain.agents import learner_profile as profile
from brain.agents.runs import AgentRuns
from brain.agents.runtime import Budget, Report, Runtime
from brain.config import Settings
from brain.db.repositories import Repos
from brain.integrations.backends import ModelBackend

logger = logging.getLogger("brain.agents.learner")

AGENT = "learner"
NUDGE = "nudge"               # a queued run after a goal, a comment or a stated interest: runs only if still due
STATED_KINDS = ("interest", "goal", "preference")      # explicit insights that start a run by themselves
# Only changes that can make a user due reach the server: updates touching goals or feedback, and new records.
STREAM = [{"$match": {"$or": [
    {"operationType": {"$in": ["insert", "replace"]}},
    {"operationType": "update", "$expr": {"$gt": [{"$size": {"$filter": {
        "input": {"$objectToArray": "$updateDescription.updatedFields"},
        "cond": {"$regexMatch": {"input": "$$this.k", "regex": "^(goals|feedback|insights)(\\.|$)"}}}}}, 0]}},
]}}, {"$project": {"documentKey": 1, "operationType": 1, "updateDescription.updatedFields": 1}}]


@dataclass
class Due:
    user_id: object
    reason: str           # "goal" | "comment" | "interest" | "feedback"


def _utc(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


async def due_users(db: AsyncDatabase, settings: Settings, now: datetime | None = None, *,
                    user_id=None, goal_delay: bool = True, from_run: bool = False) -> list[Due]:
    """Who is due now (all served users, or one). goal_delay=False: a nudge has already waited the delay (for
    goals and comments alike). Users with a learner run going on are skipped, unless asked from that run
    (from_run: the queued run checking whether it still has something to do)."""
    now = now or datetime.now(timezone.utc)
    out: list[Due] = []
    running = set() if from_run else await AgentRuns(db).running_scopes(AGENT, now)
    query: dict = {"identities.0": {"$exists": True}}
    if user_id is not None:
        query["_id"] = user_id
    cursor = db.users.find(query,
                           {"goals": 1, "feedback.at": 1, "feedback.text": 1, "feedback.text_at": 1, "learned.processed_until": 1, "learned.updated_at": 1,
                            "learned.goal_keywords.goal": 1, "insights.kind": 1, "insights.origin": 1,
                            "insights.created_at": 1})
    async for u in cursor:
        if u["_id"] in running:
            continue
        learned = u.get("learned") or {}
        active = [g for g in u.get("goals") or [] if g.get("active")]
        mapped = {profile._norm(m.get("goal", "")) for m in learned.get("goal_keywords") or []}
        unmapped = [g for g in active if profile._norm(g.get("text", "")) not in mapped]
        newest = max((_utc(g.get("created_at")) for g in active if g.get("created_at")), default=None)
        waited = (not goal_delay or newest is None
                  or newest <= now - timedelta(minutes=settings.learning_goal_delay_minutes))
        if unmapped and waited:
            out.append(Due(u["_id"], "goal"))
            continue
        since = _utc(learned.get("processed_until"))
        commented = [t for f in u.get("feedback") or [] if f.get("text")
                     and (t := profile.feedback_stamp(f)) and (since is None or t > since)]
        delay = now - timedelta(minutes=settings.learning_goal_delay_minutes)
        if commented and (not goal_delay or max(commented) <= delay):
            out.append(Due(u["_id"], "comment"))
            continue
        stated = [t for i in u.get("insights") or [] if i.get("origin") == "explicit"
                  and i.get("kind") in STATED_KINDS and (t := _utc(i.get("created_at"))) and (since is None or t > since)]
        if stated and (not goal_delay or max(stated) <= delay):
            out.append(Due(u["_id"], "interest"))
            continue
        new = sum(1 for f in u.get("feedback") or [] if f.get("at") and (since is None or _utc(f["at"]) > since))
        last = _utc(learned.get("updated_at"))
        cooled = last is None or last <= now - timedelta(minutes=settings.learning_cooldown_minutes)
        if new >= settings.learning_feedback_threshold and cooled:
            out.append(Due(u["_id"], "feedback"))
    return out


@dataclass
class Learner:
    db: AsyncDatabase
    settings: Settings
    backend: ModelBackend
    pending: dict[object, asyncio.Task] = field(default_factory=dict)     # background runs and submits
    runtime: Runtime | None = None

    def __post_init__(self):
        if self.runtime is None:
            self.runtime = Runtime(self.db)
        self.runtime.register(AGENT, self._learn, Budget(
            runs_per_day=self.settings.learning_max_runs_per_day,
            scope_tokens_per_day=self.settings.agent_user_tokens_per_day))

    async def capped(self, now: datetime) -> bool:
        return await self.runtime.capped(AGENT, now=now)

    async def _learn(self, user_id, reason: str) -> Report:
        if reason == NUDGE:                       # queued after the delay: is a goal, comment or interest still new?
            due = await due_users(self.db, self.settings, user_id=user_id, goal_delay=False, from_run=True)
            if not any(d.reason in ("goal", "comment", "interest") for d in due):
                return Report("nothing due any more")
        act = await profile.collect(self.db, user_id, self.backend.max_label)
        if act.empty:
            return Report("nothing new")
        result = await profile.learn(self.backend, act)
        await profile.save(self.db, act, result)
        await profile.after_save(Repos(self.db), act, result)
        used = sum(c.input_tokens + c.output_tokens for c in result.calls)
        return Report(f"saved ({used} tokens)", result.calls)

    async def run_user(self, user_id, reason: str, now: datetime | None = None) -> str | None:
        """Runs one user now. None if a run of this user is going on or a budget is used up."""
        return await self.runtime.run(AGENT, user_id, reason, now)

    async def run_once(self, now: datetime | None = None) -> list[tuple[object, str, str]]:
        """One check: runs every due user (within the daily cap). Returns [(user_id, reason, outcome)]."""
        now = now or datetime.now(timezone.utc)
        done = []
        for due in await due_users(self.db, self.settings, now):
            if await self.capped(now):
                break
            outcome = await self.run_user(due.user_id, due.reason, now)
            if outcome is not None:
                done.append((due.user_id, due.reason, outcome))
        return done

    def nudge(self, user_id, delay_seconds: float | None = None) -> None:
        """A goal changed, a comment came or an interest was stated: queues a run after the goal delay (in
        agent_runs, so a restart doesn't lose it and any worker runs it). Another change within the delay pushes it
        back, so several goals or a rating and its comment in a row make one run; when it's due it runs only if
        something is still new. Never raises."""
        delay = self.settings.learning_goal_delay_minutes * 60 if delay_seconds is None else delay_seconds
        try:
            self._background(self.runtime.submit(AGENT, user_id, NUDGE, timedelta(seconds=delay)))
        except Exception as e:                 # noqa: BLE001 - a nudge must never break set_goal
            logger.error(f"learning nudge failed: {type(e).__name__}: {e}")

    def _background(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.pending[object()] = task
        task.add_done_callback(lambda t: [self.pending.pop(k) for k, v in list(self.pending.items()) if v is t])

    async def on_change(self, event: dict) -> None:
        """One change of a users record, from the stream."""
        user_id = (event.get("documentKey") or {}).get("_id")
        if user_id is None:
            return
        if event.get("operationType") == "update":
            fields = list((event.get("updateDescription") or {}).get("updatedFields") or {})
        else:                                   # insert / replace: the whole record is new
            fields = ["goals", "feedback"]
        if any(f == "goals" or f.startswith("goals.") for f in fields):
            self.nudge(user_id)
        elif any(f == "insights" or f.startswith("insights.") for f in fields):
            due = await due_users(self.db, self.settings, user_id=user_id, goal_delay=False)
            if any(d.reason == "interest" for d in due):
                self.nudge(user_id)
        elif any(f == "feedback" or f.startswith("feedback.") for f in fields):
            due = await due_users(self.db, self.settings, user_id=user_id, goal_delay=False)
            if any(d.reason == "comment" for d in due):
                self.nudge(user_id)
            elif any(d.reason == "feedback" for d in due):
                self._background(self.run_user(user_id, "feedback"))

    async def catch_up(self) -> None:
        """At startup only: whoever became due while the service was down."""
        try:
            await self.run_once()
        except Exception as e:                 # noqa: BLE001
            logger.error(f"learning catch-up failed: {type(e).__name__}: {e}")

    async def watch(self) -> None:
        """For the service's lifetime: the startup catch-up, then the change stream (no polling)."""
        logger.info(f"learning: automatic, on changes of goals and ratings, backend {self.backend.name}")
        await self.catch_up()
        token, backoff = None, 1
        while True:
            try:
                async with await self.db.users.watch(STREAM, resume_after=token) as stream:
                    backoff = 1
                    async for event in stream:
                        token = stream.resume_token
                        try:
                            await self.on_change(event)
                        except Exception as e:     # noqa: BLE001 - one bad event doesn't stop the stream
                            logger.error(f"learning: change not handled: {type(e).__name__}: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:             # noqa: BLE001 - reconnect, resuming after the last event
                logger.error(f"learning: change stream dropped ({type(e).__name__}: {e}); reconnecting")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


def learner(db: AsyncDatabase, settings: Settings, runtime: Runtime | None = None) -> Learner:
    return Learner(db, settings, common.backend_for(db, settings, AGENT), runtime=runtime)
