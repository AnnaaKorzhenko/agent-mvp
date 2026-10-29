"""Learning job: turns a user's logged messages, ratings and insights into `users.learned`, which get_profile and
about_user hand to the client model so its answers adapt (docs/LEARNING_PLAN.md).

Per user: collect what's new since the last run (chat messages, feedback, insights), ask the configured model
backend (MODEL_BACKEND: anthropic now, the local model later) to merge it with the previous learned profile, check
the answer against LearnedProfile, and save it. Data above the backend's allowed label is never sent: a cloud
backend gets `public` / `internal` only. Every model call is in audit_log (model_call). Nothing else is changed:
not the bot's interest scores, not the insights.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from pymongo.asynchronous.database import AsyncDatabase

from brain.db.repositories import user_key
from brain.domain import sensitivity
from brain.domain.tag_list import TAG_LIST
from brain.integrations.backends import ModelBackend, ModelResult

logger = logging.getLogger("brain.learning")

MAX_CHAT = 300          # newest chat messages per run
MAX_FEEDBACK = 200
MAX_INSIGHTS = 300
MAX_TEXT = 600          # characters per item sent to the model
MAX_OUTPUT = 8000       # output tokens: goal keywords for several goals are long
MAP_PER_RUN = 10        # goals mapped to keywords per run; more are mapped by the next runs
FAILURES = Path(__file__).resolve().parents[1] / "eval" / "results" / "learn_failures"   # git-ignored
CHAT_LABEL = "internal"         # what the chat, feedback and the learned profile are treated as


class Interest(BaseModel):
    topic: str = Field(max_length=80)
    weight: int = Field(ge=1, le=5)


class GoalMatch(BaseModel):
    """How one active goal shows up in the news, at three levels (brain/domain/relevance.py weighs them)."""
    goal: str = Field(max_length=400, description="The goal's text, copied exactly")
    specific: list[str] = Field(default_factory=list, max_length=20)
    broader: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("tags")
    @classmethod
    def _only_listed(cls, tags: list[str]) -> list[str]:
        return [t for t in tags if t in TAG_LIST]               # theme tags must exist in the pipeline's list

    @field_validator("specific", "broader")
    @classmethod
    def _short(cls, terms: list[str]) -> list[str]:
        return [" ".join(t.split())[:60] for t in terms if t and t.strip()]


class LearnedProfile(BaseModel):
    """What the model returns, and what users.learned stores (plus bookkeeping fields)."""
    summary: str = Field(max_length=700, description="Who the user is and what they need, 2-4 sentences")
    preferences: list[str] = Field(default_factory=list, max_length=12)
    interests: list[Interest] = Field(default_factory=list, max_length=15)
    known: list[str] = Field(default_factory=list, max_length=12)
    avoid: list[str] = Field(default_factory=list, max_length=10)
    goal_candidates: list[str] = Field(default_factory=list, max_length=5)
    goal_keywords: list[GoalMatch] = Field(default_factory=list, max_length=MAP_PER_RUN)


SYSTEM = """You maintain a short profile of one user of a news service about the war in Ukraine and the defense \
sector. The service shows them daily digests and search results from Ukrainian and Russian Telegram channels; \
an AI assistant answers their questions. Your profile is given to that assistant at the start of every chat so \
its answers adapt to the user.

