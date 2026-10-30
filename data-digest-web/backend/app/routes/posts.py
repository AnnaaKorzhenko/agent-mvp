import re

from fastapi import APIRouter, Query

from ..config import LANG_MAP, PLATFORM_MAP, POST_FIELDS as F
from ..db import col
from ..normalize import category_id, get, human_date, lang_label, platform_label, values_query

router = APIRouter()


def serialize_post(doc: dict) -> dict:
    summary = get(doc, F["summary"]) or get(doc, F["text"]) or ""
    return {
        "id": str(doc.get("_id")),
        "platform": platform_label(get(doc, F["platform"])),
        "source": get(doc, F["source"]) or "",
        "lang": lang_label(get(doc, F["lang"])),
        "date": human_date(get(doc, F["date"])),
        "category": category_id(get(doc, F["category"])),
        "summary": summary,
        "url": get(doc, F["url"]) or "",
    }


@router.get("/posts")
async def search_posts(
    q: str = "",
    platform: str = "all",
    lang: str = "all",
    limit: int = Query(50, le=200),
):
    query: dict = {}
    if platform != "all" and platform in PLATFORM_MAP:
        query[F["platform"]] = values_query(PLATFORM_MAP[platform])
    if lang != "all" and lang in LANG_MAP:
        query[F["lang"]] = values_query(LANG_MAP[lang])
    q = q.strip()
    if q:
        rx = re.compile(re.escape(q), re.I)
        query["$or"] = [{F["summary"]: rx}, {F["text"]: rx}, {F["source"]: rx}]

    cursor = col("posts").find(query).sort(F["date"], -1).limit(limit)
    return [serialize_post(d) async for d in cursor]
