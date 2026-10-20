import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from cleaning import is_ad
from db import digests_collection, posts_collection, sources_collection

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

MAX_PAGES = 10

SINCE_BUFFER = timedelta(hours=1)

DEFAULT_LOOKBACK = timedelta(days=1)

MAX_LOOKBACK = timedelta(days=3)


async def collect_since() -> datetime:

    now = datetime.now(timezone.utc)
    last = await digests_collection.find_one(
        {"period": "day", "trigger": "scheduled"}, sort=[("created_at", -1)])
    since = now - DEFAULT_LOOKBACK
    if last and last.get("period_end"):
        try:
            prev_end = datetime.fromisoformat(last["period_end"])
            if prev_end.tzinfo is None:
                prev_end = prev_end.replace(tzinfo=timezone.utc)
            since = prev_end - SINCE_BUFFER
        except (TypeError, ValueError):
            pass
    return max(since, now - MAX_LOOKBACK)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_channel_html(channel_username: str, before: int | None = None) -> str | None:
    username = channel_username.replace("@", "")
    url = f"https://t.me/s/{username}"
    if before:
        url += f"?before={before}"
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


def parse_page(html: str) -> tuple[list[dict], int | None, datetime | None]:

    soup = BeautifulSoup(html, "html.parser")
    posts = []
    min_id: int | None = None
    oldest: datetime | None = None

    for msg in soup.find_all("div", class_="tgme_widget_message"):
        data_post = msg.get("data-post", "")
        tail = data_post.rsplit("/", 1)[-1]
        if tail.isdigit():
            min_id = int(tail) if min_id is None else min(min_id, int(tail))

        time_node = msg.find("time", attrs={"datetime": True})
        if time_node:
            try:
                dt = datetime.fromisoformat(time_node["datetime"])
                oldest = dt if oldest is None else min(oldest, dt)
            except ValueError:
                pass

        text_node = msg.find("div", class_="tgme_widget_message_text")
        link_node = msg.find("a", class_="tgme_widget_message_date")
        if not text_node or not link_node:
            continue

        raw_text = _extract_text(text_node)
        if not raw_text or is_ad(raw_text):
            continue

        time_node = link_node.find("time", attrs={"datetime": True}) or time_node
        published_at = time_node["datetime"] if time_node else None

        post_url = link_node.get("href", "")
        posts.append({
            "post_id": post_url.split("/")[-1] if post_url else "",
            "post_url": post_url,
            "raw_text": raw_text,
            "published_at": published_at,
            "views": _parse_views(msg),
        })

    return posts, min_id, oldest


def parse_posts(html: str) -> list[dict]:
    return parse_page(html)[0]


def scrape_tg_web_page(channel_username: str, before: int | None = None):
    html = fetch_channel_html(channel_username, before)
    if html is None:
        return [], None, None
    try:
        return parse_page(html)
    except Exception as e:
        logger.error(f"Помилка парсингу {channel_username}: {e}")
        return [], None, None


def scrape_tg_web_channel(channel_username: str) -> list[dict]:

    return scrape_tg_web_page(channel_username)[0]


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


def _is_fresh(published_at: str | None, stop_at: datetime) -> bool:
    if not published_at:
        return True
    try:
        return datetime.fromisoformat(published_at) >= stop_at
    except ValueError:
        return True


async def scrape_channel_new(channel_id, username: str, stop_at: datetime) -> tuple[int, int]:

    before = None
    found_total = saved_total = 0

    for page in range(MAX_PAGES):
        posts, min_id, oldest = await asyncio.to_thread(scrape_tg_web_page, username, before)

        fresh = [p for p in posts if _is_fresh(p["published_at"], stop_at)]
        saved = await save_posts(channel_id, fresh, username)
        found_total += len(fresh)
        saved_total += saved

        reached_known = saved < len([p for p in fresh if p["post_url"]])
        too_old = oldest is not None and oldest < stop_at
        if reached_known or too_old or min_id is None or min_id <= 1:
            break
        if before is not None and min_id >= before:
            break
        before = min_id
        await asyncio.sleep(random.uniform(1, 2))
    else:
        logger.warning(f"Канал {username}: досягнуто MAX_PAGES={MAX_PAGES}, частина старих постів могла не зібратись")

    return found_total, saved_total


async def run_tg_scraper() -> None:
    stop_at = await collect_since()
    logger.info(f"Збираємо пости, опубліковані після {stop_at:%d.%m %H:%M} UTC")
    channels = await sources_collection.find({"platform": "telegram"}).to_list(length=None)
    if not channels:
        logger.warning("У колекції 'sources' не знайдено жодного Telegram-каналу!")
        return

    for channel in channels:
        username = channel.get("username", "")
        country = channel.get("country") or channel.get("source_country") or "Unknown"
        if not username:
            continue

        found, saved = await scrape_channel_new(channel["_id"], username, stop_at)

        logger.info(
            f"Канал {username} ({country}): знайдено постів: {found}, "
            f"нових збережено: {saved}."
        )
        await asyncio.sleep(random.randint(2, 5))
