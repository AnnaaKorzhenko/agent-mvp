# utils/clean_sources_db.py
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import asyncio
from scrapers.db import sources_collection

def sanitize_username(username: str) -> str:
    """Strips all leading @ symbols and adds a single @."""
    clean_handle = username.strip().lstrip("@")
    return f"@{clean_handle}"

def sanitize_country(country: str | None) -> str:
    """Cleans country tags to prevent repetition (e.g., 'RURU' -> 'RU')."""
    if not country:
        return "RU"
    c = country.strip().upper()
    # Fix repeated 2-letter codes like RURU or UAUA
    if len(c) == 4 and c[:2] == c[2:]:
        return c[:2]
    return c

async def clean_and_deduplicate_sources():
    all_sources = await sources_collection.find({}).to_list(length=None)
    print(f"Total raw source documents found: {len(all_sources)}")

    grouped = {}
    for doc in all_sources:
        raw_user = doc.get("username", "")
        if not raw_user:
            continue
        
        # Key grouped by lowercase handle without @
        canonical_key = raw_user.strip().lstrip("@").lower()
        if canonical_key not in grouped:
            grouped[canonical_key] = []
        grouped[canonical_key].append(doc)

    print(f"Unique channels identified: {len(grouped)}")

    # Wipe existing collection to rebuild cleanly
    await sources_collection.delete_many({})

    cleaned_documents = []
    for key, docs in grouped.items():
        # Prefer the document that has an existing source_country set
        country = "RU"
        for d in docs:
            existing_country = d.get("source_country")
            if existing_country and str(existing_country).strip():
                country = sanitize_country(str(existing_country))
                break

        canonical_username = sanitize_username(docs[0].get("username", ""))

        cleaned_documents.append({
            "username": canonical_username,
            "platform": "telegram",
            "source_country": country
        })

    if cleaned_documents:
        await sources_collection.insert_many(cleaned_documents)
        print(f"✓ Cleaned database! {len(cleaned_documents)} standardized source documents inserted.")

if __name__ == "__main__":
    asyncio.run(clean_and_deduplicate_sources())