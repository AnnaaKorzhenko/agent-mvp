"""Deep dives and tracked topics, in users.topics (docs/DEEP_DIVE_PLAN.md).

A topic is one specific thing the user follows now («Спектр-К»): narrower than a goal, set by the user (not
inferred like a learned interest), and it expires. It lives through three steps:
- deep_dive(): the client asks for what the brain already has on an item (the item, its source posts, related
  posts, an existing topic with its earlier findings); the client then searches the web itself;
- save_findings(): what the client found on the web is kept on the topic (status `explored` until the user
  chooses to follow it), so the next dive and the period analysis build on it instead of starting over;
- track_topic(): the user confirms; the topic becomes `active` with names / broader terms / exclusions that later
  phases match against digests and posts. It expires after `days` (default TTL_DAYS) unless renewed.
Following feeds the pipeline: every followed, unexpired topic's names go into one `sources` record
(platform "search_keywords", name "User interests", queries [{q, region}]), the union over all users and nothing
about who follows what. Region by the term's script: Latin -> world; Cyrillic -> ua and ru, or only ua / ru when
the spelling has letters only that language uses (і ї є ґ / ы э ъ ё). The pipeline searches the web for them, keeps
what it finds in posts and builds a digest with trigger "topics"; get_digest shows each user the items that match
their own topics, first (brain/domain/digests.py).
An active topic past expires_at reads as `expired`; nothing rewrites it. From RENEW_NOTICE_DAYS before expiry
until RENEW_GRACE_DAYS after, the digest asks the client to offer renewal, at most once a day per topic
(to_renew(); `renew_offered_on` records the Kyiv date it was offered). Topics are keyed by their normalized
name, so the same name never makes two topics. Web findings are public information; a topic is `internal`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from brain.db.models import PostDoc, TopicDoc
from brain.db.repositories import Repos, to_object_id
from brain.domain import digests, posts, sensitivity
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext
from brain.domain.tag_list import TAG_LIST

TTL_DAYS = 30
MAX_DAYS = 180
MAX_TOPICS = 100            # all statuses; at the cap a new topic is refused, nothing is dropped
MAX_NAME = 80
MAX_TERM = 60
MAX_TERMS = 20
MAX_FINDINGS = 30           # kept per topic, newest
MAX_PER_CALL = 15
MAX_ALIASES_SEARCHED = 5
RELATED_DAYS = 30
RELATED_LIMIT = 10
STATUSES = ("confirmed", "claim", "unclear")

logger = logging.getLogger("brain.topics")
NO_PROFILE = "No profile for this user yet. Start the Telegram bot once to create it."
_QUOTED = re.compile(r"[«\"“]([^»\"”]{2,60})[»\"”]")


# «більше про це», «надо больше информации», «детальніше», «want more»: a request to dig deeper.
_WANTS_MORE = re.compile(r"(більше|детальн|подробн|докладн|больше|деталь|розкопа|копни|глибш|глубж|"
                         r"\bmore\b|\bdeeper\b|\bdetails?\b)", re.I)


def wants_more(text: str | None, reasons: list[str] | None = None) -> bool:
    return "want_more" in (reasons or []) or bool(_WANTS_MORE.search(text or ""))


def norm(name: str) -> str:
    return " ".join((name or "").lower().split())


def _clean(text: str | None, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def _terms(values: list[str] | None) -> list[str]:
    out = [_clean(v, MAX_TERM) for v in values or []]
    return list(dict.fromkeys(v for v in out if v))[:MAX_TERMS]


def utc(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


_LATIN = re.compile(r"[a-z]", re.I)
_CYRILLIC = re.compile(r"[а-яіїєґёыэъ]", re.I)
_ONLY_UA = re.compile(r"[іїєґ]|[а-яіїєґ]['’ʼ][а-яіїєґ]", re.I)     # the apostrophe inside a word: «зв'язок»
_ONLY_RU = re.compile(r"[ёыэъ]", re.I)
REGIONS = ("ua", "ru", "world")


def regions(term: str) -> list[str]:
    """Where to search the web for a term, by its spelling."""
    if _CYRILLIC.search(term):
        if _ONLY_UA.search(term):
            return ["ua"]
        return ["ru"] if _ONLY_RU.search(term) else ["ua", "ru"]
    return ["world"] if _LATIN.search(term) else []


def missing_regions(terms: list[str]) -> list[str]:
    """Regions none of the terms reaches: the client should add those spellings (e.g. a Latin one for world)."""
    covered = {r for t in terms for r in regions(t)}
    return [r for r in REGIONS if r not in covered]


MAX_QUERIES = 12
_WORDS = re.compile(r"[\w'’ʼ]+", re.U)


def distinctive(term: str) -> bool:
    """Whether a term can be searched on the web by itself: two words or more, or a name with a digit or a
    hyphenated part («Рассвет-3», «Спектр-К», «Magura V5»). A single plain word («Рассвет», «Rassvet», «Apex.AI»)
    is usually also an everyday word or another company: alone it brings noise."""
    several = len(_WORDS.findall(term)) >= 2 and not re.fullmatch(r"\w+[.]\w+", term.strip())   # not «Apex.AI»
    return several or any(ch.isdigit() for ch in term) or bool(re.search(r"\w-\w", term))


_ANY_QUOTE = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "\\": None})
NAME_WORDS = 2          # a name this short is searched as an exact phrase: "Рассвет-3", "Бюро 1440"


def exact(q: str) -> str:
    """One quoting style for the web search: «Сектор», “Сектор” and \\"Сектор\\" (escaped by hand in a DB editor)
    become "Сектор"; an unpaired or empty quote is dropped."""
    q = " ".join(q.translate(_ANY_QUOTE).split())
    if q.count('"') % 2:
        q = q.replace('"', "")
    return " ".join(re.sub(r'"\s*"', " ", q).split())


def quote_names(q: str, names: list[str]) -> str:
    """Puts the thing's name in quotes so the search finds that name and not the everyday word: «Рассвет
    спутниковая связь» → "Рассвет" спутниковая связь. A query that already has quotes is left as written; a
    query that is itself a short name is quoted whole ("Рассвет-3")."""
    q = exact(q)
    if '"' in q:
        return q
    if len(_WORDS.findall(q)) <= NAME_WORDS and q in names:
        return f'"{q}"'
    # the everyday-word name first (it is the one that brings noise), then the longest
    for name in sorted({exact(n).replace('"', "") for n in names}, key=lambda n: (distinctive(n), -len(n))):
        m = name and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", q, re.I)
        if m and len(_WORDS.findall(name)) <= NAME_WORDS and m.group(0) != q:
            return f'{q[:m.start()]}"{m.group(0)}"{q[m.end():]}'
    return q


def web_queries(topic: TopicDoc) -> list[str]:
    """What the pipeline searches the web for, for this topic: its own web queries (phrases written to find this
    thing and nothing else); for a topic without them, those of its names that are distinctive by themselves.
    The name in each is quoted (quote_names)."""
    names = [topic.name, *topic.specific]
    return [quote_names(q, names) for q in topic.queries or [t for t in topic.specific if distinctive(t)]]


def not_searched(topic: TopicDoc) -> list[str]:
    """Names left out of the web search as too generic (only when the topic has no web queries of its own)."""
    return [] if topic.queries else [t for t in topic.specific if not distinctive(t)]


# Spelling variants of one name are one search: Latin transliterations (Spektr / Spectr, Kh / H) and Cyrillic
# letters that differ between Ukrainian and Russian or get mistyped (Рассвет / Рассвєт).
_LATIN_VARIANTS = (("ph", "f"), ("kh", "h"), ("ck", "k"), ("c", "k"), ("q", "k"), ("w", "v"), ("y", "i"),
                   ("ii", "i"), ("ee", "i"))
_CYRILLIC_VARIANTS = str.maketrans({"є": "е", "э": "е", "ё": "е", "і": "и", "ї": "и", "ы": "и", "ґ": "г",
                                    "ь": None, "ъ": None, "'": None})


def _variant_key(word: str) -> str:
    word = word.lower().translate(_CYRILLIC_VARIANTS)
    for a, b in _LATIN_VARIANTS:
        word = word.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", word)          # doubled letters: «Рассвет» ~ «Расвет»


def query_key(q: str) -> str:
    """Two queries are the same search when they have the same words, in any order, case, punctuation and
    spelling variant: «Apex AI НРК» = «НРК Apex.AI», «Spektr-K» = «Spectr-K», «Рассвет» = «Рассвєт»."""
    return " ".join(sorted(_variant_key(w) for w in _WORDS.findall(_flat(q))))


def keyword_queries(active: list[TopicDoc]) -> list[dict]:
    """The pipeline's queries for these topics: {q, region} for each distinct web query once per region (the first
    spelling met wins, a quoted one over an unquoted one), sorted (stable for comparing). Names, broader phrases
    and look-alike words stay in the brain, which uses them for matching."""
    out: dict[tuple[str, str], dict] = {}
    for t in active:
        for q in web_queries(t):
            for region in regions(q):
                key = (query_key(q), region)
                if key not in out or ('"' in q and '"' not in out[key]["q"]):
                    out[key] = {"q": q, "region": region}
    return sorted(out.values(), key=lambda x: (x["q"].lower(), x["region"]))


async def sync_queries(repos: Repos, now: datetime | None = None) -> bool:
    """Rewrites the pipeline's keyword record from all followed, unexpired topics. True if it changed."""
    now = now or datetime.now(timezone.utc)
    queries = keyword_queries(await repos.users.all_active_topics(now))
    if await repos.sources.get_keyword_queries() == queries:
        return False
    await repos.sources.set_keyword_queries(queries)
    return True


