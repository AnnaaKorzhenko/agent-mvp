# utils/fix_scores.py
import asyncio
from scrapers.db import posts_collection

async def fix_legacy_scores():
    posts = await posts_collection.find({"relevance_score": {"$type": "string"}}).to_list(length=None)
    print(f"Found {len(posts)} posts with string relevance scores. Converting...")

    fixed = 0
    for p in posts:
        try:
            val = int(p["relevance_score"])
            await posts_collection.update_one({"_id": p["_id"]}, {"$set": {"relevance_score": val}})
            fixed += 1
        except Exception:
            pass

    print(f"✓ Fixed {fixed} posts in MongoDB.")

if __name__ == "__main__":
    asyncio.run(fix_legacy_scores())