import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

from openai import APIConnectionError, AsyncOpenAI
from pymongo import UpdateOne

from cleaning import clean_post_text, is_ad, learn_channel_footers, normalize_for_model
from db import posts_collection, sources_collection

logger = logging.getLogger(__name__)

client = AsyncOpenAI(
    base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
    api_key=os.getenv("LLM_API_KEY", "local"),
)
MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b-instruct")
CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "4"))

MIN_LEN = 30
MAX_INPUT_CHARS = 6000
MAX_ERRORS = 3
PROMPT_VERSION = 6  # 5: рос. підсумки російською; 6: промпти англійською


COUNTRY_FIELDS = ("country", "source_country")
SIDE_BY_COUNTRY = {
    "ukraine": "ua", "ua": "ua", "україна": "ua",
    "russia": "ru", "ru": "ru", "росія": "ru", "рф": "ru",
}


CATEGORIES_UA = {
    "front": "Frontline events: combat, changes to the front line, strikes, shelling, losses",
    "defense_tech": "Innovation and technology: drones, EW, air defense, robotic systems, software, new developments and their use at the front",
    "investments": "Investment and funding: rounds, grants, funds, contracts, defense startups",
    "industry": "Defense industry: production, localization, joint ventures, exports",
    "policy": "Government policy and procurement: decisions of the Ministry of Defense, Cabinet, Parliament, codification, regulation",
    "international": "International support: aid packages, agreements with partners, allies' positions",
    "sanctions": "Sanctions and the Russian economy: oil exports, sanctions evasion, strikes on refineries, the enemy's defense industry",
    "other": "Anything that does not fit the categories above",
}


CATEGORIES_RU = {
    "ru_front": "Their claims about the front: combat, advances, strikes, losses",
    "ru_tech": "Their innovation and weapons: new drones, EW, missiles, developments, production",
    "ru_about_ua": "What they think about Ukraine: assessments, forecasts, propaganda about Ukraine's government, army, society",
    "ru_other": "Other: Russia's domestic events, economy, politics",
}

CATEGORIES = {**CATEGORIES_UA, **CATEGORIES_RU}


def _cats(cats: dict) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in cats.items())


SYSTEM_UA = f"""You are a defense-sector analyst. You read ONE post from a Ukrainian Telegram channel and return ONLY a JSON object.

Categories (field "category" is exactly one key):
{_cats(CATEGORIES_UA)}

Importance (field "importance", integer 1-5):
5 - an event that substantially changes the situation: a major breakthrough or loss of positions, a massive attack, a new technology used in combat for the first time, a large contract or aid package
4 - significant news with specifics: numbers, system names, amounts, participants, dates
3 - useful but routine news
2 - low information: general statements without facts, repeats, emotional posts, opinions
1 - noise: ads, fundraising without context, stream announcements, memes, greetings

Response format:
{{"summary": "...", "category": "category key", "tags": ["..."], "importance": 3, "is_ad": false}}

Rules:
- "summary": 1-2 sentences IN UKRAINIAN. Only facts from the post; keep key numbers, names, places and weapon systems.
- If the post relays claims of the Russian side (Russian MoD, Russian media or officials), keep the attribution and do not present them as fact, e.g. «за словами МО рф…», «рос. пропаганда поширює…».
- "tags": up to 5 short tags IN UKRAINIAN, e.g. дрони, РЕБ, ППО, Brave1.
- Do not invent anything and do not add facts that are not in the post.

Answer language: summary and tags in Ukrainian. Keys and category values exactly as given above."""

SYSTEM_RU = f"""You are an analyst monitoring Russian Telegram channels to know briefly what is new on the enemy side and what they think about Ukraine.
The post is written in Russian. You return ONLY a JSON object.

Categories (field "category" is exactly one key):
{_cats(CATEGORIES_RU)}

Importance (field "importance", integer 1-5):
5 - claims of capturing settlements, new weapons, official statements by Russia's leadership
4 - specifics: places, numbers, names of equipment; a notable new opinion about Ukraine
3 - routine posts on topic
2 - emotions without specifics, repeats
1 - noise: ads, fundraising, announcements, memes

Response format:
{{"summary": "...", "category": "category key", "tags": ["..."], "importance": 3, "is_ad": false}}

Rules:
- "summary": 1-2 sentences IN RUSSIAN, the language of the post. Do NOT translate into Ukrainian or English.
  Only facts from the post; keep key numbers, places and equipment names.
  Phrase it as a claim of the source, not as fact, e.g. «Канал утверждает, что…», «По данным российских источников…», «Канал распространяет тезис, что…».
  Replace propaganda euphemisms with neutral words: «война» instead of «СВО», «власти Украины» instead of «киевский режим».
- "tags": up to 5 short tags IN UKRAINIAN, e.g. дрони, РЕБ, Покровськ (so search works the same for both sides).
- Do not invent anything and do not add facts that are not in the post.

Answer language: summary in Russian, tags in Ukrainian. Keys and category values exactly as given above."""




_PUNCT_START = re.compile(r"^[,.;:!?)%»”]")


def join_fragments(text: str) -> str:

    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        if not out:
            out.append(p)
        elif _PUNCT_START.match(p):
            out[-1] += p
        elif out[-1][-1] in ".!?…:":
            out.append(p)
        else:
            out[-1] += " " + p
    return "\n".join(out)




