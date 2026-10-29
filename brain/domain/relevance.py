"""Personal relevance: how much a news item matters to this user, and why. Used to order digests (including the
morning delivery), to rank search results, and to tell the client model why an item matters.

Signals, strongest first:
- topics the user follows (users.topics, track_topic): their name or an alias in the item, W_TRACKED each; else
  one of their broader phrases (news on the thing that doesn't name it), W_TRACKED_NEAR; neither when a look-alike
  word of the topic is there;
- active goals (users.goals) and detected goals (insights kind goal). A goal matches at three levels, from the
  learned profile's goal_keywords: specific names (full weight), broader concepts (60%), themes from the fixed tag
  list (35%); and by its own words. Only the best match per goal counts, and `why` names the matched word;
- learned interests with their weights (users.learned, written by the learning job) and detected interests;
- the bot's scores from ratings: category_scores per side, tag_scores;
- negative: what the user avoids (users.learned.avoid) and, slightly, what they already know.
Only insights and learned profiles this client may read are used. Topics match on word starts, like search;
generic words ("знайти", "року", …) are ignored so a goal doesn't match everything.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from brain.db.models import UserProfileDoc
from brain.db.repositories import Repos
from brain.domain import sensitivity
from brain.domain.identity import UserContext

W_CATEGORY = 1.0
W_TAGS = 0.5
W_TOPIC = 1.5            # × weight / 5, per matched topic
W_GOAL = 2.0             # an active goal
W_TRACKED = 3.0          # a followed topic named in the item: above any goal, so it leads «Для вас»
W_TRACKED_NEAR = 1.5     # … or only its area (a broader phrase): still into «Для вас», below the named ones
LEVELS = {"specific": 1.0, "broader": 0.6, "tag": 0.35}        # share of W_GOAL per match level
TOPIC_CAP = 3.0
W_AVOID = -1.5
W_KNOWN = -0.4
MAX_WHY = 3

# Words that say nothing about a topic ("хочу знайти 3 компанії до кінця року"): stems as query_words makes them.
_GENERIC = """знайти знайду хочу хочемо стежити слідкувати цікавить цікавлять цікаво цікава кінця кінець року
років рік вже все знаю знаємо пиши щоб треба потрібно важливо важливі компанії компаній компанія декілька кілька
більше менше дуже нові новини новий робота роботи працюю мене моїй своїх"""


TOPIC_STEM = 5          # shorter than search stems: a topic should match every form («грантами» ~ «гранти»)


def _generic() -> set[str]:
    from brain.domain.posts import _stem                # (posts imports this module)
    return {_stem(w)[:TOPIC_STEM] for w in _GENERIC.split()}


def _stems(text: str) -> list[str]:
    from brain.domain.posts import query_words
    generic = _generic()
    stems = [s[:TOPIC_STEM].rstrip("'") for s in query_words(text)]
    return list(dict.fromkeys(s for s in stems if s not in generic))


@dataclass
class Topic:
    label: str            # shown in `why`, e.g. "ціль: знайти 3 НРК-компанії"
    weight: float
    patterns: list[re.Pattern]
    need: int             # how many of the patterns must match
    group: str = ""       # goals: the goal it belongs to (only the best match per goal counts)
    tag: str | None = None   # a theme-level topic: matches this exact tag instead of words

    def matches(self, haystack: str, tags: set[str] = frozenset()) -> bool:
        if self.tag is not None:
            return self.tag in tags
        return sum(bool(p.search(haystack)) for p in self.patterns) >= self.need


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def _topic(kind: str, text: str, weight: float, label: str | None = None, group: str = "") -> Topic | None:
    from brain.domain.posts import stem_pattern
    stems = _stems(text)
    if not stems:
        return None
    need = len(stems) if len(stems) <= 2 else max(2, math.ceil(len(stems) * 0.4))
    return Topic(label or f"{kind}: {_short(text)}", weight, [re.compile(stem_pattern(s), re.I) for s in stems],
                 need, group)


@dataclass
class Relevance:
    score: float = 0.0
    why: list[str] = field(default_factory=list)


@dataclass
class Interests:
    categories: dict[str, dict[str, float]] = field(default_factory=dict)
    tags: dict[str, float] = field(default_factory=dict)
    goals: list[Topic] = field(default_factory=list)
    topics: list[Topic] = field(default_factory=list)
    avoid: list[Topic] = field(default_factory=list)
    known: list[Topic] = field(default_factory=list)
    tracked: list = field(default_factory=list)           # TopicDoc: the user's followed topics
    near: list[list[Topic]] = field(default_factory=list)  # per tracked topic: its broader phrases as Topics

    @property
    def empty(self) -> bool:
        return not (self.categories or self.tags or self.goals or self.topics or self.avoid or self.known
                    or self.tracked)

    def score(self, *, side: str | None, category: str | None, text: str, tags: list[str] = (),
              category_label: str | None = None) -> Relevance:
        r = Relevance()
        if self.empty:
            return r
        haystack = " ".join([text, *tags]).lower()
        tagset = {t.lower() for t in tags}
        positive: list[tuple[float, str]] = []
        by_cat = self.categories.get(side or "", {})
        if category and by_cat.get(category):
            top = max(abs(v) for v in by_cat.values()) or 1
            part = W_CATEGORY * by_cat[category] / top
            r.score += part
            if part >= 0.5 * W_CATEGORY:
                positive.append((part, f"категорія: {category_label or category}"))
        if tags and self.tags:
            top = max(abs(v) for v in self.tags.values()) or 1
            liked = [(self.tags[t.lower()], t) for t in tags if self.tags.get(t.lower())]
            if liked:
                part = W_TAGS * max(-1.0, min(1.0, sum(v for v, _ in liked) / top))
                r.score += part
                best = max(liked)
                if best[0] >= 0.5 * top and part >= 0.25:           # only a strongly rated tag is a reason
                    positive.append((part, f"тема, яку ви оцінювали: {best[1]}"))
        if self.tracked:
            from brain.domain.topics import matched_term       # (topics imports this module through digests)
            from brain.domain.topics import excluded
            for t, near in zip(self.tracked, self.near):
                if matched_term(t, haystack):
                    r.score += W_TRACKED
                    positive.append((W_TRACKED + 10, f"тема, яку ви відстежуєте: {t.name}"))
                elif near and any(n.matches(haystack, tagset) for n in near) and not excluded(t, haystack):
                    r.score += W_TRACKED_NEAR
                    positive.append((W_TRACKED_NEAR + 10, f"близько до теми «{t.name}»"))
        topic_score = 0.0
        best: dict[str, Topic] = {}                          # per goal: its strongest matching level
        for t in self.goals:
            if (t.group not in best or t.weight > best[t.group].weight) and t.matches(haystack, tagset):
                best[t.group] = t
        for t in best.values():
            topic_score += t.weight
            positive.append((t.weight + 1, t.label))         # goals first among the reasons
        for t in self.topics:
            if t.matches(haystack, tagset):
                part = W_TOPIC * t.weight / 5
                topic_score += part
                positive.append((part, t.label))
        r.score += min(topic_score, TOPIC_CAP)
        r.score += sum(W_AVOID for t in self.avoid if t.matches(haystack, tagset))
        r.score += sum(W_KNOWN for t in self.known if t.matches(haystack, tagset))
        seen: set[str] = set()
        for _, label in sorted(positive, key=lambda x: -x[0]):
            if label not in seen:
                r.why.append(label)
                seen.add(label)
        r.why = r.why[:MAX_WHY]
        r.score = round(r.score, 3)
        return r


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _goal_levels(out: Interests, mappings: list[dict], active: dict[str, str]) -> None:
    """Topics from the learned profile's goal_keywords, for goals that are still active."""
    for m in mappings:
        key = _norm(m.get("goal", ""))
        if key not in active:                              # a removed or renamed goal: its mapping is stale
            continue
        goal = _short(active[key], 50)
        for level in ("specific", "broader"):
            for term in m.get(level) or []:
                t = _topic("ціль", term, W_GOAL * LEVELS[level], label=f"ціль: {goal} («{_short(term, 30)}»)",
                           group=key)
                if t:
                    out.goals.append(t)
        for tag in m.get("tags") or []:
            out.goals.append(Topic(f"ціль: {goal} (тема «{tag}»)", W_GOAL * LEVELS["tag"], [], 0, key,
                                   tag=tag.lower()))


