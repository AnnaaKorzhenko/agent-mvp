"""Lasting interests and current intents in users.learned (docs/AGENT_PLAN.md §3): their bookkeeping, how much
each counts today, and the pipeline's profile keyword record.

An interest in users.learned.interests:
    {topic, weight 1-5, origin stated|inferred, web_queries, first_seen, last_seen, evidence {kind: n}, days,
     confirmed}
- The learning job's model names each interest and the kinds of evidence for it in the run's new activity
  (signals); merge() keeps the counts across runs.
- An inferred interest counts only once confirmed: a strong signal (a comment, a deep dive, a followed topic, a
  yes to a proposal), or CONFIRM_SIGNALS signals on CONFIRM_DAYS different days. One curious question doesn't
  reorder the digest. A stated one counts at once. Confirmed stays confirmed; weight() lets it fade.
- weight(): the stored weight halved every HALF_LIFE_DAYS without fresh evidence (stated: HALF_LIFE_STATED);
  below MIN_WEIGHT it no longer counts. Interests written before 2026-10-30 (no evidence yet) count as confirmed,
  last seen when the profile was written.

An intent in users.learned.intents: {text, terms, until}: what the user is working on now; it counts until
`until` and then disappears by itself.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

SIGNALS = ("rating", "comment", "search", "chat", "deep_dive", "follow", "suggestion", "other")
STRONG = {"comment", "deep_dive", "follow", "suggestion"}
CONFIRM_SIGNALS = 3
CONFIRM_DAYS = 2
MAX_DAYS = 30
HALF_LIFE_DAYS = 30
HALF_LIFE_STATED = 60
MIN_WEIGHT = 1.0
PIPELINE_MIN_WEIGHT = 2.0       # a lasting interest searched on the web for the pipeline
MAX_PROFILE_QUERIES = 60        # all users together, before the per-region copies
BOOKKEEPING = ("first_seen", "last_seen", "evidence", "days", "confirmed")


def _utc(dt) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def merge(previous: list[dict] | None, new: list[dict], now: datetime,
          previous_written: datetime | None = None) -> list[dict]:
    """This run's interests (the model's: topic, weight, origin, signals, web_queries) with the bookkeeping of the
    same interests from the previous profile. An interest the model dropped is gone."""
    old = {_norm(i.get("topic")): i for i in previous or []}
    today = now.date().isoformat()
    out = []
    for i in new:
        p = old.get(_norm(i["topic"]), {})
        legacy = bool(p) and "evidence" not in p
        signals = set(i.get("signals") or []) or ({"other"} if not p else set())   # new, but no kind given
        evidence = dict(p.get("evidence") or {})
        days = list(p.get("days") or [])
        for s in signals:
            evidence[s] = evidence.get(s, 0) + 1
        if signals and today not in days:
            days = (days + [today])[-MAX_DAYS:]
        origin = "stated" if "stated" in (i.get("origin"), p.get("origin")) else "inferred"
        confirmed = bool(legacy or p.get("confirmed") or origin == "stated" or STRONG & set(evidence)
                         or (sum(evidence.values()) >= CONFIRM_SIGNALS and len(days) >= CONFIRM_DAYS))
        seen = now if signals else (_utc(p.get("last_seen")) or _utc(previous_written) or now)
        out.append({"topic": i["topic"], "weight": i["weight"], "origin": origin,
                    "web_queries": i.get("web_queries") or p.get("web_queries") or [],
                    "first_seen": _utc(p.get("first_seen")) or _utc(previous_written if legacy else None) or now,
                    "last_seen": seen, "evidence": evidence, "days": days, "confirmed": confirmed})
    return out


def weight(i: dict, now: datetime | None = None, written: datetime | None = None) -> float | None:
    """How much the interest counts today (1-5, fading), or None if it doesn't count (not confirmed yet, or faded)."""
    if i.get("confirmed") is False:
        return None
    now = now or datetime.now(timezone.utc)
    seen = _utc(i.get("last_seen")) or _utc(written) or now
    half = HALF_LIFE_STATED if i.get("origin") == "stated" else HALF_LIFE_DAYS
    w = float(i.get("weight", 3)) * 0.5 ** (max(0.0, (now - seen).total_seconds() / 86400) / half)
    return round(w, 2) if w >= MIN_WEIGHT else None


def counted(learned: dict | None, now: datetime | None = None) -> list[tuple[dict, float]]:
    """The interests that count today, with their weight, strongest first."""
    learned = learned or {}
    written = _utc(learned.get("updated_at"))
    out = [(i, w) for i in learned.get("interests") or [] if (w := weight(i, now, written)) is not None]
    return sorted(out, key=lambda x: -x[1])


def merge_intents(new: list[dict], now: datetime) -> list[dict]:
    """The model's intents ({text, terms, days}) as stored: {text, terms, until}."""
    return [{"text": i["text"], "terms": i.get("terms") or [], "until": now + timedelta(days=int(i.get("days", 7)))}
            for i in new]


def active_intents(learned: dict | None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    return [i for i in (learned or {}).get("intents") or [] if (u := _utc(i.get("until"))) and u > now]


def for_model(learned: dict, now: datetime | None = None) -> dict:
    """The previous profile as the learning model gets it: without the bookkeeping, with intents still open."""
    out = dict(learned)
    out["interests"] = [{k: v for k, v in i.items() if k not in BOOKKEEPING} for i in learned.get("interests") or []]
    out["intents"] = [{"text": i["text"], "terms": i.get("terms") or [], "until": _utc(i["until"]).date().isoformat()}
                      for i in active_intents(learned, now)]
    return out


def profile_queries(profiles: list[dict], now: datetime | None = None) -> list[dict]:
    """The pipeline's «Profile interests» record from every user's learned profile: the web queries of the
    interests that count (weight ≥ PIPELINE_MIN_WEIGHT), one search per query whoever has it, the ones most
    users share first, at most MAX_PROFILE_QUERIES; {q, region} per region by spelling, sorted."""
    from brain.domain import topics
    score: dict[str, list] = {}               # query_key -> [users, best weight, spelling]
    for learned in profiles:
        seen_here: set[str] = set()
        for i, w in counted(learned, now):
            if w < PIPELINE_MIN_WEIGHT:
                continue
            for q in i.get("web_queries") or []:
                q = topics.exact(" ".join(str(q).split())[:120])
                if not q or not topics.distinctive(q) or (k := topics.query_key(q)) in seen_here:
                    continue
                seen_here.add(k)
                s = score.setdefault(k, [0, 0.0, q])
                s[0] += 1
                s[1] = max(s[1], w)
    best = sorted(score.values(), key=lambda s: (-s[0], -s[1], s[2].lower()))[:MAX_PROFILE_QUERIES]
    out = [{"q": q, "region": r} for _, _, q in best for r in topics.regions(q)]
    return sorted(out, key=lambda x: (x["q"].lower(), x["region"]))
