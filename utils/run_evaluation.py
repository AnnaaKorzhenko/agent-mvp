# utils/run_evaluation.py
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import asyncio
import logging
from scrapers.db import posts_collection
from processing.evaluator import evaluate_unprocessed_posts

logging.basicConfig(level=logging.INFO)

async def run_full_evaluation():
    unprocessed = await posts_collection.count_documents({
        "processed": False, 
        "summary": {"$nin": [None, ""]}
    })
    print(f"Starting Groq batch evaluation for {unprocessed} posts...")
    
    evaluated_total = 0
    while True:
        count = await evaluate_unprocessed_posts(batch_size=10)
        if count == 0:
            break
        evaluated_total += count
        print(f"Processed batch. Total evaluated so far: {evaluated_total}")
        await asyncio.sleep(1)

    print(f"\n✓ Completed! Evaluated {evaluated_total} posts via Groq.")

if __name__ == "__main__":
    asyncio.run(run_full_evaluation())