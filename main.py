import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, User)
from bson import ObjectId

import user_store as store
from user_store import digests_collection, news_sessions, users_collection

logger = logging.getLogger("bot")
KYIV = ZoneInfo("Europe/Kyiv")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
DAILY_PUSH = os.environ.get("DAILY_PUSH", "1") == "1"
PUSH_CHECK_SECONDS = 300
ASK_TIMEOUT_HOURS = 3     
SEND_PAUSE = 0.4          

dp = Dispatcher()

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
SIDE_TITLES = {"ua": "🇺🇦 Українські джерела", "ru": "🇷🇺 Російські джерела (твердження, не факти)"}

HELP = (
    "<b>Зведення новин</b>\n\n"
    "Щоранку я питаю, які категорії вам цікаві сьогодні, і надсилаю новини — "
    "кожну окремо з кнопками 👍 / 👎 / ⏭. З ваших виборів і оцінок я вчуся, що вам важливо.\n\n"
    "/all_day — усе за добу (UA + RU)\n"
    "/day — українські джерела за добу\n"
    "/ru_day — російські джерела за добу\n"
    "/all_week, /week, /ru_week — те саме за тиждень\n\n"
    "/today — обрати категорії й отримати зведення зараз\n"
    "/goals — ваші цілі\n"
    "/id — ваш Telegram ID"
)



def _is_allowed(user_id: int | None) -> bool:
    return not ALLOWED or (user_id is not None and user_id in ALLOWED)


async def _deny(message: Message) -> None:
    uid = message.from_user.id if message.from_user else "?"
    await message.answer(f"Доступ закритий. Ваш ID: <code>{uid}</code> — передайте його адміністратору.")
    logger.warning(f"Відмовлено в доступі: {uid}")


async def touch_user(u: User) -> dict:
    now = datetime.now(timezone.utc)
    await users_collection.update_one(
        {"_id": u.id},
        {"$set": {"first_name": u.first_name, "username": u.username, "last_seen": now},
         "$setOnInsert": {"created_at": now, "onboarded": False, "daily_push": True,
                          "interests": {"category_scores": {"ua": {}, "ru": {}}, "tag_scores": {}},
                          "goals": [], "feedback": []}},
        upsert=True,
    )
    return await users_collection.find_one({"_id": u.id})


def _kyiv_date() -> str:
    return datetime.now(KYIV).strftime("%Y-%m-%d")



def _original_numbers(sections: list[dict]) -> dict[tuple[int, int], int]:
   
    numbers, c = {}, 0
    for i, section in enumerate(sections):
        for j, item in enumerate(section.get("items", [])):
            c += 1
            numbers[(i, j)] = item.get("n", c)
    return numbers


def find_item(doc: dict, n: int) -> dict | None:
    sections = doc.get("sections") or []
    for (i, j), num in _original_numbers(sections).items():
        if num == n:
            item = sections[i]["items"][j]
            return {"digest_id": doc["_id"], "n": n, "side": doc.get("side"),
                    "category": sections[i]["category"], "source_post_ids": item.get("source_post_ids", []),
                    "text": item.get("text", ""), "source_urls": item.get("source_urls", [])}
    return None


def ordered_items(doc: dict, user: dict, today: list[str]) -> list[dict]:
    """Новини зведення: спершу сьогоднішні категорії, далі решта за балами людини."""
    sections = doc.get("sections") or []
    side = doc.get("side")
    order = store.category_order(user, side, first=today)
    rank = {c: i for i, c in enumerate(order)}
    nums = _original_numbers(sections)
    idx = sorted(range(len(sections)), key=lambda i: (rank.get(sections[i]["category"], len(rank)), i))
    items = []
    for i in idx:
        for j, item in enumerate(sections[i].get("items", [])):
            it = find_item(doc, nums[(i, j)])
            it["starred"] = sections[i]["category"] in today
            items.append(it)
    return items


def _news_text(item: dict) -> str:
    label = CATEGORY_LABELS.get(item["side"], {}).get(item["category"], item["category"])
    star = "⭐ " if item.get("starred") else ""
    links = [f'<a href="{u}">{i}</a>' for i, u in enumerate(item.get("source_urls", []), 1)]
    src = f"\nДжерела: {', '.join(links)}" if links else ""
    return f"<i>{star}{label}</i>\n{html.escape(item['text'], quote=False)}{src}"


