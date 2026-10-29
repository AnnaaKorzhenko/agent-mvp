"""Feedback from MCP clients: on posts, digest items, and the client's own answers and suggestions.

Stored in users.feedback next to the bot's entries (no new collection). New entries use the words of the
👍 / 👎 / 50/50 buttons (useful / not_useful / neutral); the bot's own entries keep like / dislike / skip,
and old words are still accepted as input. Every new entry has `fid`, its own id, so a comment or reasons
can be added to exactly that rating later (feedback_id), without touching scores.

- post: keyed by post_id; digest_id / item_n are null.
- digest_item: the bot's shape, keyed by (digest_id, item_n); post_id is the item's first source post.
- answer / suggestion: Claude's own output, described by `about`, with the posts it cited.
Post and digest-item ratings also keep a copy of what was rated: `about` (the post summary or the item text) and
`source_urls`, because posts are deleted within days and a comment like «more on this device» means nothing
without the item (the learning job reads it).
A new rating of the same post or digest item replaces the earlier MCP rating and reverses its points.
Only posts and digest items change interest scores. Telegram entries (no `channel`) are never touched.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from bson import ObjectId

from brain.db.repositories import Repos, to_object_id
from brain.domain import digests, scoring
from brain.domain.errors import InvalidInput, NotFound
from brain.domain.identity import UserContext

REACTIONS = ("useful", "not_useful", "neutral")
LEGACY = {"like": "useful", "dislike": "not_useful", "skip": "neutral"}
TARGETS = ("post", "digest_item", "answer", "suggestion")
SCORED_TARGETS = ("post", "digest_item")
REASONS = ("already_known", "not_relevant", "inaccurate", "too_long", "too_short", "want_more", "other")
LABELS = {"useful": "👍 корисно", "not_useful": "👎 не корисно", "neutral": "50/50"}
FEEDBACK_KEEP = 1000        # same cap as the bot (user_store.FEEDBACK_KEEP)
MAX_TEXT = 2000
MAX_TAGS = 10
MAX_ABOUT = 200
MAX_RATED_TEXT = 600        # the copy of a rated post summary / digest item text
MAX_URLS = 5
MAX_CITED = 20

NO_PROFILE = "No profile for this user yet. Start the Telegram bot once to create it."


@dataclass
class FeedbackResult:
    feedback_id: str
    target_type: str
    reaction: str
    replaced_previous: str | None
    score_changes: dict[str, float]
    confirmation: str


def normalize_reaction(reaction: str | None) -> str:
    r = LEGACY.get(reaction or "", reaction)
    if r not in REACTIONS:
        raise InvalidInput(f"reaction must be one of {', '.join(REACTIONS)}")
    return r


def _reasons(reasons: list[str] | None) -> list[str]:
    out = list(dict.fromkeys(reasons or []))
    unknown = [r for r in out if r not in REASONS]
    if unknown:
        raise InvalidInput(f"unknown reasons {unknown}; allowed: {', '.join(REASONS)}")
    return out


def _text(text: str | None) -> str:
    return " ".join((text or "").split())[:MAX_TEXT]


def _rated_text(text: str | None) -> str:
    return " ".join((text or "").split())[:MAX_RATED_TEXT]


def _confirmation(reaction: str, inc: dict, extra: str = "") -> str:
    changes = ", ".join(f"{k.rsplit('.', 1)[-1]} {v:+g}" for k, v in inc.items() if ".category_scores." in k)
    return f"Збережено: {LABELS[reaction]}" + (f" ({changes})" if changes else "") + extra


async def record_feedback(repos: Repos, ctx: UserContext, *, mcp_channels: list[str],
                          target_type: str | None = "post", reaction: str | None = None,
                          post_id: str | None = None, digest_id: str | None = None, item_n: int | None = None,
                          about: str | None = None, cited_post_ids: list[str] | None = None,
                          reasons: list[str] | None = None, text: str | None = None,
                          feedback_id: str | None = None) -> FeedbackResult:
    if feedback_id is not None:
        return await _follow_up(repos, ctx, feedback_id, reaction, reasons, text)
    target_type = target_type or "post"
    if target_type not in TARGETS:
        raise InvalidInput(f"target_type must be one of {', '.join(TARGETS)}")
    # A comment on an item the user already rated (e.g. with the cards' buttons), found by the item itself.
    comment_only = reaction is None and target_type in SCORED_TARGETS and bool(_text(text) or reasons)
    if not comment_only:
        reaction = normalize_reaction(reaction)
    channels = sorted(set(mcp_channels) | {ctx.channel})
    entry: dict = {"fid": ObjectId(), "target_type": target_type, "reaction": reaction,
                   "reasons": _reasons(reasons), "text": _text(text), "at": datetime.now(timezone.utc),
                   "channel": ctx.channel, "post_id": None, "digest_id": None, "item_n": None}
    if entry["text"]:
        entry.update(text_at=entry["at"], comment_status="commented")

    match: dict | None = None
    if target_type == "post":
        oid = to_object_id(post_id)
        if oid is None:
            raise InvalidInput("post_id must be a post id returned by search_posts, get_post or get_digest")
        post = await repos.posts.get(oid, ctx.allowed)
        if post is None:
            raise NotFound("Post not found")
        entry.update(post_id=oid, side=post.source_side, category=post.category, tags=post.tags[:MAX_TAGS],
                     about=_rated_text(post.summary), source_urls=[post.post_url] if post.post_url else [])
        # Older MCP entries have no target_type; digest_id null tells post ratings from digest-item ratings.
        match = {"post_id": oid, "digest_id": None, "channel": {"$in": channels},
                 "target_type": {"$in": [None, "post"]}}
    elif target_type == "digest_item":
        found = await digests.find_item(repos, ctx, digest_id, item_n)
        if found is None:
            raise NotFound("Digest item not found: pass digest_id and n of an item from get_digest")
        digest, section, item = found
        sources = [o for o in (to_object_id(p) for p in item.source_post_ids) if o]
        posts = await repos.posts.get_many(sources, ctx.allowed)
        tags = list(dict.fromkeys(t for p in item.source_post_ids if p in posts for t in posts[p].tags))
        entry.update(post_id=sources[0] if sources else None, digest_id=to_object_id(digest.id), item_n=item_n,
                     side=digest.side, category=section.category, tags=tags[:MAX_TAGS],
                     about=_rated_text(item.text), source_urls=item.source_urls[:MAX_URLS])
        match = {"digest_id": entry["digest_id"], "item_n": item_n, "channel": {"$in": channels}}
    else:
        about = " ".join((about or "").split())[:MAX_ABOUT]
        if not about:
            raise InvalidInput(f"{target_type} feedback needs `about`: one line saying what was rated")
        cited = [o for o in (to_object_id(p) for p in (cited_post_ids or [])) if o]
        if len(cited) != len(cited_post_ids or []) or len(cited) > MAX_CITED:
            raise InvalidInput(f"cited_post_ids must be at most {MAX_CITED} post ids from the tools' results")
        known = await repos.posts.get_many(cited, ctx.allowed)
        unknown = [str(o) for o in cited if str(o) not in known]
        if unknown:
            raise InvalidInput(f"Unknown cited_post_ids {unknown}; never invent ids")
        entry.update(about=about, cited_post_ids=list(dict.fromkeys(cited)))

    previous = await repos.users.find_feedback(ctx.user_id, match) if match else None
    if comment_only:
        if not (previous and previous.get("fid")):
            raise InvalidInput("The user hasn't rated this item yet: ask for 👍 / 👎 / 50/50 and pass it as reaction")
        return await _follow_up(repos, ctx, str(previous["fid"]), None, reasons, text)
    inc: dict = {}
    if target_type in SCORED_TARGETS:
        inc = scoring.merge_inc(scoring.points_inc(previous, -1) if previous else {}, scoring.points_inc(entry, +1))
    if not await repos.users.replace_feedback(ctx.user_id, match, entry, inc, FEEDBACK_KEEP):
        raise NotFound(NO_PROFILE)
    prev_reaction = (previous or {}).get("reaction")
    return FeedbackResult(str(entry["fid"]), target_type, reaction,
                          LEGACY.get(prev_reaction, prev_reaction), inc, _confirmation(reaction, inc))


async def _follow_up(repos: Repos, ctx: UserContext, feedback_id: str, reaction: str | None,
                     reasons: list[str] | None, text: str | None) -> FeedbackResult:
    """Adds a comment and/or reasons to an earlier rating. Scores don't change. With neither, the user
    skipped the comment (comment_status no_comment, as in the bot)."""
    fid = to_object_id(feedback_id)
    entry = await repos.users.find_feedback(ctx.user_id, {"fid": fid}) if fid else None
    if entry is None:
        raise NotFound("No such feedback_id for this user")
    stored = LEGACY.get(entry.get("reaction"), entry.get("reaction"))
    if reaction is not None and normalize_reaction(reaction) != stored:
        raise InvalidInput("To change the rating itself, call record_feedback for the target again "
                           "(without feedback_id)")
    now = datetime.now(timezone.utc)
    fields: dict = {}
    if reasons is not None:
        fields["reasons"] = list(dict.fromkeys([*entry.get("reasons", []), *_reasons(reasons)]))
    if text is not None and _text(text):
        fields.update(text=_text(text), text_at=now, comment_status="commented")
    if not fields:
        fields["comment_status"] = "no_comment"
    await repos.users.update_feedback(ctx.user_id, fid, fields)
    extra = ". Коментар додано" if "text" in fields else (". Причини додано" if "reasons" in fields else "")
    return FeedbackResult(feedback_id, entry.get("target_type", "post"), stored, None, {},
                          _confirmation(stored, {}, extra))
