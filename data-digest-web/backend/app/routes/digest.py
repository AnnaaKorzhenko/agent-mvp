from fastapi import APIRouter

from ..config import CATEGORY_MAP, POST_FIELDS as PF, STORY_FIELDS as F
from ..db import col
from ..normalize import category_id, get, relevance_level, side_from, since, values_query

router = APIRouter()

PERIOD_DAYS = {"day": 1, "week": 7}
ORDER = {"high": 0, "medium": 1, "low": 2}


def serialize_story(doc: dict) -> dict:
    count = get(doc, F["posts_count"])
    if count is None:
        count = len(get(doc, F["post_ids"]) or [])
    return {
        "id": str(doc.get("_id")),
        "title": get(doc, F["title"]) or "",
        "summary": get(doc, F["summary"]) or "",
        "category": category_id(get(doc, F["category"])),
        "side": side_from(get(doc, F["side"]), get(doc, F["lang"])),
        "relevance": relevance_level(get(doc, F["relevance"])),
        "posts_count": count,
    }


def post_as_story(doc: dict) -> dict:
    """Запасний варіант, поки сюжети ще не будуються: один пост = один сюжет."""
    summary = get(doc, PF["summary"]) or get(doc, PF["text"]) or ""
    title = summary.split(". ")[0][:140]
    return {
        "id": str(doc.get("_id")),
        "title": title,
        "summary": summary,
        "category": category_id(get(doc, PF["category"])),
        "side": side_from(None, get(doc, PF["lang"])),
        "relevance": relevance_level(get(doc, PF["relevance"])),
        "posts_count": 1,
    }


@router.get("/digest")
async def get_digest(period: str = "day", category: str = "all", limit: int = 50):
    days = PERIOD_DAYS.get(period, 1)

    query: dict = {F["date"]: {"$gte": since(days)}}
    if category != "all" and category in CATEGORY_MAP:
        query[F["category"]] = values_query(CATEGORY_MAP[category])
    docs = [d async for d in col("stories").find(query).sort(F["date"], -1).limit(limit)]
    items = [serialize_story(d) for d in docs]

    if not items:
        pq: dict = {PF["date"]: {"$gte": since(days)}}
        if category != "all" and category in CATEGORY_MAP:
            pq[PF["category"]] = values_query(CATEGORY_MAP[category])
        posts = [d async for d in col("posts").find(pq).sort(PF["date"], -1).limit(limit * 3)]
        items = [post_as_story(d) for d in posts]

    items.sort(key=lambda s: ORDER.get(s["relevance"], 1))
    return items[:limit]
