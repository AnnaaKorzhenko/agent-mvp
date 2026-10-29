"""Rebuilds users.interests.tag_scores from each user's rating history and the posts' CURRENT tags.

Why: tags used to be free-form; the pipeline now assigns them from a fixed list, and older posts were re-tagged.
Scores built from the old tags don't line up with the new ones. A rating points to a post (`feedback.post_id`),
so the scores can be recomputed: each rating adds its points (scoring.POINTS: 👍 / like +1, 👎 / dislike −1,
50/50 / skip 0) to every current tag of its post. Ratings of answers and suggestions (no post) don't count, as
before. A post that no longer exists keeps the tags the rating stored. Category scores, the ratings themselves
and everything else are not touched.

    brain/.venv/bin/python -m brain.scripts.rebuild_tag_scores --db News [--user ID]           # show only
    brain/.venv/bin/python -m brain.scripts.rebuild_tag_scores --db News [--user ID] --apply   # write

--apply first saves every affected user's old tag_scores to backups/tag_scores_<db>_<time>.json (git-ignored).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId

from brain.config import get_settings
from brain.db.client import close_client, get_client
from brain.domain import scoring

BACKUPS = Path(__file__).resolve().parents[2] / "backups"
SCORED = (None, "post", "digest_item")          # target types whose points go to interests (others: never)


def _user_id(raw: str):
    return int(raw) if raw.lstrip("-").isdigit() else ObjectId(raw)


def rebuild(feedback: list[dict], current_tags: dict) -> dict[str, float]:
    """New tag_scores from the ratings; `current_tags`: post _id -> its tags now."""
    scores: dict[str, float] = defaultdict(float)
    for f in feedback:
        if f.get("target_type") not in SCORED:
            continue
        points = scoring.POINTS.get(f.get("reaction"), 0)
        if not points:
            continue
        tags = current_tags.get(f.get("post_id"), f.get("tags") or [])
        for tag in dict.fromkeys(tags):
            if scoring.tag_key(tag):
                scores[scoring.tag_key(tag)] += points
    return {t: v for t, v in sorted(scores.items()) if v}


async def main(db_name: str | None, user: str | None, apply: bool) -> None:
    settings = get_settings()
    db = get_client(settings)[db_name or settings.mongo_db]
    query = {"_id": _user_id(user)} if user else {"feedback.0": {"$exists": True}}
    users = await db.users.find(query, {"feedback": 1, "interests.tag_scores": 1, "email": 1}).to_list(None)
    post_ids = list({f["post_id"] for u in users for f in u.get("feedback") or [] if f.get("post_id")})
    current = {p["_id"]: p.get("tags") or [] async for p in db.posts.find({"_id": {"$in": post_ids}}, {"tags": 1})}
    print(f"database {db.name}: {len(users)} user(s) with ratings; {len(current)} of {len(post_ids)} rated posts "
          f"still exist")
    changes = []
    for u in users:
        old = (u.get("interests") or {}).get("tag_scores") or {}
        new = rebuild(u.get("feedback") or [], current)
        ratings = sum(1 for f in u.get("feedback") or [] if f.get("target_type") in SCORED)
        top_old = sorted(old.items(), key=lambda x: -x[1])[:6]
        top_new = sorted(new.items(), key=lambda x: -x[1])[:6]
        print(f"\nuser {u['_id']} {u.get('email') or ''}: {ratings} ratings; tag scores {len(old)} -> {len(new)}")
        print(f"  top now:     {top_old}")
        print(f"  top rebuilt: {top_new}")
        low = sorted((x for x in new.items() if x[1] < 0), key=lambda x: x[1])[:4]
        if low:
            print(f"  most negative rebuilt: {low}")
        if new != old:
            changes.append((u, old, new))
    if not apply:
        print(f"\nshow only: {len(changes)} user(s) would change. Run with --apply to write.")
        await close_client()
        return
    if changes:
        BACKUPS.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = BACKUPS / f"tag_scores_{db.name}_{stamp}.json"
        backup.write_text(json.dumps({str(u["_id"]): old for u, old, _ in changes}, ensure_ascii=False, indent=1))
        print(f"\nbackup of the old scores: {backup}")
        for u, _, new in changes:
            await db.users.update_one({"_id": u["_id"]}, {"$set": {"interests.tag_scores": new}})
        print(f"written: {len(changes)} user(s)")
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--user")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.db, a.user, a.apply))
