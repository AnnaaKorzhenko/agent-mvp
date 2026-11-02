"""Proposals: what an agent suggests to the user (docs/AGENT_PLAN.md §2, §6). Explicit data (goals, followed
topics) never changes by itself: an agent proposes, the user's yes does it.

users.proposals[] (capped, newest kept): {id, kind, key, title, why, action {tool, args}, links, agent, priority,
status, created_at, expires_at, shown_at, shown_count, mentioned_at, answered_at, sensitivity}.
- The same thing (`key`) is proposed once, whatever the answer was: a dismissed goal never comes back.
- Order: by priority (the agent's: the scout's fit, the advisor's news over quiet notes, a goal the user just said
  first; else DEFAULT_PRIORITY by kind), and mixed: the best of each kind first, then the rest, so one agent's
  cards don't crowd out another's.
- At most the user's `cards` (default SHOW_MAX) open proposals go out with a digest; an open one goes stale after
  its days, or once it was shown MAX_SHOWN times without an answer (no nagging). Listing them on request
  (list_open) isn't showing.
- Outside a digest, at most one proposal a day is mentioned in a chat result (to_mention), unless the user turned
  mentions off; a goal they just said in that very message is mentioned at once.
- The user's settings (users.agent_prefs, set_prefs: «менше пропозицій», «не пропонуй теми», «вимкни поради»):
  how many cards, which kinds are off (agents don't even make them), and mentions on or off.
- A yes runs the action (only the tools in ACTIONS), a no just closes it. Either answer is saved as feedback on a
  suggestion, with the user's reason as its comment, so the learning job learns from it like from any rating (a
  «no, I don't want things like this» becomes something to avoid).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from brain.db.models import ProposalDoc
from brain.db.repositories import Repos, to_object_id
from brain.domain import digests, feedback, goals, sensitivity, topics
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext

KINDS = ("goal", "topic", "advice")
# set_goal / track_topic / stop_topic run here; chat is a request for the client model (a deep dive, an analysis):
# the yes hands its message back to be carried out, as if the user had typed it.
ACTIONS = ("set_goal", "track_topic", "stop_topic", "chat")
DEFAULT_PRIORITY = {"advice": 2.5, "topic": 2.0, "goal": 1.5}
JUST_SAID = 5.0             # a goal the user just said: before anything else
OPEN_DAYS = 7
SHOW_MAX = 3
MAX_SHOWN = 3
LIST_MAX = 10
KEEP = 200
MAX_TITLE = 120
MAX_WHY = 500
MAX_LINKS = 3


@dataclass
class Prefs:
    cards: int = SHOW_MAX                 # cards per digest, 0-SHOW_MAX
    off: list[str] = field(default_factory=list)     # kinds the user doesn't want
    mentions: bool = True                 # one suggestion a day in ordinary chat answers


async def prefs_of(repos: Repos, user_id) -> Prefs:
    raw = await repos.users.get_agent_prefs(user_id) or {}
    return Prefs(cards=max(0, min(SHOW_MAX, int(raw.get("cards", SHOW_MAX)))),
                 off=[k for k in raw.get("off") or [] if k in KINDS], mentions=bool(raw.get("mentions", True)))


async def set_prefs(repos: Repos, ctx: UserContext, *, cards: int | None = None, turn_off: list[str] | None = None,
                    turn_on: list[str] | None = None, mentions: bool | None = None) -> Prefs:
    p = await prefs_of(repos, ctx.user_id)
    unknown = [k for k in (turn_off or []) + (turn_on or []) if k not in KINDS]
    if unknown:
        raise InvalidInput(f"kinds must be among {', '.join(KINDS)}; got {unknown}")
    if cards is not None:
        p.cards = max(0, min(SHOW_MAX, int(cards)))
    p.off = [k for k in KINDS if (k in p.off or k in (turn_off or [])) and k not in (turn_on or [])]
    if mentions is not None:
        p.mentions = mentions
    if not await repos.users.set_agent_prefs(ctx.user_id, {"cards": p.cards, "off": p.off, "mentions": p.mentions,
                                                           "updated_at": datetime.now(timezone.utc)}):
        raise NotFound("No profile for this user yet. Start the Telegram bot once to create it.")
    return p


def key_of(kind: str, title: str) -> str:
    return f"{kind}:{' '.join(title.lower().split())}"


async def propose(repos: Repos, user_id, *, kind: str, title: str, why: str = "", action: dict | None = None,
                  agent: str, key: str | None = None, days: int = OPEN_DAYS, label: str = "internal",
                  links: list[str] | None = None, priority: float | None = None) -> str:
    """Adds a proposal. Returns "ok", "duplicate", "off" (the user turned this kind off) or "no_user"."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if action is not None and action.get("tool") not in ACTIONS:
        raise ValueError(f"action tool must be one of {ACTIONS}")
    if kind in (await prefs_of(repos, user_id)).off:
        return "off"
    title = " ".join(title.split())[:MAX_TITLE]
    now = datetime.now(timezone.utc)
    doc = {"id": ObjectId(), "kind": kind, "key": key or key_of(kind, title), "title": title,
           "why": " ".join(why.split())[:MAX_WHY], "action": action, "links": (links or [])[:MAX_LINKS],
           "agent": agent, "priority": priority, "status": "open",
           "created_at": now, "expires_at": now + timedelta(days=days), "shown_count": 0, "sensitivity": label}
    return await repos.users.push_proposal(user_id, doc, KEEP)


