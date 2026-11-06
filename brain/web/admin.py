"""The agents console of our web app (docs/WEB_PLAN.md step 5.4): for ADMIN_EMAILS only.

What the agents did (agent_runs), how their suggestions were answered (users.proposals, counted only: no titles
or user content), what the web chat used; and two controls, both through agent_runs so the agents' worker in the
MCP service obeys them wherever it runs:
- pause / unpause: a marker the runtime checks before every run (brain/agents/runs.py);
- run now: a queued run, picked up by the worker within a minute (IDLE). A per-user agent runs for the admin
  who pressed it.
And the agents' worker's heartbeat (its own service, `python -m brain.agents`): when it was last alive, stale after
WORKER_STALE. The console's markers (scope "control": pauses, the heartbeat) are never counted as runs.
The same numbers as brain/scripts/agents_report.py, for a page instead of a terminal.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pymongo.asynchronous.database import AsyncDatabase

from brain.agents import advisor, learner, morning_push, scout, scout_web
from brain.agents.runs import COLLECTION as RUNS, CONTROL, GLOBAL, AgentRuns

ENDED = ("accepted", "dismissed", "stale")
FAILURES = 3
QUEUE = 20
WORKER_STALE = timedelta(minutes=3)     # three missed heartbeats (runs.HEARTBEAT_EVERY)


@dataclass(frozen=True)
class AgentInfo:
    name: str
    label: str
    per_user: bool               # runs for one user at a time (run now: for the admin who pressed it)
    enabled_by: str              # the MCP service's setting that turns it on


AGENTS = [
    AgentInfo(learner.AGENT, "Навчання профілю", True, "LEARNING_AUTO"),
    AgentInfo(scout.TRENDS, "Скаут: тренди в постах", False, "SCOUT_AUTO"),
    AgentInfo(scout.MATCH, "Скаут: кому що запропонувати", True, "SCOUT_AUTO"),
    AgentInfo(scout_web.AGENT, "Веб-скаут", False, "SCOUT_WEB"),
    AgentInfo(advisor.DAILY, "Порадник: щоденний запуск", False, "ADVISOR_AUTO"),
    AgentInfo(advisor.AGENT, "Порадник: поради користувачу", True, "ADVISOR_AUTO"),
    AgentInfo(morning_push.AGENT, "Ранковий push у браузер", False, "MORNING_PUSH"),
]
BY_NAME = {a.name: a for a in AGENTS}


def _iso(dt: datetime | None) -> str | None:
    if not isinstance(dt, datetime):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


async def _runs(db: AsyncDatabase, since: datetime) -> dict[str, dict]:
    rows = await (await db[RUNS].aggregate([
        {"$match": {"created_at": {"$gte": since}, "scope": {"$ne": CONTROL}}},
        {"$group": {"_id": {"agent": "$agent", "status": "$status"}, "n": {"$sum": 1},
                    "calls": {"$sum": {"$ifNull": ["$model_calls", 0]}},
                    "tin": {"$sum": {"$ifNull": ["$tokens_in", 0]}}, "tout": {"$sum": {"$ifNull": ["$tokens_out", 0]}},
                    "web": {"$sum": {"$ifNull": ["$web_searches", 0]}}}}])).to_list(None)
    out: dict[str, dict] = defaultdict(lambda: {"by_status": Counter(), "model_calls": 0, "tokens_in": 0,
                                                "tokens_out": 0, "web_searches": 0})
    for r in rows:
        a = out[r["_id"]["agent"]]
        a["by_status"][r["_id"]["status"]] += r["n"]
        a["model_calls"] += r["calls"]
        a["tokens_in"] += r["tin"]
        a["tokens_out"] += r["tout"]
        a["web_searches"] += r["web"]
    return out


async def _proposals(db: AsyncDatabase, since: datetime, now: datetime) -> list[dict]:
    """Per agent and kind: made in the period and how they ended (an open one past its expiry counts as stale)."""
    rows = await (await db.users.aggregate([
        {"$match": {"proposals.created_at": {"$gte": since}}},
        {"$unwind": "$proposals"},
        {"$match": {"proposals.created_at": {"$gte": since}}},
        {"$project": {"agent": "$proposals.agent", "kind": "$proposals.kind", "status": {"$cond": [
            {"$and": [{"$eq": ["$proposals.status", "open"]}, {"$lt": ["$proposals.expires_at", now]}]},
            "stale", "$proposals.status"]}}},
        {"$group": {"_id": {"agent": "$agent", "kind": "$kind", "status": "$status"}, "n": {"$sum": 1}}},
    ])).to_list(None)
    groups: dict[tuple, Counter] = defaultdict(Counter)
    for r in rows:
        groups[(r["_id"].get("agent") or "?", r["_id"].get("kind") or "?")][r["_id"].get("status") or "open"] += r["n"]
    out = []
    for (agent, kind), c in sorted(groups.items()):
        gone = sum(c[k] for k in ENDED)
        out.append({"agent": agent, "kind": kind, "made": sum(c.values()),
                    **{k: c[k] for k in (*ENDED, "open")},
                    "accept_rate": round(c["accepted"] / gone, 2) if gone else None})
    return out


async def _chat(db: AsyncDatabase, since: datetime) -> dict:
    """The web chat's replies in the period and their tokens (all users)."""
    rows = await (await db.conversations.aggregate([
        {"$match": {"channel": "web", "updated_at": {"$gte": since}}},
        {"$unwind": "$messages"},
        {"$match": {"messages.role": "assistant", "messages.via": "chat", "messages.created_at": {"$gte": since}}},
        {"$group": {"_id": None, "replies": {"$sum": 1}, "users": {"$addToSet": "$user_id"},
                    **{k: {"$sum": {"$ifNull": [f"$messages.{k}", 0]}} for k in (
                        "input_tokens", "output_tokens", "cache_read_tokens", "research_input_tokens",
                        "research_output_tokens", "web_searches")}}},
    ])).to_list(1)
    if not rows:
        return {"replies": 0, "users": 0}
    r = rows[0]
    return {**{k: v for k, v in r.items() if k not in ("_id", "users")}, "users": len(r["users"])}


