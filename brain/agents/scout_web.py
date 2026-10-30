"""The web scout (docs/AGENT_PLAN.md phase 3): new things in the users' areas that our channels and keywords
haven't reached, found by searching the web.

Agent «scout_web», scope global, every SCOUT_WEB_EVERY_HOURS:
1. Areas, by code: each active goal's broader phrases (the learned goal_keywords) and each confirmed lasting
   interest of weight ≥ AREA_MIN_WEIGHT, from every user served in the last ACTIVE_DAYS whose profile is internal
   or public. Users with overlapping areas share one search. Only these phrases leave the brain: never a goal's
   own text, an insight or anything about who has the area.
2. Search, one model call per area (the busiest SCOUT_WEB_AREAS areas, while the day's search budget lasts): the
   model searches (at most SEARCHES_PER_AREA) for specific new things of the last days that aren't in the known
   list (today's trends, every followed topic, the keyword records, the last web finds).
3. Check, by code: a source counts only if its URL was among the search results (never one the model wrote),
   with a date within FRESH_DAYS. A find needs two sources on different sites, or one primary source (an official
   site, or the maker's own site). A find our own news already has (a post summary or a digest item of the last
   OURS_DAYS names it) is not new to anyone: dropped. The rest is dropped too.
4. The finds are the run's output (no user data) and scout_match (brain/agents/scout.py) proposes them to the users
   they fit, like the trends, marked as found on the web, with their links.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator
from pymongo.asynchronous.database import AsyncDatabase

from brain.agents import scout
from brain.agents.runs import GLOBAL, AgentRuns
from brain.agents.runtime import Budget, Report, Runtime
from brain.db.repositories import Repos
from brain.domain import interests, topics
from brain.domain.tag_list import TAG_LIST
from brain.integrations.backends import ModelBackend

logger = logging.getLogger("brain.agents.scout_web")

AGENT = "scout_web"
ACTIVE_DAYS = 30
AREA_MIN_WEIGHT = 3.0
PHRASES_PER_GOAL = 5
MERGE_SHARE = 0.5
SEARCHES_PER_AREA = 5
FINDS_PER_AREA = 4
FRESH_DAYS = 10
KNOWN_MAX = 150
OURS_DAYS = 14
# Words that can't tell one thing from another inside a longer name («Helsing AD-1» is told by «Helsing», not by
# «Drone»).
_GENERIC_LATIN = {"drone", "drones", "defense", "defence", "systems", "system", "group", "robotics", "ukraine",
                  "technologies", "technology", "industries", "aerospace", "dynamics", "military", "army", "air",
                  "missile", "interceptor", "strike", "attack", "ground", "vehicle", "unmanned"}
REMEMBER_DAYS = 7
MAX_OUTPUT = 4000
LABELS = ("public", "internal")
PRIMARY_SUFFIXES = (".gov.ua", ".gov", ".mil", ".mil.ua", ".nato.int")

SYSTEM = """You scout the web for a news service about the war in Ukraine and the defense sector. Its users want \
to hear early about specific new things in their areas, before the news channels the service reads pick them up.

You get one area (a few phrases) and the names the service already knows. Search the web (Ukrainian, Russian \
and English sources: defense media, developers and manufacturers, officials, OSINT analysts) for specific new \
things in this area from the last 7 days that are NOT among the known names: a new weapon or system, model or \
modification, company or startup, contract or deal, program, military unit, operation. Not general news, not \
places, not politicians. Prefer fewer, solid finds; none is a fine answer.

For each find (at most 4):
- name: short, as Ukrainian news would write it; what: one line in Ukrainian: what it is and what is new;
- kind: weapon, system, model, company, unit, program, operation, event, deal or other;
- specific: 2-8 spellings of the name (Ukrainian, Russian, Latin);
- web_queries: 2-3 phrases of 2-6 words that find exactly this thing, the name in «…»;
- tags: 0-3 tags from tag_list;
- sources: the pages it is from, [{"url": exact URL from your search results, "date": "YYYY-MM-DD" published}].
Your final message: one JSON object only, no other text. Inside strings never use the " character: write quotes \
as «…». Exactly: {"finds": [{"name": str, "what": str, "kind": str, "specific": [str], "web_queries": [str], \
"tags": [str], "sources": [{"url": str, "date": str}]}]}"""


