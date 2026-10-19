import re
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from groq import AsyncGroq
from config import config
from scrapers.db import posts_collection

logger = logging.getLogger(__name__)

groq_client = AsyncGroq(api_key=config.GROQ_API_KEY)

bot = Bot(
    token=config.TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
router = Router()
dp.include_router(router)


def sanitize_telegram_html(text: str) -> str:
    """Removes web-specific HTML tags, code fences, and fixes handle formatting for Telegram."""
    if not text:
        return ""

    # Remove code block fences like ```html ... ```
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)

    # Strip full document wrappers
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.IGNORECASE)
    
    # Strip unsupported HTML tags while retaining text inside them
    unsupported_tags = [
        "html", "head", "meta", "title", "body", "style", "section", 
        "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", 
        "ul", "ol", "li", "div", "span", "p", "br", "em", "strong"
    ]
    pattern = r"</?(" + "|".join(unsupported_tags) + r")[^>]*>"
    text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    # Fix duplicate '@' symbols in username references
    text = re.sub(r"@@+", "@", text)

    # Normalize excessive newlines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "<b>Executive DefTech Intelligence Assistant Active.</b>\n\n"
        "Commands:\n"
        "/digest - Generate instant technical briefing\n"
        "/status - Check database ingestion stats"
    )


@router.message(Command("status"))
async def cmd_status(message: types.Message):
    total_raw = await posts_collection.count_documents({})
    high_priority = await posts_collection.count_documents({"priority": "HIGH"})
    processed = await posts_collection.count_documents({"processed": True})

    await message.answer(
        f"<b>System Status:</b>\n"
        f"• Total Raw Posts: <b>{total_raw}</b>\n"
        f"• Processed Posts: <b>{processed}</b>\n"
        f"• High-Priority Intel Captured: <b>{high_priority}</b>"
    )


@router.message(Command("digest"))
async def cmd_digest(message: types.Message):
    await message.answer("<i>Compiling technical briefing via Groq...</i>")
    briefing_text = await generate_daily_briefing()

    builder = InlineKeyboardBuilder()
    builder.button(text="📁 Archive All", callback_data="archive_digest")

    await message.answer(briefing_text, reply_markup=builder.as_markup())


async def generate_daily_briefing() -> str:
    pipeline = [
        {
            "$match": {
                "processed": True,
                "$or": [
                    {"priority": "HIGH"},
                    {"relevance_score": {"$gte": 4}}
                ]
            }
        },
        {"$sort": {"created_at": -1}},
        {"$limit": 15},
        {
            "$lookup": {
                "from": "sources",
                "localField": "channel_id",
                "foreignField": "_id",
                "as": "source_info"
            }
        },
        {"$unwind": {"path": "$source_info", "preserveNullAndEmptyArrays": True}}
    ]

    high_value_posts = await posts_collection.aggregate(pipeline).to_list(length=15)

    if not high_value_posts:
        return "<b>Daily Executive Briefing</b>\n\nNo high-priority intelligence items captured in the last 24 hours."

    formatted_items = []
    for idx, p in enumerate(high_value_posts, 1):
        src = p.get("source_info", {})
        username = src.get("username", "Unknown").replace("@", "")
        formatted_items.append(
            f"Item {idx}:\n"
            f"- Source: @{username} (Origin: {src.get('source_country', 'Global')})\n"
            f"- Focus: {p.get('analysis_direction', 'N/A')} (Target Subject: {p.get('subject_country', 'Global')})\n"
            f"- Pillar: {p.get('intel_pillar', 'NOISE')}\n"
            f"- Summary: {p.get('summary', '')}\n"
            f"- Link: {p.get('post_url', '')}\n"
        )

    context_str = "\n\n".join(formatted_items)

    prompt = f"""
You are the Chief Intelligence Analyst for a Ukrainian DefTech R&D Director.
Synthesize the provided intel summaries into a structured executive morning digest in Ukrainian.

CRITICAL TELEGRAM FORMATTING RULES:
1. Use ONLY basic inline formatting: <b>bold</b>, <i>italic</i>, and <a href="...">links</a>.
2. ABSOLUTELY DO NOT output web HTML structure tags like <!DOCTYPE>, <html>, <head>, <body>, <style>, <section>, <h2>, <ul>, <li>, <p>, or <br>.
3. DO NOT wrap output in markdown code blocks like ```html.
4. Use normal line breaks (Enter) between points.
5. Avoid duplicate @ symbols when mentioning channels.

Structure your response into these 4 sections:
1. 🇷🇺 <b>Ворожа техніка та винахідливість (Adversary Tech)</b>
2. 🗣️ <b>Аналітика та Настрої Ворога (Adversary Sentiment & Self/Cross Analysis)</b>
3. 🇺🇦 <b>Українські досягнення & Робототехніка (Domestic Tech)</b>
4. 🌍 <b>Глобальний Контекст & Світові Аналітичні Звіти (Global Intel)</b>

For every point, explicitly note which channel is analyzing whom.

Items to synthesize:
{context_str}
"""

    try:
        response = await groq_client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )
        raw_body = response.choices[0].message.content.strip()
        clean_body = sanitize_telegram_html(raw_body)

        return f"<b>🛡️ DefTech OSINT Executive Digest</b>\n<i>{datetime.utcnow().strftime('%Y-%m-%d')}</i>\n\n{clean_body}"
    except Exception as e:
        logger.error(f"Error compiling briefing via Groq: {e}")
        return "<b>Error generating briefing summary. Please check logs.</b>"