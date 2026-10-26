"""Post search and lookup. Returns only summaries and links, never raw text.

Search is plain pattern matching on summary and tags (no text index on the pipeline's collection),
ranked here by word-match share, recency and importance.
TODO(next): semantic search through brain.integrations.embeddings + a VectorIndex (phase 5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from brain.db.models import PostDoc, SourceDoc
from brain.db.repositories import Repos
from brain.domain import sensitivity
from brain.domain.errors import NotFound
from brain.domain.identity import UserContext

MAX_DAYS, MAX_LIMIT, MAX_WORDS, CANDIDATES = 30, 25, 8, 200
W_MATCH, W_RECENCY, W_IMPORTANCE = 0.6, 0.25, 0.15

_STOP = set("""
що як де коли чому хто який яка яке які яких якому яким мені мене мій моя мої моїх про для від цей ця це ці
цього цьому там тут було буде є був була були зараз сьогодні вчора учора завтра тиждень тижня тижні тижнів
день дня днів місяць місяця місяці кварталу року новини новин новина розкажи покажи скажи дай можеш можна
треба щось також або але чи так ні останні останніх останній останнього минулого минулий наступного
наступний змінилося змінилася змінилися означає саме скільки нові нових новий нова варто вважати
обох боків джерела джерел джерелах згадуються згадували коротко коротке пункти пунктів та і й в у на з із
до по під над при
что как где когда почему кто какой какая какие мне про для это эти там тут было будет есть сейчас сегодня
вчера неделю неделя новости новость расскажи покажи скажи можно надо также или но ли и в на с из до по
the and for with what about news this that last week days
""".split())
_APOSTROPHES = "'’ʼ`"
_WORD = re.compile(rf"[\w{_APOSTROPHES}]+", re.U)
_BOUNDARY = rf"(?:^|[^\w{_APOSTROPHES}])"   # a stem must start a word; an apostrophe is part of a word
STEM_MAX = 6


@dataclass(frozen=True)
class PostView:
    post: PostDoc
    channel: SourceDoc | None
    score: float = 0.0

    @property
    def is_russian_source_claim(self) -> bool:
        return self.post.source_side == "ru"


def _stem(word: str) -> str:
    """Crude prefix stem: drop the ending, keep 4 to STEM_MAX letters (покровському -> покров, танків -> танк)."""
    letters = len(word.replace("'", ""))
    keep = max(4, min(letters - 2, STEM_MAX)) if letters > 4 else letters
    out, n = "", 0
    for ch in word:
        if n == keep:
            break
        out += ch
        n += ch != "'"
    return out.rstrip("'")


def query_words(query: str) -> list[str]:
    """Search stems: words of 3+ letters minus stop words, crudely stemmed. Apostrophes are kept as "'"."""
    text = (query or "").lower()
    for a in _APOSTROPHES:
        text = text.replace(a, "'")
    words = [w.strip("'") for w in _WORD.findall(text)]
    words = [w for w in words if len(w.replace("'", "")) >= 3 and w not in _STOP and not w.isdigit()]
    stems = [_stem(w) for w in dict.fromkeys(words)]
    return list(dict.fromkeys(stems))[:MAX_WORDS]


def stem_pattern(stem: str) -> str:
    """Regex for a stem at the start of a word; any apostrophe form matches "'"."""
    body = "".join(f"[{_APOSTROPHES}]?" if ch == "'" else re.escape(ch) for ch in stem)
    return _BOUNDARY + body


def _published(post: PostDoc) -> datetime | None:
    try:
        dt = datetime.fromisoformat(post.published_at)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def rank(post: PostDoc, stems: list[str], now: datetime, days: int) -> float:
    haystack = (post.summary + " | " + " | ".join(post.tags)).lower()
    match = (sum(1 for s in stems if re.search(stem_pattern(s), haystack)) / len(stems)) if stems else 0.0
    published = _published(post)
    age_days = (now - published).total_seconds() / 86400 if published else days
    recency = max(0.0, 1.0 - age_days / days)
    importance = max(0, min(post.importance or 0, 5)) / 5
    return round(W_MATCH * match + W_RECENCY * recency + W_IMPORTANCE * importance, 4)


async def search(repos: Repos, ctx: UserContext, query: str, side: str = "all", category: str | None = None,
                 days: int = 7, limit: int = 10) -> tuple[list[PostView], list[str]]:
    """Returns (ranked posts, the search stems used)."""
    days = max(1, min(int(days), MAX_DAYS))
    limit = max(1, min(int(limit), MAX_LIMIT))
    stems = query_words(query)
    if not stems:
        return [], []
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    candidates = await repos.posts.search_candidates(
        [stem_pattern(s) for s in stems], since_iso=since, allowed=ctx.allowed, side=side if side in ("ua", "ru") else None,
        category=category or None, limit=CANDIDATES)
    candidates = sensitivity.filter_allowed(candidates, ctx.max_sensitivity)       # second check
    ranked = sorted(candidates, key=lambda p: rank(p, stems, now, days), reverse=True)[:limit]
    channels = await repos.sources.get_many([p.channel_id for p in ranked if p.channel_id])
    return [PostView(p, channels.get(p.channel_id or ""), rank(p, stems, now, days)) for p in ranked], stems


async def get_post(repos: Repos, ctx: UserContext, post_id: str) -> PostView:
    """A missing post and a post above the caller's label look the same: not found."""
    post = await repos.posts.get(post_id, ctx.allowed)
    if post is None or not sensitivity.is_allowed(post.sensitivity, ctx.max_sensitivity):
        raise NotFound("Post not found")
    channels = await repos.sources.get_many([post.channel_id] if post.channel_id else [])
    return PostView(post, channels.get(post.channel_id or ""))