class Source(BaseModel):
    url: str
    date: str | None = None


class Find(BaseModel):
    name: str = Field(max_length=80)
    what: str = Field(max_length=300)
    kind: str = "other"
    specific: list[str] = Field(default_factory=list, max_length=8)
    web_queries: list[str] = Field(default_factory=list, max_length=3)
    tags: list[str] = Field(default_factory=list, max_length=3)
    sources: list[Source] = Field(default_factory=list, max_length=8)

    @field_validator("tags")
    @classmethod
    def _listed(cls, v: list[str]) -> list[str]:
        return [t for t in v if t in TAG_LIST]

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        return v if v in scout.KINDS else "other"


class Finds(BaseModel):
    finds: list[Find] = Field(default_factory=list, max_length=FINDS_PER_AREA * 2)


def _parse(text: str) -> Finds:
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = cleaned.rfind('{"finds"'), cleaned.rfind("}")
    start = start if start >= 0 else cleaned.find("{")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the answer")
    return Finds.model_validate(json.loads(cleaned[start:end + 1]))


# ---- areas ----------------------------------------------------------------------------------------------

def areas_of(learned: dict | None, goals: list[str], now: datetime | None = None) -> list[list[str]]:
    """One user's areas: per active goal its broader phrases (else its specific names), and their strong lasting
    interests. Never the goals' own text."""
    learned = learned or {}
    active = {" ".join(g.lower().split()) for g in goals}
    out = []
    for m in learned.get("goal_keywords") or []:
        if " ".join(str(m.get("goal", "")).lower().split()) not in active:
            continue
        phrases = (m.get("broader") or [])[:PHRASES_PER_GOAL] or (m.get("specific") or [])[:3]
        if phrases:
            out.append(phrases)
    for i, w in interests.counted(learned, now):
        if w >= AREA_MIN_WEIGHT and i.get("topic"):
            out.append([str(i["topic"])[:80]])
    return out


def merge_areas(per_user: list[list[list[str]]], limit: int) -> list[dict]:
    """All users' areas, overlapping ones merged (by their phrases' spelling-insensitive keys); the ones most
    users share first. [{phrases, users}]."""
    merged: list[dict] = []
    for areas in per_user:
        counted_here: set[int] = set()
        for phrases in areas:
            keys = {topics.query_key(p) for p in phrases if p}
            if not keys:
                continue
            home = next((n for n, a in enumerate(merged)
                         if len(keys & a["keys"]) >= MERGE_SHARE * min(len(keys), len(a["keys"]))), None)
            if home is None:
                merged.append({"keys": set(keys), "phrases": list(dict.fromkeys(phrases)), "users": 0})
                home = len(merged) - 1
            else:
                a = merged[home]
                a["keys"] |= keys
                a["phrases"] = list(dict.fromkeys(a["phrases"] + phrases))[:8]
            if home not in counted_here:
                merged[home]["users"] += 1
                counted_here.add(home)
    merged.sort(key=lambda a: (-a["users"], a["phrases"][0].lower()))
    return [{"phrases": a["phrases"], "users": a["users"]} for a in merged[:limit]]


# ---- checking the sources -------------------------------------------------------------------------------

def _norm_url(url: str) -> str:
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    return f"{u.netloc.lower().removeprefix('www.')}{u.path.rstrip('/')}" + (f"?{u.query}" if u.query else "")


