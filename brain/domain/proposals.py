"""Proposals: what an agent suggests to the user (docs/AGENT_PLAN.md §2, §6). Explicit data (goals, followed
topics) never changes by itself: an agent proposes, the user's yes does it.

users.proposals[] (capped, newest kept): {id, kind, key, title, why, action {tool, args}, links, agent, status,
created_at, expires_at, shown_at, shown_count, answered_at, sensitivity}.
- The same thing (`key`) is proposed once, whatever the answer was: a dismissed goal never comes back.
- At most SHOW_MAX open proposals go out with a digest; an open one goes stale after its days, or once it was
  shown MAX_SHOWN times without an answer (no nagging). Listing them on request (list_open) isn't showing.
- Outside a digest, one open proposal not shown today may be mentioned in a chat result (open_for with
  not_shown_since): the user sees the agents' work even if he never opens the digest.
- A yes runs the action (only the tools in ACTIONS), a no just closes it. Either answer is saved as feedback on a
  suggestion, with the user's reason as its comment, so the learning job learns from it like from any rating (a
  «no, I don't want things like this» becomes something to avoid).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from brain.db.models import ProposalDoc
from brain.db.repositories import Repos, to_object_id
from brain.domain import feedback, goals, sensitivity, topics
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext

KINDS = ("goal", "topic", "advice")
# set_goal / track_topic / stop_topic run here; chat is a request for the client model (a deep dive, an analysis):
# the yes hands its message back to be carried out, as if the user had typed it.
ACTIONS = ("set_goal", "track_topic", "stop_topic", "chat")
OPEN_DAYS = 7
SHOW_MAX = 3
MAX_SHOWN = 3
LIST_MAX = 10
KEEP = 200
MAX_TITLE = 120
MAX_WHY = 500
MAX_LINKS = 3


def key_of(kind: str, title: str) -> str:
    return f"{kind}:{' '.join(title.lower().split())}"


async def propose(repos: Repos, user_id, *, kind: str, title: str, why: str = "", action: dict | None = None,
                  agent: str, key: str | None = None, days: int = OPEN_DAYS, label: str = "internal",
                  links: list[str] | None = None) -> str:
    """Adds a proposal. Returns "ok", "duplicate" or "no_user"."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if action is not None and action.get("tool") not in ACTIONS:
        raise ValueError(f"action tool must be one of {ACTIONS}")
    title = " ".join(title.split())[:MAX_TITLE]
    now = datetime.now(timezone.utc)
    doc = {"id": ObjectId(), "kind": kind, "key": key or key_of(kind, title), "title": title,
           "why": " ".join(why.split())[:MAX_WHY], "action": action, "links": (links or [])[:MAX_LINKS],
           "agent": agent, "status": "open",
           "created_at": now, "expires_at": now + timedelta(days=days), "shown_count": 0, "sensitivity": label}
    return await repos.users.push_proposal(user_id, doc, KEEP)


def _expired(p: ProposalDoc, now: datetime) -> bool:
    return p.expires_at is not None and topics.utc(p.expires_at) <= now


async def open_for(repos: Repos, ctx: UserContext, limit: int = SHOW_MAX, mark_shown: bool = True,
                   now: datetime | None = None, not_shown_since: datetime | None = None) -> list[ProposalDoc]:
    """The open proposals this client may show, oldest first; expired ones and ones shown MAX_SHOWN times become
    stale, and goal proposals the user already has as a goal close by themselves. not_shown_since: only ones not
    shown since then."""
    now = now or datetime.now(timezone.utc)
    profile = await repos.users.get_profile(ctx.user_id)
    active = {g.text.lower() for g in (profile.goals if profile else []) if g.active}
    out = []
    for p in await repos.users.get_proposals(ctx.user_id):
        if p.status != "open":
            continue
        oid = to_object_id(p.id)
        if _expired(p, now) or (mark_shown and p.shown_count >= MAX_SHOWN):
            await repos.users.update_proposal(ctx.user_id, oid, {"status": "stale"}, status="open")
            continue
        if not_shown_since and p.shown_at and topics.utc(p.shown_at) >= not_shown_since:
            continue
        if p.kind == "goal" and p.title.lower() in active:
            await repos.users.update_proposal(ctx.user_id, oid, {"status": "accepted", "answered_at": now},
                                              status="open")
            continue
        if sensitivity.is_allowed(p.sensitivity, ctx.max_sensitivity) and len(out) < limit:
            out.append(p)
    if mark_shown:
        for p in out:
            await repos.users.update_proposal(ctx.user_id, to_object_id(p.id), {"shown_at": now},
                                              inc={"shown_count": 1})
    return out


async def list_open(repos: Repos, ctx: UserContext) -> list[ProposalDoc]:
    """The open proposals, when the user asks about them («що ти там пропонував?»): newest first, not counted as
    shown."""
    return list(reversed(await open_for(repos, ctx, limit=LIST_MAX, mark_shown=False)))[:LIST_MAX]


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
