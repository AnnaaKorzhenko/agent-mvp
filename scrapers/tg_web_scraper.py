# scrapers/tg_web_scraper.py
import logging
import random
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from config import get_db
from utils.source_loader import sync_sources_to_db

logger = logging.getLogger(__name__)

def scrape_tg_channel(source_doc: dict) -> list[dict]:
    handle = source_doc["username"]
    source_id = source_doc["_id"]
    category = source_doc.get("category")

    url = f"https://t.me/s/{handle}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        messages = soup.find_all("div", class_="tgme_widget_message")
        extracted_posts = []

        for msg in messages:
            text_node = msg.find("div", class_="tgme_widget_message_text")
            date_node = msg.find("time", class_="datetime")
            link_node = msg.find("a", class_="tgme_widget_message_date")

            if not text_node or not link_node:
                continue

            post_text = text_node.get_text(separator="\n", strip=True)
            post_url = link_node.get("href", "")
            post_id = post_url.split("/")[-1] if post_url else ""
            pub_date_str = date_node.get("datetime", "") if date_node else datetime.utcnow().isoformat()

            # Matched exact requested post schema
            extracted_posts.append({
                "post_url": post_url,
                "category": category,
                "source_id": source_id,
                "created_at": datetime.utcnow().isoformat(),
                "post_id": post_id,
                "processed": False,
                "published_at": pub_date_str,
                "raw_text": post_text
            })

        return extracted_posts

    except Exception as e:
        logger.error(f"Error scraping @{handle}: {e}")
        return []

async def run_tg_scraper(max_sources: int | None = None):
    db = get_db()
    # Sync and load sources from DB
    sources = await sync_sources_to_db()
    tg_sources = [s for s in sources if s["platform"] == "telegram"]

    # Limit sources if max_sources is set (useful for quick testing)
    if max_sources and max_sources > 0:
        tg_sources = tg_sources[:max_sources]
        logger.info(f"Limited run active: Processing {max_sources} sources.")

    for source in tg_sources:
        posts = scrape_tg_channel(source)
        inserted_count = 0
        for post in posts:
            result = await db.raw_posts.update_one(
                {"post_url": post["post_url"]},
                {"$setOnInsert": post},
                upsert=True
            )
            if result.upserted_id:
                inserted_count += 1

        logger.info(f"TG @{source['username']}: {len(posts)} fetched, {inserted_count} new saved.")
        time.sleep(random.randint(1, 3))