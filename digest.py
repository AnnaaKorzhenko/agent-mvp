import asyncio
import html
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from db import digests_collection, posts_collection
from summarizer import CATEGORIES, MODEL, client

logger = logging.getLogger(__name__)

DATE_FIELD = "published_at"
KYIV = ZoneInfo("Europe/Kyiv")


MAX_CATCHUP_PERIODS = 3


PERIODS = {
    "ua": {
        "day": (timedelta(days=1), 3, 15, 5),
        "week": (timedelta(days=7), 4, 30, 7),
    },
    "ru": {
        "day": (timedelta(days=1), 3, 10, 3),
        "week": (timedelta(days=7), 3, 20, 5),
    },
}

CATEGORY_TITLES = {
    "ua": {
        "front": "⚔️ Фронт",
        "defense_tech": "🛠 Інновації та технології",
        "investments": "💰 Інвестиції та фінансування",
        "industry": "🏭 Оборонна промисловість",
        "policy": "🏛 Держполітика й закупівлі",
        "international": "🌍 Міжнародна підтримка",
        "sanctions": "🛢 Санкції та економіка рф",
        "other": "📌 Інше",
    },
    "ru": {
        "ru_front": "⚔️ Что пишут о фронте",
        "ru_tech": "🛠 Инновации и вооружение",
        "ru_about_ua": "🗣 Что пишут об Украине",
        "ru_other": "📌 Другое",
    },
}

SECTION_SYSTEM_UA = """You are the editor of a defense news digest. You get numbered short summaries of news in ONE category from Ukrainian sources.
Task:
- merge items about the same event into one;
- drop minor items and repeats, keep at most MAX_ITEMS of the most important;
- each item is ONE sentence IN UKRAINIAN with the key facts (numbers, names, amounts);
- events reported by several sources are usually more important;
- do not add anything of your own.
Return ONLY JSON: {"items": [{"text": "...", "sources": [numbers of the input items]}]}, from most to least important.
Answer language: every "text" in Ukrainian."""

SECTION_SYSTEM_RU = """You are an analyst preparing a review of Russian Telegram channels for Ukrainian defense analysis.
You get numbered short summaries (in Russian) of posts in ONE category from Russian sources.
Task:
- merge items about the same claim or event into one;
- keep at most MAX_ITEMS of the most interesting: what is new on their side and what they think about Ukraine;
- each item is ONE sentence IN RUSSIAN (the language of the sources, do NOT translate), phrased as a claim of the source, e.g. «Рос. каналы утверждают…», «Рос. каналы распространяют…»;
- if several channels repeat the same claim, say so («несколько каналов»);
- do not judge whether claims are true and do not add anything of your own.
Return ONLY JSON: {"items": [{"text": "...", "sources": [numbers of the input items]}]}, from most to least important.
Answer language: every "text" in Russian."""

TG_LIMIT = 4000


RU_PART_MARKER = "<i>🇷🇺 Обзор российских источников (продолжение) — утверждения рос. каналов, не факты</i>"


def _side_query(side: str) -> dict:

    if side == "ru":
        return {"source_side": "ru"}
    return {"source_side": {"$in": ["ua", None]}}


async def _scheduled_since(period: str, side: str, until: datetime, delta: timedelta) -> datetime:

    fallback = until - delta
    last = await digests_collection.find_one(
        {"period": period, "side": side, "trigger": "scheduled"},
        sort=[("created_at", -1)],
    )
    if not last or not last.get("period_end"):
        return fallback
    try:
        prev_end = datetime.fromisoformat(last["period_end"])
    except (TypeError, ValueError):
        return fallback
    if prev_end.tzinfo is None:
        prev_end = prev_end.replace(tzinfo=timezone.utc)
    if prev_end >= until:

        return fallback
    return max(prev_end, until - MAX_CATCHUP_PERIODS * delta)


async def _write_section(category: str, posts: list[dict], max_items: int, side: str) -> tuple[str, dict]:
    """Повертає (HTML для Telegram, структурований розділ для збереження в базу)."""
    listing = "\n".join(
        f"[{i}] (importance {p['importance']}) {p['summary']}"
        for i, p in enumerate(posts, 1)
    )
    system = SECTION_SYSTEM_RU if side == "ru" else SECTION_SYSTEM_UA
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system.replace("MAX_ITEMS", str(max_items))},
                {"role": "user", "content": listing},
            ],
            temperature=0.2,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        items = json.loads(resp.choices[0].message.content).get("items", [])[:max_items]
    except Exception as e:
        logger.warning(f"Не вдалося зібрати розділ {category}: {e}")
        items = [{"text": p["summary"], "sources": [i]} for i, p in enumerate(posts[:max_items], 1)]

    title = CATEGORY_TITLES[side].get(category, category)
    lines = [f"<b>{title}</b>"]
    stored_items = []
    for item in items:
        raw_text = str(item.get("text", "")).strip()
        if not raw_text:
            continue
        src_posts = [posts[n - 1] for n in item.get("sources", [])[:3]
                     if isinstance(n, int) and 1 <= n <= len(posts)]
        urls = [p["post_url"] for p in src_posts if p.get("post_url")]
        links = [f'<a href="{u}">{i}</a>' for i, u in enumerate(urls, 1)]
        suffix = f" [{', '.join(links)}]" if links else ""
        stored_items.append({
            "html": f"{html.escape(raw_text, quote=False)}{suffix}",  # номер додається в build_digest
            "text": raw_text,
            "source_urls": urls,
            "source_post_ids": [p["_id"] for p in src_posts],
        })

    section = {"category": category, "title": title, "items": stored_items}
    return ("\n".join(lines) if len(lines) > 1 else ""), section


