"""Latest daily digest per side, ordered for the user exactly as the Telegram bot orders it.

Order: today's morning-picker choice (users.daily.selected, only if daily.date is today in Kyiv) first,
then the rest by category_scores; categories without a score keep the digest's own order.
Each item carries the highest sensitivity of the digest and its source posts; items above the
caller's label are removed before anything is returned.

Topics and interests: the pipeline searches the web for every user's followed topics and lasting interests (one
sources record, brain/domain/topics.sync_queries) and builds one digest for all users: side "interests" (since
2026-10-31; earlier trigger "topics"), its sections per search. Each user sees only the items that name one of
their own followed topics, else one of their lasting interests (its names), regrouped by topic or interest
(«⭐ Спектр-К»), each story once (the pipeline repeats a story under every search it matched), before the ua and
ru digests. It never counts as a side's digest, and the morning delivery doesn't wait for it.

News sides: ua, ru and world. Since 2026-11-11 the pipeline writes one document per run with every side in it
(side "news", sides.<side>); the repository gives each side as a digest of its own, id "<_id>-<side>".
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
TOPICS = "topics"            # the result's `side` for the topics-and-interests digest (and its old trigger)
INTERESTS_SIDE = "interests"  # the pipeline's side for it
SIDES = ("ua", "ru", "world")  # the news sides, in the order shown (world: foreign sources, since 2026-11-11)
REQUIRED_SIDES = ("ua", "ru")  # the morning delivery waits for these; world comes when it comes


def is_topics_digest(d: DigestDoc) -> bool:
    return d.side == INTERESTS_SIDE or d.trigger == TOPICS
TOPICS_NOTE = ("Found for the topics the user follows and their lasting interests, partly on the web. Items marked "
               "as Russian-source claims are claims, not facts.")


@dataclass
class DigestView:
    digest: DigestDoc
    sections: list[tuple[DigestSectionDoc, list[DigestItemDoc]]]
    hidden_items: int = 0
    side_note: str | None = None
    numbers: dict[int, int] = field(default_factory=dict)     # id(item) -> item number (see item_number)
    relevance: dict[int, "Relevance"] = field(default_factory=dict)     # id(item) -> personal relevance
    kind: str | None = None                                   # TOPICS for the tracked-topics digest
    claims: set[int] = field(default_factory=set)             # id(item) of Russian-source items (topics digest)

    @property
    def side(self) -> str:
        return self.kind or self.digest.side

    def claim(self, item: DigestItemDoc) -> bool:
        return self.digest.side == "ru" if self.kind is None else id(item) in self.claims

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
    """both: the user's tracked-topics items first (if any), then ua, ru and world (world only when the pipeline
    made one: it is never missing); ua / ru / world: that side only; topics: the tracked-topics items only."""
    both = side in ("both", "all", None)
    sides = list(SIDES) if both else [] if side == TOPICS else [side]
    if any(s not in SIDES for s in sides):
        raise InvalidInput("side must be 'ua', 'ru', 'world', 'topics' or 'both'")
    wanted = parse_date(day)
    start, end = kyiv_day_bounds(wanted) if wanted else (None, None)
    profile = await repos.users.get_profile(ctx.user_id)
    prefs = await relevance.load(repos, ctx, profile)
    result = DigestResultView(date=wanted.isoformat() if wanted else None)
    # The most specific first: a name inside an interest («Рассвет») before a whole interest sentence.
    interests = sorted((t for t in prefs.topics if t.label.startswith(INTEREST_LABEL)), key=lambda t: len(t.patterns))
    if (both or side == TOPICS) and (prefs.tracked or interests):
        digest = await repos.digests.latest_topics(start, end)
        if digest is not None and sensitivity.is_allowed(digest.sensitivity, ctx.max_sensitivity):
            if view := await _topics_view(repos, ctx, digest, prefs.tracked, interests):
                result.digests.append(view)
    for s in sides:
        digest = await repos.digests.latest(s, "day", start, end)
        if digest is None or not sensitivity.is_allowed(digest.sensitivity, ctx.max_sensitivity):
            if s in REQUIRED_SIDES or not both:
                result.missing_sides.append(s)
            continue
        result.digests.append(await _view(repos, ctx, digest, profile, prefs))
    if result.date is None and result.digests:
        result.date = result.digests[0].digest.created_at.astimezone(KYIV).date().isoformat()
    return result


SEEN_MAX = 500                # digest item keys remembered per user and Kyiv day (users.digest_seen)


def seen_key(digest_id: str, n: int) -> str:
    return f"{digest_id}:{n}"


async def mark_seen(repos: Repos, ctx: UserContext, res: DigestResultView, today: date | None = None) -> int:
    """Remembers the returned items of today's (Kyiv) digests as shown to the user, in any channel; a past day's
    digest marks nothing. Returns how many keys were sent."""
    today = today or kyiv_today()
    keys = [seen_key(v.digest.id, v.number(i)) for v in res.digests
            if v.digest.created_at.astimezone(KYIV).date() == today
            for _, items in v.sections for i in items]
    if keys:
        await repos.users.mark_digest_seen(ctx.user_id, today.isoformat(), keys, SEEN_MAX)
    return len(keys)


ALL_SEEN_NOTE = "Усе з сьогоднішнього дайджесту ви вже бачили."


def unseen_day(day: str | None, today: date | None = None) -> str:
    """The date for an unseen-only request: today (Kyiv) only, the default."""
    today = today or kyiv_today()
    wanted = parse_date(day)
    if wanted is not None and wanted != today:
        raise InvalidInput("unseen_only works for today's digest only: omit date or pass today's")
    return today.isoformat()


async def drop_seen(repos: Repos, ctx: UserContext, res: DigestResultView) -> None:
    """Keeps only the items the user wasn't shown today, in the same order and sections; empty sections and
    digests are dropped."""
    seen = await repos.users.digest_seen(ctx.user_id, res.date or kyiv_today().isoformat())
    for v in res.digests:
        v.sections = [(s, kept) for s, items in v.sections
                      if (kept := [i for i in items if seen_key(v.digest.id, v.number(i)) not in seen])]
    res.digests = [v for v in res.digests if v.sections]


MORNING_WAIT_UNTIL = 12      # Kyiv hour: until then, wait for both sides before delivering


@dataclass
class Morning:
    status: str                   # "new" | "not_ready" | "already_sent"
    result: DigestResultView | None = None


async def morning_ready(repos: Repos, ctx: UserContext, now: datetime) -> tuple[DigestResultView, str | None]:
    """Today's digests and their delivery key, or no key while they aren't ready: before MORNING_WAIT_UNTIL
    both sides must be in, so the user gets one complete delivery. `now` in Kyiv."""
    res = await get_digests(repos, ctx, "both", now.date().isoformat())
    if not res.digests or (res.missing_sides and now.hour < MORNING_WAIT_UNTIL):
        return res, None
    # The key is the sides' digests: a topics digest arriving later doesn't make the delivery new again.
    return res, ",".join(sorted(v.digest.id for v in res.digests if v.kind is None) or [res.digests[0].digest.id])


async def morning_digest(repos: Repos, ctx: UserContext, now: datetime | None = None) -> Morning:
    """Today's digest for the scheduled morning delivery, once per user: new only if it wasn't delivered
    before (morning_ready decides when it is ready)."""
    now = (now or datetime.now(KYIV)).astimezone(KYIV)
    res, key = await morning_ready(repos, ctx, now)
    if key is None:
        return Morning("not_ready", res)
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


INTEREST_LABEL = "інтерес: "           # relevance's label of a learned interest's Topic
# Two items are one story (the pipeline repeats a story under every search it matched, reworded) when they share
# SAME_STORY of the shorter one's words, or SAME_STORY_NAMED of them plus two proper names or numbers that aren't
# the topic's own (measured 2026-11-02: repeats share 0.30-0.83 of their words, different «Рассвет» stories up to
# 0.44, but those share only «Рассвет» itself).
SAME_STORY = 0.5
SAME_STORY_NAMED = 0.3


def _words(text: str) -> set[str]:
    """The text's words of 4+ letters, cut to 5: «Білорусь» ~ «Білорусі»."""
    import re
    return {w[:5] for w in re.findall(r"[^\W\d_]{4,}", text.lower())}