def _news_keyboard(item: dict, reaction: str | None) -> InlineKeyboardMarkup:
    base = f"r:{item['digest_id']}:{item['n']}"
    mark = lambda r, text: f"✅ {text}" if reaction == r else text  # noqa: E731
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=mark("like", "👍"), callback_data=f"{base}:l"),
        InlineKeyboardButton(text=mark("dislike", "👎"), callback_data=f"{base}:d"),
        InlineKeyboardButton(text=mark("skip", "⏭ Пропустити"), callback_data=f"{base}:s"),
    ]])


async def _send(bot: Bot, chat_id: int, text: str, reply_markup=None) -> None:
    for _ in range(3):
        try:
            await bot.send_message(chat_id, text, reply_markup=reply_markup)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest:
            await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text), parse_mode=None, reply_markup=reply_markup)
            return


END_TEXT = "На сьогодні все ✅"


def _item_key(digest_id, n) -> str:
    return f"{digest_id}:{n}"


async def send_news(bot: Bot, user: dict, docs: list, today: dict | None = None,
                    goals_hint: bool = False) -> None:
    """Починає показ зведення: по одній новині, наступна — після 👍 / 👎 / ⏭ на поточній.

    docs — документи зведень з бази або текст «зведення немає». Прогрес зберігається в news_sessions,
    тож перезапуск бота його не губить. Нове зведення замінює незавершену сесію.
    """
    today = today or {"ua": [], "ru": []}
    uid = user["_id"]
    queue = []
    for doc in docs:
        if not isinstance(doc, dict):
            await _send(bot, uid, doc)
            continue
        items = ordered_items(doc, user, today.get(doc.get("side"), []))
        header = doc["parts_html"][0].split("\n\n")[0] if doc.get("parts_html") else ""
        header = f"<b>{SIDE_TITLES.get(doc.get('side'), '')}</b>\n{header}\nНовин: {len(items)}"
        for k, item in enumerate(items):
            queue.append({"digest_id": item["digest_id"], "n": item["n"], "starred": item["starred"],
                          "header": header if k == 0 else None})
    if not queue:
        if goals_hint:
            await _send(bot, uid, GOALS_HINT)
        return

    now = datetime.now(timezone.utc)
    session = {"_id": uid, "user_id": uid, "session_id": ObjectId(), "status": "active",
               "digest_ids": [d["_id"] for d in docs if isinstance(d, dict)], "queue": queue,
               "pos": 0, "current": None, "sent": [], "goals_hint": goals_hint,
               "created_at": now, "updated_at": now}
    await news_sessions.replace_one({"_id": uid}, session, upsert=True)
    await _send_current(bot, session)


async def _send_current(bot: Bot, session: dict) -> None:
    """Надсилає новину на позиції pos (або завершальне повідомлення, якщо новини скінчилися)."""
    uid, sid = session["_id"], session["session_id"]
    queue, pos = session["queue"], session["pos"]
    now = datetime.now(timezone.utc)
    if pos >= len(queue):
        done = await news_sessions.update_one({"_id": uid, "session_id": sid, "status": "active"},
                                              {"$set": {"status": "finished", "current": None, "updated_at": now}})
        if done.modified_count:
            await _send(bot, uid, END_TEXT)
            if session.get("goals_hint"):
                await _send(bot, uid, GOALS_HINT)
        return

    entry = queue[pos]
    doc = await digests_collection.find_one({"_id": entry["digest_id"]}, {"sections": 1, "side": 1})
    item = find_item(doc, entry["n"]) if doc else None
    if not item:                                            # зведення видалили — переходимо до наступної
        session = await news_sessions.find_one_and_update(
            {"_id": uid, "session_id": sid, "pos": pos}, {"$inc": {"pos": 1}, "$set": {"updated_at": now}},
            return_document=True)
        return await _send_current(bot, session) if session else None
    item["starred"] = entry["starred"]
    key = _item_key(item["digest_id"], item["n"])
    # current ставимо до надсилання, щоб натискання одразу після появи новини вже приймалося
    await news_sessions.update_one({"_id": uid, "session_id": sid},
                                   {"$set": {"current": key, "updated_at": now}})
    if entry.get("header"):
        await _send(bot, uid, entry["header"])
    reaction = await store.get_reaction(uid, item["digest_id"], item["n"])
    await _send(bot, uid, _news_text(item), reply_markup=_news_keyboard(item, reaction))
    await news_sessions.update_one({"_id": uid, "session_id": sid}, {"$addToSet": {"sent": key}})


