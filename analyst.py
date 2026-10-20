"""AI-аналітик: відповідає на питання по базі новин через інструменти.

Як це працює для питання «що зараз в Україні на фронті»:
  1. Бот передає питання в ask(user_id, question).
  2. Модель отримує роль аналітика, сьогоднішню дату, історію розмови і список інструментів.
  3. Модель сама вирішує, що подивитись: наприклад get_digests (зведення за останні дні)
     + search_posts(category="front") + search_posts(side="ru", category="ru_front").
  4. Код виконує ці запити до MongoDB і повертає моделі компактні результати.
  5. Модель може дошукати ще (до MAX_STEPS кроків), а потім пише відповідь з джерелами.
  6. Питання й відповідь зберігаються в історії, щоб працювали уточнення «а чому?».

Модель підключається через OpenAI-сумісний API, тож провайдера можна змінити
змінними середовища, не чіпаючи код:
  ANALYST_BASE_URL, ANALYST_API_KEY, ANALYST_MODEL
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bson import ObjectId
from openai import AsyncOpenAI

from user_store import digests_collection, posts_collection
import user_store

logger = logging.getLogger(__name__)

KYIV = ZoneInfo("Europe/Kyiv")
MODEL = os.getenv("ANALYST_MODEL", "")
client = AsyncOpenAI(base_url=os.getenv("ANALYST_BASE_URL"), api_key=os.getenv("ANALYST_API_KEY"))

MAX_STEPS = 6          # скільки разів модель може звернутися до інструментів за одне питання
HISTORY_LIMIT = 10     # скільки останніх повідомлень розмови пам'ятати
TG_LIMIT = 4000

UA_CATEGORIES = ["front", "defense_tech", "investments", "industry", "policy",
                 "international", "sanctions", "other"]
RU_CATEGORIES = ["ru_front", "ru_tech", "ru_about_ua", "ru_other"]


# ---------------------------------------------------------------------------
# Роль аналітика
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Ти військовий OSINT-аналітик. Сьогодні {today} (Київ).
Ти відповідаєш ЛИШЕ на основі даних з бази новин, які отримуєш через інструменти.
Перш ніж відповідати, збери дані: для загальних питань почни з get_digests, потім уточнюй через search_posts.
Для питань «що зараз» дивись останні 1–3 дні. Якщо питання про динаміку — використай count_trend.

Як відповідати:
- Структура: КОРОТКО (2–3 речення головного) → ФАКТИ (що повідомили джерела) → ОЦІНКА (твій висновок)
  → ЩО ДАЛІ (можливий розвиток і індикатори, які це підтвердять або спростують).
- Біля кожного висновку — рівень впевненості (висока / середня / низька) і чому.
- Розглядай альтернативні пояснення.
- Твердження російських каналів (side=ru) подавай лише як їхні твердження («рос. канали стверджують…»),
  окремо від фактів з українських джерел.
- Після кожного факту став посилання на пост у дужках.
- Якщо даних замало або вони суперечливі — прямо скажи про це. Нічого не вигадуй.
- Пиши українською, простим текстом без таблиць і без markdown-заголовків (це Telegram).
- На уточнення «чому ти так вважаєш» пояснюй ланцюжок: які факти → яке припущення → який висновок.

Категорії українських джерел: {ua_categories}.
Категорії російських джерел: {ru_categories}."""


PROFILE_BLOCK = """

Хто питає (з його вибору категорій і оцінок новин):
{profile}
Враховуй це: починай з тем, які йому важливі, пропонуй ідеї й наступні кроки під його інтереси,
пояснюй, чому новина важлива саме для нього. Але не приховуй важливих подій з інших тем."""


def _system_prompt(profile: str = "") -> str:
    prompt = SYSTEM_PROMPT.format(
        today=datetime.now(KYIV).strftime("%d.%m.%Y, %H:%M"),
        ua_categories=", ".join(UA_CATEGORIES),
        ru_categories=", ".join(RU_CATEGORIES),
    )
    return prompt + (PROFILE_BLOCK.format(profile=profile) if profile else "")


# ---------------------------------------------------------------------------
# Інструменти: що модель може запитати в бази
# ---------------------------------------------------------------------------

def _side_filter(side: str | None) -> dict:
    if side == "ru":
        return {"source_side": "ru"}
    if side == "ua":
        return {"source_side": {"$in": ["ua", None]}}
    return {}


def _since(days: int) -> str:
    days = max(1, min(int(days), 30))
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


