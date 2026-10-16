import logging
from datetime import datetime
import feedparser
from config import config, get_db

logger = logging.getLogger(__name__)

async def run_rss_scraper():
    db = get_db()
    
    for feed_url in config.TARGET_RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            inserted_count = 0

            for entry in feed.entries:
                post_url = getattr(entry, "link", "")
                title = getattr(entry, "title", "")
                summary = getattr(entry, "summary", getattr(entry, "description", ""))
                published = getattr(entry, "published", datetime.utcnow().isoformat())

                full_text = f"{title}\n\n{summary}"

                post_doc = {
                    "source_type": "rss",
                    "source_channel": feed_url,
                    "post_id": post_url,
                    "post_url": post_url,
                    "raw_text": full_text,
                    "published_at": published,
                    "processed": False,
                    "created_at": datetime.utcnow().isoformat()
                }

                result = await db.raw_posts.update_one(
                    {"post_url": post_url},
                    {"$setOnInsert": post_doc},
                    upsert=True
                )
                if result.upserted_id:
                    inserted_count += 1

            logger.info(f"RSS Feed {feed_url}: Processed {len(feed.entries)} items, {inserted_count} new saved.")

        except Exception as e:
            logger.error(f"Error parsing RSS feed {feed_url}: {e}")