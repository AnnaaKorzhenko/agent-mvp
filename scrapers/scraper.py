import asyncio
import logging
import random
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from cleaning import is_ad
from db import posts_collection, sources_collection

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


def _extract_text(text_node) -> str:

    for br in text_node.find_all("br"):
        br.replace_with("\n")
    return text_node.get_text().strip()


def _parse_views(msg) -> int | None:

    node = msg.find("span", class_="tgme_widget_message_views")
    if not node:
        return None
    s = node.get_text(strip=True).upper().replace(",", ".")
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def parse_posts(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for msg in soup.find_all("div", class_="tgme_widget_message"):
        text_node = msg.find("div", class_="tgme_widget_message_text")
        link_node = msg.find("a", class_="tgme_widget_message_date")
        if not text_node or not link_node:
            continue

        raw_text = _extract_text(text_node)
        if not raw_text or is_ad(raw_text):
            continue


        time_node = link_node.find("time", attrs={"datetime": True}) or msg.find("time", attrs={"datetime": True})
        published_at = time_node["datetime"] if time_node else None

        post_url = link_node.get("href", "")
        posts.append({
            "post_id": post_url.split("/")[-1] if post_url else "",
            "post_url": post_url,
            "raw_text": raw_text,
            "published_at": published_at,
            "views": _parse_views(msg),
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
        if not post["post_url"]:
            continue
        on_insert = {
            "channel_id": channel_id,
            "post_id": post["post_id"],
            "post_url": post["post_url"],
            "raw_text": post["raw_text"],
            "summary": None,
            "category": None,
            "cross_border_focus": None,
            "processed": False,
            "created_at": utc_now_iso(),
        }

        to_set = {"views": post["views"]}
        if post["published_at"]:
            to_set["published_at"] = post["published_at"]
        else:
            on_insert["published_at"] = utc_now_iso()

        result = await posts_collection.update_one(
            {"post_url": post["post_url"]},
            {"$setOnInsert": on_insert, "$set": to_set},
            upsert=True,
        )
        if result.upserted_id:
            saved += 1
            preview = post["raw_text"].replace("\n", " ")[:80]
            logger.info(f"   ({username}): {preview}...")
    return saved


async def run_tg_scraper() -> None:
    channels = await sources_collection.find({"platform": "telegram"}).to_list(length=None)
    if not channels:
        logger.warning("У колекції 'sources' не знайдено жодного Telegram-каналу!")
        return

    for channel in channels:
        username = channel.get("username", "")
        country = channel.get("country") or channel.get("source_country") or "Unknown"
        if not username:
            continue

        posts = await asyncio.to_thread(scrape_tg_web_channel, username)
        saved = await save_posts(channel["_id"], posts, username)

        logger.info(
            f"Канал {username} ({country}): знайдено постів: {len(posts)}, "
            f"нових збережено в 'raw_posts': {saved}."
        )
        await asyncio.sleep(random.randint(2, 5))
