"""Збір постів з публічної веб-версії Telegram-каналів (t.me/s/...)."""
import asyncio
import logging
import random
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from scrapers.cleaning import clean_post_text, is_ad
from scrapers.db import posts_collection, sources_collection

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_channel_html(channel_username: str) -> str | None:

    username = channel_username.replace("@", "")
    url = f"https://t.me/s/{username}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        logger.error(f"Помилка запиту до {channel_username}: {e}")
        return None

    if response.status_code != 200:
        logger.error(f"Не вдалося завантажити {channel_username}: HTTP {response.status_code}")
        return None
    return response.text


def parse_posts(html: str) -> list[dict]:

    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for msg in soup.find_all("div", class_="tgme_widget_message"):
        text_node = msg.find("div", class_="tgme_widget_message_text")
        date_node = msg.find("time", class_="datetime")
        link_node = msg.find("a", class_="tgme_widget_message_date")

        if not text_node or not link_node:
            continue

        raw_text = text_node.get_text(separator="\n", strip=True)
        if is_ad(raw_text):
            continue

        post_text = clean_post_text(raw_text)
        if not post_text:
            continue

        post_url = link_node.get("href", "")
        posts.append({
            "post_id": post_url.split("/")[-1] if post_url else "",
            "post_url": post_url,
            "raw_text": post_text,
            "summary": None,
            "category": None,
            "cross_border_focus": None,
            "published_at": date_node.get("datetime", "") if date_node else utc_now_iso(),
            "processed": False,
            "created_at": utc_now_iso(),
        })

    return posts


def scrape_tg_web_channel(channel_username: str) -> list[dict]:
    html = fetch_channel_html(channel_username)
    if html is None:
        return []
    try:
        return parse_posts(html)
    except Exception as e:
        logger.error(f"Помилка парсингу {channel_username}: {e}")
        return []


async def save_posts(channel_id, posts: list[dict], username: str) -> int:

    saved = 0
    for post in posts:
        post_data = {"channel_id": channel_id, **post}
        result = await posts_collection.update_one(
            {"post_url": post_data["post_url"]},
            {"$setOnInsert": post_data},
            upsert=True,
        )
        if result.upserted_id:
            saved += 1
            preview = post_data["raw_text"].replace("\n", " ")[:80]
            logger.info(f"   ({username}): {preview}...")
    return saved


async def run_tg_scraper() -> None:
    channels = await sources_collection.find({"platform": "telegram"}).to_list(length=None)
    if not channels:
        logger.warning("У колекції 'sources' не знайдено жодного Telegram-каналу!")
        return

    for channel in channels:
        username = channel.get("username", "")
        country = channel.get("source_country", "Unknown")
        if not username:
            continue


        posts = await asyncio.to_thread(scrape_tg_web_channel, username)
        saved = await save_posts(channel["_id"], posts, username)

        logger.info(
            f"Канал {username} ({country}): знайдено постів: {len(posts)}, "
            f"нових збережено в 'raw_posts': {saved}."
        )
        await asyncio.sleep(random.randint(2, 5))