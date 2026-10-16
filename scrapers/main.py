import asyncio
import logging

from scraper import run_tg_scraper
from summarizer import generate_missing_summaries


async def main() -> None:
    await run_tg_scraper()
    await generate_missing_summaries()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())