def _expired(p: ProposalDoc, now: datetime) -> bool:
    return p.expires_at is not None and topics.utc(p.expires_at) <= now


def priority(p: ProposalDoc) -> float:
    return p.priority if p.priority is not None else DEFAULT_PRIORITY.get(p.kind, 1.0)


def _ranked(found: list[ProposalDoc]) -> list[ProposalDoc]:
    """By priority (then oldest first), the best of each kind before the rest."""
    by_rank = sorted(found, key=lambda p: (-priority(p), topics.utc(p.created_at) or datetime.min.replace(
        tzinfo=timezone.utc)))
    firsts, seen = [], set()
    for p in by_rank:
        if p.kind not in seen:
            firsts.append(p)
            seen.add(p.kind)
    return sorted(firsts, key=lambda p: -priority(p)) + [p for p in by_rank if p not in firsts]


async def _open(repos: Repos, ctx: UserContext, now: datetime, showing: bool) -> list[ProposalDoc]:
    """The open proposals this client may show, of kinds the user wants. Expired ones (and, when showing, ones shown
    MAX_SHOWN times) become stale; goal proposals the user already has as a goal close by themselves."""
    profile = await repos.users.get_profile(ctx.user_id)
    active = {g.text.lower() for g in (profile.goals if profile else []) if g.active}
    off = (await prefs_of(repos, ctx.user_id)).off
    out = []
    for p in await repos.users.get_proposals(ctx.user_id):
        if p.status != "open":
            continue
        oid = to_object_id(p.id)
        if _expired(p, now) or (showing and p.shown_count >= MAX_SHOWN):
            await repos.users.update_proposal(ctx.user_id, oid, {"status": "stale"}, status="open")
            continue
        if p.kind == "goal" and p.title.lower() in active:
            await repos.users.update_proposal(ctx.user_id, oid, {"status": "accepted", "answered_at": now},
                                              status="open")
            continue
        if p.kind not in off and sensitivity.is_allowed(p.sensitivity, ctx.max_sensitivity):
            out.append(p)
    return out


async def _shown(repos: Repos, ctx: UserContext, found: list[ProposalDoc], now: datetime, **fields) -> None:
    for p in found:
        await repos.users.update_proposal(ctx.user_id, to_object_id(p.id), {"shown_at": now, **fields},
                                          inc={"shown_count": 1})


async def open_for(repos: Repos, ctx: UserContext, limit: int = SHOW_MAX, mark_shown: bool = True,
                   now: datetime | None = None) -> list[ProposalDoc]:
    """The proposals to show with a digest: the best, mixed by kind, at most the user's `cards`."""
    now = now or datetime.now(timezone.utc)
    cards = min(limit, (await prefs_of(repos, ctx.user_id)).cards)
    out = _ranked(await _open(repos, ctx, now, showing=mark_shown))[:cards]
    if mark_shown:
        await _shown(repos, ctx, out, now)
    return out