async def search_posts(query: str | None = None, side: str | None = None,
                       category: str | None = None, days: int = 3, limit: int = 25) -> list[dict]:
    """Найважливіші оброблені пости за період, з фільтрами і (необов'язково) текстовим пошуком."""
    limit = max(1, min(int(limit), 40))
    q: dict = {
        "processed": True,
        "is_ad": {"$ne": True},
        "is_duplicate": {"$ne": True},
        "published_at": {"$gte": _since(days)},
        **_side_filter(side),
    }
    if category:
        q["category"] = category

    projection = {"summary": 1, "category": 1, "importance": 1, "published_at": 1,
                  "post_url": 1, "source_side": 1, "views": 1}
    if query:
        q["$text"] = {"$search": query}
        projection["score"] = {"$meta": "textScore"}
        sort = [("score", {"$meta": "textScore"}), ("importance", -1)]
    else:
        sort = [("importance", -1), ("views", -1)]

    docs = await posts_collection.find(q, projection).sort(sort).limit(limit).to_list(limit)
    return [{
        "post_id": str(d["_id"]),
        "date": str(d.get("published_at", ""))[:16],
        "side": "ru" if d.get("source_side") == "ru" else "ua",
        "category": d.get("category"),
        "importance": d.get("importance"),
        "summary": d.get("summary"),
        "url": d.get("post_url"),
    } for d in docs]


async def get_digests(side: str = "ua", period: str = "day", count: int = 3) -> list[dict]:
    """Останні зведення за розкладом — швидкий огляд, що відбувалось за кілька днів."""
    count = max(1, min(int(count), 7))
    docs = await (
        digests_collection.find({"side": side, "period": period, "trigger": "scheduled"})
        .sort("created_at", -1).limit(count).to_list(count)
    )
    result = []
    for d in docs:
        result.append({
            "period_start": str(d.get("period_start", ""))[:16],
            "period_end": str(d.get("period_end", ""))[:16],
            "sections": [{
                "title": s.get("title"),
                "items": [{"text": i.get("text"), "urls": i.get("source_urls", [])}
                          for i in s.get("items", [])],
            } for s in d.get("sections", [])],
        })
    return result


async def count_trend(query: str, days: int = 14, side: str | None = None) -> list[dict]:
    """Скільки постів по темі виходило щодня — щоб побачити, чи тема набирає оберти."""
    match = {
        "$text": {"$search": query},
        "processed": True,
        "published_at": {"$gte": _since(days)},
        **_side_filter(side),
    }
    pipeline = [
        {"$match": match},
        {"$group": {"_id": {"$substrBytes": ["$published_at", 0, 10]}, "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    rows = await posts_collection.aggregate(pipeline).to_list(None)
    return [{"day": r["_id"], "count": r["count"]} for r in rows]


TOOL_FUNCS = {
    "search_posts": search_posts,
    "get_digests": get_digests,
    "count_trend": count_trend,
}

SIDE_PARAM = {"type": "string", "enum": ["ua", "ru"],
              "description": "ua — українські джерела, ru — російські (лише їхні твердження)"}

TOOLS = [
    {"type": "function", "function": {
        "name": "get_digests",
        "description": "Останні щоденні або тижневі зведення. Почни з цього для загальних питань «що відбувається».",
        "parameters": {"type": "object", "properties": {
            "side": SIDE_PARAM,
            "period": {"type": "string", "enum": ["day", "week"]},
            "count": {"type": "integer", "description": "Скільки останніх зведень, 1–7"},
        }, "required": ["side"]},
    }},
    {"type": "function", "function": {
        "name": "search_posts",
        "description": "Пошук окремих постів з підсумками й посиланнями. Без query повертає найважливіші пости за період.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Ключові слова, напр. «Покровськ», «РЕБ», «Шахеди»"},
            "side": SIDE_PARAM,
            "category": {"type": "string", "enum": UA_CATEGORIES + RU_CATEGORIES},
            "days": {"type": "integer", "description": "За скільки останніх днів, 1–30"},
            "limit": {"type": "integer", "description": "Скільки постів, до 40"},
        }},
    }},
    {"type": "function", "function": {
        "name": "count_trend",
        "description": "Кількість постів по темі по днях — чи тема набирає оберти.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "days": {"type": "integer", "description": "1–30"},
            "side": SIDE_PARAM,
        }, "required": ["query"]},
    }},
]


