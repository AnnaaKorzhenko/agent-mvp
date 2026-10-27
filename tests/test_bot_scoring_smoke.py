"""Smoke test of the Telegram bot's scoring paths after the switch to brain.domain.scoring.

Runs the bot's own user_store functions (motor, as in production) against the throwaway test database.
Expected values are the bot's documented behaviour: like +1 / dislike −1 / skip 0 per category and tag,
choice +1, a new reaction replaces the old one and reverses its points.
"""
from datetime import datetime, timezone

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from tests.fixtures.sample_data import DIGEST_UA, P_DRONES

UID = 222000222


@pytest.fixture(scope="module")
async def store(mongo_uri, test_db_name, test_db):
    import user_store
    db = AsyncIOMotorClient(mongo_uri, tz_aware=True)[test_db_name]
    saved = {k: getattr(user_store, k) for k in ("users_collection", "posts_collection",
                                                  "digests_collection", "conversations")}
    user_store.users_collection, user_store.posts_collection = db["users"], db["posts"]
    user_store.digests_collection, user_store.conversations = db["digests"], db["conversations"]
    now = datetime.now(timezone.utc)
    await db["users"].insert_one({"_id": UID, "created_at": now, "onboarded": False, "daily_push": True,
                                  "interests": {"category_scores": {"ua": {}, "ru": {}}, "tag_scores": {}},
                                  "goals": [], "feedback": []})
    yield user_store, db
    for k, v in saved.items():
        setattr(user_store, k, v)


ITEM = {"digest_id": DIGEST_UA, "n": 1, "side": "ua", "category": "defense_tech",
        "source_post_ids": [P_DRONES], "text": "Морські дрони уразили катер."}


async def _interests(db):
    return (await db["users"].find_one({"_id": UID}))["interests"]


async def test_choice_like_dislike_remove(store):
    us, db = store
    await us.add_category_choice(UID, {"ua": ["defense_tech"], "ru": ["ru_front"]})
    i = await _interests(db)
    assert i["category_scores"]["ua"]["defense_tech"] == 1 and i["category_scores"]["ru"]["ru_front"] == 1

    await us.set_reaction(UID, ITEM, "like")
    i = await _interests(db)
    assert i["category_scores"]["ua"]["defense_tech"] == 2
    assert i["tag_scores"] == {"морські дрони": 1, "Чорне море": 1}
    user = await db["users"].find_one({"_id": UID})
    [entry] = user["feedback"]
    assert entry["post_id"] == P_DRONES and entry["reaction"] == "like" and entry["text"] == ""
    assert entry["digest_id"] == DIGEST_UA and entry["item_n"] == 1
    assert await us.get_reaction(UID, DIGEST_UA, 1) == "like"

    await us.set_reaction(UID, ITEM, "dislike")          # replaces: −1 (undo like) −1 (dislike)
    i = await _interests(db)
    assert i["category_scores"]["ua"]["defense_tech"] == 0
    assert i["tag_scores"] == {"морські дрони": -1, "Чорне море": -1}
    assert len((await db["users"].find_one({"_id": UID}))["feedback"]) == 1

    await us.set_reaction(UID, ITEM, "skip")             # skip gives no points; dislike reversed
    i = await _interests(db)
    assert i["category_scores"]["ua"]["defense_tech"] == 1
    assert i["tag_scores"] == {"морські дрони": 0, "Чорне море": 0}

    await us.set_reaction(UID, ITEM, None)               # remove
    assert await us.get_reaction(UID, DIGEST_UA, 1) is None
    assert (await db["users"].find_one({"_id": UID}))["feedback"] == []


async def test_order_and_profile_still_work(store):
    us, db = store
    user = await db["users"].find_one({"_id": UID})
    assert us.category_order(user, "ru", first=[]) == ["ru_front"]
    profile = await us.llm_user_profile(UID, {"ua": {"defense_tech": "Інновації"}, "ru": {}})
    assert "Інновації" in profile


async def test_feedback_report_skips_ratings_without_a_digest(store):
    us, db = store
    await us.set_reaction(UID, ITEM, "dislike")
    await db["users"].update_one({"_id": UID}, {"$push": {"feedback": {"$each": [
        {"target_type": "answer", "reaction": "not_useful", "about": "аналіз", "post_id": None,
         "digest_id": None, "item_n": None, "channel": "claude", "at": datetime.now(timezone.utc)},
        {"target_type": "post", "reaction": "useful", "post_id": P_DRONES, "digest_id": None, "item_n": None,
         "channel": "claude", "at": datetime.now(timezone.utc)}]}}})
    report = await us.feedback_report()
    assert report and all(row["_id"]["digest_id"] is not None for row in report)
    assert any(row["_id"] == {"digest_id": DIGEST_UA, "item_n": 1} and row["dislikes"] >= 1 for row in report)
