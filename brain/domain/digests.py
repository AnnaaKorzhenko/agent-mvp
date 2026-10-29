"""Latest daily digest per side, ordered for the user exactly as the Telegram bot orders it.

Order: today's morning-picker choice (users.daily.selected, only if daily.date is today in Kyiv) first,
then the rest by category_scores; categories without a score keep the digest's own order.
Each item carries the highest sensitivity of the digest and its source posts; items above the
caller's label are removed before anything is returned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from brain.db.models import DigestDoc, DigestItemDoc, DigestSectionDoc, PostDoc, UserProfileDoc
from brain.db.repositories import Repos
from brain.domain import relevance, scoring, sensitivity
from brain.domain.relevance import Interests, Relevance
from brain.domain.errors import InvalidInput
from brain.domain.identity import UserContext

KYIV = ZoneInfo("Europe/Kyiv")
RU_NOTE = "Claims of Russian sources, not verified facts."


@dataclass
class DigestView:
    digest: DigestDoc
    sections: list[tuple[DigestSectionDoc, list[DigestItemDoc]]]
    hidden_items: int = 0
    side_note: str | None = None
    numbers: dict[int, int] = field(default_factory=dict)     # id(item) -> item number (see item_number)
    relevance: dict[int, "Relevance"] = field(default_factory=dict)     # id(item) -> personal relevance

    def number(self, item: DigestItemDoc) -> int:
        return self.numbers[id(item)]

    def why(self, item: DigestItemDoc) -> "Relevance":
        return self.relevance.get(id(item)) or Relevance()


@dataclass
class DigestResultView:
    date: str | None
    digests: list[DigestView] = field(default_factory=list)
    missing_sides: list[str] = field(default_factory=list)


def kyiv_today() -> date:
    return datetime.now(KYIV).date()


def kyiv_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=KYIV)
    return start, start + timedelta(days=1)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise InvalidInput(f"date must be YYYY-MM-DD, got {value!r}") from None


def todays_choice(profile: UserProfileDoc | None, side: str) -> list[str]:
    daily = profile.daily if profile else None
    if daily and daily.date == kyiv_today().isoformat():
        return list(daily.selected.get(side, []))
    return []


def order_sections(sections: list[DigestSectionDoc], profile: UserProfileDoc | None,
                   side: str) -> list[DigestSectionDoc]:
    """Same ordering as main.ordered_items(): sections sorted by the user's category order, stable."""
    user = profile.as_scoring_user() if profile else {}
    order = scoring.category_order(user, side, first=todays_choice(profile, side))
    position = {c: i for i, c in enumerate(order)}
    idx = sorted(range(len(sections)), key=lambda i: (position.get(sections[i].category, len(position)), i))
    return [sections[i] for i in idx]


async def get_digests(repos: Repos, ctx: UserContext, side: str = "both", day: str | None = None) -> DigestResultView:
    sides = ["ua", "ru"] if side in ("both", "all", None) else [side]
    if any(s not in ("ua", "ru") for s in sides):
        raise InvalidInput("side must be 'ua', 'ru' or 'both'")
    wanted = parse_date(day)
    start, end = kyiv_day_bounds(wanted) if wanted else (None, None)
    profile = await repos.users.get_profile(ctx.user_id)
    prefs = await relevance.load(repos, ctx, profile)
    result = DigestResultView(date=wanted.isoformat() if wanted else None)
    for s in sides:
        digest = await repos.digests.latest(s, "day", start, end)
        if digest is None or not sensitivity.is_allowed(digest.sensitivity, ctx.max_sensitivity):
            result.missing_sides.append(s)
            continue
        result.digests.append(await _view(repos, ctx, digest, profile, prefs))
    if result.date is None and result.digests:
        result.date = result.digests[0].digest.created_at.astimezone(KYIV).date().isoformat()
    return result


MORNING_WAIT_UNTIL = 12       # Kyiv hour: until then, wait for both sides before delivering


@dataclass
class Morning:
    status: str                   # "new" | "not_ready" | "already_sent"
    result: DigestResultView | None = None


async def morning_digest(repos: Repos, ctx: UserContext, now: datetime | None = None) -> Morning:
    """Today's digest for the scheduled morning delivery, once per user: new only if it wasn't delivered
    before. Before MORNING_WAIT_UNTIL it waits until both sides are in, so the user gets one complete delivery."""
    now = (now or datetime.now(KYIV)).astimezone(KYIV)
    res = await get_digests(repos, ctx, "both", now.date().isoformat())
    if not res.digests or (res.missing_sides and now.hour < MORNING_WAIT_UNTIL):
        return Morning("not_ready", res)
    key = ",".join(sorted(v.digest.id for v in res.digests))
    if not await repos.users.claim_morning_digest(ctx.user_id, key, now.date().isoformat(), ctx.channel):
        return Morning("already_sent", res)
    return Morning("new", res)


