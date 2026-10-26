"""Validates REAL recent documents against docs/DATA_CONTRACT.md. Read-only. Opt-in:

    CONTRACT_MONGO_URI=... CONTRACT_MONGO_DB=News brain/.venv/bin/pytest tests/contract -m contract

It reports every broken field with a count and example ids instead of stopping at the first one,
so a change in the pipeline's output is visible at once.
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict

import pytest
from bson import ObjectId
from pymongo import AsyncMongoClient

from brain.db.contract import check_document, load_contract

pytestmark = pytest.mark.contract

SAMPLE = {"posts": 200, "digests": 10}


@pytest.fixture(scope="module")
async def real_db():
    uri = os.getenv("CONTRACT_MONGO_URI")
    if not uri:
        pytest.skip("CONTRACT_MONGO_URI not set")
    client = AsyncMongoClient(uri, tz_aware=True, appname="brain-contract-test")
    yield client[os.getenv("CONTRACT_MONGO_DB", "News")]
    await client.close()


async def _sample(db) -> dict[str, list[dict]]:
    posts = await db.posts.find({"processed": True}).sort("_id", -1).limit(SAMPLE["posts"]).to_list(None)
    digests = await db.digests.find({"period": "day"}).sort("created_at", -1).limit(SAMPLE["digests"]).to_list(None)
    channel_ids = list({p["channel_id"] for p in posts if isinstance(p.get("channel_id"), ObjectId)})
    sources = await db.sources.find({"_id": {"$in": channel_ids}}).to_list(None)
    users = await db.users.find({}, {"_id": 1, "interests": 1, "daily.date": 1, "daily.selected": 1, "goals": 1,
                                     "feedback": {"$slice": -50}, "identities": 1}).to_list(None)
    return {"posts": posts, "digests": digests, "sources": sources, "users": users}


async def test_recent_documents_match_contract(real_db):
    rules = load_contract()
    docs = await _sample(real_db)
    counts: Counter = Counter()
    examples: dict = defaultdict(list)
    for collection, items in docs.items():
        assert items or collection == "users", f"no {collection} found to check"
        for doc in items:
            for p in check_document(doc, rules[collection]):
                key = (collection, p.path, p.kind, p.detail)
                counts[key] += 1
                if len(examples[key]) < 3:
                    examples[key].append(p.doc_id)
    checked = {k: len(v) for k, v in docs.items()}
    report = "\n".join(f"  {c}.{path}: {kind}{' ' + d if d else ''} in {n} docs, e.g. {examples[(c, path, kind, d)]}"
                       for (c, path, kind, d), n in sorted(counts.items()))
    print(f"\nChecked {checked}")
    assert not counts, "Data contract broken:\n" + report
