# main.py
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from scrapers.scraper import run_tg_scraper
from scrapers.summarizer import generate_missing_summaries
from processing.evaluator import evaluate_unprocessed_posts
from bot.telegram_bot import bot, dp, generate_daily_briefing
from config import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def scheduled_pipeline_cycle():
    logger.info("=== STARTING PIPELINE CYCLE ===")
    try:
        await run_tg_scraper()
        await generate_missing_summaries(batch_size=2)  # Low batch size prevents Railway OOM
        await evaluate_unprocessed_posts(batch_size=10)
        logger.info("=== PIPELINE CYCLE COMPLETED SUCCESSFULLY ===")
    except Exception as e:
        logger.error(f"Error during scheduled pipeline execution: {e}")

async def proactive_morning_briefing():
    if not config.DIRECTOR_CHAT_ID:
        return
    try:
        briefing_text = await generate_daily_briefing()
        await bot.send_message(chat_id=config.DIRECTOR_CHAT_ID, text=briefing_text)
    except Exception as e:
        logger.error(f"Failed to deliver proactive briefing: {e}")

async def main() -> None:
    scheduler = AsyncIOScheduler()
    
    scheduler.add_job(
        scheduled_pipeline_cycle,
        'interval',
        minutes=config.SCRAPE_INTERVAL_MINUTES
    )
    
    scheduler.add_job(
        proactive_morning_briefing,
        CronTrigger(hour=8, minute=0)
    )

    scheduler.start()
    logger.info("Proactive DefTech Intelligence Agent Engine is online.")

    asyncio.create_task(scheduled_pipeline_cycle())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())