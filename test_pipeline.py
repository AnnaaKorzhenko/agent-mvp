# test_pipeline.py
import asyncio
import logging
from scrapers.db import posts_collection
from scrapers.summarizer import generate_missing_summaries
from processing.evaluator import evaluate_unprocessed_posts
from bot.telegram_bot import generate_daily_briefing, bot
from config import config

logging.basicConfig(level=logging.INFO)

async def test_offline_pipeline():
    try:
        print("==========================================")
        print("STEP 1: LOCAL mT5 SUMMARIZATION (OFFLINE)")
        print("==========================================")
        unsummarized_before = await posts_collection.count_documents({
            "$or": [{"summary": None}, {"summary": {"$exists": False}}]
        })
        print(f"Posts requiring local summarization in DB: {unsummarized_before}")

        await generate_missing_summaries(batch_size=4)

        unsummarized_after = await posts_collection.count_documents({
            "$or": [{"summary": None}, {"summary": {"$exists": False}}]
        })
        print(f"✓ Summarization complete. Remaining unsummarized: {unsummarized_after}")

        # --- TEST RESET: Reset processed=False for summarized posts so Groq can evaluate them ---
        await posts_collection.update_many(
            {"summary": {"$nin": [None, ""]}},
            {"$set": {"processed": False}}
        )

        print("\n==========================================")
        print("STEP 2: GROQ EVALUATION & SCORING")
        print("==========================================")
        unprocessed_before = await posts_collection.count_documents({"processed": False})
        print(f"Unprocessed posts ready for Groq: {unprocessed_before}")

        evaluated_count = await evaluate_unprocessed_posts(batch_size=10)
        print(f"✓ Groq evaluated {evaluated_count} posts in batch.")

        sample = await posts_collection.find_one({"processed": True, "relevance_score": {"$gt": 0}})
        if sample:
            print("\n[Evaluated Sample Result]")
            print(f" • Relevance Score:  {sample.get('relevance_score')}/10")
            print(f" • Priority:         {sample.get('priority')}")
            print(f" • Intel Pillar:     {sample.get('intel_pillar')}")
            print(f" • Target Subject:   {sample.get('subject_country')}")
            print(f" • Analysis Focus:   {sample.get('analysis_direction')}")
            print(f" • Summary (Local):  {sample.get('summary')}")

        print("\n==========================================")
        print("STEP 3: BRIEFING SYNTHESIS (GROQ)")
        print("==========================================")
        briefing = await generate_daily_briefing()
        print("\n--- GENERATED BRIEFING PREVIEW ---")
        print(briefing)
        print("==========================================")

        if config.DIRECTOR_CHAT_ID:
            print("\nTesting push message to Telegram...")
            try:
                await bot.send_message(chat_id=config.DIRECTOR_CHAT_ID, text=briefing)
                print("✓ Sent to Telegram successfully!")
            except Exception as e:
                print(f"⚠ Telegram push error: {e}")

    finally:
        # Gracefully close bot HTTP session to prevent unclosed client warnings
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(test_offline_pipeline())