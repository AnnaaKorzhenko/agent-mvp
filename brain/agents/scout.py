"""The scout (docs/AGENT_PLAN.md §4 ②, phase 2): new things nobody asked for, from our own posts.

Two agents on the runtime:
- «scout_trends», scope global, every SCOUT_EVERY_HOURS: the rising names in the posts (brain/domain/trends.py,
  code only), then one model call that keeps only specific things that are NEW: a thing that just appeared, or a
  new development of one (a new version or modification, a first use, a new capability, a production milestone,
  a new contract). Not established things back in the news (F-16, Мі-8, «Нептун», SpaceX), strike targets
  (refineries, plants), apps, operations, units, places, politicians, media or common words (2026-11-02: the
  director got 15 such cards in a week and accepted none). It names and describes each, with what is new.
  Candidates reviewed in the last REMEMBER_HOURS aren't sent again: kept ones keep their description, dropped
  ones stay dropped (ones reviewed before the novelty check are reviewed again). The snapshot (built from posts only, no user data) is the run's
  output. Then it queues «scout_match» for every user served in the last common.ACTIVE_DAYS.
- «scout_match», per user, code only, over the trends and the web scout's finds (brain/agents/scout_web.py):
  drops what the user already covers (followed topics, goals' own names, interests, the pipeline's keyword
  records), scores the rest on what names something of theirs (relevance `strong`, as «Для вас»: a goal's names or
  phrases, an interest, an intent; not categories or tags) for what the thing is and what is new about it, ×RU_ONLY
  when only Russian channels have it (a claim), and proposes every one at or above FIT_MIN, as many as clear it
  (brain/domain/proposals.py: «Так» = track_topic with the names and web queries the model gave). A kind of thing
  the user keeps saying no to (or letting expire) and never accepts needs more: KIND_PENALTY per net no, at most
  KIND_PENALTY_MAX. The new things that miss the bar on words alone (FP-7, «пройшла бойові випробування», for a
  goal about «нова технологія»: no word in common) get one small model call per user and run, about meaning: does
  it serve one of their goals or interests (copied exactly), and why. A yes is proposed like a word match; each
  thing is asked about once per user (CHECK_REMEMBER_HOURS, kept in the run's output: the things' keys, nothing
  about the user), at most MAX_CHECK per run. Nothing above the bar: nothing is proposed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator
from pymongo.asynchronous.database import AsyncDatabase

from brain.agents import common
from brain.agents.runs import GLOBAL, AgentRuns
from brain.agents.runtime import Budget, Report, Runtime
from brain.db.repositories import Repos
from brain.domain import proposals, relevance, sensitivity, topics, trends
from brain.domain.tag_list import TAG_LIST
from brain.integrations.backends import ModelBackend

logger = logging.getLogger("brain.agents.scout")

TRENDS = "scout_trends"
MATCH = "scout_match"
WEB = "scout_web"                  # brain/agents/scout_web.py
REMEMBER_HOURS = 72
SNAPSHOT_HOURS = 36           # an older snapshot is not matched any more
FIT_MIN = 1.5                 # relevance `strong`: «Для вас»'s bar
RU_ONLY = 0.6
KIND_PENALTY = 0.5            # per net «no» to this kind of thing (a dismissal 1, an expired card 0.5, a yes −2)
KIND_PENALTY_MAX = 2.0
MAX_CHECK = 8                 # new things per user and run that the model is asked about
CHECK_REMEMBER_HOURS = 72
MAX_CHECK_OUTPUT = 1500
MAX_INTERESTS = 8

CHECK = """You decide, for one user of a news service about the war in Ukraine and the defense sector, which new \
things they would want to follow. You get their goals and lasting interests, and new things (id, name, what it \
is, what is new about it).

For each thing that clearly serves one of their goals or interests, by its meaning, not by shared words: its id, \
the goal or interest it serves (copied exactly from the lists), and why in one short line in Ukrainian. Skip the \
others: a thing that is only loosely related, or about another country's interests than the goal names. None is \
a fine answer.
Answer with one JSON object only, no other text. Inside strings never use the " character: write quotes as «…». \
Exactly: {"fits": [{"id": int, "serves": str, "why": str}]}"""


