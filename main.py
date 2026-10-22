
import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, ErrorEvent, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, User)
from bson import ObjectId

import analyst
import user_store as store
from user_store import conversations, digests_collection, users_collection


import threading
from http.server import BaseHTTPRequestHandler, HTTPServer






logger = logging.getLogger("bot")
KYIV = ZoneInfo("Europe/Kyiv")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
DAILY_PUSH = os.environ.get("DAILY_PUSH", "1") == "1"
PUSH_CHECK_SECONDS = 300

PUSH_TIME = os.environ.get("PUSH_TIME", "08:00")

DAILY_ASK = os.environ.get("DAILY_ASK", "1") == "1"
ASK_TIMEOUT_HOURS = 3
SEND_PAUSE = 0.4
COMMENT_TIMEOUT_MINUTES = int(os.environ.get("COMMENT_TIMEOUT_MINUTES", "120"))  # скільки чекати коментар

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
    "<b>усі одразу</b> одним списком або <b>по одній</b> з кнопками 👍 / 👎 / ⏭ "
    "(наступна приходить після вашої оцінки). З ваших виборів і оцінок я вчуся, що вам важливо.\n\n"
    "/all_day — усе за добу (UA + RU)\n"
    "/ua_day — українські джерела за добу\n"
    "/ru_day — російські джерела за добу\n"
    "/all_week, /week, /ru_week — те саме за тиждень\n\n"
    "<b>Питайте мене звичайними словами</b> — я аналітик по цих новинах:\n"
    "• що сьогодні найважливіше для мене?\n"
    "• чому ти порадив цю новину? (можна відповісти Reply на саму новину)\n"
    "• що зараз набирає обертів і що буде актуально?\n"
    "• що було про інвестиції в дрони за тиждень?\n\n"
    "Писати можна будь-коли — навіть посеред новин: після відповіді я поверну вас до неоцінених.\n"
    "/chat — нова тема розмови (історія зберігається)\n"
    "/today — обрати категорії й отримати зведення зараз\n"
    "/mode — як надсилати новини: усі одразу чи по одній\n"
    "/goals — ваші цілі\n"

)

class _HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


def run_keep_alive_server() -> None:
    port = int(os.environ["PORT"])
    logger.info(f"Keep-alive на порту {port}")
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()


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



def source_links(urls: list[str]) -> str:

    return ", ".join(f'<a href="{u}">Джерело{"" if i == 1 else f" {i}"}</a>'
                     for i, u in enumerate(urls or [], 1))


def _news_text(item: dict) -> str:
    label = CATEGORY_LABELS.get(item["side"], {}).get(item["category"], item["category"])
    star = "⭐ " if item.get("starred") else ""
    links = source_links(item.get("source_urls", []))
    src = f"\n{links}" if links else ""
    return f"<i>{star}{label}</i>\n{html.escape(item['text'], quote=False)}{src}"



REACTION_STYLES = {"like": "success", "dislike": "danger", "skip": "primary"}


def _btn(text: str, callback_data: str, selected: bool, style: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style if selected else None)


def _news_keyboard(item: dict, reaction: str | None) -> InlineKeyboardMarkup:
    base = f"r:{item['digest_id']}:{item['n']}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("👍 Корисно", f"{base}:l", reaction == "like", REACTION_STYLES["like"]),
         _btn("👎 Не цікаво", f"{base}:d", reaction == "dislike", REACTION_STYLES["dislike"])],
        [_btn("⏭ Пропустити", f"{base}:s", reaction == "skip", REACTION_STYLES["skip"])],
    ])


async def _send(bot: Bot, chat_id: int, text: str, reply_markup=None) -> Message | None:
    for _ in range(3):
        try:
            return await bot.send_message(chat_id, text, reply_markup=reply_markup)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest:
            return await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text), parse_mode=None,
                                          reply_markup=reply_markup)
    return None


END_TEXT = "На сьогодні все ✅"

MODE_ONE, MODE_ALL = "one", "all"
TG_LIMIT = 4000


