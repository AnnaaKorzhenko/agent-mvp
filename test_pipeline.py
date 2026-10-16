# test_pipeline.py
import asyncio
from config import get_db
from scrapers.tg_web_scraper import run_tg_scraper
from processing.evaluator import evaluate_unprocessed_posts
from bot.telegram_bot import generate_daily_briefing

async def run_full_test():
    db = get_db()

    print("==========================================")
    print("STEP 1: TESTING TELEGRAM SCRAPER (Cap ~500 Posts)")
    print("==========================================")
    # 25 sources * ~20 posts/channel = ~500 raw posts
    await run_tg_scraper(max_sources=25)
    
    raw_count = await db.raw_posts.count_documents({})
    print(f"✓ Scraper completed. Total raw posts in DB: {raw_count}")

    print("\n==========================================")
    print("STEP 2: TESTING DEDUP & GEMINI EVALUATOR")
    print("==========================================")
    # Process a batch of 30 posts through FastEmbed dedup & Gemini API
    await evaluate_unprocessed_posts(limit=5)

    evaluated_sample = await db.raw_posts.find_one({"processed": True, "is_duplicate": {"$ne": True}})
    if evaluated_sample:
        print("\n[Evaluated Sample Result]")
        print(f" • Score:    {evaluated_sample.get('relevance_score')}/10")
        print(f" • Pillar:   {evaluated_sample.get('intel_pillar')}")
        print(f" • Target:   {evaluated_sample.get('subject_country')}")
        print(f" • Summary:  {evaluated_sample.get('summary')}")

    print("\n==========================================")
    print("STEP 3: TESTING BRIEFING GENERATION")
    print("==========================================")
    briefing = await generate_daily_briefing()
    print("\n--- GENERATED BRIEFING OUTPUT ---")
    print(briefing)
    print("==========================================")

if __name__ == "__main__":
    asyncio.run(run_full_test())