def _domain(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


_PAGE_AGE = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y")


def _date(text: str | None) -> datetime | None:
    for fmt in _PAGE_AGE:
        try:
            return datetime.strptime((text or "").strip()[:20], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _primary(domain: str, find: Find) -> bool:
    if domain.endswith(PRIMARY_SUFFIXES):
        return True
    words = {w.lower() for n in [find.name, *find.specific] for w in re.findall(r"[A-Za-z0-9]{4,}", n)}
    return any(w in domain.replace("-", "") for w in words)


def verify(find: Find, results: tuple | list, now: datetime) -> list[dict] | None:
    """The find's sources that were really among the search results and are recent, or None if that's not
    enough: two sites, or one primary site."""
    seen = {_norm_url(r["url"]): r for r in results}
    ok = []
    for s in find.sources:
        r = seen.get(_norm_url(s.url))
        if r is None:
            continue                                   # not a search result: the model wrote it
        when = _date(s.date) or _date(r.get("page_age"))
        if when is None or not (now - timedelta(days=FRESH_DAYS) <= when <= now + timedelta(days=1)):
            continue
        ok.append({"url": r["url"], "title": r.get("title") or "", "date": when.date().isoformat(),
                   "domain": _domain(r["url"])})
    domains = {s["domain"] for s in ok}
    if len(domains) >= 2 or any(_primary(d, find) for d in domains):
        return ok
    return None


def name_patterns(names: list[str]) -> list[str]:
    """Regexes that find a thing in our news: each full spelling (5+ characters), and its telling parts: a code
    with a digit («AD-1», «Х-101») or a capitalized Latin name word of 4+ letters that isn't generic («Helsing»)."""
    out: list[str] = []
    for n in names:
        n = " ".join(str(n).replace("«", " ").replace("»", " ").replace('"', " ").split())
        if len(n) >= 5:
            out.append(re.escape(n))
        for code in re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]+-?\d+[A-Za-z]*", n):
            if len(code) >= 3:
                out.append(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![0-9])")
        for word in re.findall(r"\b[A-Z][A-Za-z]{3,}\b", n):
            if word.lower() not in _GENERIC_LATIN:
                out.append(rf"\b{re.escape(word)}\b")
    return list(dict.fromkeys(out))


async def in_our_news(db: AsyncDatabase, names: list[str], now: datetime) -> bool:
    """Whether a post summary or a digest item of the last OURS_DAYS already names the thing."""
    patterns = name_patterns(names)
    if not patterns:
        return False
    rx = {"$regex": "|".join(f"(?:{p})" for p in patterns), "$options": "i"}
    since = now - timedelta(days=OURS_DAYS)
    if await db.posts.find_one({"published_at": {"$gte": since.isoformat()}, "summary": rx}, {"_id": 1}):
        return True
    return bool(await db.digests.find_one({"created_at": {"$gte": since}, "sections.items.text": rx}, {"_id": 1}))


def _flat(text: str) -> str:
    return " ".join(re.sub(r"[«»\"'’.,:;()]", " ", str(text).lower()).split())


def is_known(names: list[str], known_flat: list[str]) -> bool:
    """A name of the find is a known name, or a whole word run inside a known phrase («Мураха» in «Мураха робот»)."""
    for n in (_flat(x) for x in names):
        if len(n) >= 3 and any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", k) for k in known_flat):
            return True
    return False


# ---- the agent ------------------------------------------------------------------------------------------

class WebScout:
    def __init__(self, db: AsyncDatabase, backend: ModelBackend, runtime: Runtime, every: timedelta, *,
                 searches_per_day: int, max_areas: int):
        self.db, self.backend, self.runtime = db, backend, runtime
        self.repos = Repos(db)
        self.runs = AgentRuns(db)
        self.searches_per_day, self.max_areas = searches_per_day, max_areas
        runtime.register(AGENT, self.find, Budget(runs_per_day=max(1, int(timedelta(days=1) / every)) + 1,
                                                  web_searches_per_day=searches_per_day))
        runtime.schedule(AGENT, GLOBAL, every)

    async def _users(self, now: datetime) -> list[dict]:
        cursor = self.db.users.find({"identities.0": {"$exists": True},
                                     "last_seen": {"$gte": now - timedelta(days=ACTIVE_DAYS)}},
                                    {"goals": 1, "learned": 1})
        return [u async for u in cursor]

    async def known(self, now: datetime) -> list[str]:
        names: list[str] = []
        trends = await self.runs.latest_output(scout.TRENDS, newer_than=now - timedelta(hours=scout.SNAPSHOT_HOURS))
        names += [i["name"] for i in (trends or {}).get("kept", [])]
        last = await self.runs.latest_output(AGENT, newer_than=now - timedelta(days=REMEMBER_DAYS))
        names += [i["name"] for i in (last or {}).get("kept", [])]
        names += [t.name for t in await self.repos.users.all_active_topics(now)]
        for rec in (self.repos.sources.KEYWORDS, self.repos.sources.PROFILE_KEYWORDS):
            names += [q["q"].replace('"', "") for q in await self.repos.sources.get_keyword_queries(rec) or []]
        return list(dict.fromkeys(n for n in names if n))[:KNOWN_MAX]

    async def areas(self, now: datetime) -> list[dict]:
        users = await self._users(now)
        per_user = [areas_of(u.get("learned"), [g["text"] for g in u.get("goals") or [] if g.get("active")], now)
                    for u in users if (u.get("learned") or {}).get("sensitivity", "internal") in LABELS]
        return merge_areas(per_user, self.max_areas)

    async def search_areas(self, areas: list[dict], now: datetime, searches_left: int) -> tuple[list, int, int, list]:
        """(kept finds, dropped count, areas searched, model calls): the search and the checks, no writes. Dropped:
        unverified sources, a name we know, or a thing our own news already has."""
        known = await self.known(now)
        known_flat = [_flat(n) for n in known]
        calls, kept, dropped, searched = [], [], 0, 0
        for area in areas:
            if searches_left <= 0:
                break
            payload = {"area": area["phrases"], "known_names": known, "tag_list": list(TAG_LIST),
                       "today": now.date().isoformat()}
            call = await self.backend.search(
                [{"role": "user", "content": "Data (JSON):\n" + json.dumps(payload, ensure_ascii=False)}],
                system=SYSTEM, max_tokens=MAX_OUTPUT, max_uses=min(SEARCHES_PER_AREA, searches_left),
                sensitivity="internal", user_id=GLOBAL)
            calls.append(call)
            searches_left -= call.web_searches
            searched += 1
            try:
                finds = _parse(call.text).finds[:FINDS_PER_AREA]
            except (ValueError, ValidationError) as e:
                logger.warning(f"web scout: an area's answer didn't parse ({str(e)[:120]})")
                continue
            for f in finds:
                sources = verify(f, call.sources, now)
                names = [f.name, *f.specific]
                if sources is None or is_known(names, known_flat) or await in_our_news(self.db, names, now):
                    dropped += 1
                    continue
                known_flat += [_flat(n) for n in [f.name, *f.specific]]
                queries = [q for q in (topics.exact(q) for q in f.web_queries) if q and topics.distinctive(q)]
                kept.append({"name": f.name, "what": f.what, "kind": f.kind,
                             "specific": list(dict.fromkeys([f.name, *f.specific])), "web_queries": queries,
                             "tags": f.tags, "terms": [f.name], "origin": "web", "sources": sources,
                             "channels": len({s["domain"] for s in sources}), "posts": 0, "sides": {},
                             "examples": [], "label": "internal", "area": area["phrases"]})
        return kept, dropped, searched, calls

    async def find(self, scope, trigger: str) -> Report:
        now = datetime.now(timezone.utc)
        areas = await self.areas(now)
        if not areas:
            return Report("no areas: no active users with goals or strong interests")
        used = (await self.runs.usage(now, agent=AGENT)).web_searches
        kept, dropped, searched, calls = await self.search_areas(areas, now, self.searches_per_day - used)
        found_users = await scout.active_users(self.db, now)
        if kept:
            for uid in found_users:
                await self.runtime.submit(scout.MATCH, uid, "web")
        return Report(f"{len(areas)} area(s), {searched} searched, {len(kept)} found, {dropped} dropped"
                      + (f"; matching {len(found_users)} user(s)" if kept else ""), calls,
                      output={"at": now, "kept": kept, "areas": [a["phrases"] for a in areas]})


def web_scout(db: AsyncDatabase, settings, runtime: Runtime) -> WebScout | None:
    """The web scout, if its backend can search (only the anthropic backend can)."""
    from brain.audit.log import AuditLog
    from brain.integrations.backends import get_backend
    update = {"model_backend": "anthropic"}
    model = settings.scout_web_model or settings.scout_model
    if model:
        update["anthropic_model"] = model
    backend = get_backend(settings.model_copy(update=update), AuditLog(db))
    return WebScout(db, backend, runtime, timedelta(hours=settings.scout_web_every_hours),
                    searches_per_day=settings.scout_web_searches_per_day, max_areas=settings.scout_web_areas)