class Fit(BaseModel):
    id: int
    serves: str = Field(max_length=500)
    why: str = Field("", max_length=200)


class Fits(BaseModel):
    fits: list[Fit] = Field(default_factory=list, max_length=MAX_CHECK * 2)
NOVEL = ("new", "development")
OPEN_DAYS = 3                 # a trend is news: its proposal goes stale sooner
MAX_OUTPUT = 6000
KINDS = ("weapon", "system", "model", "company", "unit", "program", "operation", "event", "deal", "other")

SYSTEM = """You review names that suddenly rose in Ukrainian and Russian Telegram news about the war in Ukraine \
and the defense sector. A user of the news service may want to follow some of them in their daily news.

For each candidate (id, spellings found, example summaries) decide whether it is a specific thing worth \
following because something about it is NEW: a weapon or system, a model or modification, a company or startup, \
a program, a contract or deal. Judge novelty from the examples:
- new: the thing itself just appeared (a new weapon, model, startup, program);
- development: an existing thing with something new: a new version or modification, a first combat use, a new \
capability, a production milestone, a new contract or buyer;
- known: an established thing that is simply in the news again (F-16, Мі-8, С-400, «Нептун», SpaceX, Lockheed \
Martin used or mentioned as usual). Drop it.
Also drop: strike targets (refineries, plants, airfields that were hit), apps and websites, military operations, \
military units and groupings, places, people who are only news makers (politicians, officials), media outlets, \
organisations everyone knows, generic terms and common words. When unsure, drop it.

For each kept one:
- name: short, as Ukrainian news writes it;
- what: one line in Ukrainian: what it is and what happened now;
- novelty: new or development;
- new_now: a few words in Ukrainian: what exactly is new (e.g. «перше бойове застосування», «нова модифікація з \
дальністю 450 км», «контракт на 500 ракет»);
- kind: one of weapon, system, model, company, unit, program, operation, event, deal, other;
- specific: 2-8 spellings of the name only (Ukrainian, Russian, Latin);
- web_queries: 2-3 phrases of 2-6 words that find news on exactly this thing and nothing else, the name in «…»;
- tags: 0-3 tags from tag_list.
Answer with one JSON object only, no other text. Inside strings never use the " character: write quotes as «…». \
Exactly: {"keep": [{"id": int, "name": str, "what": str, "novelty": str, "new_now": str, "kind": str, \
"specific": [str], "web_queries": [str], "tags": [str]}]}"""


class Kept(BaseModel):
    id: int
    name: str = Field(max_length=80)
    what: str = Field(max_length=300)
    novelty: str = "known"
    new_now: str = Field("", max_length=160)
    kind: str = "other"
    specific: list[str] = Field(default_factory=list, max_length=8)
    web_queries: list[str] = Field(default_factory=list, max_length=3)
    tags: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("tags")
    @classmethod
    def _listed(cls, v: list[str]) -> list[str]:
        return [t for t in v if t in TAG_LIST]

    @field_validator("specific", "web_queries")
    @classmethod
    def _short(cls, v: list[str]) -> list[str]:
        return [" ".join(t.split())[:120] for t in v if t and t.strip()]

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        return v if v in KINDS else "other"


class Review(BaseModel):
    keep: list[Kept] = Field(default_factory=list)


def _keys(terms: list[str]) -> set[str]:
    return {topics.query_key(t) for t in terms if t}


def _stats(c: trends.Candidate) -> dict:
    return {"posts": c.posts, "channels": c.channels, "rise": None if c.rise == float("inf") else round(c.rise, 1),
            "sides": c.sides, "post_ids": c.post_ids[:20], "examples": c.examples, "label": c.label}


