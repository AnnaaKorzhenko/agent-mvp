import os
import re
import logging
from datetime import datetime
import pandas as pd
from config import get_db

logger = logging.getLogger(__name__)

def clean_handle(val) -> str:
    if pd.isna(val):
        return ""
    val_str = str(val).strip()
    if "t.me/" in val_str:
        val_str = val_str.split("t.me/")[-1].replace("s/", "").strip("/")
    return val_str.lstrip("@")

async def sync_sources_to_db(sources_file_path: str = "data/sources/sources.xlsx") -> list[dict]:
    """
    Reads the master sources file and upserts every row into the MongoDB `sources` collection.
    Matches exact schema:
    {
        username: 'DeepStateUA',
        platform: 'telegram',
        source_country: 'UA',
        category: null,
        created_at: '2026-10-16T09:24:52.591583'
    }
    """
    db = get_db()
    synced_sources = []

    if not os.path.exists(sources_file_path):
        # Fallback: check for any .xlsx or .csv file in data/sources/
        sources_dir = os.path.dirname(sources_file_path)
        if os.path.exists(sources_dir):
            files = [f for f in os.listdir(sources_dir) if f.endswith(('.xlsx', '.csv'))]
            if files:
                sources_file_path = os.path.join(sources_dir, files[0])
            else:
                logger.warning(f"No Excel or CSV source files found in '{sources_dir}'.")
                return synced_sources
        else:
            logger.warning(f"Sources directory missing: '{sources_dir}'")
            return synced_sources

    try:
        df = pd.read_excel(sources_file_path) if sources_file_path.endswith(".xlsx") else pd.read_csv(sources_file_path)

        # Standardize column header names to lowercase
        df.columns = [str(c).strip().lower() for c in df.columns]

        # Map column names dynamically
        username_col = next((c for c in df.columns if "user" in c or "handle" in c or "username" in c), df.columns[0])
        platform_col = next((c for c in df.columns if "plat" in c or "type" in c), None)
        country_col = next((c for c in df.columns if "countr" in c or "origin" in c), None)
        category_col = next((c for c in df.columns if "categ" in c), None)

        for _, row in df.iterrows():
            handle = clean_handle(row.get(username_col))
            if not handle:
                continue

            platform_val = str(row.get(platform_col)).strip().lower() if platform_col and not pd.isna(row.get(platform_col)) else "telegram"
            country_val = str(row.get(country_col)).strip().upper() if country_col and not pd.isna(row.get(country_col)) else "UNKNOWN"
            
            raw_category = row.get(category_col) if category_col else None
            category_val = str(raw_category).strip() if raw_category and not pd.isna(raw_category) else None
            if category_val in ["nan", "None", "", "null"]:
                category_val = None

            source_doc = {
                "username": handle,
                "platform": platform_val,
                "source_country": country_val,
                "category": category_val
            }

            # Upsert into MongoDB 'sources' collection
            result = await db.sources.find_one_and_update(
                {"username": handle, "platform": platform_val},
                {
                    "$setOnInsert": {
                        "created_at": datetime.utcnow().isoformat()
                    },
                    "$set": source_doc
                },
                upsert=True,
                return_document=True
            )
            synced_sources.append(result)

        logger.info(f"Successfully synced {len(synced_sources)} sources from '{sources_file_path}' into MongoDB.")

    except Exception as e:
        logger.error(f"Error parsing master sources file '{sources_file_path}': {e}")

    return synced_sources