PIPELINE_LABELS = ["public", "internal"]       # profiles whose interests may become pipeline searches


async def sync_profile_queries(repos: Repos, now: datetime | None = None) -> bool:
    """Rewrites the pipeline's «Profile interests» record from every user's lasting interests
    (brain/domain/interests.profile_queries). True if it changed."""
    from brain.domain import interests
    record = repos.sources.PROFILE_KEYWORDS
    queries = interests.profile_queries(await repos.users.all_learned_interests(PIPELINE_LABELS), now)
    if await repos.sources.get_keyword_queries(record) == queries:
        return False
    await repos.sources.set_keyword_queries(queries, record)
    return True


RENEW_NOTICE_DAYS = 3
RENEW_GRACE_DAYS = 2


@dataclass
class Renewal:
    topic: TopicDoc
    days_left: int            # whole days until expiry; negative once expired


async def to_renew(repos: Repos, ctx: UserContext, now: datetime | None = None) -> list[Renewal]:
    """Followed topics expiring soon (or just expired) not yet offered for renewal today; marks them offered."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(digests.KYIV).date().isoformat()
    out = []
    for t in await repos.users.get_topics(ctx.user_id):
        if t.status != "active" or not t.expires_at or t.renew_offered_on == today:
            continue
        left = utc(t.expires_at) - now
        if -timedelta(days=RENEW_GRACE_DAYS) <= left <= timedelta(days=RENEW_NOTICE_DAYS):
            await repos.users.update_topic(ctx.user_id, t.norm, {"renew_offered_on": today})
            out.append(Renewal(t, left.days))
    return out


SYNC_EVERY = timedelta(minutes=30)
_last_sync: datetime | None = None


async def sync_if_due(repos: Repos) -> None:
    """sync_queries at most every SYNC_EVERY per process, from digest reads: expired topics drop out of the
    pipeline's record without a job of their own. Never raises."""
    global _last_sync
    now = datetime.now(timezone.utc)
    if _last_sync is not None and now - _last_sync < SYNC_EVERY:
        return
    _last_sync = now
    try:
        await sync_queries(repos, now)
    except Exception as e:                 # noqa: BLE001 - the digest must never fail on this
        logger.error(f"topic keyword sync failed: {type(e).__name__}: {e}")