async def review(backend: ModelBackend, found: list[trends.Candidate], previous: dict | None) -> tuple[list[dict], list[str], list]:
    """(kept items for the snapshot, keys of dropped terms, model calls). Only candidates not reviewed recently
    go to the model."""
    prev_kept = previous.get("kept", []) if previous else []
    prev_dropped = set(previous.get("dropped", [])) if previous else set()
    by_key = {k: item for item in prev_kept if item.get("novelty") in NOVEL     # (older ones: reviewed again)
              for k in _keys([item["name"], *item.get("terms", []), *item.get("specific", [])])}
    kept, dropped, fresh = [], [], []
    for c in found:
        ks = _keys(c.terms)
        known = next((by_key[k] for k in ks if k in by_key), None)
        if known is not None:
            kept.append({**known, **_stats(c), "terms": c.terms})
        elif ks & prev_dropped:
            dropped += list(ks)
        elif sensitivity.is_allowed(c.label, backend.max_label):
            fresh.append(c)
    calls = []
    if fresh:
        payload = {"tag_list": list(TAG_LIST), "candidates": [
            {"id": n, "spellings": c.terms, "posts": c.posts, "channels": c.channels,
             "examples": [e["summary"][:300] for e in c.examples]} for n, c in enumerate(fresh)]}
        messages = [{"role": "user", "content": "Data (JSON):\n" + json.dumps(payload, ensure_ascii=False)}]
        result, calls = await common.ask_json(backend, messages, Review, system=SYSTEM, max_tokens=MAX_OUTPUT,
                                              user_id=GLOBAL, what="the model's review")
        chosen = {k.id: k for k in result.keep if 0 <= k.id < len(fresh)}
        for n, c in enumerate(fresh):
            if (k := chosen.get(n)) is None or k.novelty not in NOVEL:
                dropped += list(_keys(c.terms))
                continue
            queries = [q for q in (topics.exact(q) for q in k.web_queries) if q and topics.distinctive(q)]
            kept.append({"name": k.name, "what": k.what, "novelty": k.novelty, "new_now": k.new_now, "kind": k.kind,
                         "specific": list(dict.fromkeys([k.name, *k.specific])), "web_queries": queries,
                         "tags": k.tags, "terms": c.terms, **_stats(c)})
    return kept, sorted(set(dropped) | prev_dropped), calls


def _flat(text: str) -> str:
    return " ".join(str(text or "").lower().replace("«", " ").replace("»", " ").replace('"', " ").split())


def covered(item: dict, prefs: relevance.Interests, followed: list, keyword_queries: list[str]) -> bool:
    """The user already has it: a followed topic, a goal's own names, a named interest, or one of the pipeline's
    keyword searches."""
    names = [item["name"], *item.get("specific", []), *item.get("terms", [])]
    text = " ".join(names)
    if any(topics.matched_term(t, text) for t in followed):
        return True
    low = text.lower()
    named_goal = relevance.W_GOAL * relevance.LEVELS["specific"] - 1e-9
    if any(g.tag is None and g.weight >= named_goal and g.matches(low) for g in prefs.goals):
        return True
    if any(t.matches(low) for t in prefs.topics):
        return True
    flat_names = [f for f in (_flat(n) for n in names) if len(f) >= 3]
    return any(n in _flat(q) for q in keyword_queries for n in flat_names)


def fit(item: dict, prefs: relevance.Interests) -> relevance.Relevance:
    """How well the thing fits the user: what names something of theirs (relevance `strong`) in what the thing is
    and what is new about it (its description, what's new and names), not in its example posts (they carry other
    news too, so broad goal phrases would match by chance); categories and theme tags don't count (nearly every
    defense item has one). Only Russian channels: ×RU_ONLY."""
    text = " ".join([item.get("what", ""), item.get("new_now", ""), item.get("name", ""), *item.get("specific", [])])
    r = prefs.score(side=None, category=None, text=text)
    r.score = r.strong
    sides = item.get("sides") or {}
    if sides and set(sides) == {"ru"}:
        r.score = r.strong = round(r.strong * RU_ONLY, 3)
    return r


def kind_penalties(found: list) -> dict[str, float]:
    """Per kind of thing (weapon, company, …), how much more fit the user's answers ask for: KIND_PENALTY per net
    «no» (a dismissal 1, an expired card 0.5, a yes −2), at most KIND_PENALTY_MAX. From their scout cards."""
    net: dict[str, float] = {}
    for p in found:
        kind = (p.action.args.get("kind") if p.action else None) if p.agent == "scout" else None
        if kind:
            net[kind] = net.get(kind, 0.0) + {"dismissed": 1.0, "stale": 0.5, "accepted": -2.0}.get(p.status, 0.0)
    return {k: min(KIND_PENALTY_MAX, KIND_PENALTY * n) for k, n in net.items() if n > 0}