async def resume_sessions(bot: Bot) -> None:
    """Після перезапуску: якщо бот упав між оцінкою і надсиланням наступної новини — надсилаємо її."""
    async for session in news_sessions.find({"status": "active"}):
        if session.get("current") is None or session["current"] not in session.get("sent", []):
            try:
                await _send_current(bot, session)
            except Exception:
                logger.exception(f"Не вдалося продовжити сесію {session['_id']}")


@dp.callback_query(F.data.startswith("r:"))
async def on_reaction(cq: CallbackQuery) -> None:
    _, did, n, code = cq.data.split(":")
    reaction = {"l": "like", "d": "dislike", "s": "skip"}[code]
    doc = await digests_collection.find_one({"_id": ObjectId(did)}, {"sections": 1, "side": 1})
    item = find_item(doc, int(n)) if doc else None
    if not item:
        return await cq.answer("Новину не знайдено")
    uid = cq.from_user.id
    # Приймаємо лише поточну новину й лише один раз: повторне чи старе натискання сюди не пройде.
    session = await news_sessions.find_one_and_update(
        {"_id": uid, "status": "active", "current": _item_key(item["digest_id"], item["n"])},
        {"$inc": {"pos": 1}, "$set": {"current": None, "updated_at": datetime.now(timezone.utc)}},
        return_document=True)
    if not session:
        return await cq.answer()
    prev = await store.get_reaction(uid, item["digest_id"], item["n"])
    new = None if prev == reaction else reaction           # повторне натискання знімає оцінку
    await store.set_reaction(uid, item, new)
    await cq.answer({"like": "👍 Корисно", "dislike": "👎 Не цікаво", "skip": "⏭ Пропущено",
                     None: "Оцінку знято"}[new])
    try:
        await cq.message.edit_reply_markup(reply_markup=_news_keyboard(item, new))
    except TelegramBadRequest:
        pass
    await _send_current(cq.bot, session)


# ---------------------------------------------------------------------------
# Щоденне питання про категорії
# ---------------------------------------------------------------------------

DAILY_QUESTION = ("<b>Що вас сьогодні цікавить найбільше?</b>\n"
                  "Позначте категорії — новини з них будуть першими. Потім натисніть «Показати новини».")