_DASHES = str.maketrans({"‐": "-", "‑": "-", "–": "-", "—": "-", "’": "'", "ʼ": "'"})


def _flat(text: str) -> str:
    return " ".join((text or "").lower().translate(_DASHES).replace("ё", "е").split())


def excluded(topic: TopicDoc, text: str) -> bool:
    """A look-alike word of the topic is in the text: whatever else matches, it isn't this topic."""
    flat = _flat(text)
    return any(_flat(x) in flat for x in topic.exclude if x)


def matched_term(topic: TopicDoc, text: str) -> str | None:
    """The topic's name or alias found in the text (whole term, case-, dash- and ё-insensitive), unless a
    look-alike word of the topic is there too."""
    flat = _flat(text)
    if excluded(topic, text):
        return None
    for term in topic.specific or [topic.name]:
        t = _flat(term)
        if t and re.search(rf"(?<![\w]){re.escape(t)}(?![\w])", flat):
            return term
    return None


def until_text(topic: TopicDoc) -> str:
    """The expiry date as the user reads it (Kyiv), e.g. 28.11."""
    return utc(topic.expires_at).astimezone(digests.KYIV).strftime("%d.%m") if topic.expires_at else ""


def status_of(topic: TopicDoc, now: datetime | None = None) -> str:
    """explored | active | expired | stopped."""
    now = now or datetime.now(timezone.utc)
    if topic.status == "active" and topic.expires_at and utc(topic.expires_at) <= now:
        return "expired"
    return topic.status