def _mode(user: dict) -> str:
    return user.get("delivery_mode") if user.get("delivery_mode") in (MODE_ONE, MODE_ALL) else MODE_ONE


async def _set_mode(uid: int, mode: str) -> None:
    await users_collection.update_one({"_id": uid}, {"$set": {"delivery_mode": mode}})


def _split_blocks(blocks: list[str], limit: int = TG_LIMIT) -> list[str]:
    parts, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > limit:
            parts.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    return parts + ([cur] if cur else [])


async def send_all_at_once(bot: Bot, user: dict, docs: list, today: dict | None = None,
                           goals_hint: bool = False) -> None:

    today = today or {"ua": [], "ru": []}
    uid = user["_id"]

    await _close_open_comment(uid)
    await conversations.update_one(_ns_query(uid, status="active"), {"$set": _ns({"status": "finished", "current": None})})
    for doc in docs:
        if not isinstance(doc, dict):
            await _send(bot, uid, doc)
            continue
        items = ordered_items(doc, user, today.get(doc.get("side"), []))
        header = doc["parts_html"][0].split("\n\n")[0] if doc.get("parts_html") else ""
        blocks = [f"<b>{SIDE_TITLES.get(doc.get('side'), '')}</b>\n{header}\nНовин: {len(items)}"]
        current_cat, lines = None, []
        for item in items:
            if item["category"] != current_cat:
                if lines:
                    blocks.append("\n".join(lines))
                current_cat = item["category"]
                label = CATEGORY_LABELS.get(item["side"], {}).get(current_cat, current_cat)
                lines = [f"<b>{'⭐ ' if item.get('starred') else ''}{label}</b>"]
            links = source_links(item.get("source_urls", []))
            lines.append(f"• {html.escape(item['text'], quote=False)}" + (f" ({links})" if links else ""))
        if lines:
            blocks.append("\n".join(lines))
        for part in _split_blocks(blocks):
            await _send(bot, uid, part)
            await asyncio.sleep(SEND_PAUSE)
    await _send(bot, uid, END_TEXT)
    if goals_hint:
        await _send(bot, uid, GOALS_HINT)


async def deliver(bot: Bot, user: dict, docs: list, today: dict | None = None,
                  goals_hint: bool = False, mode: str | None = None) -> None:

    if (mode or _mode(user)) == MODE_ALL:
        await send_all_at_once(bot, user, docs, today, goals_hint)
    else:
        await send_news(bot, user, docs, today, goals_hint)


def _item_key(digest_id, n) -> str:
    return f"{digest_id}:{n}"


# Стан стрічки «по одній» живе в conversations.news_state (один документ на людину, ключ user_id).
# Код нижче працює зі «сесією» — news_state + _id = user_id.
_NS_PROJECTION = {"user_id": 1, "news_state": 1}


def _ns(fields: dict) -> dict:
    """{"pos": 1, "awaiting_comment.stage": "offer"} -> {"news_state.pos": 1, "news_state.awaiting_comment.stage": "offer"}"""
    return {f"news_state.{k}": v for k, v in fields.items()}


def _ns_query(uid: int, **conds) -> dict:
    return {"user_id": uid, **_ns(conds)}


def _session(doc: dict | None) -> dict | None:
    return {**doc["news_state"], "_id": doc["user_id"]} if doc and doc.get("news_state") else None


async def _ns_find(query: dict) -> dict | None:
    return _session(await conversations.find_one(query, _NS_PROJECTION))


async def _ns_find_and_update(query: dict, update: dict, after: bool = False) -> dict | None:
    return _session(await conversations.find_one_and_update(query, update, projection=_NS_PROJECTION,
                                                            return_document=after))


