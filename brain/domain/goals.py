"""The user's goals, added and removed by MCP clients with the bot's rules and goal shape.

The bot's rules (user_store.add_goal / remove_goal) except its 5-goal limit: any number of active goals (users
focus on many at once; decided 2026-10-29), no duplicate active goal (case-insensitive), text squashed to one
line of at most 300 characters. Removing a goal only marks it inactive, so the history stays. Entries also carry `channel`, the client that wrote them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from brain.db.repositories import Repos
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext

MAX_TEXT = 300              # same as user_store.add_goal

NO_PROFILE = "No profile for this user yet. Start the Telegram bot once to create it."


@dataclass
class GoalsResult:
    goals: list[str]          # active goals after the change
    changed: str              # the goal text that was added or removed


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())[:MAX_TEXT]


async def _profile(repos: Repos, ctx: UserContext):
    profile = await repos.users.get_profile(ctx.user_id)
    if profile is None:
        raise NotFound(NO_PROFILE)
    return profile


async def add_goal(repos: Repos, ctx: UserContext, text: str | None) -> GoalsResult:
    text = _clean(text)
    if not text:
        raise InvalidInput("The goal text is empty")
    active = [g.text for g in (await _profile(repos, ctx)).goals if g.active]
    if any(g.lower() == text.lower() for g in active):
        raise InvalidInput(f"This goal already exists: {text}")
    goal = {"text": text, "active": True, "source": "user", "created_at": datetime.now(timezone.utc),
            "channel": ctx.channel}
    if not await repos.users.push_goal(ctx.user_id, goal):
        raise NotFound(NO_PROFILE)
    return GoalsResult(goals=active + [text], changed=text)


async def remove_goal(repos: Repos, ctx: UserContext, text: str | None) -> GoalsResult:
    wanted = _clean(text).lower()
    goals = (await _profile(repos, ctx)).goals
    active = [g.text for g in goals if g.active]
    match = next((i for i, g in enumerate(goals) if g.active and g.text.lower() == wanted), None)
    if match is None:
        raise NotFound("No such active goal. Current goals: " + ("; ".join(active) or "none"))
    removed = goals[match].text
    if not await repos.users.deactivate_goal(ctx.user_id, match, removed, datetime.now(timezone.utc)):
        raise NotFound("The goal changed meanwhile; read the profile again")
    return GoalsResult(goals=[g for g in active if g != removed], changed=removed)
