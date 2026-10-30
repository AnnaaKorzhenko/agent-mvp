"""Period analysis: a week or a month of news, analysed for one user (docs/DEEP_DIVE_PLAN.md, phase E).

Not a reprint: the client model writes the analysis (what changed, trends with numbers, claims versus
confirmations, why it matters for the user's goals, gaps, what to watch) from a pack the brain prepares here, and
saves it back (save_report). Written by the client, so there is no extra model call or tool timeout; the
pack and the stored report are the same for any client (the agent core later).

The pack, from data the pipeline already keeps (digests stay a year):
- every daily digest of the period, one per side and Kyiv day (scheduled first); the tracked-topics digest's
  items only where they name one of the user's topics;
- counts done here, exactly: items per day, side and category; per goal and per topic, against the previous
  period of the same length; the top tags of the source posts still in the base;
- a selection of the items that matter to this user (personal relevance, goals and topics first, a cap per
  goal / topic / category so one story doesn't crowd out the rest, repeated texts once);
- the web findings saved on the user's topics during the period.
A report is stored per user, period and focus in `conversations` (ReportsRepo) and returned as is until the
user asks for a fresh one.
"""
from __future__ import annotations

import calendar
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from bson import ObjectId

from brain.db.models import DigestDoc, DigestItemDoc
from brain.db.repositories import Repos
from brain.domain import digests, relevance, topics
from brain.domain.errors import InvalidInput
from brain.domain.identity import UserContext

MAX_DAYS = 31
MAX_ITEMS = 120             # items in the pack (~15k tokens)
PER_GROUP = 25              # at most per goal / topic / category
MAX_TEXT = 320
TOP_CATEGORIES = 15
TOP_TAGS = 15
MAX_REPORT = 20000
MAX_HIGHLIGHTS = 7

WRITE_STEPS = (
    "Write the analysis now, from this pack only (cite source_urls; the counts are exact, use them). Not a "
    "list of news: 1) the period in 3-5 sentences: what changed for the user's goals and topics; 2) per goal "
    "and tracked topic: the developments in order, what is new against the previous period (counts: "
    "named_items name the goal's systems / programs, related_items also its broader area; compare with the "
    "previous_ counts), first appearances; 3) trends with numbers (categories, days, tags); 4) claims "
    "versus confirmations: Russian-source items are claims, say where Ukrainian or Western sources confirm or "
    "contradict them; 5) assessment: why it matters for the user's goals, what is still unknown, what to watch "
    "next. Follow the user's preferences in about_user. Then call save_report with the same start_date, end_date "
    "and focus, 3-7 highlights and the full text.")


@dataclass
class Period:
    start: date
    end: date               # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def key_part(self) -> str:
        return f"{self.start.isoformat()}:{self.end.isoformat()}"

    def previous(self) -> "Period":
        return Period(self.start - timedelta(days=self.days), self.start - timedelta(days=1))

    def bounds(self) -> tuple[datetime, datetime]:
        return digests.kyiv_day_bounds(self.start)[0], digests.kyiv_day_bounds(self.end)[1]


def period_for(kind: str, start: str | None = None, end: str | None = None, today: date | None = None) -> Period:
    """week: 7 days, month: 30 days, ending `end` (default today, Kyiv); explicit start and end win."""
    today = today or digests.kyiv_today()
    e = digests.parse_date(end) or today
    s = digests.parse_date(start)
    if s is None:
        if kind not in ("week", "month"):
            raise InvalidInput("period must be 'week' or 'month' (or pass start_date and end_date)")
        s = e - timedelta(days=6 if kind == "week" else 29)
    if s > e:
        raise InvalidInput("start_date is after end_date")
    if (e - s).days + 1 > MAX_DAYS:
        raise InvalidInput(f"At most {MAX_DAYS} days")
    return Period(s, e)


def due(today: date | None = None) -> tuple[str, Period] | None:
    """The scheduled analysis due on this day: on Mondays the week before (Mon-Sun), on the 1st the month before."""
    today = today or digests.kyiv_today()
    if today.day == 1:
        last = today - timedelta(days=1)
        return "month", Period(last.replace(day=1), last)
    if today.weekday() == 0:
        return "week", Period(today - timedelta(days=7), today - timedelta(days=1))
    return None


def report_key(period: Period, focus: str | None) -> str:
    return f"report:{period.key_part}:{topics.norm(focus) if focus else 'all'}"


@dataclass
class PackItem:
    id: str                       # digest_id/n
    date: str
    side: str                     # ua | ru | topics
    claim: bool
    category: str
    text: str
    source_urls: list[str]
    why: list[str]
    goals: list[str]
    topics: list[str]
    score: float


@dataclass
class Pack:
    period: Period
    previous: Period
    goals: list[str]
    tracked: list[str]
    summary: str | None
    counts: dict
    items: list[PackItem]
    findings: list[dict]
    digests_used: int
    focus: str | None = None
    selected_from: int = 0


