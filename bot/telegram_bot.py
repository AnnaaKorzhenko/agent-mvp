import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from anthropic import AsyncAnthropic
from config import config, get_db

logger = logging.getLogger(__name__)

bot = Bot(
    token=config.TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

anthropic_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "<b>Executive DefTech Intelligence Assistant Active.</b>\n\n"
        "Available Commands:\n"
        "/digest - Generate instant technical intelligence briefing\n"
        "/status - Check database ingestion stats"
    )

@router.message(Command("status"))
async def cmd_status(message: types.Message):
    db = get_db()
    total_raw = await db.raw_posts.count_documents({})
    high_priority = await db.raw_posts.count_documents({"priority": "HIGH"})
    
    await message.answer(
        f"<b>System Status:</b>\n"
        f"• Total Posts Ingested: <b>{total_raw}</b>\n"
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
    
    high_value_posts = await db.raw_posts.find({
        "processed": True,
        "relevance_score": {"$gte": 6},
        "created_at": {"$gte": since_time}
    }).sort("relevance_score", -1).to_list(length=15)

    if not high_value_posts:
        return "<b>Daily Executive Briefing</b>\n\nNo high-priority technical items detected in the last 24 hours."

    formatted_items = []
    for idx, p in enumerate(high_value_posts, 1):
        formatted_items.append(
            f"Item {idx} [{p.get('domain', 'General')}]:\n"
            f"- Summary: {p.get('summary')}\n"
            f"- Source: {p.get('post_url')}\n"
            f"- Score: {p.get('relevance_score')}/10"
        )

    context_str = "\n\n".join(formatted_items)

    prompt = f"""
Synthesize the following prioritized intelligence items into a single, cohesive Executive Morning Briefing for the R&D Director.
Group items by technical domain (e.g., Electronic Warfare, Drone Hardware). Keep sentences concise, direct, and actionable.

Items:
{context_str}
"""

    try:
        response = await anthropic_client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        briefing_body = response.content[0].text.strip()
        return f"<b>🛡️ Daily DefTech R&D Briefing</b>\n<i>{datetime.utcnow().strftime('%Y-%m-%d')}</i>\n\n{briefing_body}"
    except Exception as e:
        logger.error(f"Error compiling briefing with Sonnet: {e}")
        return "<b>Error generating briefing summary. Please review raw posts in status.</b>"

async def send_scheduled_morning_digest():
    if config.DIRECTOR_CHAT_ID == 0:
        logger.warning("DIRECTOR_CHAT_ID not configured. Skipping scheduled push.")
        return
        
    briefing_text = await generate_daily_briefing()
    builder = InlineKeyboardBuilder()
    builder.button(text="📁 Archive", callback_data="archive_digest")
    
    await bot.send_message(
        chat_id=config.DIRECTOR_CHAT_ID,
        text=briefing_text,
        reply_markup=builder.as_markup()
    )