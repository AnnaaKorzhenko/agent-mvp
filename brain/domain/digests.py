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

from brain.db.models import DigestDoc, DigestItemDoc, DigestSectionDoc, UserProfileDoc
from brain.db.repositories import Repos
from brain.domain import scoring, sensitivity
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
    result = DigestResultView(date=wanted.isoformat() if wanted else None)
    for s in sides:
        digest = await repos.digests.latest(s, "day", start, end)
        if digest is None or not sensitivity.is_allowed(digest.sensitivity, ctx.max_sensitivity):
            result.missing_sides.append(s)
            continue
        result.digests.append(await _view(repos, ctx, digest, profile))
    if result.date is None and result.digests:
        result.date = result.digests[0].digest.created_at.astimezone(KYIV).date().isoformat()
    return result


async def _view(repos: Repos, ctx: UserContext, digest: DigestDoc, profile: UserProfileDoc | None) -> DigestView:
    # Labels of every source post, read with all labels allowed: only used to decide what to hide.
    ids = [pid for sec in digest.sections for it in sec.items for pid in it.source_post_ids]
    labels = {pid: p.sensitivity for pid, p in
              (await repos.posts.get_many(ids, list(sensitivity.LEVELS))).items()}
    view = DigestView(digest, [], side_note=RU_NOTE if digest.side == "ru" else None)
    for section in order_sections(digest.sections, profile, digest.side):
        kept = []
        for item in section.items:
            label = sensitivity.highest([digest.sensitivity, *(labels.get(p) for p in item.source_post_ids
                                                               if p in labels)])
            if sensitivity.is_allowed(label, ctx.max_sensitivity):
                kept.append(item)
            else:
                view.hidden_items += 1
        if kept:
            view.sections.append((section, kept))
    return view