@dataclass
class _Row:
    digest: DigestDoc
    item: DigestItemDoc
    side: str
    day: str
    category: str
    haystack: str
    tags: list[str]
    claim: bool
    n: int                        # the item's number in its digest


def _pick(found: list[DigestDoc]) -> list[DigestDoc]:
    """One digest per side (or the topics digest) and Kyiv day: the latest scheduled one, else the latest."""
    best: dict[tuple[str, str], DigestDoc] = {}
    for d in found:
        kind = "topics" if d.trigger == "topics" else d.side
        k = (kind, d.created_at.astimezone(digests.KYIV).date().isoformat())
        cur = best.get(k)
        if cur is None or (d.trigger == "scheduled") >= (cur.trigger == "scheduled"):
            best[k] = d
    return sorted(best.values(), key=lambda d: d.created_at)


async def _rows(repos: Repos, ctx: UserContext, period: Period, user_topics: list) -> tuple[list[_Row], int]:
    start, end = period.bounds()
    chosen = _pick(await repos.digests.in_range(start, end))
    items = [it for d in chosen for s in d.sections for it in s.items]
    posts = await digests._source_posts(repos, items)
    labels = {pid: p.sensitivity for pid, p in posts.items()}
    rows = []
    for d in chosen:
        is_topics = d.trigger == "topics"
        day = d.created_at.astimezone(digests.KYIV).date().isoformat()
        position = 0
        for s in d.sections:
            for it in s.items:
                position += 1
                if not digests._item_allowed(d, it, labels, ctx):
                    continue
                sources = [posts[p] for p in it.source_post_ids if p in posts]
                haystack = " ".join([it.text, *(p.summary for p in sources)])
                if is_topics and not any(topics.matched_term(t, haystack) for t in user_topics):
                    continue                          # another user's topic
                claim = any(p.source_side == "ru" for p in sources) if is_topics else d.side == "ru"
                rows.append(_Row(d, it, "topics" if is_topics else d.side, day, s.category, haystack,
                                 [t for p in sources for t in p.tags], claim, digests.item_number(it, position)))
    return rows, len(chosen)


def _named(g: relevance.Topic) -> bool:
    """A goal match by a name (the goal's own words or a specific term), not by a broader phrase or a tag."""
    return g.tag is None and g.weight >= relevance.W_GOAL * relevance.LEVELS["specific"] - 1e-9


def _matches(prefs: relevance.Interests, user_topics: list, row: _Row) -> tuple[list[str], list[str], list[str]]:
    """(goals named in the item, goals it relates to by names or broader phrases, tracked topics it names).
    Tag-level matches don't count here: nearly every defense item carries such a tag, so they'd mark everything."""
    text = row.haystack.lower()
    related = [g for g in prefs.goals if g.tag is None and g.matches(text)]
    named = sorted({g.group for g in related if _named(g)})
    return named, sorted({g.group for g in related}), [t.name for t in user_topics
                                                       if topics.matched_term(t, row.haystack)]


def _count(rows: list[_Row], prefs, user_topics, goal_names: dict[str, str]) -> dict:
    named: Counter = Counter()
    related: Counter = Counter()
    per_topic: Counter = Counter()
    for r in rows:
        n, rel, t = _matches(prefs, user_topics, r)
        named.update(goal_names.get(x, x) for x in n)
        related.update(goal_names.get(x, x) for x in rel)
        per_topic.update(t)
    return {"items": len(rows), "named": named, "related": related, "topics": per_topic,
            "by_side": Counter(r.side for r in rows), "by_category": Counter(r.category for r in rows)}