@dataclass
class Subject:
    """What the dive is about."""
    text: str
    source_urls: list[str] = field(default_factory=list)
    is_russian_source_claim: bool = False
    digest_id: str | None = None
    item_n: int | None = None
    post_id: str | None = None


@dataclass
class DeepDiveSeed:
    subject: Subject
    source_posts: list[posts.PostView]
    related_posts: list[posts.PostView]
    searched_for: list[str]
    topic: TopicDoc | None
    goals: list[str]


async def _subject(repos: Repos, ctx: UserContext, digest_id, item_n, post_id, text) -> Subject:
    if digest_id is not None or item_n is not None:
        found = await digests.find_item(repos, ctx, digest_id, item_n)
        if found is None:
            raise NotFound("Digest item not found: pass digest_id and n of an item from get_digest")
        digest, _, item = found
        return Subject(item.text, list(item.source_urls), digest.side == "ru", digest.id, item_n,
                       item.source_post_ids[0] if item.source_post_ids else None)
    if post_id is not None:
        view = await posts.get_post(repos, ctx, post_id)
        return Subject(view.post.summary, [view.post.post_url], view.is_russian_source_claim, post_id=view.post.id)
    text = _clean(text, 600)
    if not text:
        raise InvalidInput("Say what to dig into: digest_id + item_n, a post_id, or the subject in words")
    return Subject(text)


def _find_topic(topics: list[TopicDoc], subject: Subject, name: str | None) -> TopicDoc | None:
    """An existing topic on the same thing: same name, same origin item, or one whose name or specific terms
    appear in the subject's text."""
    if name and (t := next((t for t in topics if t.norm == norm(name)), None)):
        return t
    for t in topics:
        o = t.origin
        if o and ((subject.digest_id and o.digest_id == subject.digest_id and o.item_n == subject.item_n)
                  or (subject.post_id and o.post_id == subject.post_id)):
            return t
    text = subject.text.lower()
    return next((t for t in topics if any(term.lower() in text for term in [t.name, *t.specific] if len(term) > 3)),
                None)


async def deep_dive(repos: Repos, ctx: UserContext, *, digest_id: str | None = None, item_n: int | None = None,
                    post_id: str | None = None, text: str | None = None, query: str | None = None,
                    name: str | None = None) -> DeepDiveSeed:
    """Everything the brain has on the subject, for the client to build on before it searches the web."""
    subject = await _subject(repos, ctx, digest_id, item_n, post_id, text)
    profile = await repos.users.get_profile(ctx.user_id)
    if profile is None:
        raise NotFound(NO_PROFILE)
    topic = _find_topic(await repos.users.get_topics(ctx.user_id, findings=True), subject, name)

    sources: dict[str, PostDoc] = {}
    if subject.digest_id:
        _, _, item = await digests.find_item(repos, ctx, subject.digest_id, subject.item_n)
        sources = await repos.posts.get_many([o for o in (to_object_id(p) for p in item.source_post_ids) if o],
                                             ctx.allowed)
    elif subject.post_id:
        sources = await repos.posts.get_many([to_object_id(subject.post_id)], ctx.allowed)
    source_views = [posts.PostView(p, None) for p in sensitivity.filter_allowed(list(sources.values()),
                                                                               ctx.max_sensitivity)]

    # Related posts: one search per name (the names are alternatives, not words that must all appear).
    names = [query] if query else (topic.specific[:MAX_ALIASES_SEARCHED] if topic and topic.specific
                                   else _QUOTED.findall(subject.text)[:MAX_ALIASES_SEARCHED])
    related: dict[str, posts.PostView] = {}
    for q in names:
        found, _ = await posts.search(repos, ctx, q, days=RELATED_DAYS, limit=RELATED_LIMIT)
        for v in found:
            if v.post.id not in sources:
                related.setdefault(v.post.id, v)
    ranked = sorted(related.values(), key=lambda v: v.score, reverse=True)[:RELATED_LIMIT]
    return DeepDiveSeed(subject, source_views, ranked, names, topic, [g.text for g in profile.goals if g.active])


