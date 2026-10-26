"""The only place in brain/ that talks to MongoDB. One repository per collection.

Every document read is parsed into a tolerant model. A document that breaks the data contract
is logged with the exact field and skipped, so one bad document never breaks a whole answer.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TypeVar

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, ValidationError
from pymongo.asynchronous.database import AsyncDatabase

from brain.db.models import DigestDoc, PostDoc, SourceDoc, UserProfileDoc

logger = logging.getLogger("brain.db")

M = TypeVar("M", bound=BaseModel)

# What a post search may return; raw_text / clean_text are never read.
POST_FIELDS = {"post_url": 1, "channel_id": 1, "published_at": 1, "processed": 1, "summary": 1,
               "source_side": 1, "category": 1, "tags": 1, "importance": 1, "is_ad": 1,
               "is_duplicate": 1, "views": 1, "sensitivity": 1}


def parse(model: type[M], doc: dict | None, collection: str) -> M | None:
    if doc is None:
        return None
    try:
        return model.model_validate(doc)
    except ValidationError as e:
        fields = ", ".join(f"{'.'.join(map(str, err['loc']))} ({err['type']})" for err in e.errors())
        logger.error(f"Data contract: {collection} {doc.get('_id')} skipped, bad fields: {fields}. "
                     "See docs/DATA_CONTRACT.md")
        return None


def to_object_id(value: str | ObjectId | None) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value)) if value else None
    except (InvalidId, TypeError):
        return None


def sensitivity_filter(allowed: list[str]) -> dict:
    """Mongo filter for the allowed labels. A missing label means 'internal'."""
    cond: list[dict] = [{"sensitivity": {"$in": list(allowed)}}]
    if "internal" in allowed:
        cond.append({"sensitivity": {"$exists": False}})
    return {"$or": cond}


class PostsRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["posts"]

    async def get(self, post_id: str | ObjectId, allowed: list[str]) -> PostDoc | None:
        oid = to_object_id(post_id)
        if oid is None:
            return None
        doc = await self.col.find_one({"_id": oid, "processed": True, **sensitivity_filter(allowed)}, POST_FIELDS)
        return parse(PostDoc, doc, "posts")

    async def get_many(self, ids: list, allowed: list[str]) -> dict[str, PostDoc]:
        oids = [o for o in (to_object_id(i) for i in ids) if o]
        if not oids:
            return {}
        out = {}
        async for doc in self.col.find({"_id": {"$in": oids}, **sensitivity_filter(allowed)}, POST_FIELDS):
            post = parse(PostDoc, doc, "posts")
            if post:
                out[post.id] = post
        return out

    async def search_candidates(self, words: list[str], *, since_iso: str, allowed: list[str],
                                side: str | None = None, category: str | None = None,
                                limit: int = 200) -> list[PostDoc]:
        """Posts whose summary or tags match any of the words (case-insensitive), newest and most important first.

        Plain pattern search: no text index (not approved on the pipeline's collection). Ranking is done
        in brain.domain.posts.
        """
        patterns = [re.compile(re.escape(w), re.IGNORECASE) for w in words if w]
        if not patterns:
            return []
        query: dict = {
            "processed": True, "is_ad": {"$ne": True}, "is_duplicate": {"$ne": True},
            "published_at": {"$gte": since_iso},
            "$and": [{"$or": [{f: p} for p in patterns for f in ("summary", "tags")]},
                     sensitivity_filter(allowed)],
        }
        if side in ("ua", "ru"):
            query["source_side"] = side
        if category:
            query["category"] = category
        cursor = self.col.find(query, POST_FIELDS).sort([("published_at", -1), ("importance", -1)]).limit(limit)
        posts = [parse(PostDoc, d, "posts") async for d in cursor]
        return [p for p in posts if p]


class SourcesRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["sources"]

    async def get_many(self, ids: list) -> dict[str, SourceDoc]:
        oids = [o for o in (to_object_id(i) for i in set(ids)) if o]
        if not oids:
            return {}
        out = {}
        async for doc in self.col.find({"_id": {"$in": oids}},
                                       {"username": 1, "platform": 1, "source_country": 1, "category": 1}):
            src = parse(SourceDoc, doc, "sources")
            if src:
                out[src.id] = src
        return out


class DigestsRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["digests"]

    async def latest(self, side: str, period: str = "day", start: datetime | None = None,
                     end: datetime | None = None) -> DigestDoc | None:
        """Latest digest of a side; scheduled first, any trigger as a fallback. start/end bound created_at."""
        base: dict = {"period": period, "side": side}
        if start or end:
            base["created_at"] = {**({"$gte": start} if start else {}), **({"$lt": end} if end else {})}
        for query in ({**base, "trigger": "scheduled"}, base):
            async for doc in self.col.find(query).sort("created_at", -1).limit(5):
                digest = parse(DigestDoc, doc, "digests")
                if digest and digest.sections:
                    return digest
        return None


class UsersRepo:
    # Never includes feedback.
    PROFILE_PROJECTION = {"first_name": 1, "daily.date": 1, "daily.selected": 1, "interests": 1,
                          "goals": 1, "identities": 1}

    def __init__(self, db: AsyncDatabase):
        self.col = db["users"]

    async def get_profile(self, user_id: int) -> UserProfileDoc | None:
        doc = await self.col.find_one({"_id": user_id}, self.PROFILE_PROJECTION)
        return parse(UserProfileDoc, doc, "users")

    async def find_by_identity(self, provider: str, subject: str) -> int | None:
        doc = await self.col.find_one({"identities": {"$elemMatch": {"provider": provider, "subject": subject}}},
                                      {"_id": 1})
        return doc["_id"] if doc else None

    async def find_feedback(self, user_id: int, post_id: ObjectId, channels: list[str]) -> dict | None:
        """The user's earlier feedback entry for this post written through one of `channels` (MCP clients)."""
        doc = await self.col.find_one(
            {"_id": user_id},
            {"feedback": {"$elemMatch": {"post_id": post_id, "channel": {"$in": channels}}}})
        entries = (doc or {}).get("feedback") or []
        return entries[0] if entries else None

    async def replace_feedback(self, user_id: int, post_id: ObjectId, channels: list[str], entry: dict,
                               inc: dict, keep: int) -> bool:
        """Removes earlier entries for this post from `channels`, pushes `entry` (capped at `keep`) and applies `inc`.

        Telegram entries (no `channel`) are never touched. Returns False if the user does not exist.
        """
        await self.col.update_one({"_id": user_id},
                                  {"$pull": {"feedback": {"post_id": post_id, "channel": {"$in": channels}}}})
        update: dict = {"$push": {"feedback": {"$each": [entry], "$slice": -keep}}}
        if inc:
            update["$inc"] = inc
        result = await self.col.update_one({"_id": user_id}, update)
        return result.matched_count == 1


class Repos:
    """All repositories over one database; what domain functions receive."""

    def __init__(self, db: AsyncDatabase):
        self.db = db
        self.posts = PostsRepo(db)
        self.sources = SourcesRepo(db)
        self.digests = DigestsRepo(db)
        self.users = UsersRepo(db)
