"""What changed for one user, as signals the advisor turns into advice (docs/AGENT_PLAN.md §4 ③, phase 4). Code
only; the counts are exact, from the daily digests (analysis._rows: the same items the user was shown).

Signals, over the last WEEK days against the WEEK days before. «As often» is per day with a digest: a week with
fewer digests isn't a quieter week. A rise needs at least BASELINE_DAYS digest days in the week before.
- spike: a followed topic named in ≥ SPIKE_MIN items and at least RISE times as often as the week before;
- findings: web findings saved on a followed topic this week (from a deep dive);
- goal_spike: an active goal named in ≥ GOAL_SPIKE_MIN items and at least RISE times as often as the week before;
- quiet: a topic followed for ≥ QUIET_DAYS with no item and no finding in that time;
- goal_quiet: an active goal set ≥ QUIET_DAYS ago that no item named in that time.
Quiet signals need digests on most of those days (COVERED_SHARE): no data is not «no news».
Each signal carries the steps that make sense for it; the advisor picks one. The items carry their source URLs,
the only URLs advice may cite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from brain.domain import analysis, digests, relevance, topics
from brain.domain.identity import UserContext
from brain.db.repositories import Repos

WEEK = 7
QUIET_DAYS = 14
SPIKE_MIN = 3
GOAL_SPIKE_MIN = 5
ITEMS_PER_SIGNAL = 5
COVERED_SHARE = 0.7
RISE = 2.0
BASELINE_DAYS = 3
MAX_TEXT = 300
STEPS = ("deep_dive", "analysis", "stop_topic", "refine_goal")
NEEDS_MODEL = ("spike", "findings", "goal_spike")       # news to explain; the quiet ones are said by code


@dataclass
class Signal:
    key: str                    # stable per thing (and week, for news): proposals are made once per key
    kind: str
    subject: str                # the topic's name or the goal's text
    facts: dict
    steps: list[str]
    items: list[dict] = field(default_factory=list)     # {text, source_urls, claim, day}

    @property
    def urls(self) -> set[str]:
        return {u for i in self.items for u in i.get("source_urls", [])}


def _utc(dt):
    return topics.utc(dt)


def _item(r) -> dict:
    text = r.item.text if len(r.item.text) <= MAX_TEXT else r.item.text[:MAX_TEXT - 1] + "…"
    return {"text": text, "source_urls": r.item.source_urls[:3], "claim": r.claim, "day": r.day}


def week_of(today: date) -> str:
    y, w, _ = today.isocalendar()
    return f"{y}-W{w:02d}"


async def signals(repos: Repos, ctx: UserContext, today: date | None = None) -> list[Signal]:
    now = datetime.now(timezone.utc)
    today = today or now.astimezone(digests.KYIV).date()
    this = analysis.Period(today - timedelta(days=WEEK - 1), today)
    before = this.previous()
    quiet = analysis.Period(today - timedelta(days=QUIET_DAYS - 1), today)
    followed = await topics.followed(repos, ctx, now)
    rows, _ = await analysis._rows(repos, ctx, analysis.Period(before.start, today), followed)
    in_this = [r for r in rows if r.day >= this.start.isoformat()]
    in_before = [r for r in rows if r.day < this.start.isoformat()]
    in_quiet = [r for r in rows if r.day >= quiet.start.isoformat()]
    week = week_of(today)
    out: list[Signal] = []
    can_be_quiet = len({r.day for r in in_quiet}) >= COVERED_SHARE * QUIET_DAYS
    days_now = len({r.day for r in in_this}) or 1
    days_before = len({r.day for r in in_before})

    def rose(n: int, was: int, at_least: int) -> dict | None:
        """The facts of a rise (per digest day), or None."""
        if n < at_least or days_before < BASELINE_DAYS:
            return None
        now_rate, was_rate = n / days_now, was / days_before
        if now_rate < RISE * max(was_rate, 1 / WEEK):
            return None
        return {"this_week": n, "week_before": was, "per_day": round(now_rate, 1), "per_day_before": round(was_rate, 1),
                "digest_days": days_now, "digest_days_before": days_before}

    prefs = await relevance.load(repos, ctx)
    full = {t.norm: t for t in await repos.users.get_topics(ctx.user_id, findings=True)}
    for t in followed:
        named = [r for r in in_this if topics.matched_term(t, r.haystack)]
        was = sum(1 for r in in_before if topics.matched_term(t, r.haystack))
        if facts := rose(len(named), was, SPIKE_MIN):
            named.sort(key=lambda r: -prefs.score(side=r.side, category=r.category, text=r.haystack).score)
            out.append(Signal(f"spike:{t.norm}:{week}", "spike", t.name, facts,
                              ["deep_dive", "analysis"], [_item(r) for r in named[:ITEMS_PER_SIGNAL]]))
        found = [f for f in (full.get(t.norm).findings if full.get(t.norm) else [])
                 if f.at and _utc(f.at) >= now - timedelta(days=WEEK)]
        if found:
            out.append(Signal(f"findings:{t.norm}:{week}", "findings", t.name, {"findings": len(found)},
                              ["analysis", "deep_dive"],
                              [{"text": f"{f.title}: {f.summary}"[:MAX_TEXT], "source_urls": [f.url], "claim":
                                f.status == "claim", "day": _utc(f.at).date().isoformat()} for f in found[:ITEMS_PER_SIGNAL]]))
        followed_since = _utc(t.created_at)
        if can_be_quiet and followed_since and followed_since <= now - timedelta(days=QUIET_DAYS):
            heard = any(topics.matched_term(t, r.haystack) for r in in_quiet) or any(
                f.at and _utc(f.at) >= now - timedelta(days=QUIET_DAYS)
                for f in (full.get(t.norm).findings if full.get(t.norm) else []))
            if not heard:
                until = _utc(t.expires_at).date().isoformat() if t.expires_at else ""
                out.append(Signal(f"quiet:{t.norm}:{until}", "quiet", t.name, {"days": QUIET_DAYS}, ["stop_topic"]))

    profile = await repos.users.get_profile(ctx.user_id)
    goals = [g for g in (profile.goals if profile else []) if g.active]
    for g in goals:
        key = relevance._norm(g.text)
        tops = [x for x in prefs.goals if x.group == key and x.tag is None]
        if not tops:
            continue

        def names(r) -> bool:
            return any(analysis._named(x) and x.matches(r.haystack.lower()) for x in tops)
        hit = [r for r in in_this if names(r)]
        was = sum(1 for r in in_before if names(r))
        if facts := rose(len(hit), was, GOAL_SPIKE_MIN):
            hit.sort(key=lambda r: -prefs.score(side=r.side, category=r.category, text=r.haystack).score)
            out.append(Signal(f"goal_spike:{key}:{week}", "goal_spike", g.text, facts,
                              ["analysis", "deep_dive"], [_item(r) for r in hit[:ITEMS_PER_SIGNAL]]))
        set_at = _utc(g.created_at)
        if can_be_quiet and (set_at is None or set_at <= now - timedelta(days=QUIET_DAYS)) \
                and not any(names(r) for r in in_quiet):
            out.append(Signal(f"goal_quiet:{key}", "goal_quiet", g.text, {"days": QUIET_DAYS}, ["refine_goal"]))
    return out