def _split(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts, current = [], ""
    for block in text.split("\n\n"):
        if current and len(current) + len(block) + 2 > limit:
            parts.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        parts.append(current)
    return parts


async def _save_digest(doc: dict) -> None:

    try:
        await digests_collection.insert_one(doc)
    except Exception as e:
        logger.error(f"Не вдалося зберегти зведення в базу: {e}")


async def ensure_digest_indexes() -> None:
    await digests_collection.create_index([("side", 1), ("period", 1), ("created_at", -1)])
    await digests_collection.create_index([("trigger", 1), ("created_at", -1)])


async def get_latest_digest(period: str, side: str, max_age_minutes: int | None = None) -> dict | None:

    query: dict = {"period": period, "side": side}
    if max_age_minutes is not None:
        query["created_at"] = {"$gte": datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)}
    return await digests_collection.find_one(query, sort=[("created_at", -1)])


async def build_digest(period: str = "day", side: str = "ua",
                       categories: list[str] | None = None, save: bool = True,
                       trigger: str = "on_demand", until: datetime | None = None) -> list[str]:

    delta, min_importance, per_category, max_items = PERIODS[side][period]
    now = datetime.now(timezone.utc)
    until_dt = (until or now).astimezone(timezone.utc).replace(microsecond=0)

    if trigger == "scheduled":
        since_dt = await _scheduled_since(period, side, until_dt, delta)
    else:
        since_dt = until_dt - delta
    since = since_dt.replace(microsecond=0).isoformat()
    until_iso = until_dt.isoformat()

    query = {
        DATE_FIELD: {"$gte": since, "$lt": until_iso},
        "processed": True,
        "importance": {"$gte": min_importance},
        "is_ad": False,
        "is_duplicate": {"$ne": True},  # репости вже представлені оригіналом
        **_side_query(side),
    }
    if categories:
        query["category"] = {"$in": [c for c in categories if c in CATEGORIES]}

    posts = await (
        posts_collection.find(query, {"summary": 1, "category": 1, "importance": 1,
                                      "views": 1, "post_url": 1})
        .sort([("importance", -1), ("views", -1)])
        .to_list(3000)
    )

    title = "за добу" if period == "day" else "за тиждень"
    title_ru = "за сутки" if period == "day" else "за неделю"
    # У заголовку — точний період, за який зібрано новини (за Києвом),
    # щоб було видно, що ранкове зведення охоплює попередню добу.
    date = (f"{since_dt.astimezone(KYIV):%d.%m %H:%M} – "
            f"{until_dt.astimezone(KYIV):%d.%m %H:%M}")
    if side == "ru":
        header = (f"<b>🇷🇺 Обзор российских источников {title_ru}</b> ({date})\n"
                  "<i>Утверждения российских каналов, не проверенные факты.</i>")
    else:
        header = f"<b>Зведення {title}</b> ({date})"

    if not posts:
        if side == "ru":
            return ["За этот период важных сообщений не найдено."]
        return ["За цей період важливих новин не знайдено."]
    blocks = [header]

    grouped: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        if len(grouped[p["category"]]) < per_category:
            grouped[p["category"]].append(p)

    order = [c for c in CATEGORY_TITLES[side] if c in grouped]
    results = await asyncio.gather(*(_write_section(c, grouped[c], max_items, side) for c in order))
    sections = [section for _, section in results if section["items"]]

    # Наскрізна нумерація пунктів по всьому зведенню (1, 2, 3 … через усі розділи).
    # Номер зберігається в базі (items[].n), щоб відгук «не сподобалась новина №5»
    # можна було зв'язати з конкретним пунктом і постами, з яких він зібраний.
    n = 0
    for section in sections:
        lines = [f"<b>{section['title']}</b>"]
        for item in section["items"]:
            n += 1
            item["n"] = n
            lines.append(f"{n}. {item.pop('html')}")
        blocks.append("\n".join(lines))

    if side != "ru":
        parts = _split("\n\n".join(blocks))
    else:
        raw_parts = _split("\n\n".join(blocks), limit=TG_LIMIT - len(RU_PART_MARKER) - 2)
        parts = [raw_parts[0], *[f"{RU_PART_MARKER}\n\n{p}" for p in raw_parts[1:]]]

    if save:
        await _save_digest({
            "period": period,
            "trigger": trigger,
            "side": side,
            "created_at": now,
            "period_start": since,
            "period_end": until_iso,
            "categories_filter": categories,
            "posts_considered": len(posts),
            "items_count": n,
            "sections": sections,
            "parts_html": parts,
            "model": MODEL,
        })

    return parts
