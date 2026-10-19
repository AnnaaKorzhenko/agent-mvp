# utils/check_prod_db.py
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import asyncio
from scrapers.db import posts_collection

async def check_production_db():
    print("=== PRODUCTION MONGO-DB DIAGNOSTIC ===")
    total = await posts_collection.count_documents({})
    processed_true = await posts_collection.count_documents({"processed": True})
    processed_false = await posts_collection.count_documents({"processed": False})
    high_priority = await posts_collection.count_documents({"priority": "HIGH"})
    
    print(f"Total raw posts in DB:        {total}")
    print(f"Processed == True:            {processed_true}")
    print(f"Processed == False:           {processed_false}")
    print(f"Priority == 'HIGH':           {high_priority}")

    print("\n--- SAMPLE HIGH-PRIORITY POST IN PRODUCTION ---")
    high_sample = await posts_collection.find_one({"priority": "HIGH"})
    if high_sample:
        print(f" • ID:              {high_sample.get('_id')}")
        print(f" • Processed:       {high_sample.get('processed')} (Type: {type(high_sample.get('processed'))})")
        print(f" • Relevance Score: {high_sample.get('relevance_score')} (Type: {type(high_sample.get('relevance_score'))})")
        print(f" • Summary:         {bool(high_sample.get('summary'))}")
        print(f" • Created At:      {high_sample.get('created_at')}")
    else:
        print("No sample found with priority == 'HIGH'")

    print("\n--- CHECKING QUERY MATCH COUNT FOR DIGEST ---")
    digest_query = {
        "processed": True,
        "$or": [
            {"priority": "HIGH"},
            {"relevance_score": {"$gte": 4}}
        ]
    }
    match_count = await posts_collection.count_documents(digest_query)
    print(f"Posts matching /digest query: {match_count}")

if __name__ == "__main__":
    asyncio.run(check_production_db())