async def build(repos: Repos, ctx: UserContext, period: Period, focus: str | None = None) -> Pack:
    profile = await repos.users.get_profile(ctx.user_id)
    prefs = await relevance.load(repos, ctx, profile)
    user_topics = [t for t in await repos.users.get_topics(ctx.user_id) if t.status != "explored"]
    goal_texts = [g.text for g in profile.goals if g.active] if profile else []
    goal_names = {relevance._norm(g): g for g in goal_texts}

    rows, used = await _rows(repos, ctx, period, user_topics)
    prev_rows, _ = await _rows(repos, ctx, period.previous(), user_topics)
    now, prev = _count(rows, prefs, user_topics, goal_names), _count(prev_rows, prefs, user_topics, goal_names)

    by_day = Counter(r.day for r in rows)
    tags = Counter(t for r in rows for t in dict.fromkeys(r.tags))
    counts = {
        "items": now["items"], "previous_items": prev["items"],
        "per_day": [{"date": (period.start + timedelta(days=i)).isoformat(),
                     "items": by_day.get((period.start + timedelta(days=i)).isoformat(), 0)}
                    for i in range(period.days)],
        "by_side": dict(now["by_side"]),
        "by_category": [{"category": c, "items": n, "previous_items": prev["by_category"].get(c, 0)}
                        for c, n in now["by_category"].most_common(TOP_CATEGORIES)],
        # named: the goal's names appear; related: names or broader phrases (includes named).
        "goals": [{"goal": g, "named_items": now["named"].get(g, 0), "previous_named_items": prev["named"].get(g, 0),
                   "related_items": now["related"].get(g, 0), "previous_related_items": prev["related"].get(g, 0)}
                  for g in goal_texts],
        "topics": [{"topic": t.name, "status": topics.status_of(t), "items": now["topics"].get(t.name, 0),
                    "previous_items": prev["topics"].get(t.name, 0)} for t in user_topics],
        "top_tags": [{"tag": t, "items": n} for t, n in tags.most_common(TOP_TAGS)],
    }

    focus_topic = relevance._topic("фокус", focus, 1) if focus else None
    if focus and focus_topic is None:
        raise InvalidInput("The focus has no searchable words: name the goal, topic or thing")
    candidates: dict[str, PackItem] = {}
    for r in rows:
        if focus_topic and not (focus_topic.matches(r.haystack.lower())
                                or any(topics.norm(focus) == t.norm and topics.matched_term(t, r.haystack)
                                       for t in user_topics)):
            continue
        rel = prefs.score(side=r.side, category=r.category, text=r.haystack, tags=r.tags)
        _, g, t = _matches(prefs, user_topics, r)
        text = " ".join(r.item.text.split())
        key = re.sub(r"\W+", " ", text.lower()).strip()
        item = PackItem(id=f"{r.digest.id}/{r.n}", date=r.day, side=r.side, claim=r.claim, category=r.category,
                        text=text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…",
                        source_urls=list(r.item.source_urls[:2]), why=rel.why, goals=[goal_names.get(x, x) for x in g],
                        topics=t, score=rel.score + (relevance.W_TRACKED if t else 0))
        if key not in candidates or candidates[key].date < item.date:    # the same text on several days: once
            candidates[key] = item
    ranked = sorted(candidates.values(), key=lambda i: (-i.score, i.date))
    groups: Counter = Counter()
    picked: list[PackItem] = []
    for i in ranked:
        group = (i.topics or i.goals or [i.category])[0]
        if groups[group] >= PER_GROUP:
            continue
        groups[group] += 1
        picked.append(i)
        if len(picked) >= MAX_ITEMS:
            break
    picked.sort(key=lambda i: i.date)

    start, end = period.bounds()
    findings = []
    for t in await repos.users.get_topics(ctx.user_id, findings=True):
        for f in t.findings:
            if f.at and start <= topics.utc(f.at) < end:
                findings.append({"topic": t.name, "title": f.title, "url": f.url, "source": f.source,
                                 "date": f.date, "status": f.status, "summary": f.summary})
    learned = await repos.users.get_learned(ctx.user_id) or {}
    return Pack(period, period.previous(), goal_texts, [t.name for t in user_topics], learned.get("summary"),
                counts, picked, findings, used, focus, len(candidates))


@dataclass
class Report:
    key: str
    period: Period
    focus: str | None
    title: str
    highlights: list[str]
    text: str
    created_at: datetime | None
    conversation_id: str | None = None


async def stored(repos: Repos, ctx: UserContext, period: Period, focus: str | None) -> Report | None:
    doc = await repos.reports.get(ctx.user_id, report_key(period, focus))
    if not doc or not doc.get("messages"):
        return None
    m = doc["messages"][-1]
    return Report(report_key(period, focus), period, focus, m.get("title", ""), m.get("highlights") or [],
                  m.get("content", ""), m.get("created_at"), str(doc["_id"]))


async def save_report(repos: Repos, ctx: UserContext, period: Period, focus: str | None, title: str,
                      highlights: list[str], text: str) -> Report:
    text = (text or "").strip()
    if len(text) < 200:
        raise InvalidInput("The report is too short: write the full analysis")
    if len(text) > MAX_REPORT:
        raise InvalidInput(f"The report is longer than {MAX_REPORT} characters: tighten it")
    highlights = [" ".join(h.split())[:300] for h in highlights if h and h.strip()][:MAX_HIGHLIGHTS]
    title = " ".join((title or "").split())[:200] or f"Аналіз {period.start:%d.%m}–{period.end:%d.%m}"
    now = datetime.now(timezone.utc)
    message = {"id": ObjectId(), "role": "assistant", "via": "analysis", "written_by": "model",
               "client_channel": ctx.channel, "title": title, "highlights": highlights, "content": text,
               "created_at": now}
    key = report_key(period, focus)
    cid = await repos.reports.save(ctx.user_id, key, {"start": period.start.isoformat(),
                                                      "end": period.end.isoformat(), "days": period.days},
                                   focus, message)
    return Report(key, period, focus, title, highlights, text, now, str(cid))


def month_name(period: Period) -> str | None:
    """«жовтень 2026» when the period is exactly one calendar month."""
    last = calendar.monthrange(period.start.year, period.start.month)[1]
    if period.start.day == 1 and period.end == period.start.replace(day=last):
        names = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень",
                 "жовтень", "листопад", "грудень"]
        return f"{names[period.start.month - 1]} {period.start.year}"
    return None
