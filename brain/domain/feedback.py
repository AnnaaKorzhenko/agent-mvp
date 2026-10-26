"""Feedback from MCP clients, written in the bot's feedback shape and scored with the shared scoring.

An entry is keyed by post (MCP clients see posts, not digest items): digest_id and item_n are null,
and `channel` says which client wrote it. A new reaction replaces the same post's earlier reaction from
an MCP client and reverses its points. Telegram entries are never touched.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from brain.db.repositories import Repos, to_object_id
from brain.domain import scoring
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext

REACTIONS = ("like", "dislike", "skip")
FEEDBACK_KEEP = 1000        # same cap as the bot (user_store.FEEDBACK_KEEP)
MAX_TEXT = 2000
MAX_TAGS = 10


@dataclass
class FeedbackResult:
    post_id: str
    reaction: str
    replaced_previous: str | None
    score_changes: dict[str, float]


async def record_feedback(repos: Repos, ctx: UserContext, post_id: str | None, reaction: str,
                          text: str | None, mcp_channels: list[str]) -> FeedbackResult:
    if reaction not in REACTIONS:
        raise InvalidInput(f"reaction must be one of {', '.join(REACTIONS)}")
    oid = to_object_id(post_id)
    if oid is None:
        raise InvalidInput("post_id must be a post id returned by search_posts, get_post or get_digest")
    post = await repos.posts.get(oid, ctx.allowed)
    if post is None:
        raise NotFound("Post not found")

    entry = {"post_id": oid, "reaction": reaction, "text": " ".join((text or "").split())[:MAX_TEXT],
             "digest_id": None, "item_n": None, "side": post.source_side, "category": post.category,
             "tags": post.tags[:MAX_TAGS], "at": datetime.now(timezone.utc), "channel": ctx.channel}
    channels = sorted(set(mcp_channels) | {ctx.channel})
    previous = await repos.users.find_feedback(ctx.user_id, oid, channels)
    inc = scoring.merge_inc(scoring.points_inc(previous, -1) if previous else {}, scoring.points_inc(entry, +1))
    if not await repos.users.replace_feedback(ctx.user_id, oid, channels, entry, inc, FEEDBACK_KEEP):
        raise NotFound("No profile for this user yet. Start the Telegram bot once to create it.")
    return FeedbackResult(str(oid), reaction, (previous or {}).get("reaction"), inc)
