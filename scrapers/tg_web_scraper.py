import logging
import random
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from config import config, get_db

logger = logging.getLogger(__name__)

def scrape_tg_web_channel(channel_username: str) -> list[dict]:
    url = f"https://t.me/s/{channel_username.replace('@', '')}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            logger.error(f"Failed to fetch Telegram channel {channel_username}: HTTP {response.status_code}")
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

            extracted_posts.append({
                "source_type": "telegram",
                "source_channel": f"@{channel_username}",
                "post_id": post_id,
                "post_url": post_url,
                "raw_text": post_text,
                "published_at": pub_date_str,
                "processed": False,
                "created_at": datetime.utcnow().isoformat()
            })

        return extracted_posts

    except Exception as e:
        logger.error(f"Error scraping Telegram web channel {channel_username}: {e}")
        return []

async def run_tg_scraper():
    db = get_db()
    for channel in config.TARGET_TG_CHANNELS:
        posts = scrape_tg_web_channel(channel)
        inserted_count = 0
        for post in posts:
            result = await db.raw_posts.update_one(
                {"post_url": post["post_url"]},
                {"$setOnInsert": post},
                upsert=True
            )
            if result.upserted_id:
                inserted_count += 1

        logger.info(f"Telegram channel @{channel}: Found {len(posts)} posts, {inserted_count} new saved.")
        time.sleep(random.randint(2, 5))