async def send_news(bot: Bot, user: dict, docs: list, today: dict | None = None,
                    goals_hint: bool = False) -> None:

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

    state = {"session_id": ObjectId(), "status": "active", "queue": queue, "pos": 0, "current": None,
             "sent": [], "goals_hint": goals_hint}
    await _close_open_comment(uid)
    await conversations.update_one({"user_id": uid}, {"$set": {"news_state": state},
                                                      "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
                                   upsert=True)
    await _send_current(bot, {**state, "_id": uid})


async def _send_current(bot: Bot, session: dict, with_header: bool = True) -> None:

    uid, sid = session["_id"], session["session_id"]
    queue, pos = session["queue"], session["pos"]
    if pos >= len(queue):
        done = await conversations.update_one(_ns_query(uid, session_id=sid, status="active"),
                                              {"$set": _ns({"status": "finished", "current": None})})
        if done.modified_count:
            await _send(bot, uid, END_TEXT)
            if session.get("goals_hint"):
                await _send(bot, uid, GOALS_HINT)
        return

    entry = queue[pos]
    doc = await digests_collection.find_one({"_id": entry["digest_id"]}, {"sections": 1, "side": 1})
    item = find_item(doc, entry["n"]) if doc else None
    if not item:
        session = await _ns_find_and_update(_ns_query(uid, session_id=sid, pos=pos),
                                            {"$inc": _ns({"pos": 1})}, after=True)
        return await _send_current(bot, session, with_header) if session else None
    item["starred"] = entry["starred"]
    key = _item_key(item["digest_id"], item["n"])

    await conversations.update_one(_ns_query(uid, session_id=sid), {"$set": _ns({"current": key})})
    if with_header and entry.get("header"):
        await _send(bot, uid, entry["header"])
    reaction = await store.get_reaction(uid, item["digest_id"], item["n"])
    await _send(bot, uid, _news_text(item), reply_markup=_news_keyboard(item, reaction))
    await conversations.update_one(_ns_query(uid, session_id=sid), {"$addToSet": _ns({"sent": key})})


async def resume_sessions(bot: Bot) -> None:

    async for doc in conversations.find({"news_state.status": "active"}, _NS_PROJECTION):
        session = _session(doc)
        aw = session.get("awaiting_comment")
        if aw:                                  # чекаємо коментар — наступну новину не шлемо
            if not aw.get("prompt_sent"):
                try:
                    await _send_comment_prompt(bot, session["_id"], aw)
                except Exception:
                    logger.exception(f"Не вдалося надіслати питання про коментар {session['_id']}")
            continue
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
    labels = {"like": "👍 Корисно", "dislike": "👎 Не цікаво", "skip": "⏭ Пропущено", None: "Оцінку знято"}


    session = await _ns_find_and_update(
        _ns_query(uid, status="active", current=_item_key(item["digest_id"], item["n"])),
        {"$inc": _ns({"pos": 1}), "$set": _ns({"current": None})}, after=True)
    if session:
        new = reaction
    else:

        prev = await store.get_reaction(uid, item["digest_id"], item["n"])
        new = None if prev == reaction else reaction

    await store.set_reaction(uid, item, new)
    await cq.answer(labels[new])
    try:
        await cq.message.edit_reply_markup(reply_markup=_news_keyboard(item, new))
    except TelegramBadRequest:
        pass
    if session:
        if new in ("like", "dislike"):
            await _start_comment(cq.bot, session, item, new)
        else:
            await _send_current(cq.bot, session)


# ---------------------------------------------------------------------------
# Коментар до оцінки: після 👍 / 👎 — «💬 Прокоментувати» або «Без коментаря»
# ---------------------------------------------------------------------------

COMMENT_OFFER = "Хочете пояснити, чому?"
COMMENT_ASK = {"like": "Напишіть, чому ця новина вам сподобалась",
               "dislike": "Напишіть, чому ця новина вам не сподобалась"}
COMMENT_NOT_TEXT = "Будь ласка, напишіть коментар текстом або натисніть «Без коментаря»"


def _comment_keyboard(aw: dict) -> InlineKeyboardMarkup:
    base = f"{aw['digest_id']}:{aw['item_n']}"
    no = InlineKeyboardButton(text="Без коментаря", callback_data=f"cm:n:{base}")
    if aw["stage"] == "offer":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Прокоментувати", callback_data=f"cm:c:{base}"), no]])
    return InlineKeyboardMarkup(inline_keyboard=[[no]])


async def _send_comment_prompt(bot: Bot, uid: int, aw: dict) -> None:
    text = COMMENT_OFFER if aw["stage"] == "offer" else COMMENT_ASK[aw["reaction"]]
    msg = await _send(bot, uid, text, reply_markup=_comment_keyboard(aw))
    update = {"awaiting_comment.prompt_sent": True}
    if msg:   # id потрібен, щоб видалити це питання, якщо людина натисне «Без коментаря»
        update[f"awaiting_comment.{'offer' if aw['stage'] == 'offer' else 'ask'}_message_id"] = msg.message_id
    await conversations.update_one(
        _ns_query(uid, **{"awaiting_comment.digest_id": aw["digest_id"], "awaiting_comment.item_n": aw["item_n"],
                          "awaiting_comment.stage": aw["stage"]}), {"$set": _ns(update)})


async def _start_comment(bot: Bot, session: dict, item: dict, reaction: str) -> None:
    """Сесія вже на наступній позиції; наступну новину надішлемо, коли людина закінчить з коментарем."""
    aw = {"digest_id": item["digest_id"], "item_n": item["n"], "reaction": reaction, "stage": "offer",
          "awaiting_since": datetime.now(timezone.utc), "prompt_sent": False}
    await conversations.update_one(_ns_query(session["_id"], session_id=session["session_id"]),
                                   {"$set": _ns({"awaiting_comment": aw})})
    await _send_comment_prompt(bot, session["_id"], aw)


async def _close_comment(query: dict, uid: int, fields: dict) -> dict | None:
    """Атомарно закриває очікування й пише fields у запис feedback. Повертає сесію (до закриття) або None.

    query — умови на news_state (без префікса), напр. {"awaiting_comment.stage": "waiting"}.
    """
    session = await _ns_find_and_update(_ns_query(uid, **query), {"$unset": _ns({"awaiting_comment": ""})})
    if session:
        aw = session["awaiting_comment"]
        await store.set_comment(uid, aw["digest_id"], aw["item_n"], fields)
    return session


async def _close_open_comment(uid: int) -> None:
    """Нове зведення, а коментар ще чекаємо — закриваємо як timeout."""
    await _close_comment({"awaiting_comment": {"$exists": True}}, uid, {"comment_status": "timeout"})


async def _drop_buttons(cq: CallbackQuery) -> None:
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


async def _delete_prompts(bot: Bot, uid: int, aw: dict, pressed_id: int | None) -> None:
    """«Без коментаря»: прибирає з чату питання про коментар (і нагадування). Новину й повідомлення людини не чіпає."""
    ids = [aw.get("offer_message_id"), aw.get("ask_message_id"), *aw.get("reminder_message_ids", []), pressed_id]
    for mid in dict.fromkeys(i for i in ids if i):
        try:
            await bot.delete_message(uid, mid)
        except Exception as e:      # уже видалено, застаре тощо — наступну новину все одно надсилаємо
            logger.warning(f"Не вдалося видалити повідомлення {mid} у {uid}: {e}")


@dp.callback_query(F.data.startswith("cm:"))
async def on_comment_button(cq: CallbackQuery) -> None:
    _, code, did, n = cq.data.split(":")
    uid, did, n = cq.from_user.id, ObjectId(did), int(n)
    now = datetime.now(timezone.utc)
    current = {"awaiting_comment.digest_id": did, "awaiting_comment.item_n": n}
    if code == "c":
        session = await _ns_find_and_update(
            _ns_query(uid, **current, **{"awaiting_comment.stage": "offer"}),
            {"$set": _ns({"awaiting_comment.stage": "waiting", "awaiting_comment.awaiting_since": now,
                          "awaiting_comment.prompt_sent": False})}, after=True)
        await cq.answer()
        if session:
            await _drop_buttons(cq)
            await _send_comment_prompt(cq.bot, uid, session["awaiting_comment"])
        return
    session = await _close_comment(current, uid, {"comment_status": "no_comment"})
    await cq.answer()
    if session:
        await _delete_prompts(cq.bot, uid, session["awaiting_comment"], cq.message.message_id if cq.message else None)
        await _send_current(cq.bot, session)


async def _waiting_for_comment(message: Message) -> bool:
    return bool(message.from_user and await conversations.find_one(
        _ns_query(message.from_user.id, **{"awaiting_comment.stage": "waiting"}), {"_id": 1}))


@dp.message(F.text & ~F.text.startswith("/"), _waiting_for_comment)
async def on_comment_text(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    uid = message.from_user.id
    session = await _close_comment({"awaiting_comment.stage": "waiting"}, uid, {
        "text": message.text.strip(), "text_at": datetime.now(timezone.utc), "comment_status": "commented"})
    if session:
        await _send_current(message.bot, session)


@dp.message(~F.text, _waiting_for_comment)
async def on_comment_not_text(message: Message) -> None:
    uid = message.from_user.id
    session = await _ns_find({"user_id": uid})
    if session and session.get("awaiting_comment"):
        msg = await message.answer(COMMENT_NOT_TEXT, reply_markup=_comment_keyboard(session["awaiting_comment"]))
        await conversations.update_one(_ns_query(uid, awaiting_comment={"$exists": True}),
                                       {"$push": _ns({"awaiting_comment.reminder_message_ids": msg.message_id})})


async def comment_timeout_middleware(handler, event, data):

    user = data.get("event_from_user")
    expired = None
    if user:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=COMMENT_TIMEOUT_MINUTES)
        expired = await _close_comment({"awaiting_comment.awaiting_since": {"$lt": cutoff}},
                                       user.id, {"comment_status": "timeout"})
    try:
        return await handler(event, data)
    finally:
        if expired:
            s = await _ns_find({"user_id": user.id})
            if (s and s.get("session_id") == expired["session_id"] and s.get("status") == "active"
                    and s.get("current") is None and not s.get("awaiting_comment")):
                await _send_current(data["bot"], s)


dp.message.outer_middleware(comment_timeout_middleware)
dp.callback_query.outer_middleware(comment_timeout_middleware)


_ask_locks: dict[int, asyncio.Lock] = {}


def _answer_keyboard(msg_id, reaction: str | None) -> InlineKeyboardMarkup:
    base = f"ar:{msg_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("👍 Корисна відповідь", f"{base}:l", reaction == "like", "success"),
        _btn("👎 Не те", f"{base}:d", reaction == "dislike", "danger"),
    ]])


