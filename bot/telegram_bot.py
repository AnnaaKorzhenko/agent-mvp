import re
import logging
from datetime import datetime
from typing import List, Optional

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BotCommand, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

from config import config
from scrapers.db import digests_collection

logger = logging.getLogger(__name__)

bot = Bot(
    token=config.TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

TG_LIMIT = 4000
NO_DIGEST_TEXT = "No digest available for this selection yet."

# Sides that count as the "overall" digest for /day and /week.
OVERALL_SIDES = ["all", "overall", None]

# key -> (period, side, button label)
# Telegram command names only allow [a-z0-9_], so /day_ua is the canonical form;
# /day-ua is also accepted when typed manually.
SELECTIONS = {
    "day": ("day", None, "📅 Today"),
    "day_ua": ("day", "ua", "🇺🇦 Today (UA)"),
    "day_ru": ("day", "ru", "🇷🇺 Today (RU)"),
    "week": ("week", None, "📆 Weekly"),
}

BOT_COMMANDS = [
    BotCommand(command="digest", description="Digest menu (today's digest)"),
    BotCommand(command="day", description="Latest daily digest"),
    BotCommand(command="day_ua", description="Daily digest — UA sources"),
    BotCommand(command="day_ru", description="Daily digest — RU sources"),
    BotCommand(command="week", description="Latest weekly digest"),
    BotCommand(command="status", description="Latest available digests"),
]


def sanitize_telegram_html(text: str) -> str:
    """Removes web-specific HTML tags, code fences, and fixes handle formatting for Telegram."""
    if not text:
        return ""

    # Remove code block fences like ```html ... ```
    text = re.sub(r"^\s*```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    # Strip full document wrappers and non-content blocks
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<(style|script|head|title)\b[^>]*>.*?</\1\s*>", "", text,
                  flags=re.IGNORECASE | re.DOTALL)

    # Map semantic tags to Telegram-supported equivalents
    text = re.sub(r"<(/?)strong\b[^>]*>", r"<\1b>", text, flags=re.IGNORECASE)
    text = re.sub(r"<(/?)em\b[^>]*>", r"<\1i>", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li\b[^>]*>", "• ", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6]|section|article|header|footer)\s*>", "\n", text,
                  flags=re.IGNORECASE)

    # Strip unsupported HTML tags while retaining text inside them
    unsupported_tags = [
        "html", "head", "meta", "title", "body", "style", "section",
        "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "div", "span", "p", "br", "link", "script"
    ]
    pattern = r"</?(" + "|".join(unsupported_tags) + r")\b[^>]*>"
    text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    # Escape bare ampersands that are not already HTML entities
    text = re.sub(r"&(?!#?\w+;)", "&amp;", text)

    # Fix duplicate '@' symbols in username references
    text = re.sub(r"@@+", "@", text)

    # Normalize whitespace-only lines and excessive newlines
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _split(text: str, limit: int = TG_LIMIT) -> List[str]:
    """Splits text into Telegram-sized chunks on paragraph, then line boundaries."""
    if len(text) <= limit:
        return [text]

    # Units are paragraphs; oversized paragraphs fall back to lines, then hard cuts.
    units = []
    for block in text.split("\n\n"):
        if len(block) <= limit:
            units.append(("\n\n", block))
            continue
        for i, line in enumerate(block.split("\n")):
            sep = "\n\n" if i == 0 else "\n"
            while len(line) > limit:
                units.append((sep, line[:limit]))
                line, sep = line[limit:], ""
            units.append((sep, line))

    parts, current = [], ""
    for sep, unit in units:
        if current and len(current) + len(sep) + len(unit) > limit:
            parts.append(current)
            current = unit
        else:
            current = f"{current}{sep}{unit}" if current else unit
    if current:
        parts.append(current)
    return parts


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")


async def fetch_latest_digest(period: str, side: Optional[str] = None) -> Optional[dict]:
    """Returns the newest digest for the given period/side, or None."""
    sort = [("created_at", -1), ("timestamp", -1), ("_id", -1)]

    if side:
        return await digests_collection.find_one({"period": period, "side": side}, sort=sort)

    # No side requested: prefer the overall digest, otherwise the newest of any side.
    doc = await digests_collection.find_one(
        {"period": period, "side": {"$in": OVERALL_SIDES}}, sort=sort
    )
    if doc is None:
        doc = await digests_collection.find_one({"period": period}, sort=sort)
    return doc


async def get_latest_digest(period: str = "day", side: Optional[str] = None) -> str:
    """Returns the newest digest as one sanitized HTML string (may exceed Telegram's limit)."""
    doc = await fetch_latest_digest(period, side)
    if not doc:
        return f"<b>{NO_DIGEST_TEXT}</b>"
    return "\n\n".join(render_digest(doc))


def _digest_date(doc: dict) -> str:
    value = doc.get("created_at") or doc.get("timestamp")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            return value
    return "unknown date"


def render_digest(doc: Optional[dict]) -> List[str]:
    """Turns a digest document into sanitized, Telegram-sized HTML messages."""
    if not doc:
        return [NO_DIGEST_TEXT]

    content = doc.get("content") or doc.get("text") or ""
    blocks = content if isinstance(content, list) else [content]

    messages = []
    for block in blocks:
        clean = sanitize_telegram_html(str(block))
        if clean:
            messages.extend(_split(clean))
    return messages or [NO_DIGEST_TEXT]


def digest_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, (_, _, label) in SELECTIONS.items():
        builder.button(text=label, callback_data=f"digest:{key}")
    builder.adjust(1, 2, 1)
    return builder.as_markup()


async def send_html(chat_id: int, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    """Sends HTML, falling back to plain text if Telegram rejects the entities."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        logger.warning(f"HTML rejected by Telegram ({e}); resending as plain text")
        await bot.send_message(chat_id=chat_id, text=_strip_tags(text)[:4096],
                               reply_markup=reply_markup, parse_mode=None)


async def send_digest(chat_id: int, key: str, with_keyboard: bool = True) -> bool:
    """Sends the latest digest for a selection. Returns False if none was found."""
    period, side, _ = SELECTIONS[key]
    doc = await fetch_latest_digest(period, side)
    messages = render_digest(doc)
    markup = digest_keyboard() if with_keyboard else None

    for idx, text in enumerate(messages):
        is_last = idx == len(messages) - 1
        await send_html(chat_id, text, reply_markup=markup if is_last else None)
    return doc is not None


@router.message(Command("start", "help"))
async def cmd_start(message: types.Message):
    await message.answer(
        "<b>Executive DefTech Intelligence Assistant Active.</b>\n\n"
        "Commands:\n"
        "/digest - Digest menu (today's digest)\n"
        "/day - Latest daily digest\n"
        "/day_ua - Daily digest (UA sources)\n"
        "/day_ru - Daily digest (RU sources)\n"
        "/week - Latest weekly digest\n"
        "/status - Latest available digests",
        reply_markup=digest_keyboard()
    )


@router.message(Command("digest", "day", "week", "day_ua", "day_ru", "day-ua", "day-ru"))
async def cmd_digest(message: types.Message):
    command = message.text.split()[0].lstrip("/").split("@")[0].lower().replace("-", "_")
    # /digest defaults to today's digest; the attached keyboard switches between options.
    await send_digest(message.chat.id, "day" if command == "digest" else command)


@router.callback_query(F.data.startswith("digest:"))
async def cb_digest(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    await callback.answer()
    if key in SELECTIONS and callback.message:
        await send_digest(callback.message.chat.id, key)


@router.message(Command("status"))
async def cmd_status(message: types.Message):
    lines = ["<b>Latest available digests:</b>"]
    for key, (period, side, label) in SELECTIONS.items():
        doc = await fetch_latest_digest(period, side)
        lines.append(f"• {label}: <b>{_digest_date(doc) if doc else 'none'}</b>")
    await message.answer("\n".join(lines), reply_markup=digest_keyboard())


async def setup_bot_commands():
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        logger.warning(f"Failed to register bot commands: {e}")