@dataclass
class TopicResult:
    topic: TopicDoc
    created: bool
    added_findings: int = 0
    duplicate_findings: int = 0
    unknown_tags: list[str] = field(default_factory=list)


def _origin(subject: Subject | None) -> dict | None:
    if subject is None:
        return None
    return {"digest_id": to_object_id(subject.digest_id) if subject.digest_id else None, "item_n": subject.item_n,
            "post_id": to_object_id(subject.post_id) if subject.post_id else None, "text": _clean(subject.text, 600)}


async def _get(repos: Repos, ctx: UserContext, key: str) -> TopicDoc | None:
    return next((t for t in await repos.users.get_topics(ctx.user_id, findings=True) if t.norm == key), None)


async def _ensure(repos: Repos, ctx: UserContext, name: str, origin: dict | None) -> tuple[TopicDoc, bool]:
    """The topic `name`, created as `explored` if it doesn't exist yet."""
    name = _clean(name, MAX_NAME)
    if not name:
        raise InvalidInput("The topic needs a short name, e.g. «Спектр-К»")
    key = norm(name)
    if existing := await _get(repos, ctx, key):
        return existing, False
    now = datetime.now(timezone.utc)
    doc = {"id": ObjectId(), "name": name, "norm": key, "status": "explored", "created_at": now, "updated_at": now,
           "expires_at": None, "channel": ctx.channel, "origin": origin, "goal": None, "specific": [name],
           "broader": [], "exclude": [], "tags": [], "findings": [], "hits": [], "last_hit_at": None,
           "sensitivity": "internal"}
    r = await repos.users.push_topic(ctx.user_id, doc, MAX_TOPICS)
    if r == "no_user":
        raise NotFound(NO_PROFILE)
    if r == "full":
        raise InvalidInput(f"The user already has {MAX_TOPICS} topics; stop ones they no longer need first")
    return await _get(repos, ctx, key), r == "ok"


async def _origin_for(repos, ctx, digest_id, item_n, post_id) -> dict | None:
    if digest_id is None and item_n is None and post_id is None:
        return None
    return _origin(await _subject(repos, ctx, digest_id, item_n, post_id, None))


async def save_findings(repos: Repos, ctx: UserContext, name: str, findings: list[dict], *,
                        digest_id: str | None = None, item_n: int | None = None,
                        post_id: str | None = None) -> TopicResult:
    """Keeps what the client found on the web on the topic `name` (created as `explored` if new). Findings with a
    URL already saved on the topic are skipped; the newest MAX_FINDINGS are kept."""
    if not findings:
        raise InvalidInput("No findings to save")
    if len(findings) > MAX_PER_CALL:
        raise InvalidInput(f"At most {MAX_PER_CALL} findings per call")
    topic, created = await _ensure(repos, ctx, name, await _origin_for(repos, ctx, digest_id, item_n, post_id))
    seen = {f.url for f in topic.findings}
    now = datetime.now(timezone.utc)
    new = []
    for f in findings:
        url = _clean(f.get("url"), 500)
        if not url.startswith(("http://", "https://")):
            raise InvalidInput(f"Every finding needs the http(s) url it came from; got {url!r}")
        if url in seen:
            continue
        seen.add(url)
        status = f.get("status") or "unclear"
        if status not in STATUSES:
            raise InvalidInput(f"status must be one of {', '.join(STATUSES)}")
        new.append({"title": _clean(f.get("title"), 200) or url, "url": url, "source": _clean(f.get("source"), 80),
                    "date": _clean(f.get("date"), 20) or None, "summary": _clean(f.get("summary"), 400),
                    "status": status, "at": now})
    await repos.users.update_topic(ctx.user_id, topic.norm, {"updated_at": now}, new, MAX_FINDINGS)
    return TopicResult(await _get(repos, ctx, topic.norm), created, len(new), len(findings) - len(new))


