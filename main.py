# main.py
# Railway entrypoint: serves pre-computed digests from MongoDB.
# Scraping and LLM summarization run externally (e.g. Kaggle) and write to News-data.digests.
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.telegram_bot import bot, dp, send_digest, setup_bot_commands
from config import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def proactive_morning_briefing():
    if not config.DIRECTOR_CHAT_ID:
        logger.info("DIRECTOR_CHAT_ID not set; skipping proactive briefing.")
        return
    try:
        found = await send_digest(config.DIRECTOR_CHAT_ID, "day")
        logger.info(f"Proactive briefing delivered (digest found: {found}).")
    except Exception as e:
        logger.error(f"Failed to deliver proactive briefing: {e}")


async def main() -> None:
    config.validate()

    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(
        proactive_morning_briefing,
        CronTrigger(hour=config.DAILY_PUSH_HOUR, minute=config.DAILY_PUSH_MINUTE,
                    timezone=config.TIMEZONE)
    )
    scheduler.start()
    logger.info(
        f"DefTech digest bot is online (daily push at "
        f"{config.DAILY_PUSH_HOUR:02d}:{config.DAILY_PUSH_MINUTE:02d} {config.TIMEZONE})."
    )

    await setup_bot_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
