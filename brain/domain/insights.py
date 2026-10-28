"""Knowledge the user shares (save_insight) and its recall (get_insights), in users.insights.

Private by default: an insight is `confidential` unless the user chooses otherwise, and is returned only to
clients allowed its label (the same sensitivity rules as posts). Writing any label is allowed: the text has
already reached the server. At most MAX_INSIGHTS per user; at the cap saving is refused, nothing is dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from bson import ObjectId

from brain.db.models import InsightDoc
from brain.db.repositories import Repos
from brain.domain import sensitivity
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext
from brain.domain.posts import query_words, stem_pattern

DEFAULT_SENSITIVITY = "confidential"
MAX_INSIGHTS = 1000
MAX_TEXT = 4000
MAX_AREA = 60
MAX_TAGS = 10
MAX_RESULTS = 20

NO_PROFILE = "No profile for this user yet. Start the Telegram bot once to create it."


@dataclass
class SavedInsight:
    id: ObjectId
    sensitivity: str
    visible_here: bool


@dataclass
class Recall:
    insights: list[InsightDoc]
    hidden: int          # matching insights this client may not read (their labels are above its maximum)


def _clean_tags(tags: list[str] | None) -> list[str]:
    return list(dict.fromkeys(" ".join(t.split())[:40].lower() for t in tags or [] if t and t.strip()))[:MAX_TAGS]


async def save_insight(repos: Repos, ctx: UserContext, text: str | None, area: str | None = None,
                       tags: list[str] | None = None, label: str | None = None,
                       message_id: ObjectId | None = None) -> SavedInsight:
    text = (text or "").strip()[:MAX_TEXT]
    if not text:
        raise InvalidInput("The insight text is empty")
    label = label or DEFAULT_SENSITIVITY
    if label not in sensitivity.LEVELS:
        raise InvalidInput(f"sensitivity must be one of {', '.join(sensitivity.LEVELS)}")
    insight = {"id": ObjectId(), "text": text, "area": " ".join((area or "").split())[:MAX_AREA].lower() or None,
               "tags": _clean_tags(tags), "sensitivity": label, "created_at": datetime.now(timezone.utc),
               "channel": ctx.channel, "conversation_id": None, "message_id": message_id}
    status = await repos.users.push_insight(ctx.user_id, insight, MAX_INSIGHTS)
    if status == "no_user":
        raise NotFound(NO_PROFILE)
    if status == "full":
        raise InvalidInput(f"The user already has {MAX_INSIGHTS} saved insights; nothing was saved")
    return SavedInsight(insight["id"], label, sensitivity.is_allowed(label, ctx.max_sensitivity))


async def get_insights(repos: Repos, ctx: UserContext, query: str | None = None,
                       area: str | None = None, limit: int = MAX_RESULTS) -> Recall:
    """Newest first; with `query`, ranked by how many query words they contain (word starts, as in search)."""
    items = await repos.users.get_insights(ctx.user_id)
    if area:
        items = [i for i in items if (i.area or "") == " ".join(area.split()).lower()]
    words = query_words(query or "")
    if query and not words:
        raise InvalidInput("The query has no searchable words. Use 2-4 specific key words.")
    patterns = [re.compile(stem_pattern(w), re.IGNORECASE) for w in words]

    def hits(i: InsightDoc) -> int:
        haystack = " ".join([i.text, i.area or "", *i.tags]).lower()
        return sum(bool(p.search(haystack)) for p in patterns)

    newest = sorted(items, key=lambda i: i.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    matched = [i for i in newest if hits(i)] if patterns else newest
    if patterns:
        matched.sort(key=hits, reverse=True)                      # stable: newest first within equal hits
    visible = [i for i in matched if sensitivity.is_allowed(i.sensitivity, ctx.max_sensitivity)]
    return Recall(visible[:limit], len(matched) - len(visible))
