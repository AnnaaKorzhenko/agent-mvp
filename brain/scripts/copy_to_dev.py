"""Copies the real news data (posts, sources, digests) from News into a development database.

    brain/.venv/bin/python -m brain.scripts.copy_to_dev [--source News] [--db News_dev] [--dry-run]

Reads the source database only. In the target, these three collections are replaced (with their
indexes); users and audit_log are left as they are. Only databases named News_dev* may be written.
The pipeline keeps writing to News, so rerun this before a test session.
"""
from __future__ import annotations

import argparse
import asyncio

from brain.config import get_settings
from brain.db.client import close_client, get_client

COLLECTIONS = ("posts", "sources", "digests")
BATCH = 500


async def main(source: str, target: str, dry_run: bool) -> None:
    if not target.startswith("News_dev"):
        raise SystemExit("Refusing: the target database name must start with News_dev")
    if source == target:
        raise SystemExit("Refusing: source and target are the same database")
    client = get_client(get_settings())
    src, dst = client[source], client[target]
    for name in COLLECTIONS:
        total = await src[name].count_documents({})
        if dry_run:
            print(f"dry run: would replace {target}.{name} with {total} documents from {source}.{name}")
            continue
        indexes = [i async for i in await src[name].list_indexes() if i["name"] != "_id_"]
        await dst[name].drop()
        batch = []
        async for doc in src[name].find({}):
            batch.append(doc)
            if len(batch) >= BATCH:
                await dst[name].insert_many(batch, ordered=False)
                batch = []
        if batch:
            await dst[name].insert_many(batch, ordered=False)
        for i in indexes:
            options = {k: v for k, v in i.items() if k not in ("key", "v", "ns")}
            await dst[name].create_index(list(i["key"].items()), **options)
        print(f"{target}.{name}: {await dst[name].count_documents({})} documents, {len(indexes)} indexes")
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="News")
    ap.add_argument("--db", default="News_dev")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.source, a.db, a.dry_run))
