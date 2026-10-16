import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from google import genai
from google.genai import types as genai_types
from config import config, get_db

# Initialize Logger
logger = logging.getLogger(__name__)

# Initialize Gemini Client
gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)

# Initialize Aiogram Bot & Dispatcher
bot = Bot(
    token=config.TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
router = Router()
dp.include_router(router)


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
    db = get_db()
    total_raw = await db.raw_posts.count_documents({})
    high_priority = await db.raw_posts.count_documents({"priority": "HIGH"})
    ru_count = await db.raw_posts.count_documents({"country": "RU"})
    ua_count = await db.raw_posts.count_documents({"country": "UA"})

    await message.answer(
        f"<b>System Status:</b>\n"
        f"• Total Posts Ingested: <b>{total_raw}</b>\n"
        f"• Russian Sources Ingested: <b>{ru_count}</b>\n"
        f"• Ukrainian Sources Ingested: <b>{ua_count}</b>\n"
        f"• High-Priority Intel Captured: <b>{high_priority}</b>"
    )


@router.message(Command("digest"))
async def cmd_digest(message: types.Message):
    await message.answer("<i>Compiling technical briefing...</i>")
    briefing_text = await generate_daily_briefing()

    builder = InlineKeyboardBuilder()
    builder.button(text="📁 Archive All", callback_data="archive_digest")

    await message.answer(briefing_text, reply_markup=builder.as_markup())


async def generate_daily_briefing() -> str:
    db = get_db()
    since_time = (datetime.utcnow() - timedelta(hours=24)).isoformat()

    # MongoDB Pipeline joining raw_posts -> sources
    pipeline = [
        {
            "$match": {
                "processed": True,
                "relevance_score": {"$gte": 4},  # Threshold for digest inclusion
                "created_at": {"$gte": since_time}
            }
        },
        {"$sort": {"relevance_score": -1}},
        {"$limit": 20},
        {
            "$lookup": {
                "from": "sources",
                "localField": "source_id",
                "foreignField": "_id",
                "as": "source_info"
            }
        },
        {"$unwind": "$source_info"}
    ]

    high_value_posts = await db.raw_posts.aggregate(pipeline).to_list(length=20)

    if not high_value_posts:
        return "<b>Daily Executive Briefing</b>\n\nNo high-priority intelligence items captured in the last 24 hours."

    formatted_items = []
    for idx, p in enumerate(high_value_posts, 1):
        src = p.get("source_info", {})
        formatted_items.append(
            f"Item {idx}:\n"
            f"- Source: @{src.get('username')} (Origin: {src.get('source_country')})\n"
            f"- Analysis Focus: {p.get('analysis_direction')} (Target Subject: {p.get('subject_country')})\n"
            f"- Category/Pillar: {p.get('intel_pillar')} | {p.get('domain')}\n"
            f"- Summary: {p.get('summary')}\n"
            f"- Link: {p.get('post_url')}\n"
        )

    context_str = "\n\n".join(formatted_items)

    prompt = f"""
You are the Chief Intelligence Analyst for a Ukrainian DefTech R&D Director.
Synthesize the provided intel items into a structured executive morning digest in Ukrainian.

Structure your response into distinct sections using clean HTML formatting:
1. 🇷🇺 **Ворожа техніка та винахідливість (Adversary Tech)**
2. 🗣️ **Аналітика та Настрої Ворога (Adversary Sentiment & Self/Cross Analysis)**
3. 🇺🇦 **Українські досягнення & Робототехніка (Domestic Tech)**
4. 🌍 **Глобальний Контекст & Світові Аналітичні Звіти (Global Intel)**

For every item, explicitly attribute who is performing the analysis and on whom (e.g., "Канал @rybar аналізує ситуацію навколо...").

Items:
{context_str}
"""

    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(temperature=0.2)
        )
        briefing_body = response.text.strip()
        return f"<b>🛡️ DefTech OSINT Executive Digest</b>\n<i>{datetime.utcnow().strftime('%Y-%m-%d')}</i>\n\n{briefing_body}"
    except Exception as e:
        logger.error(f"Error compiling briefing: {e}")
        return "<b>Error generating briefing summary. Please check logs.</b>"