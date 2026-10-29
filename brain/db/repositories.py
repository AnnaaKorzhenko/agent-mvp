"""The only place in brain/ that talks to MongoDB. One repository per collection.

Every document read is parsed into a tolerant model. A document that breaks the data contract
is logged with the exact field and skipped, so one bad document never breaks a whole answer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from bson import ObjectId
from bson.errors import InvalidId
from bson.regex import Regex
from pydantic import BaseModel, ValidationError
from pymongo.errors import DuplicateKeyError
from pymongo.asynchronous.database import AsyncDatabase

from brain.db.models import DigestDoc, InsightDoc, PostDoc, SourceDoc, UserProfileDoc

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

    async def search_candidates(self, patterns: list[str], *, since_iso: str, allowed: list[str],
                                side: str | None = None, category: str | None = None,
                                limit: int = 200) -> list[PostDoc]:
        """Posts whose summary or tags match any of the regex patterns (case-insensitive), newest first.

        Plain pattern search: no text index (not approved on the pipeline's collection). Patterns are built
        and results ranked in brain.domain.posts.
        """
        # (*UCP): without it MongoDB's \w is ASCII-only, so Cyrillic letters would count as word boundaries.
        patterns = [Regex("(*UCP)" + p, "i") for p in patterns if p]
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

    async def get(self, digest_id: str | ObjectId) -> DigestDoc | None:
        oid = to_object_id(digest_id)
        return parse(DigestDoc, await self.col.find_one({"_id": oid}), "digests") if oid else None


UserId = int | str     # Telegram users: int; users created for an OAuth login: the ObjectId as hex


def user_key(user_id: UserId) -> int | ObjectId:
    """users._id for a user id: Telegram ids stay ints, hex strings become ObjectIds."""
    if isinstance(user_id, int):
        return user_id
    oid = to_object_id(user_id)
    if oid is None:
        raise ValueError(f"not a user id: {user_id!r}")
    return oid


def _public_id(key: int | ObjectId) -> UserId:
    return key if isinstance(key, int) else str(key)


class UsersRepo:
    # Never includes feedback.
    # Never includes feedback; of insights only their labels (for a count), never the texts.
    PROFILE_PROJECTION = {"first_name": 1, "daily.date": 1, "daily.selected": 1, "interests": 1,
                          "goals": 1, "identities": 1, "insights.sensitivity": 1}

    def __init__(self, db: AsyncDatabase):
        self.col = db["users"]

    async def get_profile(self, user_id: UserId) -> UserProfileDoc | None:
        doc = await self.col.find_one({"_id": user_key(user_id)}, self.PROFILE_PROJECTION)
        return parse(UserProfileDoc, doc, "users")

    async def touch(self, user_id: UserId, every: timedelta = timedelta(minutes=5)) -> None:
        """Sets last_seen to now, at most once per `every` (one cheap conditional write, not one per call)."""
        now = datetime.now(timezone.utc)
        await self.col.update_one({"_id": user_key(user_id), "$or": [{"last_seen": {"$lt": now - every}},
                                                                    {"last_seen": {"$exists": False}}]},
                                  {"$set": {"last_seen": now}})

    async def find_by_identity(self, provider: str, subject: str) -> UserId | None:
        doc = await self.col.find_one({"identities": {"$elemMatch": {"provider": provider, "subject": subject}}},
                                      {"_id": 1})
        return _public_id(doc["_id"]) if doc else None

    async def create_identity_user(self, provider: str, subject: str, channel: str,
                                   email: str | None = None) -> UserId:
        """A new users record for an OAuth login. Never touches Telegram users.

        Shaped so the bot ignores it: no Telegram id, onboarded False, daily_push False.
        The unique index on identities (brain.scripts.setup_indexes) makes concurrent first calls safe.
        """
        now = datetime.now(timezone.utc)
        doc = {"_id": ObjectId(), "source": channel, "email": email, "created_at": now, "last_seen": now,
               "identities": [{"provider": provider, "subject": subject, "created_at": now}],
               "onboarded": False, "daily_push": False,
               "interests": {"category_scores": {"ua": {}, "ru": {}}, "tag_scores": {}},
               "goals": [], "feedback": []}
        try:
            await self.col.insert_one(doc)
            return str(doc["_id"])
        except DuplicateKeyError:
            existing = await self.find_by_identity(provider, subject)
            if existing is None:
                raise
            return existing

    async def find_feedback(self, user_id: UserId, match: dict) -> dict | None:
        """The user's first feedback entry matching `match` (a condition on entry fields), or None."""
        doc = await self.col.find_one({"_id": user_key(user_id)}, {"feedback": {"$elemMatch": match}})
        entries = (doc or {}).get("feedback") or []
        return entries[0] if entries else None

    async def replace_feedback(self, user_id: UserId, match: dict | None, entry: dict, inc: dict,
                               keep: int) -> bool:
        """Removes earlier entries matching `match` (if given), pushes `entry` (capped at `keep`), applies `inc`.

        Callers put a `channel` condition into `match`, so Telegram entries (no `channel`) are never touched.
        Returns False if the user does not exist.
        """
        key = user_key(user_id)
        if match:
            await self.col.update_one({"_id": key}, {"$pull": {"feedback": match}})
        update: dict = {"$push": {"feedback": {"$each": [entry], "$slice": -keep}}}
        if inc:
            update["$inc"] = inc
        result = await self.col.update_one({"_id": key}, update)
        return result.matched_count == 1

    async def update_feedback(self, user_id: UserId, fid: ObjectId, fields: dict) -> bool:
        """Sets `fields` on the user's feedback entry `fid`. Returns False if there is no such entry."""
        result = await self.col.update_one(
            {"_id": user_key(user_id), "feedback.fid": fid},
            {"$set": {f"feedback.$[f].{k}": v for k, v in fields.items()}},
            array_filters=[{"f.fid": fid}])
        return result.matched_count == 1


    async def push_goal(self, user_id: UserId, goal: dict) -> bool:
        """Appends a goal. Returns False if the user does not exist."""
        result = await self.col.update_one({"_id": user_key(user_id)}, {"$push": {"goals": goal}})
        return result.matched_count == 1

    async def deactivate_goal(self, user_id: UserId, index: int, text: str, at: datetime) -> bool:
        """Marks goals[index] inactive, like the bot's remove_goal (history stays). The text check guards
        against the array having changed since it was read."""
        result = await self.col.update_one(
            {"_id": user_key(user_id), f"goals.{index}.text": text, f"goals.{index}.active": True},
            {"$set": {f"goals.{index}.active": False, f"goals.{index}.removed_at": at}})
        return result.modified_count == 1


    async def push_insight(self, user_id: UserId, insight: dict, cap: int) -> str:
        """Appends an insight unless the user already has `cap`, or one of the same kind and normalized text.
        Returns "ok", "duplicate", "full" or "no_user"."""
        key = user_key(user_id)
        same = {"kind": insight["kind"], "norm": insight["norm"]}
        result = await self.col.update_one(
            {"_id": key, f"insights.{cap - 1}": {"$exists": False}, "insights": {"$not": {"$elemMatch": same}}},
            {"$push": {"insights": insight}})
        if result.matched_count:
            return "ok"
        if await self.col.count_documents({"_id": key, "insights": {"$elemMatch": same}}, limit=1):
            return "duplicate"
        return "full" if await self.col.count_documents({"_id": key}, limit=1) else "no_user"

    async def link_insight(self, user_id: UserId, insight_id: ObjectId, conversation_id: ObjectId,
                           message_id: ObjectId | None = None) -> None:
        fields = {"insights.$.conversation_id": conversation_id}
        if message_id is not None:
            fields["insights.$.message_id"] = message_id
        await self.col.update_one({"_id": user_key(user_id), "insights.id": insight_id}, {"$set": fields})

    async def claim_morning_digest(self, user_id: UserId, key: str, day: str, channel: str) -> bool:
        """Marks today's digest (`key`: its digest ids) as delivered to the user by the morning delivery.
        Atomic: True only for the one call that delivers it; False if it was delivered before."""
        result = await self.col.update_one(
            {"_id": user_key(user_id), "morning_digest.key": {"$ne": key}},
            {"$set": {"morning_digest": {"key": key, "date": day, "channel": channel,
                                         "at": datetime.now(timezone.utc)}}})
        return result.modified_count == 1

    async def get_learned(self, user_id: UserId) -> dict | None:
        """users.learned: the profile the learning job wrote (brain/learning/profile.py), if any."""
        doc = await self.col.find_one({"_id": user_key(user_id)}, {"learned": 1})
        return (doc or {}).get("learned")

    async def get_insights(self, user_id: UserId) -> list[InsightDoc]:
        doc = await self.col.find_one({"_id": user_key(user_id)}, {"insights": 1})
        return [i for i in (parse(InsightDoc, x, "users") for x in (doc or {}).get("insights") or []) if i]


async def ensure_user_indexes(db: AsyncDatabase) -> None:
    """One users record per OAuth identity. Run from brain.scripts.setup_indexes, not at import time."""
    await db["users"].create_index([("identities.provider", 1), ("identities.subject", 1)], unique=True,
                                   name="identity_unique",
                                   partialFilterExpression={"identities.subject": {"$exists": True}})


class Repos:
    """All repositories over one database; what domain functions receive."""

    def __init__(self, db: AsyncDatabase):
        self.db = db
        self.posts = PostsRepo(db)
        self.sources = SourcesRepo(db)
        self.digests = DigestsRepo(db)
        self.users = UsersRepo(db)