You get the previous profile (may be empty) and the user's new activity: their own chat messages, their ratings \
of news items and answers (with reasons and comments), and things they shared or that were detected about them \
(insights, with a kind). Update the profile:
- Merge new evidence with the previous profile. When statements conflict, the newer one wins.
- preferences: how they want answers and news (length, format, language, what to include or skip).
- interests: topics they care about, weight 1-5 by how strong and repeated the evidence is (ratings and repeated \
questions count more than one mention). Drop topics with only weak, old evidence.
- known: things they said they already know (don't repeat these to them).
- avoid: topics or kinds of news they rated as not useful repeatedly or said they don't want.
- Each rating's "about" is the text of the rated news item; its "comment" refers to that item («this device», \
«this topic» mean what "about" names). A comment asking for more on something specific is strong evidence: add \
an interest named by that specific thing itself (the system, model, company or event from "about", written as \
«назва: що саме цікавить»), weight 4-5, not a general «wants details».
- followed_topics: specific things the user chose to follow in their news (strong, explicit evidence): each \
should be among the interests, named by the topic itself, weight 4-5, while it is followed; drop such an \
interest once the topic is no longer followed unless other evidence keeps it.
- goal_candidates: goals they expressed that are not in their active goals.
- summary: 2-4 sentences: who they are (work, role, organization if stated) and what they need from the service.
- goal_keywords: ONLY for each goal in goals_to_map (copy its text exactly into "goal"); the other active goals \
are mapped already. How news relevant to the goal would be worded. \
The news is written in Ukrainian and Russian, whatever the goal's language. Three levels:
  - specific: names of systems, models, weapons, companies, programs, places, written as Ukrainian and Russian \
news writes them (e.g. «Герань», «Геран», «Шахед», «Brave1»); 5-15 terms;
  - broader: short phrases and concepts covering the goal's whole area, so news that names nothing specific \
still matches (e.g. «ударні дрони», «ударные беспилотники», «протидія дронам», «нова модифікація»); 5-15 terms, \
Ukrainian and Russian forms;
  - tags: 1-4 tags from tag_list (in the data) that this goal's news would carry; only tags from that list.
  Terms are matched on word starts in post summaries: prefer 1-3 word terms; avoid generic words alone \
(«новини», «технології», «Україна»). A specific term must point at the goal by itself: no country, region or \
city names alone, no broad field names («ППО», «дрони»): put those into a broader phrase instead \
(«дрони над аеропортом», «ППО збила нову модифікацію»).
- Only state what the evidence supports; no guesses. Write the texts in Ukrainian, short.
Answer with one JSON object only, no other text. Inside strings never use the " character: write quotes as «…». \
Exactly these keys:
{"summary": str, "preferences": [str], "interests": [{"topic": str, "weight": 1-5}], "known": [str], \
"avoid": [str], "goal_candidates": [str], \
"goal_keywords": [{"goal": str, "specific": [str], "broader": [str], "tags": [str]}]}"""


@dataclass
class Activity:
    user_id: Any
    previous: dict | None
    chat: list[dict] = field(default_factory=list)
    feedback: list[dict] = field(default_factory=list)
    insights: list[dict] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    until: datetime | None = None          # the newest timestamp included: the next run starts after it
    withheld: int = 0                      # items above the backend's label, not sent
    labels: list[str] = field(default_factory=list)    # labels of everything sent
    goals_without_keywords: list[str] = field(default_factory=list)   # active goals the profile doesn't map yet
    # Topics the user follows now: context for every run, not new activity (following one doesn't start a run).
    followed_topics: list[dict] = field(default_factory=list)

    @property
    def label(self) -> str:
        """The payload's (and so the learned profile's) label: the highest of what's sent."""
        return sensitivity.highest(self.labels) if self.labels else "public"

    @property
    def empty(self) -> bool:
        return not (self.chat or self.feedback or self.insights or self.goals_without_keywords)

    def payload(self) -> dict:
        # The previous profile without its goal mappings (long; kept and merged by save()), and only the goals
        # that still need one: each run stays small however many goals the user has.
        previous = {k: v for k, v in (self.previous or {}).items() if k != "goal_keywords"}
        return {"previous_profile": previous, "active_goals": self.goals,
                "goals_to_map": self.goals_without_keywords[:MAP_PER_RUN], "tag_list": list(TAG_LIST),
                "followed_topics": self.followed_topics, "chat_messages": self.chat, "ratings": self.feedback,
                "insights": self.insights}

    def size(self) -> int:
        return len(json.dumps(self.payload(), ensure_ascii=False))


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _cut(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


ABOUT_SHORT = 200       # what a rating without a comment was about: enough to name the topic
NEUTRAL = ("neutral", "skip")


def _about(f: dict, text: str | None) -> str | None:
    """The rated item's text: in full when the user commented or gave reasons (the comment refers to it), shorter
    for a plain 👍 / 👎, left out for 50/50 (says little, costs tokens)."""
    if not text:
        return None
    if f.get("text") or f.get("reasons") or f.get("target_type") in ("answer", "suggestion"):
        return _cut(text)
    if f.get("reaction") in NEUTRAL:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= ABOUT_SHORT else text[:ABOUT_SHORT - 1] + "…"


def _day(dt: datetime | None) -> str | None:
    return dt.date().isoformat() if dt else None


def _newer(dt: datetime | None, since: datetime | None) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return since is None or dt > since


def feedback_stamp(f: dict) -> datetime | None:
    """When a rating last carried news: a comment added later (text_at) makes an old rating new again."""
    stamps = [s if s.tzinfo else s.replace(tzinfo=timezone.utc)
              for s in (f.get("at"), f.get("text_at") if f.get("text") else None) if isinstance(s, datetime)]
    return max(stamps, default=None)


async def _rated_texts(db: AsyncDatabase, fb: list[dict]) -> dict[int, str]:
    """What each rating was about, for ratings that didn't keep a copy (the bot's, and MCP ratings before
    2026-10-29): the post summary if the post still exists, else the digest item's text (digests stay a year)."""
    out: dict[int, str] = {}
    need = [(i, f) for i, f in enumerate(fb) if not f.get("about")]
    ids = list({f["post_id"] for _, f in need if f.get("post_id")})
    summaries = {p["_id"]: p.get("summary") async for p in db.posts.find({"_id": {"$in": ids}}, {"summary": 1})}
    by_item: dict = {}
    for i, f in need:
        if summaries.get(f.get("post_id")):
            out[i] = summaries[f["post_id"]]
        elif f.get("digest_id") is not None and f.get("item_n") is not None:
            by_item[(f["digest_id"], f["item_n"])] = i
    digest_ids = list({d for d, _ in by_item})
    async for d in db.digests.find({"_id": {"$in": digest_ids}}, {"sections.items": 1}):
        c = 0
        for s in d.get("sections") or []:
            for it in s.get("items") or []:
                c += 1
                if (i := by_item.get((d["_id"], it.get("n", c)))) is not None:
                    out[i] = it.get("text") or ""
    return out


async def collect(db: AsyncDatabase, user_id, max_label: str, since: datetime | None = None) -> Activity:
    """Everything new for this user since users.learned.processed_until (or `since`, to reprocess from a date),
    at or below `max_label`."""
    key = user_key(user_id) if not isinstance(user_id, int) else user_id
    user = await db.users.find_one({"_id": key}, {"learned": 1, "goals": 1, "insights": 1,
                                                 "feedback": {"$slice": -1000}, "topics.name": 1,
                                                 "topics.status": 1, "topics.expires_at": 1, "topics.goal": 1,
                                                 "topics.created_at": 1, "topics.origin.text": 1}) or {}
    learned = user.get("learned") or {}
    since = since or learned.get("processed_until")
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    previous = {k: learned[k] for k in LearnedProfile.model_fields if k in learned} or None
    if previous and not sensitivity.is_allowed(learned.get("sensitivity", CHAT_LABEL), max_label):
        previous = None                     # learned from data this backend may not see: start fresh
    act = Activity(user_id=key, previous=previous,
                   goals=[g["text"] for g in user.get("goals") or [] if g.get("active")])
    now = datetime.now(timezone.utc)
    for t in user.get("topics") or []:
        ends = t.get("expires_at")
        if t.get("status") == "active" and ends and (ends if ends.tzinfo else ends.replace(tzinfo=timezone.utc)) > now:
            act.followed_topics.append({k: v for k, v in {
                "name": t.get("name"), "since": _day(t.get("created_at")), "goal": t.get("goal"),
                "started_from": _cut((t.get("origin") or {}).get("text")) or None}.items() if v})
    if previous:
        act.labels.append(learned.get("sensitivity", CHAT_LABEL))
    mapped = {_norm(g.get("goal", "")) for g in (previous or {}).get("goal_keywords") or []}
    act.goals_without_keywords = [g for g in act.goals if _norm(g) not in mapped]
    if act.goals_without_keywords and not act.labels:
        act.labels.append(CHAT_LABEL)
    if act.followed_topics:
        act.labels.append("internal")                  # topics are internal (brain/domain/topics.py)
    stamps: list[datetime] = []

    chat_allowed = sensitivity.is_allowed(CHAT_LABEL, max_label)
    if chat_allowed:
        async for doc in db.conversations.find({"user_id": key}, {"messages": 1}):
            for m in doc.get("messages") or []:
                if m.get("role") == "user" and m.get("via") in (None, "chat", "search_query", "feedback_text",
                                                                 "goal", "insight") \
                        and _newer(m.get("created_at"), since):
                    act.chat.append({"at": m["created_at"], "via": m.get("via") or "telegram",
                                     "text": _cut(m.get("content"))})
        act.chat.sort(key=lambda m: m["at"])
        act.chat = act.chat[-MAX_CHAT:]
        stamps += [m["at"] for m in act.chat]
        for m in act.chat:
            m["at"] = _day(m["at"])

        fb = [f for f in user.get("feedback") or [] if _newer(feedback_stamp(f), since)][-MAX_FEEDBACK:]
        stamps += [feedback_stamp(f) for f in fb]
        looked_up = await _rated_texts(db, fb)
        # The rated posts' current tags (from the fixed list), like brain.scripts.rebuild_tag_scores.
        ids = list({f["post_id"] for f in fb if f.get("post_id")})
        current = {p["_id"]: p.get("tags") or [] async for p in db.posts.find({"_id": {"$in": ids}}, {"tags": 1})}
        if act.chat or fb:
            act.labels.append(CHAT_LABEL)
        act.feedback = [{k: v for k, v in {
            "at": _day(f.get("at")), "target": f.get("target_type") or ("digest_item" if f.get("digest_id") else "post"),
            "reaction": f.get("reaction"), "category": f.get("category"),
            "tags": current.get(f.get("post_id"), f.get("tags") or [])[:6],
            "about": _about(f, f.get("about") or looked_up.get(i)), "reasons": f.get("reasons") or None,
            "comment": _cut(f.get("text")) if f.get("text") else None}.items() if v} for i, f in enumerate(fb)]
    else:
        act.withheld += 1

    for i in user.get("insights") or []:
        if not _newer(i.get("created_at"), since):
            continue
        if not sensitivity.is_allowed(i.get("sensitivity", "internal"), max_label):
            act.withheld += 1
            continue
        act.insights.append({"at": _day(i.get("created_at")), "kind": i.get("kind", "other"),
                             "origin": i.get("origin", "explicit"), "text": _cut(i.get("text"))})
        act.labels.append(i.get("sensitivity", "internal"))
        stamps.append(i["created_at"])
    act.insights = act.insights[-MAX_INSIGHTS:]
    done = learned.get("processed_until")          # a reprocess from an earlier date never moves this back
    if done is not None:
        stamps.append(done)
    act.until = max((s if s.tzinfo else s.replace(tzinfo=timezone.utc) for s in stamps), default=since)
    return act


def _save_failure(user_id, attempt: int, text: str) -> Path:
    FAILURES.mkdir(parents=True, exist_ok=True)
    path = FAILURES / f"{user_id}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}_{attempt}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def parse(text: str) -> LearnedProfile:
    """The model's answer as a LearnedProfile: tolerates ```json fences and text around the object."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the answer")
    return LearnedProfile.model_validate(json.loads(cleaned[start:end + 1]))


@dataclass
class Outcome:
    profile: LearnedProfile
    calls: list[ModelResult]


async def learn(backend: ModelBackend, act: Activity) -> Outcome:
    """Asks the backend for the updated profile; one retry with the error if the answer doesn't parse."""
    label = act.label
    messages = [{"role": "user", "content": "Data (JSON):\n" + json.dumps(act.payload(), ensure_ascii=False,
                                                                         default=str)}]
    calls: list[ModelResult] = []
    for attempt in (1, 2):
        result = await backend.complete(messages, system=SYSTEM, max_tokens=MAX_OUTPUT, sensitivity=label,
                                        user_id=str(act.user_id))
        calls.append(result)
        try:
            return Outcome(parse(result.text), calls)
        except (ValueError, ValidationError) as e:
            cut = result.stop_reason == "max_tokens"
            saved = _save_failure(act.user_id, attempt, result.text)
            problem = (f"the answer was cut off at {MAX_OUTPUT} tokens" if cut else str(e)[:300])
            if attempt == 2:
                raise ValueError(f"the model's answer didn't match the profile format twice ({problem}); "
                                 f"answers saved in {saved.parent}") from e
            hint = ("It was too long: keep every list within its limit and the texts short." if cut else
                    "Check that every string is closed and contains no \" character (use «…»).")
            messages += [{"role": "assistant", "content": result.text},
                         {"role": "user", "content": f"That wasn't valid JSON for the format ({problem}). {hint} "
                                                     "Answer again with only the JSON object."}]
    raise RuntimeError("unreachable")


def merged_goal_keywords(act: Activity, new: list[GoalMatch]) -> list[dict]:
    """Earlier mappings of goals that are still active, plus this run's (which win for the same goal)."""
    active = {_norm(g) for g in act.goals}
    fresh = {_norm(m.goal): m.model_dump() for m in new if _norm(m.goal) in active}
    kept = [m for m in (act.previous or {}).get("goal_keywords") or []
            if _norm(m.get("goal", "")) in active and _norm(m.get("goal", "")) not in fresh]
    return kept + list(fresh.values())


async def save(db: AsyncDatabase, act: Activity, outcome: Outcome) -> None:
    last = outcome.calls[-1]
    profile = outcome.profile.model_dump()
    profile["goal_keywords"] = merged_goal_keywords(act, outcome.profile.goal_keywords)
    doc = {**profile, "processed_until": act.until, "updated_at": datetime.now(timezone.utc),
           "backend": last.backend, "model": last.model, "sensitivity": act.label,
           "input": {"chat": len(act.chat), "feedback": len(act.feedback), "insights": len(act.insights)}}
    await db.users.update_one({"_id": act.user_id}, {"$set": {"learned": doc}})


async def users_with_activity(db: AsyncDatabase) -> list:
    """Users with something the job could learn from: logged conversations, MCP feedback or insights."""
    ids = set(await db.conversations.distinct("user_id", {"session_key": {"$exists": True}}))
    ids |= set(await db.users.distinct("_id", {"$or": [{"insights.0": {"$exists": True}},
                                                     {"feedback.channel": {"$exists": True}}]}))
    return sorted(ids, key=str)
