"""Creates the MCP server's indexes: audit_log (TTL), users.identities (one record per OAuth login), the
conversations indexes for MCP sessions and agent_runs (one run at a time per agent and user; TTL). The server also
creates the agent_runs ones by itself on its first run. Never touches the pipeline's collections.

    brain/.venv/bin/python -m brain.scripts.setup_indexes [--db NAME] [--dry-run] [--replace-conversations-unique]

--replace-conversations-unique drops the bot's old unique {user_id} index on conversations (`user_id_1`), which
forbids more than one document per user. Run it only after the bot with the new user_store.py is deployed
(docs/CONVERSATIONS_PLAN.md §1.3): the new unique (user_id, session_key) index keeps one Telegram document per user.
"""
from __future__ import annotations

import argparse
import asyncio

from brain.audit.conversations import COLLECTION as CONVERSATIONS
from brain.audit.conversations import UNIQUE_INDEX, ensure_conversation_indexes
from brain.audit.log import COLLECTION, ensure_audit_indexes
from brain.agents.runs import COLLECTION as AGENT_RUNS
from brain.agents.runs import ensure_indexes as ensure_agent_indexes
from brain.config import get_settings
from brain.db.client import close_client, get_client
from brain.db.repositories import ensure_user_indexes

OLD_UNIQUE = "user_id_1"


async def _indexes(db, name: str) -> list[str]:
    return [i["name"] async for i in await db[name].list_indexes()] if name in await db.list_collection_names() \
        else []


async def main(db_name: str | None, dry_run: bool, replace_unique: bool) -> None:
    settings = get_settings()
    db = get_client(settings)[db_name or settings.mongo_db]
    for name in (COLLECTION, "users", CONVERSATIONS, AGENT_RUNS):
        print(f"database {db.name}: {name} indexes now: {await _indexes(db, name) or 'none'}")
    old = OLD_UNIQUE in await _indexes(db, CONVERSATIONS)
    if dry_run:
        print(f"dry run: would ensure {COLLECTION}.ttl_at (expire after {settings.audit_ttl_days} days), "
              f"{COLLECTION}.user_at, users.identity_unique, {CONVERSATIONS}.{UNIQUE_INDEX}, .user_updated, "
              f".mcp_session, {AGENT_RUNS} (one_running, one_queued, queue, agent_started, ttl)" + (f"; and drop {CONVERSATIONS}.{OLD_UNIQUE}" if old and replace_unique else ""))
    else:
        await ensure_audit_indexes(db, settings.audit_ttl_days)
        await ensure_user_indexes(db)
        await ensure_conversation_indexes(db)
        await ensure_agent_indexes(db)
        if old and replace_unique:
            await db[CONVERSATIONS].drop_index(OLD_UNIQUE)
            print(f"dropped {CONVERSATIONS}.{OLD_UNIQUE}")
        for name in (COLLECTION, "users", CONVERSATIONS, AGENT_RUNS):
            print(f"ensured {name}: {await _indexes(db, name)}")
    if old and not replace_unique:
        print(f"NOTE: {CONVERSATIONS}.{OLD_UNIQUE} (unique user_id) still exists: MCP sessions can't be logged "
              "into this database until it's dropped with --replace-conversations-unique.")
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace-conversations-unique", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.db, a.dry_run, a.replace_conversations_unique))