async def _run_tool(name: str, raw_args: str) -> object:
    func = TOOL_FUNCS.get(name)
    if func is None:
        return {"error": f"Невідомий інструмент {name}"}
    try:
        args = json.loads(raw_args or "{}")
        return await func(**args)
    except Exception as e:
        logger.warning(f"Інструмент {name} впав: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Пам'ять розмови — колекції conversations і messages (див. user_store.py)
# ---------------------------------------------------------------------------

async def reset_conversation(user_id: int) -> None:
    """Почати розмову з чистого аркуша (наприклад, команда /new)."""
    await user_store.close_conversation(user_id)


# ---------------------------------------------------------------------------
# Головна функція
# ---------------------------------------------------------------------------

async def ensure_analyst_indexes() -> None:
    # Текстовий індекс для search_posts і count_trend. "none" — без стемінгу,
    # бо українська й російська в MongoDB не підтримуються.
    try:
        await posts_collection.create_index(
            [("summary", "text"), ("raw_text", "text")],
            default_language="none", name="posts_text",
        )
    except Exception as e:
        logger.warning(f"Текстовий індекс не створено (можливо, вже є інший): {e}")


async def ask(user_id: int, question: str) -> str:
    conv = await user_store.get_or_start_conversation(user_id, title=question)
    history = await user_store.conversation_history(conv["_id"], limit=HISTORY_LIMIT)
    profile = await user_store.llm_user_profile(user_id)
    messages: list[dict] = [{"role": "system", "content": _system_prompt(profile)}, *history,
                            {"role": "user", "content": question}]
    used_urls: list[str] = []
    sources: dict[str, dict] = {}   # url → {post_id, url}
    tool_log: list[dict] = []
    tokens_in = tokens_out = 0
    answer = ""

    for step in range(MAX_STEPS):
        resp = await client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, temperature=0.2, max_tokens=2000,
        )
        if resp.usage:
            tokens_in += resp.usage.prompt_tokens
            tokens_out += resp.usage.completion_tokens
        msg = resp.choices[0].message

        if not msg.tool_calls:
            answer = msg.content or ""
            break

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            logger.info(f"[{user_id}] крок {step + 1}: {tc.function.name}({tc.function.arguments})")
            tool_log.append({"name": tc.function.name, "args": tc.function.arguments})
            result = await _run_tool(tc.function.name, tc.function.arguments)
            used_urls.extend(_collect_urls(result))
            for item in result if isinstance(result, list) else []:
                if isinstance(item, dict) and item.get("url") and item.get("post_id"):
                    sources[item["url"]] = {"post_id": ObjectId(item["post_id"]), "url": item["url"]}
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
    else:
        # Модель вичерпала кроки — просимо відповісти з того, що вже зібрано
        messages.append({"role": "user", "content": "Достатньо даних. Сформулюй відповідь на основі зібраного."})
        resp = await client.chat.completions.create(model=MODEL, messages=messages,
                                                    temperature=0.2, max_tokens=2000)
        answer = resp.choices[0].message.content or ""

    logger.info(f"[{user_id}] токени: вхід {tokens_in}, вихід {tokens_out}")
    await user_store.add_message(conv["_id"], user_id, "user", question)
    await user_store.add_message(
        conv["_id"], user_id, "assistant", answer,
        sources=[sources.get(u, {"url": u}) for u in dict.fromkeys(used_urls)], tool_calls=tool_log,
        model=MODEL, tokens={"in": tokens_in, "out": tokens_out})
    return answer or "Не вдалося сформувати відповідь. Спробуйте переформулювати питання."


def _collect_urls(result: object) -> list[str]:
    urls: list[str] = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                if item.get("url"):
                    urls.append(item["url"])
                for s in item.get("sections", []):
                    for i in s.get("items", []):
                        urls.extend(i.get("urls", []))
    return urls


def split_for_telegram(text: str, limit: int = TG_LIMIT) -> list[str]:
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


# ---------------------------------------------------------------------------
# Приклад підключення до бота (aiogram 3) — адаптуйте під свій бот
# ---------------------------------------------------------------------------
#
# from aiogram.filters import Command, CommandObject
# from analyst import ask, reset_conversation, split_for_telegram
#
# @router.message(Command("ask"))
# async def cmd_ask(message: Message, command: CommandObject):
#     if message.from_user.id not in ALLOWED_USERS:
#         return
#     question = (command.args or "").strip()
#     if not question:
#         await message.answer("Напишіть питання після /ask, напр.: /ask що зараз на фронті?")
#         return
#     await message.answer("Аналізую базу новин…")
#     answer = await ask(message.from_user.id, question)
#     for part in split_for_telegram(answer):
#         await message.answer(part, disable_web_page_preview=True)
#
# @router.message(Command("new"))
# async def cmd_new(message: Message):
#     await reset_conversation(message.from_user.id)
#     await message.answer("Почали нову розмову.")