async def load(repos: Repos, ctx: UserContext, profile: UserProfileDoc | None = None) -> Interests:
    """Everything this client may use about the user, read once per request."""
    profile = profile or await repos.users.get_profile(ctx.user_id)
    out = Interests()
    if profile is None:
        return out
    out.categories = {side: scores for side, cats in (profile.interests.category_scores or {}).items()
                      if (scores := {c: float(v) for c, v in cats.items() if v})}
    out.tags = {str(t).lower(): float(v) for t, v in (profile.interests.tag_scores or {}).items() if v}
    seen: set[str] = set()

    def add(bucket: list[Topic], kind: str, text: str, weight: float) -> None:
        key = " ".join(text.lower().split())
        if key in seen or not (t := _topic(kind, text, weight)):
            return
        seen.add(key)
        bucket.append(t)

    active = [g.text for g in profile.goals if g.active]
    for g in active:                                       # the goal's own words, at full weight
        seen.add(_norm(g))
        if t := _topic("ціль", g, W_GOAL, group=_norm(g)):
            out.goals.append(t)
    learned = await repos.users.get_learned(ctx.user_id)
    if learned and sensitivity.is_allowed(learned.get("sensitivity", "internal"), ctx.max_sensitivity):
        _goal_levels(out, learned.get("goal_keywords") or [], {_norm(g): g for g in active})
        for g in learned.get("goal_candidates") or []:
            add(out.topics, "ціль", g, 4)
        for i in learned.get("interests") or []:
            add(out.topics, "інтерес", i.get("topic", ""), float(i.get("weight", 3)))
        for a in learned.get("avoid") or []:
            add(out.avoid, "уникати", a, 5)
        for k in learned.get("known") or []:
            add(out.known, "відомо", k, 5)
    from brain.domain.topics import followed
    out.tracked = await followed(repos, ctx)
    out.near = [[n for b in t.broader if (n := _topic("тема", b, W_TRACKED_NEAR))] for t in out.tracked]
    for i in await repos.users.get_insights(ctx.user_id):
        if not sensitivity.is_allowed(i.sensitivity, ctx.max_sensitivity):
            continue
        if i.kind == "goal":
            add(out.topics, "ціль", i.text, 4)
        elif i.kind == "interest":
            add(out.topics, "інтерес", i.text, 3)
        elif i.kind == "known":
            add(out.known, "відомо", i.text, 5)
    return out
