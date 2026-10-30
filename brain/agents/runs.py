"""agent_runs: the agents' task queue and run log (docs/AGENT_PLAN.md §5). One document per run:

    {agent, scope, trigger, status, lease_until, not_before, attempts, outcome, error, model, model_calls,
     tokens_in, tokens_out, web_searches, output, created_at, started_at, finished_at, ttl_at}

- scope: the user's key (users._id) for a per-user agent, "global" for one that works for everyone.
- status: queued (waiting for not_before) → running (until lease_until) → done | failed | expired (a worker
  died holding it: its lease ran out).
- At most one queued and one running run per (agent, scope): two unique partial indexes. A second worker or a
  second process can't start the same work twice.
- Usage per day (runs that called a model, tokens) is summed from here, so budgets hold across restarts and
  workers.
- Runs expire after KEEP_DAYS (TTL index). Only ids, counts and short outcomes are stored: no user content.
  `output` holds a global agent's result for later runs (the scout's trend snapshot: built from posts, never
  from users' data).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

COLLECTION = "agent_runs"
KEEP_DAYS = 60
LEASE = timedelta(minutes=15)
GLOBAL = "global"
MAX_OUTCOME = 300


def _day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


async def ensure_indexes(db: AsyncDatabase) -> None:
    """Idempotent; run at startup and by brain.scripts.setup_indexes. The two uniqueness indexes have different
    key orders, so they are different indexes on any MongoDB version (same keys with two filters needs 5.0+)."""
    col = db[COLLECTION]
    await col.create_index([("agent", 1), ("scope", 1)], unique=True, name="one_running",
                           partialFilterExpression={"status": "running"})
    await col.create_index([("scope", 1), ("agent", 1)], unique=True, name="one_queued",
                           partialFilterExpression={"status": "queued"})
    await col.create_index([("status", 1), ("not_before", 1)], name="queue")
    await col.create_index([("agent", 1), ("started_at", -1)], name="agent_started")
    await col.create_index("ttl_at", expireAfterSeconds=0, name="ttl")


@dataclass
class Usage:
    runs: int = 0             # runs that called a model
    tokens: int = 0
    web_searches: int = 0


class AgentRuns:
    def __init__(self, db: AsyncDatabase):
        self.col = db[COLLECTION]

    async def submit(self, agent: str, scope: Any, trigger: str, not_before: datetime | None = None) -> None:
        """Queues a run. Already queued: the newer not_before wins (a burst of events makes one run, after the
        last of them)."""
        now = datetime.now(timezone.utc)
        try:
            await self.col.update_one(
                {"agent": agent, "scope": scope, "status": "queued"},
                {"$set": {"not_before": not_before or now, "trigger": trigger},
                 "$setOnInsert": {"created_at": now, "attempts": 0, "ttl_at": now + timedelta(days=KEEP_DAYS)}},
                upsert=True)
        except DuplicateKeyError:          # two submits raced on the upsert: the other one queued it
            pass

    async def _expire_stale(self, now: datetime, agent: str | None = None, scope: Any = None) -> None:
        query: dict = {"status": "running", "lease_until": {"$lt": now}}
        if agent is not None:
            query.update(agent=agent, scope=scope)
        await self.col.update_many(query, {"$set": {"status": "expired", "finished_at": now}})

    async def start(self, agent: str, scope: Any, trigger: str, now: datetime | None = None,
                    lease: timedelta = LEASE) -> ObjectId | None:
        """Starts a run now. None if this agent is already running for this scope."""
        now = now or datetime.now(timezone.utc)
        await self._expire_stale(now, agent, scope)
        doc = {"_id": ObjectId(), "agent": agent, "scope": scope, "trigger": trigger, "status": "running",
               "lease_until": now + lease, "attempts": 1, "created_at": now, "started_at": now,
               "ttl_at": now + timedelta(days=KEEP_DAYS)}
        try:
            await self.col.insert_one(doc)
        except DuplicateKeyError:
            return None
        return doc["_id"]

    async def take(self, agents: list[str], now: datetime | None = None,
                   lease: timedelta = LEASE) -> dict | None:
        """The next queued run that is due, now running (claimed by this worker), or None."""
        now = now or datetime.now(timezone.utc)
        await self._expire_stale(now)
        query = {"status": "queued", "not_before": {"$lte": now}, "agent": {"$in": agents}}
        doc = await self.col.find_one(query, sort=[("not_before", 1)])
        if doc is None:
            return None
        try:
            return await self.col.find_one_and_update(
                {"_id": doc["_id"], "status": "queued"},
                {"$set": {"status": "running", "started_at": now, "lease_until": now + lease},
                 "$inc": {"attempts": 1}},
                return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:          # the same work is running now: try again after it
            await self.col.update_one({"_id": doc["_id"]}, {"$set": {"not_before": now + timedelta(minutes=1)}})
            return None

    async def next_due(self, agents: list[str]) -> datetime | None:
        doc = await self.col.find_one({"status": "queued", "agent": {"$in": agents}}, {"not_before": 1},
                                      sort=[("not_before", 1)])
        return doc["not_before"].replace(tzinfo=timezone.utc) if doc and doc.get("not_before") else None

    async def finish(self, run_id: ObjectId, status: str, outcome: str, *, model: str | None = None,
                     model_calls: int = 0, tokens_in: int = 0, tokens_out: int = 0, error: str | None = None,
                     output: dict | None = None, web_searches: int = 0) -> None:
        fields = {"status": status, "outcome": outcome[:MAX_OUTCOME], "error": (error or "")[:MAX_OUTCOME] or None,
                  "model": model, "model_calls": model_calls, "tokens_in": tokens_in, "tokens_out": tokens_out,
                  "web_searches": web_searches, "finished_at": datetime.now(timezone.utc)}
        if output is not None:
            fields["output"] = output
        await self.col.update_one({"_id": run_id}, {"$set": fields, "$unset": {"lease_until": ""}})

    async def latest_output(self, agent: str, scope: Any = GLOBAL, newer_than: datetime | None = None) -> dict | None:
        """The output of the newest finished run of `agent` that has one (finished after `newer_than`)."""
        query: dict = {"agent": agent, "scope": scope, "status": "done", "output": {"$exists": True}}
        if newer_than is not None:
            query["finished_at"] = {"$gte": newer_than}
        doc = await self.col.find_one(query, {"output": 1}, sort=[("finished_at", -1)])
        return doc["output"] if doc else None

    async def last_created(self, agent: str, scope: Any = GLOBAL) -> datetime | None:
        doc = await self.col.find_one({"agent": agent, "scope": scope}, {"created_at": 1}, sort=[("created_at", -1)])
        return doc["created_at"].replace(tzinfo=timezone.utc) if doc and doc.get("created_at") else None

    async def running_scopes(self, agent: str, now: datetime | None = None) -> set:
        now = now or datetime.now(timezone.utc)
        return {d["scope"] async for d in self.col.find(
            {"agent": agent, "status": "running", "lease_until": {"$gte": now}}, {"scope": 1})}

    async def usage(self, now: datetime | None = None, *, agent: str | None = None, scope: Any = None) -> Usage:
        """Today's (UTC) runs that called a model, and their tokens: of one agent, one scope, or all."""
        now = now or datetime.now(timezone.utc)
        match: dict = {"started_at": {"$gte": _day_start(now)}, "model_calls": {"$gt": 0}}
        if agent is not None:
            match["agent"] = agent
        if scope is not None:
            match["scope"] = scope
        rows = await (await self.col.aggregate([
            {"$match": match},
            {"$group": {"_id": None, "runs": {"$sum": 1}, "tokens": {"$sum": {"$add": [
                {"$ifNull": ["$tokens_in", 0]}, {"$ifNull": ["$tokens_out", 0]}]}},
                        "web_searches": {"$sum": {"$ifNull": ["$web_searches", 0]}}}}])).to_list(1)
        return Usage(rows[0]["runs"], rows[0]["tokens"], rows[0]["web_searches"]) if rows else Usage()
