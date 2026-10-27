"""Lists posts whose summaries may need review: posts cited by answers or suggestions that a user rated
👎 with the reason `inaccurate`. Read-only; nothing is written to posts (the pipeline owns them).

    brain/.venv/bin/python -m brain.scripts.review_queue [--db News_dev] [--days 30]

See docs/REQUESTS_TO_LOCAL_MODEL_TRACK.md for how the local-model track may use it.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from pymongo.asynchronous.database import AsyncDatabase

from brain.config import get_settings
from brain.db.client import close_client, get_client

FLAGGED = {"target_type": {"$in": ["answer", "suggestion"]}, "reaction": "not_useful", "reasons": "inaccurate"}


async def review_queue(db: AsyncDatabase, days: int = 30) -> list[dict]:
    """[{post_id, summary, post_url, flags: [{about, text, at, channel}]}], most flagged first."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    pipeline = [
        {"$match": {"feedback": {"$elemMatch": FLAGGED}}},
        {"$unwind": "$feedback"},
        {"$match": {**{f"feedback.{k}": v for k, v in FLAGGED.items()}, "feedback.at": {"$gte": since}}},
        {"$unwind": "$feedback.cited_post_ids"},
        {"$group": {"_id": "$feedback.cited_post_ids",
                    "flags": {"$push": {"about": "$feedback.about", "text": "$feedback.text",
                                        "at": "$feedback.at", "channel": "$feedback.channel"}}}},
    ]
    groups = await (await db["users"].aggregate(pipeline)).to_list(None)
    posts = {p["_id"]: p async for p in db["posts"].find({"_id": {"$in": [g["_id"] for g in groups]}},
                                                         {"summary": 1, "post_url": 1})}
    out = [{"post_id": str(g["_id"]), "summary": posts.get(g["_id"], {}).get("summary"),
            "post_url": posts.get(g["_id"], {}).get("post_url"), "flags": g["flags"]} for g in groups]
    return sorted(out, key=lambda r: -len(r["flags"]))


async def main(db_name: str | None, days: int) -> None:
    settings = get_settings()
    rows = await review_queue(get_client(settings)[db_name or settings.mongo_db], days)
    if not rows:
        print(f"No answers rated inaccurate in the last {days} days.")
    for r in rows:
        print(f"{r['post_id']}  {r['post_url'] or '(post deleted)'}  flagged {len(r['flags'])}x")
        print(f"  summary: {(r['summary'] or '')[:300]}")
        for f in r["flags"]:
            print(f"  - {f['at']:%Y-%m-%d} [{f.get('channel')}] {f.get('about')}: «{f.get('text') or ''}»")
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    asyncio.run(main(a.db, a.days))