async def track_topic(repos: Repos, ctx: UserContext, name: str, *, specific: list[str] | None = None,
                      broader: list[str] | None = None, exclude: list[str] | None = None,
                      tags: list[str] | None = None, goal: str | None = None, days: int = TTL_DAYS,
                      digest_id: str | None = None, item_n: int | None = None,
                      post_id: str | None = None, queries: list[str] | None = None) -> TopicResult:
    """Starts (or renews) following a topic: status active for `days`. Lists given replace the stored ones; the
    name is always among the specific terms. Tags outside the fixed list are dropped and reported. `queries`: the
    web searches for the pipeline, each a phrase that finds this thing and nothing else (a lone generic word is
    refused)."""
    days = max(1, min(int(days), MAX_DAYS))
    topic, created = await _ensure(repos, ctx, name, await _origin_for(repos, ctx, digest_id, item_n, post_id))
    now = datetime.now(timezone.utc)
    fields: dict = {"status": "active", "updated_at": now, "expires_at": now + timedelta(days=days)}
    if specific is not None:
        fields["specific"] = _terms([topic.name, *specific])
    if broader is not None:
        fields["broader"] = _terms(broader)
    if exclude is not None:
        fields["exclude"] = _terms(exclude)
    unknown: list[str] = []
    if tags is not None:
        allowed = {t.lower(): t for t in TAG_LIST}
        fields["tags"] = list(dict.fromkeys(allowed[t.lower().strip()] for t in tags if t.lower().strip() in allowed))
        unknown = [t for t in tags if t.lower().strip() not in allowed]
    if goal is not None:
        fields["goal"] = _clean(goal, 300) or None
    if queries is not None:
        cleaned = list({query_key(c): c for c in reversed([exact(_clean(q, 120)) for q in queries])
                        if c}.values())[::-1]
        vague = [q for q in cleaned if not distinctive(q)]
        if vague:
            raise InvalidInput(f"Web queries need context, not a lone word (it would bring noise): {vague}. Write "
                               "the name with what it is, e.g. «Рассвет Бюро 1440 спутники»")
        if len(cleaned) > MAX_QUERIES:
            raise InvalidInput(f"At most {MAX_QUERIES} web queries")
        fields["queries"] = cleaned
    await repos.users.update_topic(ctx.user_id, topic.norm, fields)
    await sync_queries(repos, now)
    return TopicResult(await _get(repos, ctx, topic.norm), created, unknown_tags=unknown)


async def stop_topic(repos: Repos, ctx: UserContext, name: str) -> TopicResult:
    """Stops following a topic; it stays (with its findings) for the period analysis."""
    topic = await _get(repos, ctx, norm(name))
    if topic is None:
        names = [t.name for t in await repos.users.get_topics(ctx.user_id)]
        raise NotFound("No such topic. The user's topics: " + ("; ".join(names) or "none"))
    await repos.users.update_topic(ctx.user_id, topic.norm, {"status": "stopped",
                                                             "updated_at": datetime.now(timezone.utc)})
    await sync_queries(repos)
    return TopicResult(await _get(repos, ctx, topic.norm), False)


async def list_topics(repos: Repos, ctx: UserContext) -> list[TopicDoc]:
    return await repos.users.get_topics(ctx.user_id)


async def followed(repos: Repos, ctx: UserContext, now: datetime | None = None) -> list[TopicDoc]:
    """The user's active, unexpired topics."""
    now = now or datetime.now(timezone.utc)
    return [t for t in await repos.users.get_topics(ctx.user_id) if status_of(t, now) == "active"]
