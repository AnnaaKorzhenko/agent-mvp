"""Creates the MCP server's indexes: audit_log (TTL) and users.identities (one record per OAuth login).
Never touches the pipeline's collections.

    brain/.venv/bin/python -m brain.scripts.setup_indexes [--db NAME] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio

from brain.audit.log import COLLECTION, ensure_audit_indexes
from brain.config import get_settings
from brain.db.client import close_client, get_client
from brain.db.repositories import ensure_user_indexes


async def main(db_name: str | None, dry_run: bool) -> None:
    settings = get_settings()
    db = get_client(settings)[db_name or settings.mongo_db]
    existing = [i["name"] async for i in await db[COLLECTION].list_indexes()] \
        if COLLECTION in await db.list_collection_names() else []
    print(f"database {db.name}: {COLLECTION} indexes now: {existing or 'none'}")
    users_idx = [i["name"] async for i in await db.users.list_indexes()]
    print(f"database {db.name}: users indexes now: {users_idx}")
    if dry_run:
        print(f"dry run: would ensure {COLLECTION}.ttl_at (expire after {settings.audit_ttl_days} days), "
              f"{COLLECTION}.user_at and users.identity_unique")
    else:
        await ensure_audit_indexes(db, settings.audit_ttl_days)
        await ensure_user_indexes(db)
        print("ensured:", [i["name"] async for i in await db[COLLECTION].list_indexes()],
              [i["name"] async for i in await db.users.list_indexes()])
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.db, a.dry_run))