def _daily_keyboard(selected: dict) -> InlineKeyboardMarkup:
    rows = []
    for side, title in (("ua", "— Українські джерела —"), ("ru", "— Російські джерела —")):
        rows.append([InlineKeyboardButton(text=title, callback_data="noop")])
        for key, label in CATEGORY_LABELS[side].items():
            mark = "✅ " if key in selected.get(side, []) else "▫️ "
            rows.append([InlineKeyboardButton(text=mark + label, callback_data=f"dc:{side}:{key}")])
    rows.append([InlineKeyboardButton(text="📰 Показати новини", callback_data="dcgo")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def latest_daily_docs() -> list[dict]:
    docs = []
    for side in ("ua", "ru"):
        d = await digests_collection.find_one({"period": "day", "side": side, "trigger": "scheduled"},
                                              sort=[("created_at", -1)])
        if d:
            docs.append(d)
    return docs


async def ask_today(bot: Bot, user: dict, docs: list[dict]) -> None:
    """Ставить ранкове питання. Вибір зберігається в users.daily."""
    await users_collection.update_one({"_id": user["_id"]}, {"$set": {"daily": {
        "date": _kyiv_date(), "digest_ids": [d["_id"] for d in docs],
        "selected": {"ua": [], "ru": []}, "status": "asked", "asked_at": datetime.now(timezone.utc)}}})
    await _send(bot, user["_id"], DAILY_QUESTION, reply_markup=_daily_keyboard({"ua": [], "ru": []}))


@dp.callback_query(F.data == "noop")
async def on_noop(cq: CallbackQuery) -> None:
    await cq.answer()


@dp.callback_query(F.data.startswith("dc:"))
async def on_daily_toggle(cq: CallbackQuery) -> None:
    _, side, key = cq.data.split(":", 2)
    user = await users_collection.find_one({"_id": cq.from_user.id}, {"daily": 1})
    daily = (user or {}).get("daily") or {}
    if daily.get("status") != "asked":
        return await cq.answer("Це питання вже неактуальне")
    selected = {s: list(v) for s, v in daily.get("selected", {}).items()}
    lst = selected.setdefault(side, [])
    lst.remove(key) if key in lst else lst.append(key)
    await users_collection.update_one({"_id": cq.from_user.id}, {"$set": {"daily.selected": selected}})
    await cq.answer()
    await cq.message.edit_reply_markup(reply_markup=_daily_keyboard(selected))


@dp.callback_query(F.data == "dcgo")
async def on_daily_go(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    user = await users_collection.find_one({"_id": uid})
    daily = (user or {}).get("daily") or {}
    if daily.get("status") != "asked":
        return await cq.answer("Новини вже надіслано")
    selected = daily.get("selected", {"ua": [], "ru": []})
    await users_collection.update_one({"_id": uid}, {"$set": {
        "daily.status": "sent", "daily.sent_at": datetime.now(timezone.utc), "onboarded": True}})
    await store.add_category_choice(uid, selected)          # +1 кожній обраній категорії
    await cq.answer()
    chosen = [CATEGORY_LABELS[s][k] for s in ("ua", "ru") for k in selected.get(s, [])]
    await cq.message.edit_text("Сьогодні першими: " + (", ".join(chosen) if chosen else "за вашими балами"))
    user = await users_collection.find_one({"_id": uid})
    docs = [d async for d in digests_collection.find({"_id": {"$in": daily.get("digest_ids", [])}})]
    docs.sort(key=lambda d: 0 if d.get("side") == "ua" else 1)
    if not docs:
        await cq.message.answer("Свіжого зведення ще немає — воно з'являється щоранку близько 6:30.")
        return
    await send_news(cq.bot, user, docs, today=selected, goals_hint=not store.active_goals(user))



async def push_new_digests(bot: Bot) -> None:
    now = datetime.now(timezone.utc)

  
    timeout = now - timedelta(hours=ASK_TIMEOUT_HOURS)
    async for user in users_collection.find({"daily.status": "asked", "daily.asked_at": {"$lt": timeout}}):
        await users_collection.update_one({"_id": user["_id"]}, {"$set": {
            "daily.status": "sent", "daily.sent_at": now, "daily.timeout": True}})
        docs = [d async for d in digests_collection.find({"_id": {"$in": user["daily"]["digest_ids"]}})]
        docs.sort(key=lambda d: 0 if d.get("side") == "ua" else 1)
        try:
            await send_news(bot, user, docs)
        except TelegramForbiddenError:
            await users_collection.update_one({"_id": user["_id"]}, {"$set": {"daily_push": False}})
        except Exception:
            logger.exception(f"Не вдалося надіслати зведення {user['_id']}")

   
    docs = [d for d in await latest_daily_docs() if d["created_at"] >= now - timedelta(hours=20)]
    if not docs:
        return
    async for user in users_collection.find({"onboarded": True, "daily_push": {"$ne": False}}):
        uid = user["_id"]
        if not _is_allowed(uid) or docs[0]["_id"] in ((user.get("daily") or {}).get("digest_ids") or []):
            continue
        try:
            await ask_today(bot, user, docs)
            logger.info(f"Ранкове питання надіслано {uid}")
        except TelegramForbiddenError:
            await users_collection.update_one({"_id": uid}, {"$set": {"daily_push": False}})
        except Exception:
            logger.exception(f"Не вдалося надіслати питання {uid}")
        await asyncio.sleep(1)


async def daily_push_loop(bot: Bot) -> None:
    while True:
        try:
            await push_new_digests(bot)
        except Exception:
            logger.exception("Помилка щоденної розсилки")
        await asyncio.sleep(PUSH_CHECK_SECONDS)



GOALS_HINT = ("<b>Що ви найбільше хочете зробити?</b>\n"
              "Додайте ціль — аналітик пояснюватиме новини й пропонуватиме кроки під неї:\n"
              "<code>/goal знайти перспективні напрямки для інвестицій у defense tech</code>")


def _goals_view(goals: list[str]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not goals:
        return GOALS_HINT, None
    lines = "\n".join(f"{i}. {html.escape(g, quote=False)}" for i, g in enumerate(goals, 1))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 Видалити {i}", callback_data=f"goaldel:{i}")] for i in range(1, len(goals) + 1)])
    return f"<b>Ваші цілі</b>\n{lines}\n\nДодати: <code>/goal текст</code> (до {store.MAX_GOALS})", kb


@dp.message(Command("goals"))
async def cmd_goals(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    user = await touch_user(message.from_user)
    text, kb = _goals_view(store.active_goals(user))
    await message.answer(text, reply_markup=kb)


@dp.message(Command("goal"))
async def cmd_goal(message: Message, command: CommandObject) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    await touch_user(message.from_user)
    if not (command.args or "").strip():
        return await message.answer(GOALS_HINT)
    if await store.add_goal(message.from_user.id, command.args):
        user = await users_collection.find_one({"_id": message.from_user.id})
        text, kb = _goals_view(store.active_goals(user))
        await message.answer("Ціль збережено ✅\n\n" + text, reply_markup=kb)
    else:
        await message.answer(f"Не вдалося додати: така ціль уже є або їх уже {store.MAX_GOALS}. Видаліть зайву в /goals.")


@dp.callback_query(F.data.startswith("goaldel:"))
async def on_goal_delete(cq: CallbackQuery) -> None:
    await store.remove_goal(cq.from_user.id, int(cq.data.split(":", 1)[1]))
    user = await users_collection.find_one({"_id": cq.from_user.id})
    text, kb = _goals_view(store.active_goals(user))
    await cq.answer("Ціль видалено")
    await cq.message.edit_text(text, reply_markup=kb)



async def _get_digest(period: str, side: str):
    doc = await digests_collection.find_one({"period": period, "side": side, "trigger": "scheduled"},
                                            sort=[("created_at", -1)])
    if not doc:
        doc = await digests_collection.find_one({"period": period, "side": side}, sort=[("created_at", -1)])
    if doc and doc.get("sections"):
        return doc
    what = "тижневого" if period == "week" else "щоденного"
    return (f"Ще немає {what} зведення ({'рос.' if side == 'ru' else 'укр.'} джерела). "
            "Щоденне з'являється щоранку близько 6:30, тижневе — по понеділках.")


async def _cmd_digest(message: Message, period: str, sides: list[str]) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    user = await touch_user(message.from_user)
    docs = [await _get_digest(period, side) for side in sides]
    await send_news(message.bot, user, docs)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    user = await touch_user(message.from_user)
    await message.answer(HELP)
    if not user.get("onboarded"):
        await ask_today(message.bot, user, await latest_daily_docs())


@dp.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    user = await touch_user(message.from_user)
    await ask_today(message.bot, user, await latest_daily_docs())


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    await touch_user(message.from_user)
    await message.answer(HELP)


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш ID: <code>{message.from_user.id}</code>")


@dp.message(Command("all_day"))
async def cmd_all_day(message: Message) -> None:
    await _cmd_digest(message, "day", ["ua", "ru"])


@dp.message(Command("all_week"))
async def cmd_all_week(message: Message) -> None:
    await _cmd_digest(message, "week", ["ua", "ru"])


@dp.message(Command("day"))
async def cmd_day(message: Message) -> None:
    await _cmd_digest(message, "day", ["ua"])


@dp.message(Command("week"))
async def cmd_week(message: Message) -> None:
    await _cmd_digest(message, "week", ["ua"])


@dp.message(Command("ru_day"))
async def cmd_ru_day(message: Message) -> None:
    await _cmd_digest(message, "day", ["ru"])


@dp.message(Command("ru_week"))
async def cmd_ru_week(message: Message) -> None:
    await _cmd_digest(message, "week", ["ru"])


async def main() -> None:
    if not TOKEN:
        raise SystemExit("Не задано TELEGRAM_BOT_TOKEN у .env")
    await store.ensure_indexes()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    await bot.set_my_commands([
        BotCommand(command="today", description="Обрати категорії й отримати новини"),
        BotCommand(command="all_day", description="Усе за добу (UA + RU)"),
        BotCommand(command="day", description="Українські джерела за добу"),
        BotCommand(command="ru_day", description="Російські джерела за добу"),
        BotCommand(command="all_week", description="Усе за тиждень"),
        BotCommand(command="goals", description="Мої цілі"),
        BotCommand(command="id", description="Мій Telegram ID"),
    ])
    await resume_sessions(bot)
    if DAILY_PUSH:
        asyncio.create_task(daily_push_loop(bot))
    logger.info("Бот запущено: " + (f"доступ для {len(ALLOWED)} користувачів" if ALLOWED else "відкритий доступ")
                + (", щоденна розсилка увімкнена" if DAILY_PUSH else ""))
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
