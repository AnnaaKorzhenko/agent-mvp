"""The user's profile as the MCP server may show it: choices, goals, top interests. Never feedback."""
from __future__ import annotations

from dataclasses import dataclass, field

from brain.db.repositories import Repos
from brain.domain import scoring
from brain.domain.digests import todays_choice
from brain.domain.errors import NotFound
from brain.domain.identity import UserContext

TOP = 10


@dataclass
class ProfileView:
    categories_today: dict[str, list[str]]
    goals: list[str]
    top_categories: list[tuple[str, str, float]] = field(default_factory=list)   # (side, category, score)
    top_tags: list[tuple[str, float]] = field(default_factory=list)


async def get_profile(repos: Repos, ctx: UserContext) -> ProfileView:
    profile = await repos.users.get_profile(ctx.user_id)
    if profile is None:
        raise NotFound("No profile for this user yet. Start the Telegram bot once to create it.")
    scores = profile.interests.category_scores
    cats = sorted(((side, cat, score) for side, by_cat in scores.items() for cat, score in by_cat.items()),
                  key=lambda x: -x[2])
    tags = scoring.top_tags(profile.as_scoring_user(), TOP)
    return ProfileView(
        categories_today={side: todays_choice(profile, side) for side in scoring.SIDES},
        goals=[g.text for g in profile.goals if g.active],
        top_categories=cats[:TOP],
        top_tags=[(t["tag"], t["score"]) for t in tags],
    )
