# utils/debug_db.py
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import asyncio
from scrapers.db import posts_collection

async def inspect_database():
    print("=== MONGOBD DIAGNOSTIC ===")
    total = await posts_collection.count_documents({})
    processed = await posts_collection.count_documents({"processed": True})
    unprocessed = await posts_collection.count_documents({"processed": False})
    high_priority = await posts_collection.count_documents({"priority": "HIGH"})
    
    print(f"Total posts:           {total}")
    print(f"Processed:             {processed}")
    print(f"Unprocessed:           {unprocessed}")
    print(f"Priority = 'HIGH':     {high_priority}")

    print("\n--- SAMPLE HIGH-PRIORITY POST ---")
    sample_high = await posts_collection.find_one({"priority": "HIGH"})
    if sample_high:
        print(f"• ID:              {sample_high.get('_id')}")
        print(f"• Processed:       {sample_high.get('processed')} (type: {type(sample_high.get('processed'))})")
        print(f"• Relevance Score: {sample_high.get('relevance_score')} (type: {type(sample_high.get('relevance_score'))})")
        print(f"• Priority:        {sample_high.get('priority')}")
        print(f"• Created At:      {sample_high.get('created_at')}")
        print(f"• Has Summary:     {bool(sample_high.get('summary'))}")
    else:
        print("No post found with priority == 'HIGH'")

    print("\n--- SAMPLE PROCESSED POST ---")
    sample_proc = await posts_collection.find_one({"processed": True})
    if sample_proc:
        print(f"• Relevance Score: {sample_proc.get('relevance_score')} (type: {type(sample_proc.get('relevance_score'))})")
        print(f"• Priority:        {sample_proc.get('priority')}")
    else:
        print("No post found with processed == True")

if __name__ == "__main__":
    asyncio.run(inspect_database())