def _split_plain(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n"):
        if cur and len(cur) + len(para) + 1 > limit:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    return parts + ([cur] if cur else [])


_URL = re.compile(r"(?:(?:\(\s*)?(?:Джерело|Джерела|Source)\s*[:—-]?\s*)?\(?(https?://[^\s<>()«»\"']+)\)?", re.I)


def _answer_html(text: str) -> str:

    def link(m: re.Match) -> str:
        url = m.group(1).rstrip(".,;:!?")
        tail = m.group(1)[len(url):]
        return f'(<a href="{url}">Джерело</a>){tail}'
    return _URL.sub(link, html.escape(text, quote=False))


async def _send_answer(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.answer(_answer_html(text), reply_markup=reply_markup)
    except TelegramBadRequest:          # якщо HTML не сподобався Telegram — надсилаємо як є
        await message.answer(text, parse_mode=None, reply_markup=reply_markup)


@dp.message(F.text & ~F.text.startswith("/"))
async def on_question(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    await touch_user(message.from_user)
    uid = message.from_user.id
    lock = _ask_locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        return await message.answer("Ще думаю над попереднім питанням — хвилинку…")
    reply_to = None
    if message.reply_to_message and message.reply_to_message.from_user \
            and message.reply_to_message.from_user.is_bot:
        reply_to = message.reply_to_message.text or message.reply_to_message.caption


    closed = await _close_comment({"awaiting_comment.stage": "offer"}, uid, {"comment_status": "no_comment"})
    if closed:
        await _delete_prompts(message.bot, uid, closed["awaiting_comment"], None)

    async with lock:
        status = await message.answer("🤔 Аналізую…")
        try:
            await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        except Exception:
            pass
        try:
            res = await analyst.answer(uid, message.text, reply_to=reply_to)
        except Exception:
            logger.exception("Аналітик повністю недоступний")
            res = {"text": "Зараз не можу відповісти — технічна проблема. Спробуйте за кілька хвилин.",
                   "message_id": None, "fallback": True}
        try:
            await status.delete()
        except Exception:
            pass
        parts = _split_plain(res["text"])
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            kb = _answer_keyboard(res["message_id"], None) if last and res.get("message_id") \
                and not res.get("fallback") else None
            await _send_answer(message, part, kb)
    await _resume_news(message.bot, uid)


async def _resume_news(bot: Bot, uid: int) -> None:

    try:
        s = await _ns_find(_ns_query(uid, status="active", awaiting_comment={"$exists": False}))
        if not s or len(s["queue"]) - s["pos"] <= 0:
            return
        await _send(bot, uid, f"↩️ Повертаємось до новин — залишилось {len(s['queue']) - s['pos']}")
        await _send_current(bot, s, with_header=False)
    except Exception:
        logger.exception(f"Не вдалося повернути до новин {uid}")


@dp.callback_query(F.data.startswith("ar:"))
async def on_answer_rating(cq: CallbackQuery) -> None:
    parts = cq.data.split(":")
    reaction = {"l": "like", "d": "dislike"}.get(parts[-1])
    if not reaction:
        return await cq.answer()
    if len(parts) == 4:
        await store.set_message_reaction(ObjectId(parts[1]), ObjectId(parts[2]), cq.from_user.id, reaction)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            _btn("👍 Корисна відповідь", f"ar:{parts[1]}:{parts[2]}:l", reaction == "like", "success"),
            _btn("👎 Не те", f"ar:{parts[1]}:{parts[2]}:d", reaction == "dislike", "danger")]])
    else:
        await store.set_chat_reaction(ObjectId(parts[1]), cq.from_user.id, reaction)
        kb = _answer_keyboard(parts[1], reaction)
    await cq.answer("Дякую, врахую")
    try:
        await cq.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass


