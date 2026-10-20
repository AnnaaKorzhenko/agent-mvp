
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from db import digests_collection, posts_collection

logger = logging.getLogger("cleanup")

MIN_DAYS = 3
JUNK_DAYS = 3
UNIMPORTANT_DAYS = 14
TEXT_DAYS = 7
TEXT_FIELDS = ["raw_text", "clean_text"]
MAX_DAYS = 180
KEEP_IMPORTANCE = 4
DIGESTS_DAYS = 365


def _cutoff(days: int) -> str:

    days = max(days, MIN_DAYS)
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


async def run_cleanup(dry_run: bool = False) -> dict:
    stats: dict[str, int] = {}

    rules = [
        ("junk", {
            "published_at": {"$lt": _cutoff(JUNK_DAYS)},
            "processed": True,
            "$or": [{"is_ad": True}, {"is_duplicate": True}],
        }),
        ("unimportant", {
            "published_at": {"$lt": _cutoff(UNIMPORTANT_DAYS)},
            "processed": True,
            "importance": {"$lte": 2},
        }),
        ("too_old", {
            "published_at": {"$lt": _cutoff(MAX_DAYS)},
            "processed": True,
            "importance": {"$not": {"$gte": KEEP_IMPORTANCE}},
        }),
    ]

    for name, query in rules:
        if dry_run:
            stats[name] = await posts_collection.count_documents(query)
        else:
            stats[name] = (await posts_collection.delete_many(query)).deleted_count

    strip_query = {
        "published_at": {"$lt": _cutoff(TEXT_DAYS)},
        "processed": True,
        "$or": [{f: {"$exists": True}} for f in TEXT_FIELDS],
    }
    if dry_run:
        stats["raw_text_removed"] = await posts_collection.count_documents(strip_query)
    else:
        res = await posts_collection.update_many(
            strip_query, {"$unset": {f: "" for f in TEXT_FIELDS}, "$set": {"text_removed": True}})
        stats["raw_text_removed"] = res.modified_count

    digests_cutoff = datetime.now(timezone.utc) - timedelta(days=DIGESTS_DAYS)
    digest_query = {"created_at": {"$lt": digests_cutoff}}
    if dry_run:
        stats["old_digests"] = await digests_collection.count_documents(digest_query)
    else:
        stats["old_digests"] = (await digests_collection.delete_many(digest_query)).deleted_count

    stats["posts_left"] = await posts_collection.count_documents({})
    label = "Перевірка (нічого не видалено)" if dry_run else "Очищення"
    logger.info(
        f"{label}: реклама/дублікати {stats['junk']}, неважливі {stats['unimportant']}, "
        f"старші за {MAX_DAYS} дн. {stats['too_old']}, тексти прибрано з {stats['raw_text_removed']}, "
        f"старі зведення {stats['old_digests']}; постів у базі: {stats['posts_left']}"
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_cleanup(dry_run="--dry-run" in sys.argv))