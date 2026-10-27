"""Baseline eval: runs every questions.jsonl question through search_posts and records what comes back.

    brain/.venv/bin/python -m brain.eval.run_questions [--db News] [--limit 10]

Read-only. Writes brain/eval/results/<stamp>.jsonl and <stamp>.csv (git-ignored). Mark the CSV's
`relevant` column (y/n) by hand; the marked file is the baseline for comparing the local and Anthropic
backends later (phase 5).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from datetime import datetime
from pathlib import Path

from brain.config import get_settings
from brain.db.client import close_client, get_client
from brain.db.repositories import Repos
from brain.domain import posts as posts_domain
from brain.domain import sensitivity
from brain.domain.identity import UserContext

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "results"


async def main(db_name: str | None, limit: int) -> None:
    settings = get_settings()
    repos = Repos(get_client(settings)[db_name or settings.mongo_db])
    ctx = UserContext(user_id=0, trust="cloud", channel="eval", max_sensitivity=sensitivity.max_for("cloud"))
    questions = [json.loads(line) for line in (ROOT / "questions.jsonl").read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    OUT.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rows, summary = [], []
    for q in questions:
        days = min(int(q.get("window_days") or 7), posts_domain.MAX_DAYS)
        views, words = await posts_domain.search(repos, ctx, q["question"], days=days, limit=limit)
        summary.append({"id": q["id"], "type": q.get("type"), "question": q["question"], "days": days,
                        "search_words": words, "hits": len(views),
                        "results": [{"rank": i, "post_id": v.post.id, "score": v.score, "side": v.post.source_side,
                                     "published_at": v.post.published_at, "post_url": v.post.post_url,
                                     "summary": v.post.summary} for i, v in enumerate(views, 1)]})
        for i, v in enumerate(views, 1):
            rows.append({"id": q["id"], "question": q["question"], "rank": i, "score": v.score,
                         "side": v.post.source_side, "published_at": v.post.published_at[:10],
                         "summary": v.post.summary[:300], "post_url": v.post.post_url, "relevant": ""})
        print(f"{q['id']:>4}  hits {len(views):>2}  words {words}")
    (OUT / f"{stamp}.jsonl").write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in summary) + "\n",
                                        encoding="utf-8")
    with (OUT / f"{stamp}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["id"])
        w.writeheader()
        w.writerows(rows)
    empty = [s["id"] for s in summary if not s["hits"]]
    print(f"\n{len(questions)} questions, {sum(1 for s in summary if s['hits'])} with results; "
          f"no results: {', '.join(empty) or 'none'}")
    print(f"Wrote {OUT / (stamp + '.jsonl')} and .csv (mark the 'relevant' column)")
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()
    asyncio.run(main(a.db, a.limit))
