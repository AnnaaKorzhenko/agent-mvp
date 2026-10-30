"""The scout (docs/AGENT_PLAN.md §4 ②, phase 2): new things nobody asked for, from our own posts.

Two agents on the runtime:
- «scout_trends», scope global, every SCOUT_EVERY_HOURS: the rising names in the posts (brain/domain/trends.py,
  code only), then one model call that keeps only specific things worth following (a weapon, a model, a company,
  a unit, a program, an operation, a deal; not places, politicians, media or common words) and names and
  describes each. Candidates reviewed in the last REMEMBER_HOURS aren't sent again: kept ones keep their
  description, dropped ones stay dropped. The snapshot (built from posts only, no user data) is the run's
  output. Then it queues «scout_match» for every user served in the last ACTIVE_DAYS.
- «scout_match», per user, code only, over the trends and the web scout's finds (brain/agents/scout_web.py):
  drops what the user already covers (followed topics, goals' own names,
  interests, the pipeline's keyword records), scores the rest on fit to their goals and interests
  (relevance.score of what the thing is), spread and credibility (Russian channels only: a claim, ×RU_ONLY), and proposes the best
  MAX_NEW at or above FIT_MIN as topics to follow (brain/domain/proposals.py: «Так» = track_topic with the names
  and web queries the model gave). Nothing above the threshold: nothing is proposed.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, ValidationError, field_validator
from pymongo.asynchronous.database import AsyncDatabase

from brain.agents.runs import GLOBAL, AgentRuns
from brain.agents.runtime import Budget, Report, Runtime
from brain.db.repositories import Repos, _public_id
from brain.domain import proposals, relevance, sensitivity, topics, trends
from brain.domain.identity import UserContext
from brain.domain.tag_list import TAG_LIST
from brain.integrations.backends import ModelBackend

logger = logging.getLogger("brain.agents.scout")

TRENDS = "scout_trends"
MATCH = "scout_match"
WEB = "scout_web"                  # brain/agents/scout_web.py
REMEMBER_HOURS = 72
SNAPSHOT_HOURS = 36           # an older snapshot is not matched any more
ACTIVE_DAYS = 30
FIT_MIN = 1.0
RU_ONLY = 0.6
MAX_NEW = 2
OPEN_DAYS = 3                 # a trend is news: its proposal goes stale sooner
MAX_OUTPUT = 6000
KINDS = ("weapon", "system", "model", "company", "unit", "program", "operation", "event", "deal", "other")

SYSTEM = """You review names that suddenly rose in Ukrainian and Russian Telegram news about the war in Ukraine \
and the defense sector. A user of the news service may want to follow some of them in their daily news.

For each candidate (id, spellings found, example summaries) decide whether it is a specific thing worth \
following: a weapon or system, a model or modification, a company or startup, a military unit, a program, an \
operation, a named event or deal. Drop places (cities, regions, countries), people who are only news makers \
(politicians, officials, celebrities), media outlets, organisations everyone knows (ministries, NATO), generic \
terms and common words. When unsure, drop it.

For each kept one:
- name: short, as Ukrainian news writes it;
- what: one line in Ukrainian: what it is and what happened now;
- kind: one of weapon, system, model, company, unit, program, operation, event, deal, other;
- specific: 2-8 spellings of the name only (Ukrainian, Russian, Latin);
- web_queries: 2-3 phrases of 2-6 words that find news on exactly this thing and nothing else, the name in «…»;
- tags: 0-3 tags from tag_list.
Answer with one JSON object only, no other text. Inside strings never use the " character: write quotes as «…». \
Exactly: {"keep": [{"id": int, "name": str, "what": str, "kind": str, "specific": [str], "web_queries": [str], \
"tags": [str]}]}"""


class Kept(BaseModel):
    id: int
    name: str = Field(max_length=80)
    what: str = Field(max_length=300)
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