async def _source_posts(repos: Repos, items: list[DigestItemDoc]) -> dict[str, PostDoc]:
    # The source posts, read with all labels allowed: their labels decide what to hide, their tags feed relevance
    # (only for items that are shown).
    ids = [pid for it in items for pid in it.source_post_ids]
    return await repos.posts.get_many(ids, list(sensitivity.LEVELS))


async def _source_labels(repos: Repos, items: list[DigestItemDoc]) -> dict[str, str]:
    return {pid: p.sensitivity for pid, p in (await _source_posts(repos, items)).items()}


def _item_allowed(digest: DigestDoc, item: DigestItemDoc, labels: dict[str, str], ctx: UserContext) -> bool:
    label = sensitivity.highest([digest.sensitivity, *(labels.get(p) for p in item.source_post_ids if p in labels)])
    return sensitivity.is_allowed(label, ctx.max_sensitivity)


def item_number(item: DigestItemDoc, position: int) -> int:
    """The item's n; items without one are numbered by position (1, 2, …), as the bot does."""
    return item.n if item.n is not None else position


async def find_item(repos: Repos, ctx: UserContext, digest_id: str | None,
                    item_n: int | None) -> tuple[DigestDoc, DigestSectionDoc, DigestItemDoc] | None:
    """One digest item by (digest_id, n), or None if it doesn't exist or the caller may not see it."""
    digest = await repos.digests.get(digest_id) if digest_id and item_n is not None else None
    if digest is None:
        return None
    position = 0
    for section in digest.sections:
        for item in section.items:
            position += 1
            if item_number(item, position) == item_n:
                labels = await _source_labels(repos, [item])
                return (digest, section, item) if _item_allowed(digest, item, labels, ctx) else None
    return None


FOR_YOU = DigestSectionDoc(category="for_you", title="⭐ Для вас")
FOR_YOU_MIN = 1.2        # relevance needed for «Для вас»: a goal or interest match, not a favourite category alone
FOR_YOU_MAX = 8          # items per side in it


async def _view(repos: Repos, ctx: UserContext, digest: DigestDoc, profile: UserProfileDoc | None,
                prefs: Interests | None = None) -> DigestView:
    """Items the caller may see, ordered for this user: first «⭐ Для вас», the most relevant items by personal
    relevance (goals, interests, rated topics); then the remaining items in their sections, today's picked
    categories first (as the bot does), each section by relevance. Without any signal: the digest's own order."""
    items = [it for sec in digest.sections for it in sec.items]          # the digest's own order
    posts = await _source_posts(repos, items)
    labels = {pid: p.sensitivity for pid, p in posts.items()}
    view = DigestView(digest, [], side_note=RU_NOTE if digest.side == "ru" else None)
    view.numbers = {id(it): item_number(it, pos) for pos, it in enumerate(items, 1)}
    chosen = set(todays_choice(profile, digest.side))
    ordered = []
    for base, section in enumerate(order_sections(digest.sections, profile, digest.side)):
        kept = []
        for item in section.items:
            if not _item_allowed(digest, item, labels, ctx):
                view.hidden_items += 1
                continue
            kept.append(item)
            if prefs is not None:
                tags = [t for pid in item.source_post_ids if pid in posts for t in posts[pid].tags]
                view.relevance[id(item)] = prefs.score(side=digest.side, category=section.category,
                                                       category_label=section.title, text=item.text, tags=tags)
        if kept:
            kept.sort(key=lambda it: -view.why(it).score)                 # stable
            ordered.append(((0 if section.category in chosen else 1, base), section, kept))
    sections = [(section, kept) for _, section, kept in sorted(ordered, key=lambda x: x[0])]
    picked = sorted((it for _, kept in sections for it in kept if view.why(it).score >= FOR_YOU_MIN),
                    key=lambda it: -view.why(it).score)[:FOR_YOU_MAX]
    if picked:
        taken = {id(it) for it in picked}
        sections = [(FOR_YOU, picked)] + [(s, [it for it in kept if id(it) not in taken]) for s, kept in sections]
        sections = [(s, kept) for s, kept in sections if kept]
    view.sections = sections
    return view
