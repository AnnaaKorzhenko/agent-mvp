import asyncio
import html
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from db import posts_collection
from summarizer import CATEGORIES, MODEL, client

logger = logging.getLogger(__name__)

DATE_FIELD = "published_at"


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
        "ru_front": "⚔️ Що кажуть про фронт",
        "ru_tech": "🛠 Їхні інновації та озброєння",
        "ru_about_ua": "🗣 Що думають про Україну",
        "ru_other": "📌 Інше",
    },
}

SECTION_SYSTEM_UA = """Ти редактор оборонного дайджесту. Тобі дають пронумеровані короткі підсумки новин однієї категорії з українських джерел.
Завдання:
- об'єднай пункти про одну й ту саму подію в один;
- відкинь дрібниці й повтори, залиш до MAX_ITEMS найважливіших;
- кожен пункт — одне речення українською з ключовими фактами (цифри, назви, суми);
- події, про які пишуть кілька джерел, зазвичай важливіші;
- нічого не додавай від себе.
Поверни ЛИШЕ JSON: {"items": [{"text": "...", "sources": [номери вхідних пунктів]}]}, від найважливішого до менш важливого."""

SECTION_SYSTEM_RU = """Ти аналітик, який готує огляд російських Telegram-каналів для української оборонної аналітики.
Тобі дають пронумеровані короткі підсумки постів однієї категорії з російських джерел.
Завдання:
- об'єднай пункти про одне й те саме твердження чи подію в один;
- залиш до MAX_ITEMS найцікавіших: що нового у них і що вони думають про Україну;
- кожен пункт — одне речення українською у формі твердження джерела («рос. джерела стверджують…», «рос. канали поширюють…»);
- якщо кілька каналів повторюють одне твердження, зазнач це («кілька каналів»);
- не оцінюй правдивість і нічого не додавай від себе.
Поверни ЛИШЕ JSON: {"items": [{"text": "...", "sources": [номери вхідних пунктів]}]}, від найважливішого до менш важливого."""

TG_LIMIT = 4000


RU_PART_MARKER = "<i>🇷🇺 Огляд російських джерел (продовження) — твердження рос. каналів, не факти</i>"


def _side_query(side: str) -> dict:

    if side == "ru":
        return {"source_side": "ru"}
    return {"source_side": {"$in": ["ua", None]}}


async def _write_section(category: str, posts: list[dict], max_items: int, side: str) -> str:
    listing = "\n".join(
        f"[{i}] (важливість {p['importance']}) {p['summary']}"
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

    lines = [f"<b>{CATEGORY_TITLES[side].get(category, category)}</b>"]
    for item in items:
        text = html.escape(str(item.get("text", "")).strip())
        if not text:
            continue
        links = []
        for n in item.get("sources", [])[:3]:
            if isinstance(n, int) and 1 <= n <= len(posts) and posts[n - 1].get("post_url"):
                links.append(f'<a href="{posts[n - 1]["post_url"]}">{len(links) + 1}</a>')
        suffix = f" [{', '.join(links)}]" if links else ""
        lines.append(f"• {text}{suffix}")
    return "\n".join(lines) if len(lines) > 1 else ""


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


async def build_digest(period: str = "day", side: str = "ua",
                       categories: list[str] | None = None) -> list[str]:

    delta, min_importance, per_category, max_items = PERIODS[side][period]
    since = (datetime.now(timezone.utc) - delta).isoformat()

    query = {
        DATE_FIELD: {"$gte": since},
        "processed": True,
        "importance": {"$gte": min_importance},
        "is_ad": False,
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
    date = datetime.now().strftime("%d.%m.%Y")
    if side == "ru":
        header = (f"<b>🇷🇺 Огляд російських джерел {title}</b> ({date})\n"
                  "<i>Твердження рос. каналів, не перевірені факти.</i>")
    else:
        header = f"<b>Зведення {title}</b> ({date})"

    if not posts:
        return ["За цей період важливих новин не знайдено."]
    blocks = [header]

    grouped: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        if len(grouped[p["category"]]) < per_category:
            grouped[p["category"]].append(p)

    order = [c for c in CATEGORY_TITLES[side] if c in grouped]
    sections = await asyncio.gather(*(_write_section(c, grouped[c], max_items, side) for c in order))
    blocks.extend(s for s in sections if s)

    if side != "ru":
        return _split("\n\n".join(blocks))

    parts = _split("\n\n".join(blocks), limit=TG_LIMIT - len(RU_PART_MARKER) - 2)
    return [parts[0], *[f"{RU_PART_MARKER}\n\n{p}" for p in parts[1:]]]