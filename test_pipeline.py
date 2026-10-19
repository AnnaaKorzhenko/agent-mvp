# test_pipeline.py
import asyncio
import logging
from scrapers.scraper import run_tg_scraper
from scrapers.summarizer import analyze_pending_posts
from bot.telegram_bot import generate_daily_briefing

logging.basicConfig(level=logging.INFO)

async def test_limited_run():
    print("=== STARTING FAST TEST PIPELINE ===")
    
    print("\n1. Scraping 2 channels (max 3 posts each)...")
    await run_tg_scraper(max_channels=2, max_posts_per_channel=3)
    
    print("\n2. Analyzing 5 pending posts via LLM...")
    await analyze_pending_posts(chunk=5, max_posts=5)
    
    print("\n3. Synthesizing executive briefing...")
    briefing = await generate_daily_briefing()
    
    print("\n=== GENERATED BRIEFING RESULT ===")
    print(briefing)

if __name__ == "__main__":
    asyncio.run(test_limited_run())