@dp.message(Command("chat"))
async def cmd_new(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    await store.new_chat_topic(message.from_user.id)
    await message.answer("Нова тема 🙂 Попередня розмова збережена, але в цю я її не тягну. Питайте.")


@dp.errors()
async def on_error(event: ErrorEvent) -> bool:

    logger.exception(f"Помилка в обробнику: {event.exception}", exc_info=event.exception)
    upd = event.update
    try:
        if upd.message:
            await upd.message.answer("Щось пішло не так — спробуйте ще раз.", parse_mode=None)
        elif upd.callback_query:
            await upd.callback_query.answer("Щось пішло не так — спробуйте ще раз")
    except Exception:
        pass
    return True



DAILY_QUESTION = ("<b>Що вас сьогодні цікавить найбільше?</b>\n"
                  "Позначте категорії — новини з них будуть першими (обрані підсвічуються). "
                  "Потім оберіть, як показати новини.")

MODE_BUTTONS = {MODE_ALL: "📋 Усі одразу", MODE_ONE: "👆 По одній з оцінкою"}


def _daily_keyboard(selected: dict, mode: str = MODE_ONE) -> InlineKeyboardMarkup:
    rows = []
    for side, title in (("ua", "— Українські джерела —"), ("ru", "— Російські джерела —")):
        rows.append([InlineKeyboardButton(text=title, callback_data="noop")])
        for key, label in CATEGORY_LABELS[side].items():
            rows.append([_btn(label, f"dc:{side}:{key}", key in selected.get(side, []), "primary")])

    rows.append([_btn(MODE_BUTTONS[m], f"dcgo:{m}", m == mode, "success") for m in (MODE_ALL, MODE_ONE)])
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

    await users_collection.update_one({"_id": user["_id"]}, {"$set": {"daily": {
        "date": _kyiv_date(), "digest_ids": [d["_id"] for d in docs],
        "selected": {"ua": [], "ru": []}, "status": "asked", "asked_at": datetime.now(timezone.utc)}}})
    await _send(bot, user["_id"], DAILY_QUESTION, reply_markup=_daily_keyboard({"ua": [], "ru": []}, _mode(user)))


@dp.callback_query(F.data == "noop")
async def on_noop(cq: CallbackQuery) -> None:
    await cq.answer()


@dp.callback_query(F.data.startswith("dc:"))
async def on_daily_toggle(cq: CallbackQuery) -> None:
    _, side, key = cq.data.split(":", 2)
    user = await users_collection.find_one({"_id": cq.from_user.id}, {"daily": 1, "delivery_mode": 1})
    daily = (user or {}).get("daily") or {}
    if daily.get("status") != "asked":
        return await cq.answer("Це питання вже неактуальне")
    selected = {s: list(v) for s, v in daily.get("selected", {}).items()}
    lst = selected.setdefault(side, [])
    lst.remove(key) if key in lst else lst.append(key)
    await users_collection.update_one({"_id": cq.from_user.id}, {"$set": {"daily.selected": selected}})
    await cq.answer()
    await cq.message.edit_reply_markup(reply_markup=_daily_keyboard(selected, _mode(user or {})))


@dp.callback_query(F.data.startswith("dcgo"))
async def on_daily_go(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    mode = cq.data.split(":", 1)[1] if ":" in cq.data else None   # старі кнопки «dcgo» — режим людини
    if mode in (MODE_ONE, MODE_ALL):
        await _set_mode(uid, mode)                                 # запам'ятовуємо вибір на наступні рази
    user = await users_collection.find_one({"_id": uid})
    daily = (user or {}).get("daily") or {}
    if daily.get("status") != "asked":
        return await cq.answer("Новини вже надіслано")
    selected = daily.get("selected", {"ua": [], "ru": []})
    await users_collection.update_one({"_id": uid}, {"$set": {
        "daily.status": "sent", "daily.sent_at": datetime.now(timezone.utc), "onboarded": True}})
    await store.add_category_choice(uid, selected)  # +1 кожній обраній категорії
    await cq.answer()
    chosen = [CATEGORY_LABELS[s][k] for s in ("ua", "ru") for k in selected.get(s, [])]
    await cq.message.edit_text("Сьогодні першими: " + (", ".join(chosen) if chosen else "за вашими балами"))
    user = await users_collection.find_one({"_id": uid})
    docs = [d async for d in digests_collection.find({"_id": {"$in": daily.get("digest_ids", [])}})]
    docs.sort(key=lambda d: 0 if d.get("side") == "ua" else 1)
    if not docs:
        await cq.message.answer("Свіжого зведення ще немає — воно з'являється щоранку близько 6:30.")
        return
    await deliver(cq.bot, user, docs, today=selected, goals_hint=not store.active_goals(user))


async def push_new_digests(bot: Bot) -> None:
    now = datetime.now(timezone.utc)

    timeout = now - timedelta(hours=ASK_TIMEOUT_HOURS)
    async for user in users_collection.find({"daily.status": "asked", "daily.asked_at": {"$lt": timeout}}):
        await users_collection.update_one({"_id": user["_id"]}, {"$set": {
            "daily.status": "sent", "daily.sent_at": now, "daily.timeout": True}})
        docs = [d async for d in digests_collection.find({"_id": {"$in": user["daily"]["digest_ids"]}})]
        docs.sort(key=lambda d: 0 if d.get("side") == "ua" else 1)
        try:
            await deliver(bot, user, docs)
        except TelegramForbiddenError:
            await users_collection.update_one({"_id": user["_id"]}, {"$set": {"daily_push": False}})
        except Exception:
            logger.exception(f"Не вдалося надіслати зведення {user['_id']}")


    hh, mm = (int(x) for x in PUSH_TIME.split(":"))
    kyiv_now = datetime.now(KYIV)
    push_at = kyiv_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if kyiv_now < push_at:
        return

    day_start = kyiv_now.replace(hour=0, minute=0, second=0, microsecond=0)
    docs = [d for d in await latest_daily_docs() if d["created_at"] >= day_start]
    if not docs:
        return
    async for user in users_collection.find({"onboarded": True, "daily_push": {"$ne": False}}):
        uid = user["_id"]
        if not _is_allowed(uid) or docs[0]["_id"] in ((user.get("daily") or {}).get("digest_ids") or []):
            continue
        try:
            if DAILY_ASK:
                await ask_today(bot, user, docs)
                logger.info(f"Ранкове питання надіслано {uid}")
            else:

                await users_collection.update_one({"_id": uid}, {"$set": {"daily": {
                    "date": _kyiv_date(), "digest_ids": [d["_id"] for d in docs],
                    "selected": {"ua": [], "ru": []}, "status": "sent", "sent_at": now}}})
                await deliver(bot, user, docs)
                logger.info(f"Ранкове зведення надіслано {uid}")
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
        await message.answer(
            f"Не вдалося додати: така ціль уже є або їх уже {store.MAX_GOALS}. Видаліть зайву в /goals.")


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
    await deliver(message.bot, user, docs)


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


def _mode_keyboard(mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(MODE_BUTTONS[m], f"mode:{m}", m == mode, "success") for m in (MODE_ALL, MODE_ONE)]])


