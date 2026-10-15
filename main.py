import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import config
from scrapers.tg_web_scraper import run_tg_scraper
from scrapers.web_rss_scraper import run_rss_scraper
from processing.evaluator import evaluate_unprocessed_posts
from bot.telegram_bot import bot, dp, send_scheduled_morning_digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("main")

async def pipeline_job():
    logger.info("--- Starting Ingestion & Processing Pipeline ---")
    await run_tg_scraper()
    await run_rss_scraper()
    await evaluate_unprocessed_posts()
    logger.info("--- Pipeline Completed Successfully ---")

async def main():
    logger.info("Initializing DefTech Executive AI Engine...")

    # Configure Async Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(pipeline_job, "interval", minutes=config.SCRAPE_INTERVAL_MINUTES)
    scheduler.add_job(send_scheduled_morning_digest, "cron", hour=7, minute=30)
    scheduler.start()

    # Trigger immediate initial run on boot
    asyncio.create_task(pipeline_job())

    # Start Telegram Bot Polling
    logger.info("Telegram Bot started listening...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())