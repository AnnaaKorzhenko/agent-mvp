"""Shows (default) or rewrites (--apply) the pipeline's keyword record in `sources` from users' followed topics.

    brain/.venv/bin/python -m brain.scripts.sync_topic_queries [--db News] [--apply]

The MCP server keeps the record in sync by itself (on every follow / stop, and every 30 minutes from digest
reads); this is for checking it, or for a first write before the server has run. The record:
{platform: "search_keywords", name: "User interests", queries: [{q, region: ua / ru / world, exclude?}]}.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from brain.config import get_settings
from brain.db.client import close_client, get_client
from brain.db.repositories import Repos
from brain.domain import topics


async def main(db_name: str | None, apply: bool) -> None:
    settings = get_settings()
    repos = Repos(get_client(settings)[db_name or settings.mongo_db])
    active = await repos.users.all_active_topics(datetime.now(timezone.utc))
    wanted = topics.keyword_queries(active)
    current = await repos.sources.get_keyword_queries()
    print(f"database {repos.db.name}: {len(active)} followed topic(s): {sorted({t.name for t in active})}")
    print("record now:", "missing" if current is None else json.dumps(current, ensure_ascii=False))
    print("should be: ", json.dumps(wanted, ensure_ascii=False))
    if current == wanted:
        print("in sync")
    elif apply:
        await repos.sources.set_keyword_queries(wanted)
        print("written")
    else:
        print("differs: run with --apply to write it")
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.db, a.apply))
