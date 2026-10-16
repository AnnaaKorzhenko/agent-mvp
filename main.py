# proactive attempt
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from bot.telegram_bot import bot, dp, generate_daily_briefing
from config import config
from scrapers.tg_web_scraper import run_tg_scraper
from processing.evaluator import evaluate_unprocessed_posts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def scheduled_ingestion():
    """Periodic job to keep the database fresh."""
    logger.info("Starting background ingestion cycle...")
    try:
        await run_tg_scraper(max_sources=50)  # Adjust source batch size as needed
        await evaluate_unprocessed_posts(limit=10)
        logger.info("Background ingestion cycle complete.")
    except Exception as e:
        logger.error(f"Error in background ingestion: {e}")

async def send_proactive_briefing():
    """Proactive push job that sends the morning briefing directly to Telegram."""
    if not config.DIRECTOR_CHAT_ID:
        logger.warning("DIRECTOR_CHAT_ID not configured. Skipping proactive push.")
        return

    logger.info("Generating proactive morning briefing...")
    try:
        briefing = await generate_daily_briefing()
        await bot.send_message(chat_id=config.DIRECTOR_CHAT_ID, text=briefing)
        logger.info("Proactive morning briefing sent successfully to Director.")
    except Exception as e:
        logger.error(f"Failed to deliver proactive briefing: {e}")

async def main():
    scheduler = AsyncIOScheduler()

    # Task 1: Scrape and evaluate every 30 minutes
    scheduler.add_job(
        scheduled_ingestion, 
        'interval', 
        minutes=config.SCRAPE_INTERVAL_MINUTES
    )

    # Task 2: Proactive push briefing every morning at 08:00 AM local time
    scheduler.add_job(
        send_proactive_briefing,
        CronTrigger(hour=7, minute=30)
    )

    scheduler.start()
    logger.info("Proactive Agent Engine initialized and running.")

    # Start bot listener (allows manual /digest triggers if needed)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())