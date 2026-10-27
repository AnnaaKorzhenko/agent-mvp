"""Loads the handwritten sample documents into a local development database (phase 3: sample data only).

    brain/.venv/bin/python -m brain.scripts.load_dev_data [--db News_dev] [--reset]

Only databases named News_dev* may be written. Then run the server with
MONGO_DB=News_dev DEV_USER_ID=111000111 TRUST_LEVEL=cloud.
"""
from __future__ import annotations

import argparse
import asyncio

from brain.audit.log import ensure_audit_indexes
from brain.config import get_settings
from brain.db.client import close_client, get_client
from tests.fixtures.sample_data import USER_ID, load_samples


async def main(db_name: str, reset: bool) -> None:
    if not db_name.startswith("News_dev"):
        raise SystemExit("Refusing: the dev database name must start with News_dev")
    client = get_client(get_settings())
    if db_name in await client.list_database_names():
        if not reset:
            raise SystemExit(f"{db_name} already exists; pass --reset to drop and reload it")
        await client.drop_database(db_name)
    db = client[db_name]
    await load_samples(db)
    await ensure_audit_indexes(db, get_settings().audit_ttl_days)
    print(f"Loaded sample data into {db_name}. Dev user: DEV_USER_ID={USER_ID}")
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="News_dev")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.db, a.reset))
