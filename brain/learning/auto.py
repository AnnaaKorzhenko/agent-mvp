"""Runs the learning job by itself, inside the MCP service, triggered by the changes themselves: no polling.

MongoDB pushes every change of users.goals and users.feedback to an open change stream (watch()), whoever wrote it
(Claude's set_goal and record_feedback, the bot's /goal and buttons). For the users the MCP server serves (records
with an OAuth login):
- a goal change starts a run LEARNING_GOAL_DELAY_MINUTES after the last one (several goals in a row: one run), if
  an active goal then has no keywords (removing a goal doesn't cost a model call);
- a comment on a rating (a new rating with text, or text added later) starts a run after the same delay: it is
  the strongest signal there is («more on this device»), so it doesn't wait for the threshold;
- a rating counts toward LEARNING_FEEDBACK_THRESHOLD ratings since the last run; at the threshold, and at least
  LEARNING_COOLDOWN_MINUTES after the last run, it starts a run.
The job's own writes (learned, learning_lock, last_seen) don't trigger anything. Runs go in the background, so the
stream keeps reading; after a dropped connection the stream resumes from the last event. One scan at startup
(catch_up) runs whoever became due while the service was down. A lock on the user record (learning_lock) keeps two
runs from overlapping; LEARNING_MAX_RUNS_PER_DAY caps all runs (costs). Off unless LEARNING_AUTO is true. Every
model call is in audit_log (model_call).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from pymongo.asynchronous.database import AsyncDatabase

from brain.audit.log import AuditLog
from brain.config import Settings
from brain.integrations.backends import ModelBackend, get_backend
from brain.learning import profile

logger = logging.getLogger("brain.learning.auto")

LOCK_MINUTES = 15
# Only changes that can make a user due reach the server: updates touching goals or feedback, and new records.
STREAM = [{"$match": {"$or": [
    {"operationType": {"$in": ["insert", "replace"]}},
    {"operationType": "update", "$expr": {"$gt": [{"$size": {"$filter": {
        "input": {"$objectToArray": "$updateDescription.updatedFields"},
        "cond": {"$regexMatch": {"input": "$$this.k", "regex": "^(goals|feedback)(\\.|$)"}}}}}, 0]}},
]}}, {"$project": {"documentKey": 1, "operationType": 1, "updateDescription.updatedFields": 1}}]


@dataclass
class Due:
    user_id: object
    reason: str           # "goal" | "comment" | "feedback"


def _utc(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


async def due_users(db: AsyncDatabase, settings: Settings, now: datetime | None = None, *,
                    user_id=None, goal_delay: bool = True) -> list[Due]:
    """Who is due now (all served users, or one). goal_delay=False: a nudge has already waited the delay (for
    goals and comments alike)."""
    now = now or datetime.now(timezone.utc)
    out: list[Due] = []
    query: dict = {"identities.0": {"$exists": True}}
    if user_id is not None:
        query["_id"] = user_id
    cursor = db.users.find(query,
                           {"goals": 1, "feedback.at": 1, "feedback.text": 1, "feedback.text_at": 1, "learned.processed_until": 1, "learned.updated_at": 1,
                            "learned.goal_keywords.goal": 1, "learning_lock": 1})
    async for u in cursor:
        lock = _utc(u.get("learning_lock"))
        if lock and lock > now:
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
        new = sum(1 for f in u.get("feedback") or [] if f.get("at") and (since is None or _utc(f["at"]) > since))
        last = _utc(learned.get("updated_at"))
        cooled = last is None or last <= now - timedelta(minutes=settings.learning_cooldown_minutes)
        if new >= settings.learning_feedback_threshold and cooled:
            out.append(Due(u["_id"], "feedback"))
    return out


async def _claim(db: AsyncDatabase, user_id, now: datetime) -> bool:
    r = await db.users.update_one(
        {"_id": user_id, "$or": [{"learning_lock": {"$exists": False}}, {"learning_lock": {"$lt": now}}]},
        {"$set": {"learning_lock": now + timedelta(minutes=LOCK_MINUTES)}})
    return r.modified_count == 1


@dataclass
class Learner:
    db: AsyncDatabase
    settings: Settings
    backend: ModelBackend
    runs: dict[date, int] = field(default_factory=dict)       # runs per UTC day, all users (the cost cap)
    pending: dict[object, asyncio.Task] = field(default_factory=dict)     # nudged users waiting for the delay

    def capped(self, now: datetime) -> bool:
        if self.runs.get(now.date(), 0) >= self.settings.learning_max_runs_per_day:
            logger.warning(f"learning: daily cap of {self.settings.learning_max_runs_per_day} runs reached")
            return True
        return False

    async def run_user(self, user_id, reason: str, now: datetime | None = None) -> str | None:
        """Runs one user under the lock. None if another run holds the lock or the daily cap is reached."""
        now = now or datetime.now(timezone.utc)
        if self.capped(now) or not await _claim(self.db, user_id, now):
            return None
        try:
            act = await profile.collect(self.db, user_id, self.backend.max_label)
            if act.empty:
                outcome = "nothing new"
            else:
                self.runs[now.date()] = self.runs.get(now.date(), 0) + 1
                result = await profile.learn(self.backend, act)
                await profile.save(self.db, act, result)
                used = sum(c.input_tokens + c.output_tokens for c in result.calls)
                outcome = f"saved ({used} tokens)"
        except Exception as e:                 # noqa: BLE001 - one user's failure doesn't stop the others
            outcome = f"failed: {type(e).__name__}: {e}"
        finally:
            await self.db.users.update_one({"_id": user_id}, {"$unset": {"learning_lock": ""}})
        logger.info(f"learning {user_id} ({reason}): {outcome}")
        return outcome

    async def run_once(self, now: datetime | None = None) -> list[tuple[object, str, str]]:
        """One check: runs every due user (within the daily cap). Returns [(user_id, reason, outcome)]."""
        now = now or datetime.now(timezone.utc)
        done = []
        for due in await due_users(self.db, self.settings, now):
            if self.capped(now):
                break
            outcome = await self.run_user(due.user_id, due.reason, now)
            if outcome is not None:
                done.append((due.user_id, due.reason, outcome))
        return done

    def nudge(self, user_id, delay_seconds: float | None = None) -> None:
        """A goal changed or a comment came: after the goal delay, run this user if an active goal lacks keywords
        or a comment is unprocessed. Another change within the delay restarts it, so several goals or a rating and
        its comment in a row make one run. Never raises."""
        delay = self.settings.learning_goal_delay_minutes * 60 if delay_seconds is None else delay_seconds
        try:
            if (old := self.pending.get(user_id)) and not old.done():
                old.cancel()

            async def later():
                await asyncio.sleep(delay)
                self.pending.pop(user_id, None)
                due = await due_users(self.db, self.settings, user_id=user_id, goal_delay=False)
                if reason := next((d.reason for d in due if d.reason in ("goal", "comment")), None):
                    await self.run_user(user_id, "goal added" if reason == "goal" else "comment")

            self.pending[user_id] = asyncio.get_running_loop().create_task(later())
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


def learner(db: AsyncDatabase, settings: Settings) -> Learner:
    backend_name = settings.learning_backend or settings.model_backend
    backend = get_backend(settings.model_copy(update={"model_backend": backend_name}), AuditLog(db))
    return Learner(db, settings, backend)