def _parse(text: str) -> Review:
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the answer")
    return Review.model_validate(json.loads(cleaned[start:end + 1]))


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
    by_key = {k: item for item in prev_kept for k in _keys([item["name"], *item.get("terms", []),
                                                            *item.get("specific", [])])}
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
        result = None
        for attempt in (1, 2):
            call = await backend.complete(messages, system=SYSTEM, max_tokens=MAX_OUTPUT, sensitivity="internal",
                                          user_id=GLOBAL)
            calls.append(call)
            try:
                result = _parse(call.text)
                break
            except (ValueError, ValidationError) as e:
                if attempt == 2:
                    raise ValueError(f"the model's review didn't match the format twice: {str(e)[:200]}") from e
                messages += [{"role": "assistant", "content": call.text},
                             {"role": "user", "content": "That wasn't valid JSON for the format. Answer again with "
                                                         "only the JSON object; no \" inside strings."}]
        chosen = {k.id: k for k in result.keep if 0 <= k.id < len(fresh)}
        for n, c in enumerate(fresh):
            if (k := chosen.get(n)) is None:
                dropped += list(_keys(c.terms))
                continue
            queries = [q for q in (topics.exact(q) for q in k.web_queries) if q and topics.distinctive(q)]
            kept.append({"name": k.name, "what": k.what, "kind": k.kind,
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
    """How well the thing fits the user: their relevance score for what it is (its description and names), not
    for its example posts (they carry other news too, so broad goal phrases would match by chance), and without
    theme tags (nearly every defense item has one, so they'd mark everything)."""
    text = " ".join([item.get("what", ""), item.get("name", ""), *item.get("specific", [])])
    r = prefs.score(side=None, category=None, text=text)
    sides = item.get("sides") or {}
    if sides and set(sides) == {"ru"}:
        r.score = round(r.score * RU_ONLY, 3)
    return r


def _why(item: dict, r: relevance.Relevance) -> str:
    reason = f"; для вас: {r.why[0]}" if r.why else ""
    if item.get("origin") == "web":
        sites = ", ".join(dict.fromkeys(s["domain"] for s in item.get("sources", [])))
        return f"{item.get('what', '')} Знайдено в інтернеті, не в наших каналах: {sites}{reason}"
    sides = item.get("sides") or {}
    who = "лише російські канали (заяви)" if set(sides) == {"ru"} else f"{item.get('channels', 0)} каналів"
    return f"{item.get('what', '')} Нове за 2 доби: {item.get('posts', 0)} дописів, {who}{reason}"


class Scout:
    def __init__(self, db: AsyncDatabase, backend: ModelBackend, runtime: Runtime, every: timedelta,
                 trends_on: bool = True):
        self.db, self.backend, self.runtime = db, backend, runtime
        self.repos = Repos(db)
        self.runs = AgentRuns(db)
        runtime.register(TRENDS, self.find, Budget(runs_per_day=max(1, int(timedelta(days=1) / every)) + 1))
        runtime.register(MATCH, self.match)
        if trends_on:                       # off: only scout_match runs, for the web scout's finds
            runtime.schedule(TRENDS, GLOBAL, every)

    async def find(self, scope, trigger: str) -> Report:
        now = datetime.now(timezone.utc)
        found = await trends.scan(self.db, now)          # public / internal posts: the snapshot goes to cloud clients
        previous = await self.runs.latest_output(TRENDS, newer_than=now - timedelta(hours=REMEMBER_HOURS))
        kept, dropped, calls = await review(self.backend, found, previous)
        users = await active_users(self.db, now)
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
        ctx = UserContext(_public_id(user_key), "cloud", "agent", "internal")
        prefs = await relevance.load(self.repos, ctx)
        followed = await topics.followed(self.repos, ctx)
        records = [q["q"] for rec in (self.repos.sources.KEYWORDS, self.repos.sources.PROFILE_KEYWORDS)
                   for q in await self.repos.sources.get_keyword_queries(rec) or []]
        scored = []
        for item in items:
            if not sensitivity.is_allowed(item.get("label", "internal"), ctx.max_sensitivity):
                continue
            if covered(item, prefs, followed, records):
                continue
            r = fit(item, prefs)
            if r.score >= FIT_MIN:
                scored.append((r.score, item, r))
        scored.sort(key=lambda x: -x[0])
        made = 0
        for _, item, r in scored:
            if made >= MAX_NEW:
                break
            goal = next((w.split(": ", 1)[1].split(" («")[0] for w in r.why if w.startswith("ціль: ")), None)
            args = {"name": item["name"], "specific": item.get("specific") or [item["name"]],
                    "tags": item.get("tags") or [], **({"queries": item["web_queries"]} if item.get("web_queries")
                                                       else {}), **({"goal": goal} if goal else {})}
            status = await proposals.propose(self.repos, user_key, kind="topic", title=item["name"],
                                             why=_why(item, r), agent="scout", days=OPEN_DAYS,
                                             label=item.get("label", "internal"),
                                             links=[x["url"] for x in item.get("sources", [])],
                                             action={"tool": "track_topic", "args": args})
            made += status == "ok"
        return Report(f"{len(items)} trends, {len(scored)} fit, {made} proposed")


async def active_users(db: AsyncDatabase, now: datetime) -> list:
    """Users the MCP server served in the last ACTIVE_DAYS."""
    cursor = db.users.find({"identities.0": {"$exists": True},
                            "last_seen": {"$gte": now - timedelta(days=ACTIVE_DAYS)}}, {"_id": 1})
    return [d["_id"] async for d in cursor]


def scout(db: AsyncDatabase, settings, runtime: Runtime) -> Scout:
    from brain.audit.log import AuditLog
    from brain.integrations.backends import get_backend
    update = {"model_backend": settings.scout_backend or settings.learning_backend or settings.model_backend}
    if settings.scout_model:
        update["anthropic_model"] = settings.scout_model
    backend = get_backend(settings.model_copy(update=update), AuditLog(db))
    return Scout(db, backend, runtime, timedelta(hours=settings.scout_every_hours), trends_on=settings.scout_auto)