MODE_TEXT = ("<b>Як надсилати новини?</b>\n"
             "📋 <b>Усі одразу</b> — одним списком, без оцінок.\n"
             "👆 <b>По одній</b> — з кнопками 👍 / 👎 / ⏭; наступна приходить після оцінки.")


@dp.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _deny(message)
    user = await touch_user(message.from_user)
    await message.answer(MODE_TEXT, reply_markup=_mode_keyboard(_mode(user)))


@dp.callback_query(F.data.startswith("mode:"))
async def on_mode(cq: CallbackQuery) -> None:
    mode = cq.data.split(":", 1)[1]
    if mode not in (MODE_ONE, MODE_ALL):
        return await cq.answer()
    await _set_mode(cq.from_user.id, mode)
    await cq.answer("Збережено")
    try:
        await cq.message.edit_reply_markup(reply_markup=_mode_keyboard(mode))
    except TelegramBadRequest:
        pass


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


@dp.message(Command("ua_day"))
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
    analyst.CATEGORY_LABELS = CATEGORY_LABELS
    logger.info(f"Аналітик: модель {analyst.MODEL} за адресою {analyst.client.base_url}")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    await bot.set_my_commands([
        BotCommand(command="today", description="Обрати категорії й отримати новини"),
        BotCommand(command="all_day", description="Усе за добу (UA + RU)"),
        BotCommand(command="ua_day", description="Українські джерела за добу"),
        BotCommand(command="ru_day", description="Російські джерела за добу"),
        BotCommand(command="all_week", description="Усе за тиждень"),
        BotCommand(command="chat", description="Нова тема розмови з аналітиком"),
        BotCommand(command="mode", description="Усі новини одразу чи по одній"),
        BotCommand(command="goals", description="Мої цілі"),

    ])
    if os.environ.get("PORT"):
        threading.Thread(target=run_keep_alive_server, daemon=True).start()
    try:
        await resume_sessions(bot)
    except Exception:
        logger.exception("Не вдалося відновити сесії новин — продовжую запуск")
    if DAILY_PUSH:
        asyncio.create_task(daily_push_loop(bot))
    logger.info("Бот запущено: " + (f"доступ для {len(ALLOWED)} користувачів" if ALLOWED else "відкритий доступ")
                + (f", щоденна розсилка о {PUSH_TIME} (Київ)" if DAILY_PUSH else ""))
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
