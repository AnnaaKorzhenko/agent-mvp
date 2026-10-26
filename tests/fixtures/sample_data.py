"""Handwritten sample documents in the real shapes (docs/DATA_CONTRACT.md). No production data."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bson import ObjectId

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat()          # '2026-10-26T08:15:00+00:00', the pipeline's format


SRC_UA = ObjectId("65f000000000000000000001")
SRC_RU = ObjectId("65f000000000000000000002")

P_DRONES = ObjectId("66a000000000000000000001")      # ua, public
P_GRANTS = ObjectId("66a000000000000000000002")      # ua, no label (= internal)
P_RU_CLAIM = ObjectId("66a000000000000000000003")    # ru
P_SECRET = ObjectId("66a000000000000000000004")      # ua, confidential
P_TOP = ObjectId("66a000000000000000000005")         # ua, restricted
P_AD = ObjectId("66a000000000000000000006")          # ad, excluded
P_OLD = ObjectId("66a000000000000000000007")         # 20 days old

USER_ID = 111000111
DIGEST_UA = ObjectId("67b000000000000000000001")
DIGEST_RU = ObjectId("67b000000000000000000002")

SOURCES = [
    {"_id": SRC_UA, "username": "ua_defense_news", "platform": "telegram", "category": None,
     "created_at": iso(NOW - timedelta(days=90)), "source_country": "UA"},
    {"_id": SRC_RU, "username": "ru_war_channel", "platform": "telegram", "category": None,
     "created_at": iso(NOW - timedelta(days=90)), "source_country": "RU"},
]


def _post(_id, *, summary, side="ua", category="defense_tech", tags=(), importance=3, hours_ago=5,
          channel=SRC_UA, **extra) -> dict:
    return {"_id": _id, "post_url": f"https://t.me/{'ru_war_channel' if side == 'ru' else 'ua_defense_news'}/{_id}",
            "category": category, "channel_id": channel, "created_at": iso(NOW - timedelta(hours=hours_ago)),
            "cross_border_focus": None, "post_id": str(int(str(_id)[-4:], 16)), "processed": True,
            "published_at": iso(NOW - timedelta(hours=hours_ago)), "summary": summary, "views": 1200,
            "analyzed_at": NOW, "importance": importance, "is_ad": False, "prompt_version": 6,
            "source_side": side, "tags": list(tags), "text_removed": False, **extra}


POSTS = [
    _post(P_DRONES, summary="Українські морські дрони уразили катер у Чорному морі.",
          tags=["морські дрони", "Чорне море"], importance=4, sensitivity="public"),
    _post(P_GRANTS, summary="Brave1 видав гранти на розробку засобів РЕБ.", category="investments",
          tags=["Brave1", "гранти", "РЕБ"], importance=5, hours_ago=20),
    _post(P_RU_CLAIM, summary="Росийские каналы заявляют о продвижении под Покровском.", side="ru",
          category="ru_front", tags=["Покровск"], importance=3, channel=SRC_RU),
    _post(P_SECRET, summary="Внутрішня оцінка постачання дронів підрозділу.", tags=["дрони"],
          importance=5, sensitivity="confidential"),
    _post(P_TOP, summary="Закритий звіт про РЕБ.", tags=["РЕБ"], importance=5, sensitivity="restricted"),
    _post(P_AD, summary="Купуйте дрони зі знижкою", tags=["дрони"], is_ad=True),
    _post(P_OLD, summary="Старі новини про дрони.", tags=["дрони"], hours_ago=24 * 20),
]

DIGESTS = [
    {"_id": DIGEST_UA, "period": "day", "trigger": "scheduled", "side": "ua", "created_at": NOW - timedelta(hours=2),
     "period_start": iso(NOW - timedelta(days=1, hours=2)), "period_end": iso(NOW - timedelta(hours=2)),
     "categories_filter": None, "posts_considered": 5, "items_count": 3, "model": "qwen2.5:7b-instruct",
     "parts_html": ["<b>Зведення</b>\n\n…"],
     "sections": [
         {"category": "defense_tech", "title": "🛠 Інновації", "items": [
             {"text": "Морські дрони уразили катер.", "source_urls": [POSTS[0]["post_url"]],
              "source_post_ids": [P_DRONES], "n": 1},
             {"text": "Оцінка постачання дронів.", "source_urls": [POSTS[3]["post_url"]],
              "source_post_ids": [P_SECRET], "n": 2}]},
         {"category": "investments", "title": "💰 Інвестиції", "items": [
             {"text": "Brave1 видав гранти на РЕБ.", "source_urls": [POSTS[1]["post_url"]],
              "source_post_ids": [P_GRANTS], "n": 3}]},
     ]},
    {"_id": DIGEST_RU, "period": "day", "trigger": "scheduled", "side": "ru", "created_at": NOW - timedelta(hours=2),
     "period_start": iso(NOW - timedelta(days=1, hours=2)), "period_end": iso(NOW - timedelta(hours=2)),
     "categories_filter": None, "posts_considered": 1, "items_count": 1, "model": "qwen2.5:7b-instruct",
     "parts_html": [],
     "sections": [{"category": "ru_front", "title": "⚔️ Фронт", "items": [
         {"text": "Каналы заявляют о продвижении.", "source_urls": [POSTS[2]["post_url"]],
          "source_post_ids": [P_RU_CLAIM], "n": 1}]}]},
]

USERS = [
    {"_id": USER_ID, "first_name": "Test", "username": "test_user", "created_at": NOW - timedelta(days=10),
     "onboarded": True, "daily_push": True, "delivery_mode": "one", "last_seen": NOW,
     "daily": {"date": "2000-01-01", "digest_ids": [DIGEST_UA], "selected": {"ua": ["investments"], "ru": []},
               "status": "sent"},
     "interests": {"category_scores": {"ua": {"defense_tech": 3, "investments": 1, "policy": -1},
                                       "ru": {"ru_front": 2}},
                   "tag_scores": {"РЕБ": 3, "дрони": 2, "мобілізація": -1}},
     "goals": [{"text": "Знайти напрямки для інвестицій у defense tech", "active": True, "source": "user",
                "created_at": NOW - timedelta(days=5)},
               {"text": "Стара ціль", "active": False, "source": "user", "created_at": NOW - timedelta(days=9)}],
     "feedback": [{"post_id": P_GRANTS, "reaction": "like", "text": "", "digest_id": DIGEST_UA, "item_n": 3,
                   "side": "ua", "category": "investments", "tags": ["Brave1"], "at": NOW - timedelta(days=1)}],
     "usage": {"tokens_total": 0}},
]


async def load_samples(db) -> None:
    await db.sources.insert_many(SOURCES)
    await db.posts.insert_many(POSTS)
    await db.digests.insert_many(DIGESTS)
    await db.users.insert_many(USERS)
