"""What the agents did and how their suggestions were answered. Reads only; writes nothing.

    brain/.venv/bin/python -m brain.scripts.agents_report [--db News] [--days 7] [--user ID]

1. Runs per agent: by status, model calls, tokens, web searches; the last outcome and the latest failures.
2. Suggestions per user (users served in the period, or --user): per agent and kind, made in the period and how
   they ended: accepted / dismissed / stale (expired or shown 3 times unanswered) / still open; how often shown;
   the accept rate (accepted of those answered or gone stale). Then the user's settings and the titles.
3. The pipeline's keyword record and the interests digests of the period.
The raw material for tuning the agents' thresholds and for the weekly self-report (docs/AGENT_PLAN.md, «Later»).
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from brain.agents import common
from brain.agents.runs import COLLECTION as RUNS
from brain.config import get_settings
from brain.db.client import close_client, get_client
from brain.db.repositories import Repos, user_key
from brain.domain import topics

ENDED = ("accepted", "dismissed", "stale")


def _utc(dt):
    return topics.utc(dt) if isinstance(dt, datetime) else None


async def runs_part(db, since: datetime) -> None:
    print(f"\n== Runs since {since:%Y-%m-%d %H:%M} UTC")
    rows = await (await db[RUNS].aggregate([
        {"$match": {"created_at": {"$gte": since}, "status": {"$ne": "paused"}}},     # not the console's markers
        {"$group": {"_id": {"agent": "$agent", "status": "$status"}, "n": {"$sum": 1},
                    "calls": {"$sum": {"$ifNull": ["$model_calls", 0]}},
                    "tin": {"$sum": {"$ifNull": ["$tokens_in", 0]}}, "tout": {"$sum": {"$ifNull": ["$tokens_out", 0]}},
                    "web": {"$sum": {"$ifNull": ["$web_searches", 0]}}}}])).to_list(None)
    if not rows:
        print("  no runs (are LEARNING_AUTO / SCOUT_AUTO / ADVISOR_AUTO on?)")
        return
    by_agent: dict[str, dict] = defaultdict(lambda: {"status": Counter(), "calls": 0, "tin": 0, "tout": 0, "web": 0})
    for r in rows:
        a = by_agent[r["_id"]["agent"]]
        a["status"][r["_id"]["status"]] += r["n"]
        for k in ("calls", "tin", "tout", "web"):
            a[k] += r[k]
    for agent, a in sorted(by_agent.items()):
        status = ", ".join(f"{k} {v}" for k, v in a["status"].most_common())
        print(f"  {agent:14} {sum(a['status'].values()):4} runs ({status}); {a['calls']} model calls, "
              f"{a['tin']:,} + {a['tout']:,} tokens" + (f", {a['web']} web searches" if a["web"] else ""))
        last = await db[RUNS].find_one({"agent": agent, "finished_at": {"$exists": True}},
                                       {"outcome": 1, "finished_at": 1}, sort=[("finished_at", -1)])
        if last:
            print(f"  {'':14} last: {_utc(last['finished_at']):%m-%d %H:%M} {last.get('outcome', '')[:90]}")
        async for f in db[RUNS].find({"agent": agent, "status": "failed", "created_at": {"$gte": since}},
                                     {"error": 1, "finished_at": 1}).sort("finished_at", -1).limit(2):
            print(f"  {'':14} failed {_utc(f.get('finished_at')):%m-%d %H:%M}: {(f.get('error') or '')[:90]}")


def _rate(c: Counter) -> str:
    gone = sum(c[k] for k in ENDED)
    return f"{100 * c['accepted'] // gone}% accepted" if gone else "none answered yet"


async def proposals_part(db, since: datetime, users: list) -> None:
    print(f"\n== Suggestions made since {since:%Y-%m-%d}")
    repos = Repos(db)
    total: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for key in users:
        found = [p for p in await repos.users.get_proposals(key) if (_utc(p.created_at) or since) >= since]
        prefs = await repos.users.get_agent_prefs(key)
        name = (await db.users.find_one({"_id": key}, {"first_name": 1, "email": 1})) or {}
        print(f"\n  user {key} {name.get('first_name') or name.get('email') or ''}"
              + (f"  settings {({k: v for k, v in prefs.items() if k != 'updated_at'})}" if prefs else ""))
        if not found:
            print("    nothing proposed")
            continue
        groups: dict[tuple[str, str], list] = defaultdict(list)
        for p in found:
            groups[(p.agent or "?", p.kind)].append(p)
        for (agent, kind), ps in sorted(groups.items()):
            c = Counter(p.status for p in ps)
            total[(agent, kind)].update(c)
            shown = sum(p.shown_count for p in ps) / len(ps)
            print(f"    {agent:8} {kind:7} {len(ps):3} made: " + ", ".join(f"{k} {c[k]}" for k in (*ENDED, "open")
                                                                         if c[k])
                  + f"; shown {shown:.1f}× on average; {_rate(c)}")
            for p in sorted(ps, key=lambda p: _utc(p.created_at) or since, reverse=True)[:5]:
                print(f"      [{p.status:9}] {p.title[:70]}")
    if len(users) > 1 and total:
        print("\n  all users:")
        for (agent, kind), c in sorted(total.items()):
            print(f"    {agent:8} {kind:7} {sum(c.values()):3} made; {_rate(c)}")


async def pipeline_part(db, since: datetime) -> None:
    repos = Repos(db)
    queries = await repos.sources.get_keyword_queries() or []
    print(f"\n== Pipeline\n  keyword record «User interests»: {len(queries)} searches "
          f"({len({topics.query_key(q['q']) for q in queries})} distinct)")
    n = 0
    async for d in db.digests.find({**repos.digests.TOPICS_DIGEST, "period": "day", "created_at": {"$gte": since}},
                                   {"created_at": 1, "sections.items.text": 1}).sort("created_at", 1):
        n += 1
        items = sum(len(s.get("items") or []) for s in d.get("sections") or [])
        print(f"  interests digest {_utc(d['created_at']):%m-%d %H:%M}: {items} items")
    if not n:
        print("  no interests digest in the period")


async def main(db_name: str | None, days: int, user: str | None) -> None:
    settings = get_settings()
    db = get_client(settings)[db_name or settings.mongo_db]
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    print(f"database {db.name}: the agents over the last {days} day(s)")
    await runs_part(db, since)
    users = [user_key(int(user) if user.isdigit() else user)] if user else await common.active_users(db, now, days)
    await proposals_part(db, since, users)
    await pipeline_part(db, since)
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--user")
    a = ap.parse_args()
    asyncio.run(main(a.db, a.days, a.user))
