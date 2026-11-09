
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from brain.db.models import PostDoc, SourceDoc
from brain.db.repositories import Repos
from brain.domain import sensitivity
from brain.domain.errors import NotFound
from brain.domain.identity import UserContext

logger = logging.getLogger("brain.posts")

MAX_DAYS, MAX_LIMIT, MAX_WORDS, CANDIDATES = 30, 25, 8, 200
W_MATCH, W_RECENCY, W_IMPORTANCE = 0.6, 0.25, 0.15
W_PERSONAL = 0.2          # × personal relevance (relevance.py), clipped to [-1, 2] and halved

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
    why: list[str] = field(default_factory=list)       # why it matters to this user

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


def hits(post: PostDoc, stems: list[str]) -> int:
    """How many of the query stems the post's summary or tags contain."""
    haystack = (post.summary + " | " + " | ".join(post.tags)).lower()
    return sum(1 for s in stems if re.search(stem_pattern(s), haystack))


def min_hits(best: int) -> int:
    """Weak matches are dropped: with 1-2 words matched at best, all of them are needed; with more, all but
    one. «морські дрони безекіпажні катери»: posts that only mention «дрони» don't make the list."""
    return best if best <= 2 else best - 1


def rank(post: PostDoc, stems: list[str], now: datetime, days: int) -> float:
    match = hits(post, stems) / len(stems) if stems else 0.0
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
    found = await _semantic(repos, ctx, query, side, category, days, limit)
    if found:
        return found, stems or [query.strip()]
    if not stems:
        return [], []
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    candidates = await repos.posts.search_candidates(
        [stem_pattern(s) for s in stems], since_iso=since, allowed=ctx.allowed, side=side if side in ("ua", "ru") else None,
        category=category or None, limit=CANDIDATES)
    candidates = sensitivity.filter_allowed(candidates, ctx.max_sensitivity)       # second check
    counts = {id(p): hits(p, stems) for p in candidates}
    if counts:
        need = min_hits(max(counts.values()))
        candidates = [p for p in candidates if counts[id(p)] >= need]
    from brain.domain import relevance                       # (relevance imports this module)
    prefs = await relevance.load(repos, ctx)
    personal = {id(p): prefs.score(side=p.source_side, category=p.category, text=p.summary, tags=p.tags)
                for p in candidates}

    def score(p: PostDoc) -> float:
        rel = max(-1.0, min(2.0, personal[id(p)].score)) / 2
        return round(rank(p, stems, now, days) + W_PERSONAL * rel, 4)

    ranked = sorted(candidates, key=score, reverse=True)[:limit]
    channels = await repos.sources.get_many([p.channel_id for p in ranked if p.channel_id])
    return [PostView(p, channels.get(p.channel_id or ""), score(p), personal[id(p)].why) for p in ranked], stems


async def _semantic(repos: Repos, ctx: UserContext, query: str, side: str, category: str | None,
                    days: int, limit: int) -> list[PostView]:
    """Hybrid semantic search (brain.domain.semantic), then the same checks as the keyword path: the posts are
    loaded through repos with the caller's sensitivity labels, and personal relevance is blended in.
    [] when it is off, unavailable or finds nothing: the caller falls back to the keyword search."""
    from brain.domain import semantic
    if not semantic.enabled() or len((query or "").strip()) < 2:
        return []
    try:
        found = await semantic.search(query, side=side, category=category or None, days=days, limit=limit)
    except semantic.SemanticUnavailable as e:
        logger.warning(f"semantic search unavailable, keyword fallback: {e}")
        return []
    except Exception:
        logger.exception("semantic search failed, keyword fallback")
        return []
    if not found:
        logger.info(f"semantic search «{query[:60]}»: nothing found (days={days}, side={side}) → keyword fallback")
        return []
    posts = await repos.posts.get_many([h.post_id for h in found], ctx.allowed)
    pairs = [(posts[h.post_id], h) for h in found if h.post_id in posts]
    pairs = [(p, h) for p, h in pairs if sensitivity.is_allowed(p.sensitivity, ctx.max_sensitivity)]
    if not pairs:
        logger.info(f"semantic search «{query[:60]}»: {len(found)} found, but none of them in {repos.posts.col.database.name}"
                    f".posts for this user → keyword fallback")
        return []
    from brain.domain import relevance                       # (relevance imports this module)
    prefs = await relevance.load(repos, ctx)
    personal = {p.id: prefs.score(side=p.source_side, category=p.category, text=p.summary, tags=p.tags)
                for p, _ in pairs}

    def score(p: PostDoc, h) -> float:
        rel = max(-1.0, min(2.0, personal[p.id].score)) / 2
        return round(h.score + W_PERSONAL * rel, 4)

    ranked = sorted(pairs, key=lambda ph: score(*ph), reverse=True)[:limit]
    logger.info(f"semantic search «{query[:60]}»: {len(found)} found, {len(ranked)} returned "
                f"(top relevance {found[0].score:.2f})")
    channels = await repos.sources.get_many([p.channel_id for p, _ in ranked if p.channel_id])
    return [PostView(p, channels.get(p.channel_id or ""), score(p, h), personal[p.id].why) for p, h in ranked]


async def get_post(repos: Repos, ctx: UserContext, post_id: str) -> PostView:
    """A missing post and a post above the caller's label look the same: not found."""
    post = await repos.posts.get(post_id, ctx.allowed)
    if post is None or not sensitivity.is_allowed(post.sensitivity, ctx.max_sensitivity):
        raise NotFound("Post not found")
    channels = await repos.sources.get_many([post.channel_id] if post.channel_id else [])
    from brain.domain import relevance
    why = (await relevance.load(repos, ctx)).score(side=post.source_side, category=post.category,
                                                    text=post.summary, tags=post.tags).why
    return PostView(post, channels.get(post.channel_id or ""), why=why)


FULL_TEXT_MAX = 4000
FULL_TEXT_REMOVED = "Повний текст уже видалено за терміном зберігання."


@dataclass(frozen=True)
class FullText:
    text: str | None = None
    note: str | None = None


async def full_text(repos: Repos, view: PostView) -> FullText | None:
    """A Russian post's own text: its Telegram link doesn't open in Ukraine, so the text is the only way to read it.
    None for any other post (its link works). `view` comes from get_post, so the sensitivity check is done."""
    if not view.is_russian_source_claim:
        return None
    doc = await repos.posts.own_text(view.post.id)
    text = (doc.get("clean_text") or doc.get("raw_text") or "").strip()
    if not text:
        return FullText(note=FULL_TEXT_REMOVED if doc.get("text_removed") else None)
    return FullText(text=cut(text, FULL_TEXT_MAX))


def cut(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a word, with «…» when cut."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    if not text[limit - 1].isspace() and " " in head:
        head = head.rsplit(None, 1)[0]
    return head.rstrip() + "…"