async def overview(db: AsyncDatabase, days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    runs = AgentRuns(db)

    async def one(a: AgentInfo) -> dict:
        s = stats.get(a.name) or {"by_status": Counter(), "model_calls": 0, "tokens_in": 0, "tokens_out": 0,
                                  "web_searches": 0}
        last = await db[RUNS].find_one({"agent": a.name, "finished_at": {"$exists": True}, "scope": {"$ne": CONTROL}},
                                       {"status": 1, "outcome": 1, "finished_at": 1}, sort=[("finished_at", -1)])
        failures = [{"at": _iso(f.get("finished_at")), "error": (f.get("error") or "")[:300]}
                    async for f in db[RUNS].find({"agent": a.name, "status": "failed", "created_at": {"$gte": since}},
                                                 {"error": 1, "finished_at": 1}).sort("finished_at", -1).limit(FAILURES)]
        today = await runs.usage(now, agent=a.name)
        p = paused.get(a.name)
        return {
            "name": a.name, "label": a.label, "per_user": a.per_user, "enabled_by": a.enabled_by,
            "runs": dict(s["by_status"]), "model_calls": s["model_calls"], "tokens_in": s["tokens_in"],
            "tokens_out": s["tokens_out"], "web_searches": s["web_searches"],
            "last": {"status": last.get("status"), "outcome": (last.get("outcome") or "")[:300],
                     "at": _iso(last.get("finished_at"))} if last else None,
            "failures": failures,
            "today": {"runs": today.runs, "tokens": today.tokens, "web_searches": today.web_searches},
            "paused": {"by": p.get("paused_by"), "since": _iso(p.get("since"))} if p else None,
        }

    async def queue() -> list[dict]:
        return [{"agent": d["agent"], "scope": "усі" if d.get("scope") == GLOBAL else "користувач",
                 "trigger": d.get("trigger"), "not_before": _iso(d.get("not_before"))}
                async for d in db[RUNS].find({"status": "queued"}, {"agent": 1, "scope": 1, "trigger": 1,
                                                                    "not_before": 1}).sort("not_before", 1).limit(QUEUE)]

    # Independent reads, in parallel: the page waits for the slowest, not for their sum.
    stats, paused, beat = await asyncio.gather(_runs(db, since), runs.paused_agents(), runs.heartbeat())
    *agents, queued, running, props, chat = await asyncio.gather(
        *(one(a) for a in AGENTS), queue(),
        db[RUNS].count_documents({"status": "running", "lease_until": {"$gte": now}, "scope": {"$ne": CONTROL}}),
        _proposals(db, since, now), _chat(db, since))
    return {"days": days, "since": _iso(since), "agents": agents, "queue": queued, "running": running,
            "proposals": props, "chat": chat, "worker": _worker(beat, now)}


def _worker(beat: dict | None, now: datetime) -> dict:
    """The worker's last heartbeat; stale when older than WORKER_STALE or missing (never ran, or the agents still
    run inside the MCP service)."""
    if not beat or not isinstance(beat.get("at"), datetime):
        return {"at": None, "host": None, "pid": None, "stale": True}
    at = beat["at"] if beat["at"].tzinfo else beat["at"].replace(tzinfo=timezone.utc)
    return {"at": _iso(at), "host": beat.get("host"), "pid": beat.get("pid"), "stale": now - at > WORKER_STALE}


async def set_paused(db: AsyncDatabase, name: str, paused: bool, by: str | None) -> None:
    await AgentRuns(db).set_paused(name, paused, by)


async def run_now(db: AsyncDatabase, name: str, admin_scope) -> str:
    """Queues a run; returns whom it runs for ("усі" or "вас")."""
    agent = BY_NAME[name]
    await AgentRuns(db).submit(name, admin_scope if agent.per_user else GLOBAL, "console")
    return "вас" if agent.per_user else "усі"
