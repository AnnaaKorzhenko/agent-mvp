"""The user corrects what was inferred about them («мене це не цікавить», «я вже не займаюсь цим»): explicit beats
inferred (docs/AGENT_PLAN.md §2), at once, without waiting for the learning job.

not_interested(text):
- removes the learned interests and current intents that match the text (by their words' first PREFIX letters,
  either way: «космос» drops «космічні запуски», «Рассвет» drops «Рассвет: супутники»);
- adds the text to learned.avoid, so relevance pushes such news down and the scout doesn't propose it;
- closes the open proposals about it (as dismissed);
- saves it as an explicit insight («не цікавить: …», kind preference), so the next learning runs keep it.
Followed topics are the user's own choice: matching ones are returned, not stopped (the client asks).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from brain.db.repositories import Repos, to_object_id
from brain.domain import insights, relevance, topics
from brain.domain.errors import InvalidInput
from brain.domain.identity import UserContext

MAX_TEXT = 120
MAX_AVOID = 10          # LearnedProfile.avoid's limit


@dataclass
class Corrected:
    text: str
    dropped_interests: list[str] = field(default_factory=list)
    dropped_intents: list[str] = field(default_factory=list)
    closed_proposals: list[str] = field(default_factory=list)
    followed_matching: list[str] = field(default_factory=list)


PREFIX = 4              # «космос» ~ «космічні», «кораблі» ~ «корабельний»: Ukrainian endings change the 5th letter


def matches(text: str, other: str) -> bool:
    """Whether two phrases are about the same thing: all word starts of the shorter one are in the longer one."""
    a, b = ({w[:PREFIX] for w in relevance._stems(x)} for x in (text, other))
    if not a or not b:
        return False
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return small <= big


async def not_interested(repos: Repos, ctx: UserContext, text: str | None, *,
                         profile_keywords: bool = False) -> Corrected:
    text = " ".join((text or "").split())[:MAX_TEXT]
    if not text:
        raise InvalidInput("Say what the user isn't interested in, in their words")
    out = Corrected(text)
    learned = await repos.users.get_learned(ctx.user_id) or {}
    keep_i, keep_t = [], []
    for i in learned.get("interests") or []:
        (out.dropped_interests.append(i.get("topic", "")) if matches(text, i.get("topic", "")) else keep_i.append(i))
    for t in learned.get("intents") or []:
        (out.dropped_intents.append(t.get("text", "")) if matches(text, t.get("text", "")) else keep_t.append(t))
    avoid = [a for a in learned.get("avoid") or [] if relevance._norm(a) != relevance._norm(text)]
    avoid = (avoid + [text])[-MAX_AVOID:]
    if learned:
        await repos.users.update_learned(ctx.user_id, {"interests": keep_i, "intents": keep_t, "avoid": avoid})
    else:
        await repos.users.update_learned(ctx.user_id, {"avoid": avoid, "sensitivity": "internal"})
    now = datetime.now(timezone.utc)
    for p in await repos.users.get_proposals(ctx.user_id):
        if p.status == "open" and (matches(text, p.title) or matches(text, " ".join(
                (p.action.args.get("specific") or []) if p.action else []))):
            if await repos.users.update_proposal(ctx.user_id, to_object_id(p.id),
                                                 {"status": "dismissed", "answered_at": now}, status="open"):
                out.closed_proposals.append(p.title)
    out.followed_matching = [t.name for t in await topics.followed(repos, ctx)
                             if matches(text, t.name) or any(matches(text, s) for s in t.specific)]
    try:
        await insights.save_insight(repos, ctx, f"не цікавить: {text}", kind="preference", origin="explicit")
    except InvalidInput:
        pass                                   # full: the learned profile already holds it
    if profile_keywords and out.dropped_interests:
        await topics.sync_profile_queries(repos)
    return out
