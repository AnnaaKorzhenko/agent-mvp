import asyncio
import html
import logging
import os
import re
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from motor.motor_asyncio import AsyncIOMotorClient
load_dotenv()
MONGO_URI = os.environ["MONGO_URI"]
db = AsyncIOMotorClient(MONGO_URI, tz_aware=True)["News"]
digests_collection = db["digests"]



from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, User)
from bson import ObjectId

from db import db, digests_collection

logger = logging.getLogger("bot")
KYIV = ZoneInfo("Europe/Kyiv")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
TG_LIMIT = 4000
DAILY_PUSH = os.environ.get("DAILY_PUSH", "1") == "1"
PUSH_CHECK_SECONDS = 300

users_collection = db["users"]
interest_log = db["interest_log"]
daily_choices = db["daily_choices"]
deliveries_collection = db["deliveries"]

dp = Dispatcher()

HELP = (
    "<b>Зведення новин</b>\n\n"
    "/all_day — усе за добу: українські джерела + огляд російських\n"
    "/all_week — усе за тиждень\n\n"
    "/day — українські джерела за добу\n"
    "/week — українські джерела за тиждень\n"
    "/ru_day — огляд російських джерел за добу\n"
    "/ru_week — огляд російських джерел за тиждень\n\n"
    "/categories — змінити категорії, які показувати першими"
)

# Назви категорій для вибору (українською для обох сторін)
CATEGORY_LABELS = {
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
        "ru_front": "⚔️ Їхні заяви про фронт",
        "ru_tech": "🛠 Їхні інновації та озброєння",
        "ru_about_ua": "🗣 Що думають про Україну",
        "ru_other": "📌 Інше (рф)",
    },
}

FEEDBACK_PROMPT = ("Позначте номери новин, які були неважливими чи неточними, "
                   "і натисніть «Надіслати відгук».")


def _is_allowed(user_id: int | None) -> bool:
    return not ALLOWED or (user_id is not None and user_id in ALLOWED)


def _allowed(message: Message) -> bool:
    return _is_allowed(message.from_user.id if message.from_user else None)


async def _deny(message: Message) -> None:
    uid = message.from_user.id if message.from_user else "?"
    await message.answer(f"Доступ закритий. Ваш ID: <code>{uid}</code> — передайте його адміністратору.")
    logger.warning(f"Відмовлено в доступі: {uid}")


async def touch_user(u: User) -> dict:
    """Створює запис користувача при першому зверненні, оновлює ім'я й час останньої активності."""
    now = datetime.now(timezone.utc)
    await users_collection.update_one(
        {"_id": u.id},
        {
            "$set": {"username": u.username, "first_name": u.first_name, "last_name": u.last_name,
                     "language_code": u.language_code, "last_seen": now},
            "$setOnInsert": {"created_at": now, "categories": {"ua": [], "ru": []},
                             "onboarded": False, "daily_push": True},
        },
        upsert=True,
    )
    return await users_collection.find_one({"_id": u.id})


CAT_STEP_TEXT = {
    "ua": ("<b>Крок 1 з 2. Українські джерела</b>\n\n"
           "Оберіть категорії, які вам найцікавіші — вони будуть першими у зведенні. "
           "Можна кілька; порядок вибору = порядок у зведенні. Інші новини теж будуть, але нижче."),
    "ru": ("<b>Крок 2 з 2. Огляд російських джерел</b>\n\n"
           "Оберіть, що з російських каналів вам найцікавіше."),
}