def _why(item: dict, r: relevance.Relevance) -> str:
    reason = f"; для вас: {r.why[0]}" if r.why else ""
    if item.get("new_now"):
        reason = f"; нове: {item['new_now']}{reason}"
    if item.get("origin") == "web":
        sites = ", ".join(dict.fromkeys(s["domain"] for s in item.get("sources", [])))
        return f"{item.get('what', '')} Знайдено в інтернеті, не в наших каналах: {sites}{reason}"
    sides = item.get("sides") or {}
    who = "лише російські канали (заяви)" if set(sides) == {"ru"} else f"{item.get('channels', 0)} каналів"
    return f"{item.get('what', '')} Нове за 2 доби: {item.get('posts', 0)} дописів, {who}{reason}"


class Scout:
    def __init__(self, db: AsyncDatabase, backend: ModelBackend, runtime: Runtime, every: timedelta,
                 trends_on: bool = True, user_tokens_per_day: int | None = None):
        self.db, self.backend, self.runtime = db, backend, runtime
        self.repos = Repos(db)
        self.runs = AgentRuns(db)
        runtime.register(TRENDS, self.find, Budget(runs_per_day=max(1, int(timedelta(days=1) / every)) + 1))
        runtime.register(MATCH, self.match, Budget(scope_tokens_per_day=user_tokens_per_day))
        if trends_on:                       # off: only scout_match runs, for the web scout's finds
            runtime.schedule(TRENDS, GLOBAL, every)

    async def find(self, scope, trigger: str) -> Report:
        now = datetime.now(timezone.utc)
        found = await trends.scan(self.db, now)          # public / internal posts: the snapshot goes to cloud clients
        previous = await self.runs.latest_output(TRENDS, newer_than=now - timedelta(hours=REMEMBER_HOURS))
        kept, dropped, calls = await review(self.backend, found, previous)
        users = await common.active_users(self.db, now)
        for uid in users:
            await self.runtime.submit(MATCH, uid, "trends")
        return Report(f"{len(found)} rising, {len(kept)} kept; matching {len(users)} user(s)", calls,
                      output={"at": now, "kept": kept, "dropped": dropped})

    async def match(self, user_key, trigger: str) -> Report:
        now = datetime.now(timezone.utc)
        fresh = now - timedelta(hours=SNAPSHOT_HOURS)
        items = []
        for agent in (TRENDS, WEB):                    # our posts' trends, then the web scout's finds
            items += ((await self.runs.latest_output(agent, newer_than=fresh)) or {}).get("kept", [])
        if not items:
            return Report("no fresh trends")
        ctx = common.agent_ctx(user_key)
        prefs = await relevance.load(self.repos, ctx)
        followed = await topics.followed(self.repos, ctx)
        records = [q["q"] for q in await self.repos.sources.get_keyword_queries() or []]
        penalty = kind_penalties(await self.repos.users.get_proposals(ctx.user_id))
        scored, maybe = [], []
        for item in items:
            if not sensitivity.is_allowed(item.get("label", "internal"), ctx.max_sensitivity):
                continue
            if item.get("origin") != "web" and item.get("novelty") not in NOVEL:
                continue                              # reviewed before the novelty check: not proposed
            if covered(item, prefs, followed, records):
                continue
            r = fit(item, prefs)
            bar = FIT_MIN + penalty.get(item.get("kind", "other"), 0.0)
            if r.score >= bar:
                scored.append((r.score, item, r, None))
            elif penalty.get(item.get("kind", "other"), 0.0) < KIND_PENALTY_MAX:
                maybe.append((r.score, item))
        checked, calls = await self._check(ctx, maybe)
        scored += checked
        scored.sort(key=lambda x: -x[0])
        made = 0
        for _, item, r, serves in scored:           # every one that clears the bar: no count limit
            goal = serves or next((w.split(": ", 1)[1].split(" («")[0] for w in r.why if w.startswith("ціль: ")),
                                  None)
            args = {"name": item["name"], "specific": item.get("specific") or [item["name"]],
                    "kind": item.get("kind", "other"), "tags": item.get("tags") or [], **({"queries": item["web_queries"]} if item.get("web_queries")
                                                       else {}), **({"goal": goal} if goal else {})}
            status = await proposals.propose(self.repos, user_key, kind="topic", title=item["name"],
                                             why=_why(item, r), agent="scout", days=OPEN_DAYS,
                                             label=item.get("label", "internal"),
                                             links=[x["url"] for x in item.get("sources", [])],
                                             action={"tool": "track_topic", "args": args}, priority=round(r.score, 2))
            made += status == "ok"
        asked = [item_key(i) for _, i in maybe[:MAX_CHECK]]
        return Report(f"{len(items)} trends, {len(scored)} fit ({len(checked)} by meaning), {made} proposed", calls,
                      output={"checked": asked} if asked else None)

    async def _check(self, ctx, maybe: list) -> tuple[list, list]:
        """The model's word on the new things that missed the bar on words: [(score, item, Relevance, the goal it
        serves or None)] for the yeses, and the calls. Things asked about for this user in the last
        CHECK_REMEMBER_HOURS aren't asked again."""
        if not maybe:
            return [], []
        since = datetime.now(timezone.utc) - timedelta(hours=CHECK_REMEMBER_HOURS)
        seen: set[str] = set()
        async for d in self.runs.col.find({"agent": MATCH, "scope": user_key_of(ctx), "status": "done",
                                           "finished_at": {"$gte": since}, "output.checked": {"$exists": True}},
                                          {"output.checked": 1}):
            seen.update(d["output"]["checked"])
        todo = [i for _, i in sorted(maybe, key=lambda x: -x[0]) if item_key(i) not in seen][:MAX_CHECK]
        maybe[:] = [(0.0, i) for i in todo]          # what is asked about now (remembered in the run's output)
        if not todo:
            return [], []
        profile = await self.repos.users.get_profile(ctx.user_id)
        goals = [g.text for g in (profile.goals if profile else []) if g.active]
        from brain.domain import interests as interests_domain
        likes = [i.get("topic", "") for i, _ in interests_domain.counted(
            await self.repos.users.get_learned(ctx.user_id))][:MAX_INTERESTS]
        if not goals and not likes:
            maybe[:] = []                          # nothing asked: nothing to remember
            return [], []
        payload = {"goals": goals, "interests": likes, "things": [
            {"id": n, "name": i["name"], "what": i.get("what", ""), "new_now": i.get("new_now", "")}
            for n, i in enumerate(todo)]}
        messages = [{"role": "user", "content": "Data (JSON):\n" + json.dumps(payload, ensure_ascii=False)}]
        try:
            answer, calls = await common.ask_json(self.backend, messages, Fits, system=CHECK,
                                                  max_tokens=MAX_CHECK_OUTPUT, user_id=str(ctx.user_id),
                                                  what="the model's goal check")
        except common.BadAnswer as e:              # the word matches still go out
            logger.warning(f"scout goal check for {ctx.user_id}: {e}")
            return [], e.calls
        out, allowed = [], {*goals, *likes}
        for f in answer.fits:
            if not (0 <= f.id < len(todo)) or f.serves not in allowed:
                continue                           # a thing it wasn't given, or a goal the user doesn't have
            label = ("ціль: " if f.serves in goals else "інтерес: ") + relevance._short(f.serves, 50)
            r = relevance.Relevance(FIT_MIN, [f"{label} ({f.why})" if f.why else label], FIT_MIN)
            out.append((FIT_MIN, todo[f.id], r, f.serves if f.serves in goals else None))
        return out, calls


def item_key(item: dict) -> str:
    return topics.query_key(item.get("name", ""))


def user_key_of(ctx) -> object:
    from brain.db.repositories import user_key
    return user_key(ctx.user_id)


def scout(db: AsyncDatabase, settings, runtime: Runtime) -> Scout:
    backend = common.backend_for(db, settings, "scout")
    return Scout(db, backend, runtime, timedelta(hours=settings.scout_every_hours), trends_on=settings.scout_auto,
                 user_tokens_per_day=settings.agent_user_tokens_per_day)