def _parse(raw: str, side: str) -> dict:
    data = json.loads(raw)
    allowed = CATEGORIES_RU if side == "ru" else CATEGORIES_UA
    category = data.get("category")
    if category not in allowed:
        category = "ru_other" if side == "ru" else "other"
    try:
        importance = max(1, min(5, int(data.get("importance", 2))))
    except (TypeError, ValueError):
        importance = 2
    is_ad_flag = bool(data.get("is_ad", False))
    if is_ad_flag:
        importance = 1
    tags = [str(t).strip().lower() for t in (data.get("tags") or []) if str(t).strip()][:5]
    summary = str(data.get("summary", "")).strip()
    if not summary:
        raise ValueError("порожнє самарі")

    return {
        "summary": summary,
        "category": category,
        "tags": tags,
        "importance": importance,
        "is_ad": is_ad_flag,
    }


async def analyze_text(text: str, side: str, sem: asyncio.Semaphore, attempts: int = 2,
                       system: str | None = None) -> dict:
    messages = [
        {"role": "system", "content": system or (SYSTEM_RU if side == "ru" else SYSTEM_UA)},
        {"role": "user", "content": normalize_for_model(text)[:MAX_INPUT_CHARS]},
    ]
    last_error: Exception | None = None
    for attempt in range(attempts):
        async with sem:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.1 + 0.2 * attempt,  # друга спроба трохи інша, щоб не повторити ту саму помилку
                max_tokens=600,
                response_format={"type": "json_object"},
            )
        try:
            return _parse(resp.choices[0].message.content, side)
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
    raise last_error




_footer_cache: dict = {}
_side_cache: dict = {}


async def _channel_footers(channel_id) -> set[str]:

    if channel_id is None:
        return set()
    if channel_id not in _footer_cache:
        docs = await (
            posts_collection.find({"channel_id": channel_id, "raw_text": {"$nin": [None, ""]}}, {"raw_text": 1})
            .sort("published_at", -1)
            .limit(50)
            .to_list(50)
        )
        _footer_cache[channel_id] = learn_channel_footers([d.get("raw_text") or "" for d in docs])
    return _footer_cache[channel_id]


async def _channel_side(channel_id) -> str:

    if channel_id is None:
        return "unknown"
    if channel_id not in _side_cache:
        src = await sources_collection.find_one(
            {"_id": channel_id}, {**{f: 1 for f in COUNTRY_FIELDS}, "username": 1}
        ) or {}
        country = next((str(src[f]).strip() for f in COUNTRY_FIELDS if src.get(f)), "")
        side = SIDE_BY_COUNTRY.get(country.lower(), "unknown")
        if channel_id not in _side_cache:
            _side_cache[channel_id] = side
            if side == "unknown":
                logger.warning(
                    f"Канал {src.get('username', channel_id)}: країна '{country or '—'}' не розпізнана "
                    f"(очікується Ukraine/UA або Russia/RU) — його пости не потраплять у зведення."
                )
    return _side_cache[channel_id]



async def _process_post(post: dict, sem: asyncio.Semaphore) -> UpdateOne:
    now = datetime.now(timezone.utc)
    raw = post["raw_text"]
    side = await _channel_side(post.get("channel_id"))
    base = {"source_side": side, "processed": True, "analyzed_at": now, "prompt_version": PROMPT_VERSION}

    if is_ad(raw):
        return UpdateOne({"_id": post["_id"]}, {"$set": {
            **base, "summary": "", "category": "ru_other" if side == "ru" else "other", "tags": [],
            "importance": 1, "is_ad": True,
        }})

    footers = await _channel_footers(post.get("channel_id"))
    text = join_fragments(clean_post_text(raw, footers))

    if len(text) < MIN_LEN:
        return UpdateOne({"_id": post["_id"]}, {"$set": {
            **base, "clean_text": text, "summary": text,
            "category": "ru_other" if side == "ru" else "other",
            "tags": [], "importance": 1, "is_ad": False,
        }})

    try:
        result = await analyze_text(text, side, sem)
    except APIConnectionError:
        raise
    except Exception as e:
        logger.warning(f"Помилка аналізу {post['_id']}: {e}")
        return UpdateOne({"_id": post["_id"]}, {
            "$set": {"analysis_error": str(e)[:500]},
            "$inc": {"error_count": 1},
        })

    return UpdateOne({"_id": post["_id"]}, {
        "$set": {**base, **result, "clean_text": text},
        "$unset": {"analysis_error": ""},
    })


async def ensure_indexes() -> None:
    await posts_collection.create_index([("published_at", -1), ("importance", -1)])
    await posts_collection.create_index([("processed", 1)])
    await posts_collection.create_index([("category", 1), ("published_at", -1)])
    await posts_collection.create_index([("source_side", 1), ("published_at", -1)])
    await posts_collection.create_index([("post_url", 1)])
    await posts_collection.create_index([("channel_id", 1), ("published_at", -1)])


async def analyze_pending_posts(chunk: int = 20) -> None:

    try:
        await client.models.list()
    except APIConnectionError:
        logger.error(
            f"Модель недоступна за адресою {client.base_url}. "
            "Перевірте, що Ollama/vLLM запущені (або LLM_BASE_URL). Аналіз пропущено."
        )
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    query = {
        "processed": {"$ne": True},
        "raw_text": {"$nin": [None, ""]},
        "error_count": {"$not": {"$gte": MAX_ERRORS}},
    }
    done = 0
    while True:
        posts = await posts_collection.find(query, {"raw_text": 1, "channel_id": 1}).limit(chunk).to_list(chunk)
        if not posts:
            break
        try:
            ops = await asyncio.gather(*(_process_post(p, sem) for p in posts))
        except APIConnectionError:
            logger.error("Втрачено з'єднання з моделлю, прогін зупинено. Пости оброблять наступного разу.")
            return
        await posts_collection.bulk_write(ops)
        done += len(posts)
        logger.info(f"Проаналізовано {done}")
    logger.info("Аналіз завершено" if done else "Нових постів для аналізу немає")
