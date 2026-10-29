"""What the user shares about themselves, in users.insights: saved explicitly (save_insight) or detected by the
client model in their messages (log_turn). Recalled with get_insights and summarized for get_profile / log_turn.

Each insight has a kind (KINDS) and an origin (explicit / detected). Default label `internal`, so Claude can read
them back and adapt; the user can make one private (`confidential`, readable only by the local client). Returned
only to clients allowed the label. The same kind + text is stored once. At most MAX_INSIGHTS per user; at the cap
saving is refused, nothing is dropped.
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

DEFAULT_SENSITIVITY = "internal"
PRIVATE = "confidential"
KINDS = ("fact", "goal", "interest", "preference", "feedback", "known", "other")
ORIGINS = ("explicit", "detected")
MAX_INSIGHTS = 1000
MAX_TEXT = 4000
MAX_AREA = 60
MAX_TAGS = 10
MAX_RESULTS = 20

NO_PROFILE = "No profile for this user yet. Start the Telegram bot once to create it."


@dataclass
class SavedInsight:
    id: ObjectId | None          # None: the same kind + text was already saved
    sensitivity: str
    visible_here: bool
    kind: str = "other"
    duplicate: bool = False


@dataclass
class Recall:
    insights: list[InsightDoc]
    hidden: int          # matching insights this client may not read (their labels are above its maximum)


def normalized(text: str) -> str:
    return " ".join(text.lower().split())


def _clean_tags(tags: list[str] | None) -> list[str]:
    return list(dict.fromkeys(" ".join(t.split())[:40].lower() for t in tags or [] if t and t.strip()))[:MAX_TAGS]


async def save_insight(repos: Repos, ctx: UserContext, text: str | None, area: str | None = None,
                       tags: list[str] | None = None, label: str | None = None,
                       message_id: ObjectId | None = None, kind: str | None = None,
                       origin: str = "explicit") -> SavedInsight:
    text = (text or "").strip()[:MAX_TEXT]
    if not text:
        raise InvalidInput("The insight text is empty")
    label = label or DEFAULT_SENSITIVITY
    if label not in sensitivity.LEVELS:
        raise InvalidInput(f"sensitivity must be one of {', '.join(sensitivity.LEVELS)}")
    kind = kind or "other"
    if kind not in KINDS or origin not in ORIGINS:
        raise InvalidInput(f"kind must be one of {', '.join(KINDS)}")
    insight = {"id": ObjectId(), "kind": kind, "origin": origin, "text": text, "norm": normalized(text),
               "area": " ".join((area or "").split())[:MAX_AREA].lower() or None, "tags": _clean_tags(tags),
               "sensitivity": label, "created_at": datetime.now(timezone.utc), "channel": ctx.channel,
               "conversation_id": None, "message_id": message_id}
    status = await repos.users.push_insight(ctx.user_id, insight, MAX_INSIGHTS)
    if status == "no_user":
        raise NotFound(NO_PROFILE)
    if status == "full":
        raise InvalidInput(f"The user already has {MAX_INSIGHTS} saved insights; nothing was saved")
    visible = sensitivity.is_allowed(label, ctx.max_sensitivity)
    if status == "duplicate":
        return SavedInsight(None, label, visible, kind, duplicate=True)
    return SavedInsight(insight["id"], label, visible, kind)


async def get_insights(repos: Repos, ctx: UserContext, query: str | None = None,
                       area: str | None = None, limit: int = MAX_RESULTS, kind: str | None = None) -> Recall:
    """Newest first; with `query`, ranked by how many query words they contain (word starts, as in search)."""
    items = await repos.users.get_insights(ctx.user_id)
    if kind:
        items = [i for i in items if i.kind == kind]
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


ABOUT_ORDER = ("preference", "goal", "interest", "known", "fact", "feedback", "other")
ABOUT_PER_KIND = 8
ABOUT_MAX_CHARS = 2000


LEARNED_KEYS = {"preferences": "preference", "known": "known", "avoid": "avoid",
                "goal_candidates": "goal"}


async def about_user(repos: Repos, ctx: UserContext) -> dict[str, list[str]]:
    """For the client model to adapt its answers, bounded in size: the learned profile first (users.learned,
    written by the learning job, if this client may read it), the topics the user follows, then the newest
    readable insights per kind."""
    out: dict[str, list[str]] = {}
    used = 0
    seen: set[str] = set()

    def add(kind: str, text: str) -> bool:
        nonlocal used
        text = text if len(text) <= 200 else text[:199] + "…"
        if normalized(text) in seen:
            return True
        if used + len(text) > ABOUT_MAX_CHARS:
            return False
        out.setdefault(kind, []).append(text)
        seen.add(normalized(text))
        used += len(text)
        return True

    learned = await repos.users.get_learned(ctx.user_id)
    if learned and sensitivity.is_allowed(learned.get("sensitivity", "internal"), ctx.max_sensitivity):
        if learned.get("summary"):
            add("summary", learned["summary"])
        for key, kind in LEARNED_KEYS.items():
            for text in learned.get(key) or []:
                add(kind, text)
        for i in learned.get("interests") or []:
            add("interest", f"{i['topic']} ({i['weight']}/5)")

    from brain.domain import topics                     # (topics imports posts, which imports this module's deps)
    for t in await topics.followed(repos, ctx):
        add("tracked", f"{t.name} (до {topics.until_text(t)})" + (f": для цілі «{t.goal}»" if t.goal else ""))

    items = await repos.users.get_insights(ctx.user_id)
    items = [i for i in items if sensitivity.is_allowed(i.sensitivity, ctx.max_sensitivity)]
    items.sort(key=lambda i: i.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for kind in ABOUT_ORDER:
        for i in [x for x in items if x.kind == kind][:ABOUT_PER_KIND]:
            if not add(kind, i.text):
                return out
    return out