def _today(now: datetime) -> datetime:
    return digests.kyiv_day_bounds(now.astimezone(digests.KYIV).date())[0]


async def to_mention(repos: Repos, ctx: UserContext, now: datetime | None = None,
                     just_said: str | None = None) -> ProposalDoc | None:
    """The one proposal to mention in an ordinary chat answer, or None: at most one a day (whichever it was), the
    best not shown today, unless the user turned mentions off. just_said: the id of a proposal made from this very
    message (a goal they just said): mentioned at once."""
    now = now or datetime.now(timezone.utc)
    if not (await prefs_of(repos, ctx.user_id)).mentions:
        return None
    found = await _open(repos, ctx, now, showing=True)
    pick = next((p for p in found if p.id == just_said), None) if just_said else None
    if pick is None:
        today = _today(now)
        if any(p.mentioned_at and topics.utc(p.mentioned_at) >= today
               for p in await repos.users.get_proposals(ctx.user_id)):
            return None
        pick = next((p for p in _ranked(found) if not (p.shown_at and topics.utc(p.shown_at) >= today)), None)
    if pick is not None:
        await _shown(repos, ctx, [pick], now, mentioned_at=now)
    return pick


async def list_open(repos: Repos, ctx: UserContext) -> list[ProposalDoc]:
    """The open proposals, when the user asks about them («що ти там пропонував?»): newest first, not counted as
    shown."""
    found = await _open(repos, ctx, datetime.now(timezone.utc), showing=False)
    return sorted(found, key=lambda p: topics.utc(p.created_at) or datetime.min.replace(tzinfo=timezone.utc),
                  reverse=True)[:LIST_MAX]


@dataclass
class Answered:
    proposal: ProposalDoc
    accepted: bool
    done: str                 # what the yes did, in Ukrainian, for the confirmation
    ask: str | None = None    # chat: the request to carry out now


async def answer(repos: Repos, ctx: UserContext, proposal_id: str, accept: bool, *,
                 mcp_channels: list[str], reason: str | None = None) -> Answered:
    oid = to_object_id(proposal_id)
    found = next((p for p in await repos.users.get_proposals(ctx.user_id) if oid and to_object_id(p.id) == oid), None)
    if found is None or not sensitivity.is_allowed(found.sensitivity, ctx.max_sensitivity):
        raise NotFound("Proposal not found: pass a proposal_id from the digest's proposals")
    if found.status != "open":
        raise InvalidInput(f"This proposal is already closed ({found.status})")
    done, ask = "закрито", None
    if accept and found.action is not None:
        args = found.action.args
        if found.action.tool == "set_goal":
            try:
                await goals.add_goal(repos, ctx, args.get("text") or found.title)
                done = f"додано ціль «{args.get('text') or found.title}»"
            except InvalidInput as e:
                if "already exists" not in str(e):
                    raise
                done = "така ціль уже є"
        elif found.action.tool == "track_topic":
            r = await topics.track_topic(repos, ctx, args.get("name") or found.title,
                                         specific=args.get("specific"), broader=args.get("broader"),
                                         exclude=args.get("exclude"), tags=args.get("tags"), goal=args.get("goal"),
                                         queries=args.get("queries"))
            done = f"відстежується тема «{r.topic.name}» до {topics.until_text(r.topic)}"
        elif found.action.tool == "stop_topic":
            r = await topics.stop_topic(repos, ctx, args.get("name") or found.title)
            done = f"тему «{r.topic.name}» більше не відстежуємо"
        elif found.action.tool == "chat":
            ask = args.get("message") or found.title
            done = f"запит: {ask}"
    now = datetime.now(timezone.utc)
    if not await repos.users.update_proposal(ctx.user_id, oid, {"status": "accepted" if accept else "dismissed",
                                                                "answered_at": now}, status="open"):
        raise InvalidInput("This proposal was answered meanwhile")
    await feedback.record_feedback(repos, ctx, mcp_channels=mcp_channels, target_type="suggestion",
                                   reaction="useful" if accept else "not_useful",
                                   about=f"пропозиція ({found.kind}): {found.title}", text=reason)
    return Answered(found, accept, done, ask)