def _names(text: str) -> set[str]:
    """Proper names (capitalized, not starting a sentence) and numbers of 2+ digits; names cut to 4 letters, as
    their case endings start at the 5th («Ткач» ~ «Ткача»)."""
    import re
    out = {m.group(1).lower()[:4] for m in re.finditer(r"(?<=[\w,;:)»\"] )([A-ZА-ЯІЇЄҐЁ][\w'’-]{2,})", text)}
    return out | set(re.findall(r"\d{2,}", text))


def _same_story(a: tuple[set, set], b: tuple[set, set], own: set[str]) -> bool:
    (wa, na), (wb, nb) = a, b
    if not (wa and wb):
        return False
    share = len(wa & wb) / min(len(wa), len(wb))
    return share >= SAME_STORY or (share >= SAME_STORY_NAMED and len((na & nb) - own) >= 2)


async def _topics_view(repos: Repos, ctx: UserContext, digest: DigestDoc, tracked: list,
                       interests: list | None = None) -> DigestView | None:
    """The topics-and-interests digest as this user sees it: only items naming one of their followed topics (in the
    item or its source posts), else one of their lasting interests; one section per topic, in the order they follow
    them, then per interest. A story the pipeline put under several of its searches is shown once. None if nothing
    is theirs."""
    from brain.domain.topics import matched_term                 # (topics imports this module)
    items = [it for sec in digest.sections for it in sec.items]
    posts = await _source_posts(repos, items)
    labels = {pid: p.sensitivity for pid, p in posts.items()}
    view = DigestView(digest, [], side_note=TOPICS_NOTE, kind=TOPICS)
    view.numbers = {id(it): item_number(it, pos) for pos, it in enumerate(items, 1)}
    groups: dict[str, list[DigestItemDoc]] = {}
    titles: dict[str, str] = {t.norm: t.name for t in tracked}
    seen_posts: set[str] = set()
    seen: dict[str, list[tuple[set, set]]] = {}           # per section: its stories' words and names
    for item in items:
        if not _item_allowed(digest, item, labels, ctx):
            view.hidden_items += 1
            continue
        if seen_posts & set(item.source_post_ids):
            continue                            # the same story under another search
        sources = [posts[p] for p in item.source_post_ids if p in posts]
        haystack = " ".join([item.text, *(p.summary for p in sources)])
        topic = next((t for t in tracked if matched_term(t, haystack)), None)
        if topic is not None:
            key, why = topic.norm, Relevance(relevance.W_TRACKED, [f"тема, яку ви відстежуєте: {topic.name}"],
                                             relevance.W_TRACKED)
            own = {w for n in [topic.name, *topic.specific] for w in _words(n) | _names(" " + n)}
        else:
            interest = next((t for t in interests or [] if t.matches(haystack.lower())), None)
            if interest is None:                # another user's: not theirs to see, not "hidden"
                continue
            name = interest.label[len(INTEREST_LABEL):]
            key = f"interest:{name.lower()}"
            titles[key] = name
            weight = relevance.W_TOPIC * interest.weight / 5
            why = Relevance(weight, [interest.label], weight)
            own = _words(name) | _names(" " + name)
        story = (_words(item.text), _names(item.text))
        if any(_same_story(story, other, own) for other in seen.get(key, [])):
            continue                            # the same story, reworded, under another search
        seen.setdefault(key, []).append(story)
        seen_posts.update(item.source_post_ids)
        groups.setdefault(key, []).append(item)
        view.relevance[id(item)] = why
        if any(p.source_side == "ru" for p in sources):
            view.claims.add(id(item))
    order = [t.norm for t in tracked] + [k for k in groups if k.startswith("interest:")]
    view.sections = [(DigestSectionDoc(category=f"topic:{k}", title=f"⭐ {titles[k]}"), groups[k])
                     for k in order if k in groups]
    return view if view.sections else None


FOR_YOU = DigestSectionDoc(category="for_you", title="⭐ Для вас")
# «Для вас» needs this much of the score that names something of the user's (relevance `strong`): a goal's own name
# (2.0), a followed topic (3.0) or its area (1.5), an intent (1.5), a strong interest (weight 5: 1.5), or broader
# phrases of two goals. One broad phrase (1.2), theme tags, categories and rated tags alone only order the sections.
FOR_YOU_MIN = 1.5
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
    picked = sorted((it for _, kept in sections for it in kept if view.why(it).strong >= FOR_YOU_MIN),
                    key=lambda it: -view.why(it).score)[:FOR_YOU_MAX]
    if picked:
        taken = {id(it) for it in picked}
        sections = [(FOR_YOU, picked)] + [(s, [it for it in kept if id(it) not in taken]) for s, kept in sections]
        sections = [(s, kept) for s, kept in sections if kept]
    view.sections = sections
    return view
