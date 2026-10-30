"""Rising names in our own posts (docs/AGENT_PLAN.md §4 ②, step A): what many channels started mentioning in the
last WINDOW_HOURS, well above its usual rate. Code only, no model: the scout agent then has a model name and
describe the candidates (brain/agents/scout.py).

- Terms are names, not words: «…» quotes, words with a digit or a hyphenated part («Рассвет-3», «Х-101»), Latin
  names inside Cyrillic text («Starlink»), and runs of two or three capitalized words that don't start a
  sentence. A single capitalized Cyrillic word counts only in quotes: unquoted it is nearly always a place or a
  person in its case form («Литве», «Алешках»), the alerts' noise.
- A term counts once per post. Spelling variants are one term (topics.query_key).
- Rising: in at least MIN_CHANNELS different channels in the window, at least MIN_POSTS posts, and either new
  (never in the baseline before it) or MIN_RISE times its baseline daily rate.
- Terms that keep showing up together (most posts of one are posts of the other) merge into one candidate.
- Ads, duplicates and posts without a summary are left out.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from brain.domain import topics

WINDOW_HOURS = 48
BASELINE_DAYS = 14
MIN_CHANNELS = 3
MIN_POSTS = 4
MIN_RISE = 3.0
MAX_CANDIDATES = 30
MERGE_SHARE = 0.6
EXAMPLES = 3

_L = "A-Za-zА-Яа-яІіЇїЄєҐґЁё"
_UP = "A-ZА-ЯІЇЄҐЁ"
_QUOTED = re.compile(r"«([^»]{2,40})»")
_CODED = re.compile(rf"(?<![{_L}\d-])([{_L}]+-?\d+[{_L}\d]*|[{_L}]+-[{_L}\d]+|\d+[{_L}]+)(?![{_L}\d-])")
_LATIN = re.compile(r"(?<![A-Za-z])([A-Z][A-Za-z0-9.]{2,}(?:\s[A-Z][A-Za-z0-9.]+)?)(?![A-Za-z])")
_CAPS = re.compile(rf"(?<=[{_L}\d,;:)] )([{_UP}][{_L}'’]+(?: [{_UP}][{_L}'’]+){{1,2}})")
# Always frequent, never news by themselves.
_STOP = {topics.query_key(w) for w in """Україна України Україні Росія Росії РФ ЗСУ ВСУ ЗС Сили Оборони ППО ПВО БПЛА
    ДРГ ЛНР ДНР США ЄС НАТО Telegram Генштаб Генштабу Минобороны Міноборони Зеленський Путин Путін Кремль Київ Киев
    Москва Москвы ВС РФ Russia Ukraine FPV Group""".split()}


def terms_of(text: str) -> set[str]:
    """The name-like terms of one summary."""
    found: set[str] = set()
    for rx in (_QUOTED, _CODED, _LATIN, _CAPS):
        for m in rx.finditer(text or ""):
            t = " ".join(m.group(1).strip(" .,-").split())
            if rx is _CODED and not any(ch.isdigit() or ch.isupper() for ch in t):
                continue                                   # «из-за», «чат-бот»: words, not names
            if len(t) >= 3 and not t.isdigit() and topics.query_key(t) not in _STOP:
                found.add(t)
    return found


@dataclass
class Candidate:
    terms: list[str]                       # spellings, most used first
    posts: int
    channels: int
    baseline_per_day: float
    sides: dict[str, int]
    post_ids: list[str]
    examples: list[dict] = field(default_factory=list)    # {summary, post_url, side}
    label: str = "internal"

    @property
    def name(self) -> str:
        return self.terms[0]

    @property
    def rise(self) -> float:
        per_day = self.posts / (WINDOW_HOURS / 24)
        return per_day / self.baseline_per_day if self.baseline_per_day else math.inf

    @property
    def momentum(self) -> float:
        """For ranking: how much it rose (capped for new terms), times how widely it spread."""
        return min(self.rise, 20.0) * math.log2(1 + self.channels)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def detect(window: list[dict], baseline: list[dict], now: datetime | None = None) -> list[Candidate]:
    """Rising candidates from the window's posts against the baseline's. Posts: {_id, summary, channel_id,
    source_side, post_url, sensitivity}."""
    by_key: dict[str, dict] = defaultdict(lambda: {"posts": [], "channels": set(), "spell": Counter()})
    for p in window:
        for t in terms_of(p.get("summary") or ""):
            k = topics.query_key(t)
            e = by_key[k]
            if not e["posts"] or e["posts"][-1] is not p:
                e["posts"].append(p)
                e["channels"].add(str(p.get("channel_id")))
            e["spell"][t] += 1
    base = Counter()
    for p in baseline:
        for k in {topics.query_key(t) for t in terms_of(p.get("summary") or "")}:
            base[k] += 1
    rising = []
    for k, e in by_key.items():
        posts, channels = len(e["posts"]), len(e["channels"])
        if posts < MIN_POSTS or channels < MIN_CHANNELS:
            continue
        per_day = base[k] / BASELINE_DAYS
        if per_day and posts / (WINDOW_HOURS / 24) < MIN_RISE * per_day:
            continue
        rising.append((k, e, per_day))
    # Merge terms that travel together: most of the smaller one's posts are the bigger one's posts.
    rising.sort(key=lambda x: -len(x[1]["posts"]))
    groups: list[dict] = []
    for k, e, per_day in rising:
        ids = {str(p["_id"]) for p in e["posts"]}
        home = next((g for g in groups if len(ids & g["ids"]) >= MERGE_SHARE * len(ids)), None)
        if home is None:
            groups.append({"ids": set(ids), "e": e, "per_day": per_day, "spell": Counter(e["spell"])})
        else:
            home["spell"].update(e["spell"])
    out = []
    for g in groups:
        e = g["e"]
        sides = Counter(p.get("source_side") or "?" for p in e["posts"])
        examples = sorted(e["posts"], key=lambda p: (p.get("source_side") != "ua", -len(p.get("summary") or "")))
        out.append(Candidate(
            terms=[t for t, _ in g["spell"].most_common(6)], posts=len(e["posts"]), channels=len(e["channels"]),
            baseline_per_day=round(g["per_day"], 3), sides=dict(sides), post_ids=[str(p["_id"]) for p in e["posts"]],
            examples=[{"summary": (p.get("summary") or "")[:400], "post_url": p.get("post_url"),
                       "side": p.get("source_side")} for p in examples[:EXAMPLES]],
            label="internal" if all((p.get("sensitivity") or "internal") in ("public", "internal")
                                    for p in e["posts"]) else "confidential"))
    out.sort(key=lambda c: -c.momentum)
    return out[:MAX_CANDIDATES]


POST_FIELDS = {"summary": 1, "channel_id": 1, "source_side": 1, "post_url": 1, "sensitivity": 1}
USABLE = {"summary": {"$nin": [None, ""]}, "is_ad": {"$ne": True}, "is_duplicate": {"$ne": True}}


async def scan(db, now: datetime | None = None, labels: tuple[str, ...] = ("public", "internal")) -> list[Candidate]:
    """detect() over the posts collection: the window and the baseline before it (posts within `labels`)."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=WINDOW_HOURS)
    base_start = start - timedelta(days=BASELINE_DAYS)
    allowed = {"$or": [{"sensitivity": {"$in": list(labels)}}, {"sensitivity": {"$exists": False}}]}

    async def read(lo: datetime, hi: datetime) -> list[dict]:
        q = {**USABLE, **allowed, "published_at": {"$gte": _iso(lo), "$lt": _iso(hi)}}
        return [p async for p in db.posts.find(q, POST_FIELDS)]
    return detect(await read(start, now), await read(base_start, start), now)