def _cat_keyboard(side: str, selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for key, label in CATEGORY_LABELS[side].items():
        mark = f"✅ {selected.index(key) + 1}. " if key in selected else "▫️ "
        rows.append([InlineKeyboardButton(text=mark + label, callback_data=f"cat:{side}:{key}")])
    nav = "Далі → російські джерела" if side == "ua" else "Готово"
    rows.append([InlineKeyboardButton(text=nav, callback_data=f"catnext:{side}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ask_categories(message: Message, user: dict, side: str = "ua") -> None:
    selected = (user.get("categories") or {}).get(side, [])
    await message.answer(CAT_STEP_TEXT[side], reply_markup=_cat_keyboard(side, selected))


@dp.callback_query(F.data.startswith("cat:"))
async def on_cat_toggle(cq: CallbackQuery) -> None:
    if not _is_allowed(cq.from_user.id):
        return await cq.answer("Доступ закритий")
    _, side, key = cq.data.split(":", 2)
    user = await touch_user(cq.from_user)
    selected = list((user.get("categories") or {}).get(side, []))
    if key in selected:
        selected.remove(key)
    elif key in CATEGORY_LABELS[side]:
        selected.append(key)
    await users_collection.update_one({"_id": cq.from_user.id}, {"$set": {f"categories.{side}": selected}})
    await cq.answer()
    await cq.message.edit_reply_markup(reply_markup=_cat_keyboard(side, selected))


@dp.callback_query(F.data.startswith("catnext:"))
async def on_cat_next(cq: CallbackQuery) -> None:
    if not _is_allowed(cq.from_user.id):
        return await cq.answer("Доступ закритий")
    side = cq.data.split(":", 1)[1]
    user = await touch_user(cq.from_user)
    await cq.answer()
    if side == "ua":
        selected = (user.get("categories") or {}).get("ru", [])
        await cq.message.edit_text(CAT_STEP_TEXT["ru"], reply_markup=_cat_keyboard("ru", selected))
        return

    await users_collection.update_one({"_id": cq.from_user.id},
                                      {"$set": {"onboarded": True, "categories_updated_at": datetime.now(timezone.utc)}})
    cats = user.get("categories") or {}
    await log_interest(cq.from_user.id, cats, source="categories")
    chosen = [CATEGORY_LABELS["ua"][k] for k in cats.get("ua", [])] + \
             [CATEGORY_LABELS["ru"][k] for k in cats.get("ru", [])]
    summary = "\n".join(f"• {c}" for c in chosen) if chosen else "нічого — новини будуть у стандартному порядку"
    await cq.message.edit_text(f"Збережено. Першими показуватимемо:\n{summary}\n\nЗмінити: /categories")
    await cq.message.answer(HELP)



def _item_html(item: dict) -> str:
    links = [f'<a href="{u}">{i}</a>' for i, u in enumerate(item.get("source_urls", []), 1)]
    return html.escape(item.get("text", ""), quote=False) + (f" [{', '.join(links)}]" if links else "")


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


def _original_numbers(sections: list[dict]) -> dict[tuple[int, int], int]:
    numbers, c = {}, 0
    for i, section in enumerate(sections):
        for j, item in enumerate(section.get("items", [])):
            c += 1
            numbers[(i, j)] = item.get("n", c)
    return numbers


def render_digest(doc: dict, preferred: list[str], k: int, refs: list[dict]) -> tuple[list[str], int]:

    sections = doc.get("sections") or []
    if not sections or not doc.get("parts_html"):
        return doc.get("parts_html", []), k
    orig_n = _original_numbers(sections)
    rank = {c: i for i, c in enumerate(preferred)}
    order = sorted(range(len(sections)), key=lambda i: (rank.get(sections[i]["category"], len(rank)), i))

    header = doc["parts_html"][0].split("\n\n")[0]
    blocks = [header]
    for i in order:
        section = sections[i]
        star = "⭐ " if section["category"] in rank else ""
        lines = [f"<b>{star}{html.escape(section['title'], quote=False)}</b>"]
        for j, item in enumerate(section.get("items", [])):
            k += 1
            refs.append({"digest_id": doc["_id"], "n": orig_n[(i, j)], "side": doc.get("side"),
                         "category": section["category"]})
            lines.append(f"{k}. {_item_html(item)}")
        blocks.append("\n".join(lines))
    return _split("\n\n".join(blocks)), k


async def _safe_send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramBadRequest:
        await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text), parse_mode=None)


async def send_digests(bot: Bot, chat_id: int, user: dict, entries: list, kind: str,
                       prefs: dict | None = None, note: str | None = None) -> None:

    if prefs is None:
        prefs = await effective_prefs(user)
    refs: list[dict] = []
    k = 0
    digest_ids = []
    for entry in entries:
        if isinstance(entry, dict):
            digest_ids.append(entry["_id"])
            parts, k = render_digest(entry, prefs.get(entry.get("side"), []), k, refs)
        else:
            parts = entry
        for part in parts:
            await _safe_send(bot, chat_id, part)
            await asyncio.sleep(0.4)

    if note:
        await bot.send_message(chat_id, note)
    delivery = {"user_id": user["_id"], "kind": kind, "digest_ids": digest_ids, "items": refs, "prefs": prefs,
                "disliked": [], "submitted": False, "sent_at": datetime.now(timezone.utc)}
    delivery_id = (await deliveries_collection.insert_one(delivery)).inserted_id
    if refs:
        await bot.send_message(chat_id, FEEDBACK_PROMPT, reply_markup=_fb_keyboard(delivery_id, len(refs), set()))


FB_PER_ROW = 6


def _fb_keyboard(delivery_id, count: int, disliked: set[int]) -> InlineKeyboardMarkup:
    did = str(delivery_id)
    buttons = [InlineKeyboardButton(text=f"❌ {n}" if n in disliked else str(n), callback_data=f"fb:{did}:{n}")
               for n in range(1, count + 1)]
    rows = [buttons[i:i + FB_PER_ROW] for i in range(0, len(buttons), FB_PER_ROW)]
    rows.append([
        InlineKeyboardButton(text="👍 Усе корисно", callback_data=f"fbok:{did}"),
        InlineKeyboardButton(text="📨 Надіслати відгук", callback_data=f"fbs:{did}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.regexp(r"^(fb|fbs|fbok):"))
async def on_feedback(cq: CallbackQuery) -> None:
    parts = cq.data.split(":")
    prefix, did = parts[0], ObjectId(parts[1])
    delivery = await deliveries_collection.find_one({"_id": did, "user_id": cq.from_user.id})
    if not delivery:
        return await cq.answer("Зведення не знайдено")
    if delivery.get("submitted"):
        await cq.answer("Відгук уже надіслано, дякуємо!")
        return await cq.message.edit_text("Дякуємо за відгук!")

    if prefix == "fb":
        n = int(parts[2])
        disliked = set(delivery.get("disliked", []))
        disliked.symmetric_difference_update({n})
        await deliveries_collection.update_one({"_id": did}, {"$set": {"disliked": sorted(disliked)}})
        await cq.answer(f"Новину {n} позначено" if n in disliked else f"Позначку з {n} знято")
        await cq.message.edit_reply_markup(reply_markup=_fb_keyboard(did, len(delivery["items"]), disliked))
        return

    disliked = [] if prefix == "fbok" else sorted(delivery.get("disliked", []))
    await deliveries_collection.update_one({"_id": did}, {"$set": {
        "disliked": disliked, "submitted": True, "submitted_at": datetime.now(timezone.utc)}})
    await cq.answer("Відгук збережено")
    text = ("Дякуємо! Позначено новини: " + ", ".join(map(str, disliked))) if disliked else "Дякуємо за відгук!"
    await cq.message.edit_text(text)


async def feedback_report(days: int = 7) -> list[dict]:

    since = datetime.now(timezone.utc) - timedelta(days=days)
    counts: dict[tuple, dict] = {}
    async for d in deliveries_collection.find({"submitted": True, "submitted_at": {"$gte": since}}):
        for k in d.get("disliked", []):
            if 1 <= k <= len(d["items"]):
                ref = d["items"][k - 1]
                key = (ref["digest_id"], ref["n"])
                counts.setdefault(key, {**ref, "dislikes": 0})["dislikes"] += 1
    result = []
    for (digest_id, n), row in counts.items():
        doc = await digests_collection.find_one({"_id": digest_id}, {"sections": 1, "created_at": 1}) or {}
        sections = doc.get("sections", [])
        for (i, j), num in _original_numbers(sections).items():
            if num == n:
                item = sections[i]["items"][j]
                row.update(text=item.get("text"), source_urls=item.get("source_urls", []),
                           date=doc.get("created_at"))
        result.append(row)
    return sorted(result, key=lambda r: -r["dislikes"])


async def _get_digest(period: str, side: str):

    doc = await digests_collection.find_one(
        {"period": period, "side": side, "trigger": "scheduled"}, sort=[("created_at", -1)])
    if not doc:
        doc = await digests_collection.find_one({"period": period, "side": side}, sort=[("created_at", -1)])
    if doc and doc.get("parts_html"):
        return doc
    what = "тижневе" if period == "week" else "щоденне"
    return [f"Ще немає готового зведення ({what}, {'рос.' if side == 'ru' else 'укр.'} джерела). "
            "Щоденне з'являється щоранку близько 6:30, тижневе — по понеділках."]


async def _send_digest(message: Message, period: str, sides: list[str]) -> None:
    if not _allowed(message):
        return await _deny(message)
    user = await touch_user(message.from_user)

    try:
        entries = [await _get_digest(period, side) for side in sides]
    except Exception:
        logger.exception("Помилка читання зведення з бази")
        await message.answer("Не вдалося отримати зведення. Спробуйте пізніше.")
        return
    await send_digests(message.bot, message.chat.id, user, list(entries), kind=f"on_demand_{period}")



LEARN_DAYS = 7
HISTORY_DAYS = 21
DECAY = 0.85
DISLIKE_PENALTY = 0.5
APPROVAL_BONUS = 0.05
ASK_TIMEOUT_HOURS = 3


def _kyiv_date() -> str:
    return datetime.now(KYIV).strftime("%Y-%m-%d")


async def log_interest(user_id: int, categories: dict, source: str) -> None:
    await interest_log.insert_one({
        "user_id": user_id, "date": _kyiv_date(), "source": source,
        "categories": {"ua": list(categories.get("ua", [])), "ru": list(categories.get("ru", []))},
        "at": datetime.now(timezone.utc),
    })


async def days_of_choices(user_id: int) -> int:

    return len(await interest_log.distinct("date", {"user_id": user_id, "source": "daily"}))


async def learned_prefs(user_id: int) -> dict:

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=HISTORY_DAYS)
    scores: dict[str, dict[str, float]] = {"ua": {}, "ru": {}}

    def add(side: str, cat: str, value: float) -> None:
        if side in scores and cat in CATEGORY_LABELS[side]:
            scores[side][cat] = scores[side].get(cat, 0.0) + value

    async for log in interest_log.find({"user_id": user_id, "at": {"$gte": since}}):
        w = DECAY ** max((now - log["at"]).days, 0)
        for side in ("ua", "ru"):
            chosen = log["categories"].get(side, [])
            for pos, cat in enumerate(chosen):
                add(side, cat, w * (1.0 + 0.2 * (len(chosen) - pos - 1)))  # перші в списку — трохи вагоміші

    async for d in deliveries_collection.find({"user_id": user_id, "submitted": True, "submitted_at": {"$gte": since}}):
        w = DECAY ** max((now - d["submitted_at"]).days, 0)
        disliked = set(d.get("disliked", []))
        for k, ref in enumerate(d.get("items", []), 1):
            add(ref.get("side"), ref.get("category"), -w * DISLIKE_PENALTY if k in disliked else w * APPROVAL_BONUS)

    return {side: [c for c, v in sorted(sc.items(), key=lambda x: -x[1]) if v > 0] for side, sc in scores.items()}


async def effective_prefs(user: dict) -> dict:
    """Порядок категорій для зведення на запит: вивчений, якщо даних уже достатньо, інакше — збережений вибір."""
    if await days_of_choices(user["_id"]) >= LEARN_DAYS:
        learned = await learned_prefs(user["_id"])
        if learned["ua"] or learned["ru"]:
            return learned
    return user.get("categories") or {"ua": [], "ru": []}


def _daily_keyboard(choice_id, selected: dict) -> InlineKeyboardMarkup:
    cid = str(choice_id)
    rows = []
    for side, title in (("ua", "Українські джерела"), ("ru", "Російські джерела")):
        rows.append([InlineKeyboardButton(text=f"— {title} —", callback_data="noop")])
        for key, label in CATEGORY_LABELS[side].items():
            mark = "✅ " if key in selected.get(side, []) else "▫️ "
            rows.append([InlineKeyboardButton(text=mark + label, callback_data=f"dc:{cid}:{side}:{key}")])
    rows.append([InlineKeyboardButton(text="📰 Показати зведення", callback_data=f"dcgo:{cid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


DAILY_QUESTION = ("<b>Ранкове зведення готове.</b>\n"
                  "Що вас сьогодні цікавить найбільше? Ці категорії будуть першими. "
                  "Позначте й натисніть «Показати зведення».")


async def ask_daily(bot: Bot, user: dict, docs: list[dict]) -> None:
    last = await interest_log.find_one({"user_id": user["_id"]}, sort=[("at", -1)])
    preselected = (last or {}).get("categories") or user.get("categories") or {"ua": [], "ru": []}
    choice_id = (await daily_choices.insert_one({
        "user_id": user["_id"], "digest_ids": [d["_id"] for d in docs], "selected": preselected,
        "status": "pending", "asked_at": datetime.now(timezone.utc),
    })).inserted_id
    await bot.send_message(user["_id"], DAILY_QUESTION, reply_markup=_daily_keyboard(choice_id, preselected))


@dp.callback_query(F.data == "noop")
async def on_noop(cq: CallbackQuery) -> None:
    await cq.answer()


@dp.callback_query(F.data.startswith("dc:"))
async def on_daily_toggle(cq: CallbackQuery) -> None:
    _, cid, side, key = cq.data.split(":", 3)
    choice = await daily_choices.find_one({"_id": ObjectId(cid), "user_id": cq.from_user.id})
    if not choice or choice["status"] != "pending":
        return await cq.answer("Це питання вже неактуальне")
    selected = {s: list(v) for s, v in choice["selected"].items()}
    lst = selected.setdefault(side, [])
    lst.remove(key) if key in lst else lst.append(key)
    await daily_choices.update_one({"_id": choice["_id"]}, {"$set": {"selected": selected}})
    await cq.answer()
    await cq.message.edit_reply_markup(reply_markup=_daily_keyboard(cid, selected))


@dp.callback_query(F.data.startswith("dcgo:"))
async def on_daily_go(cq: CallbackQuery) -> None:
    cid = cq.data.split(":", 1)[1]
    choice = await daily_choices.find_one({"_id": ObjectId(cid), "user_id": cq.from_user.id})
    if not choice or choice["status"] != "pending":
        return await cq.answer("Зведення вже надіслано")
    await daily_choices.update_one({"_id": choice["_id"]},
                                   {"$set": {"status": "answered", "answered_at": datetime.now(timezone.utc)}})
    await cq.answer()
    await cq.message.edit_text("Дякую! Надсилаю зведення.")
    await log_interest(cq.from_user.id, choice["selected"], source="daily")
    await _send_daily(cq.bot, choice, choice["selected"])


async def _send_daily(bot: Bot, choice: dict, prefs: dict, note: str | None = None) -> None:
    user = await users_collection.find_one({"_id": choice["user_id"]})
    docs = [d async for d in digests_collection.find({"_id": {"$in": choice["digest_ids"]}})]
    docs.sort(key=lambda d: 0 if d.get("side") == "ua" else 1)
    await send_digests(bot, choice["user_id"], user, docs, kind="daily", prefs=prefs, note=note)



async def push_new_digests(bot: Bot) -> None:

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=20)
    docs = []
    for side in ("ua", "ru"):
        d = await digests_collection.find_one(
            {"period": "day", "side": side, "trigger": "scheduled", "created_at": {"$gte": since}},
            sort=[("created_at", -1)])
        if d:
            docs.append(d)

    # 1) Хто не відповів на питання за ASK_TIMEOUT_HOURS — надсилаємо зведення з попереднім вибором
    timeout = now - timedelta(hours=ASK_TIMEOUT_HOURS)
    async for choice in daily_choices.find({"status": "pending", "asked_at": {"$lt": timeout}}):
        await daily_choices.update_one({"_id": choice["_id"]}, {"$set": {"status": "timeout"}})
        try:
            await _send_daily(bot, choice, choice["selected"])
        except Exception:
            logger.exception(f"Не вдалося надіслати зведення {choice['user_id']}")

    if not docs:
        return


    async for user in users_collection.find({"onboarded": True, "daily_push": {"$ne": False}}):
        uid = user["_id"]
        if not _is_allowed(uid):
            continue
        if await deliveries_collection.find_one({"user_id": uid, "kind": "daily", "digest_ids": docs[0]["_id"]}):
            continue
        if await daily_choices.find_one({"user_id": uid, "digest_ids": docs[0]["_id"]}):
            continue
        try:
            if await days_of_choices(uid) < LEARN_DAYS:
                await ask_daily(bot, user, docs)
                logger.info(f"Питання про інтереси надіслано {uid}")
            else:
                prefs = await learned_prefs(uid)
                await send_digests(bot, uid, user, docs, kind="daily", prefs=prefs,
                                   note="Порядок категорій — за вашими виборами й відгуками за останні тижні. "
                                        "Змінити: /categories")
                logger.info(f"Персональне зведення надіслано {uid}")
        except TelegramForbiddenError:
            await users_collection.update_one({"_id": uid}, {"$set": {"daily_push": False}})
        except Exception:
            logger.exception(f"Не вдалося надіслати {uid}")
        await asyncio.sleep(1)


async def daily_push_loop(bot: Bot) -> None:
    while True:
        try:
            await push_new_digests(bot)
        except Exception:
            logger.exception("Помилка щоденної розсилки")
        await asyncio.sleep(PUSH_CHECK_SECONDS)



@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not _allowed(message):
        return await _deny(message)
    user = await touch_user(message.from_user)
    if not user.get("onboarded"):
        await message.answer("Вітаю! Налаштуймо, які новини показувати вам першими.")
        return await _ask_categories(message, user, "ua")
    await message.answer(HELP)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not _allowed(message):
        return await _deny(message)
    await touch_user(message.from_user)
    await message.answer(HELP)


@dp.message(Command("categories"))
async def cmd_categories(message: Message) -> None:
    if not _allowed(message):
        return await _deny(message)
    user = await touch_user(message.from_user)
    await _ask_categories(message, user, "ua")


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш ID: <code>{message.from_user.id}</code>")


@dp.message(Command("all_day"))
async def cmd_all_day(message: Message) -> None:
    await _send_digest(message, "day", ["ua", "ru"])


@dp.message(Command("all_week"))
async def cmd_all_week(message: Message) -> None:
    await _send_digest(message, "week", ["ua", "ru"])


@dp.message(Command("day"))
async def cmd_day(message: Message) -> None:
    await _send_digest(message, "day", ["ua"])


@dp.message(Command("week"))
async def cmd_week(message: Message) -> None:
    await _send_digest(message, "week", ["ua"])


@dp.message(Command("ru_day"))
async def cmd_ru_day(message: Message) -> None:
    await _send_digest(message, "day", ["ru"])


@dp.message(Command("ru_week"))
async def cmd_ru_week(message: Message) -> None:
    await _send_digest(message, "week", ["ru"])


async def main() -> None:
    if not TOKEN:
        raise SystemExit("Не задано TELEGRAM_BOT_TOKEN (додайте в .env і виконайте: set -a; source .env; set +a)")

    await deliveries_collection.create_index([("user_id", 1), ("kind", 1), ("digest_ids", 1)])
    await deliveries_collection.create_index([("submitted_at", -1)])
    await interest_log.create_index([("user_id", 1), ("at", -1)])
    await daily_choices.create_index([("user_id", 1), ("digest_ids", 1)])
    await daily_choices.create_index([("status", 1), ("asked_at", 1)])

    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    await bot.set_my_commands([
        BotCommand(command="all_day", description="Усе за добу (UA + огляд RU)"),
        BotCommand(command="all_week", description="Усе за тиждень (UA + огляд RU)"),
        BotCommand(command="day", description="Зведення UA за добу"),
        BotCommand(command="week", description="Зведення UA за тиждень"),
        BotCommand(command="ru_day", description="Огляд рос. джерел за добу"),
        BotCommand(command="ru_week", description="Огляд рос. джерел за тиждень"),
        BotCommand(command="categories", description="Мої категорії"),
        BotCommand(command="id", description="Мій Telegram ID"),
    ])
    if DAILY_PUSH:
        asyncio.create_task(daily_push_loop(bot))
    logger.info("Бот запущено: " + (f"доступ для {len(ALLOWED)} користувачів" if ALLOWED else "відкритий доступ")
                + (", щоденна розсилка увімкнена" if DAILY_PUSH else ""))
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    asyncio.run(main())