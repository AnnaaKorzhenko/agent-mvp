"""(Messages are the UI's, in Ukrainian: only the web screen calls this.)

The user edits their own profile on the «Профіль та інтереси» screen: what the learning job wrote about them
(users.learned: summary, preferences, interests, known, avoid) is theirs to change.

An edit is applied to users.learned at once (relevance, the chat and the scout read it from there) and saved as an
explicit insight «Зміна профілю: …» (kind by field), so the learning job, which merges its previous profile with
the new activity, keeps it: its prompt says these are the user's own edits, applied exactly. Without them the
next run could bring a removed item back from older evidence.

Fields: summary (set); preferences, known, avoid, interests (add, edit, remove; by the exact text shown). An added
interest is stated (counts at once, fades slowly) and gets its web searches from the next learning run; a renamed
one keeps its history and loses its searches (they were about the old name).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from brain.db.repositories import Repos
from brain.domain import insights, sensitivity
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext

EDIT_PREFIX = "Зміна профілю:"
# field -> (max items, max length of one, its name in the edit note, insight kind); limits as LearnedProfile's
LISTS = {
    "preferences": (12, 300, "як отримувати новини", "preference"),
    "known": (12, 300, "що я вже знаю", "known"),
    "avoid": (10, 120, "чого уникати", "preference"),
    "interests": (15, 80, "інтереси", "interest"),
}
SUMMARY_MAX = 700
FIELDS = ("summary", *LISTS)
ACTIONS = ("set", "add", "edit", "remove")
STATED_WEIGHT = 4


@dataclass
class Editable:
    """What the screen may edit: the learned profile's fields as stored (interests by topic)."""
    summary: str = ""
    preferences: list[str] | None = None
    known: list[str] | None = None
    avoid: list[str] | None = None
    interests: list[str] | None = None

    def as_dict(self) -> dict:
        return {"summary": self.summary, "preferences": self.preferences or [], "known": self.known or [],
                "avoid": self.avoid or [], "interests": self.interests or []}


def _clean(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise InvalidInput("Порожній текст")
    if len(text) > limit:
        raise InvalidInput(f"Щонайбільше {limit} символів")
    return text


def _same(a: str, b: str) -> bool:
    return insights.normalized(a) == insights.normalized(b)


async def _learned(repos: Repos, ctx: UserContext) -> dict:
    """users.learned, if this client may read it ({} before the first learning run)."""
    learned = await repos.users.get_learned(ctx.user_id) or {}
    if learned and not sensitivity.is_allowed(learned.get("sensitivity", "internal"), ctx.max_sensitivity):
        raise InvalidInput("Профіль містить дані, які тут не показуються; змініть його в локальному клієнті")
    return learned


async def editable(repos: Repos, ctx: UserContext) -> Editable | None:
    """The fields as the screen shows them for editing; None if this client may not read the profile."""
    try:
        learned = await _learned(repos, ctx)
    except InvalidInput:
        return None
    return Editable(summary=learned.get("summary") or "", preferences=list(learned.get("preferences") or []),
                    known=list(learned.get("known") or []), avoid=list(learned.get("avoid") or []),
                    interests=[i.get("topic", "") for i in learned.get("interests") or [] if i.get("topic")])


def _stated(topic: str, now: datetime) -> dict:
    """A new interest the user named: stated, counts at once (interests.merge's bookkeeping)."""
    return {"topic": topic, "weight": STATED_WEIGHT, "origin": "stated", "web_queries": [], "first_seen": now,
            "last_seen": now, "evidence": {"other": 1}, "days": [now.date().isoformat()], "confirmed": True}


async def edit(repos: Repos, ctx: UserContext, field: str, action: str, text: str | None = None,
               old: str | None = None) -> Editable:
    """Applies one edit and returns the fields after it. summary: set; the lists: add (text), edit (old -> text),
    remove (old)."""
    if field not in FIELDS or action not in ACTIONS:
        raise InvalidInput(f"field must be one of {', '.join(FIELDS)}; action one of {', '.join(ACTIONS)}")
    learned = await _learned(repos, ctx)
    now = datetime.now(timezone.utc)
    if field == "summary":
        if action != "set":
            raise InvalidInput("Опис можна лише замінити")
        new = " ".join((text or "").split())
        if len(new) > SUMMARY_MAX:
            raise InvalidInput(f"Щонайбільше {SUMMARY_MAX} символів")
        update, note, kind = {"summary": new}, f"коротко про мене — «{new}»" if new else "коротко про мене — прибрав", \
            "fact"
    else:
        cap, limit, name, kind = LISTS[field]
        items = list(learned.get(field) or [])
        key = (lambda i: i.get("topic", "")) if field == "interests" else (lambda i: i)
        at = next((n for n, i in enumerate(items) if old is not None and _same(key(i), old)), None)
        if action in ("edit", "remove") and at is None:
            raise NotFound("Цього вже немає в профілі (його могли змінити): оновіть сторінку")
        if action in ("add", "edit"):
            text = _clean(text, limit)
            if any(_same(key(i), text) for n, i in enumerate(items) if n != at):
                raise InvalidInput("Це вже є в профілі")
        if action == "add":
            if len(items) >= cap:
                raise InvalidInput(f"Тут щонайбільше {cap} пунктів: спершу приберіть або змініть один")
            items.append(_stated(text, now) if field == "interests" else text)
            note = f"{name} — додав «{text}»"
        elif action == "edit":
            was = key(items[at])
            items[at] = {**items[at], "topic": text, "origin": "stated", "web_queries": [], "last_seen": now,
                         "confirmed": True} if field == "interests" else text
            note = f"{name} — змінив «{was}» на «{text}»"
        elif action == "remove":
            note = f"{name} — прибрав «{key(items.pop(at))}»"
        else:
            raise InvalidInput("Пункт списку можна додати, змінити або прибрати")
        update = {field: items}
    if not learned:
        update["sensitivity"] = "internal"          # the first content of users.learned: the user's own
    if not await repos.users.update_learned(ctx.user_id, update):
        raise NotFound(insights.NO_PROFILE)
    try:
        await insights.save_insight(repos, ctx, f"{EDIT_PREFIX} {note}", kind=kind, origin="explicit")
    except InvalidInput:
        pass                                        # insights full: the edit itself is in users.learned
    if field == "interests":
        from brain.domain import topics             # (topics imports posts, which imports this module's deps)
        await topics.sync_queries(repos)            # a removed or renamed interest's searches leave the record
    return await editable(repos, ctx) or Editable()

