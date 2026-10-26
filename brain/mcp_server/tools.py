"""Thin MCP tool wrappers: resolve the caller -> call domain/ -> typed result -> audit.

No business logic here. user_id never comes from tool arguments.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from brain.audit.log import AuditLog
from brain.config import Settings
from brain.db.repositories import Repos
from brain.domain import digests as digests_domain
from brain.domain import feedback as feedback_domain
from brain.domain import posts as posts_domain
from brain.domain import profile as profile_domain
from brain.domain import sensitivity
from brain.domain.errors import DomainError
from brain.domain.identity import IdentityError, UserContext, VerifiedToken, resolve_user
from brain.mcp_server import schemas as out

TokenProvider = Callable[[], VerifiedToken | None]


@dataclass
class Services:
    settings: Settings
    repos: Repos
    audit: AuditLog
    token: TokenProvider          # phase 3: always None (dev user); phase 4: the verified OAuth token

    @property
    def mcp_channels(self) -> list[str]:
        return sorted(set(self.settings.channel_by_client.values()) | {"dev", "unknown"})


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _post_out(v: posts_domain.PostView) -> out.PostOut:
    p = v.post
    return out.PostOut(post_id=p.id, summary=p.summary, tags=p.tags, category=p.category,
                       source_side=p.source_side, is_russian_source_claim=v.is_russian_source_claim,
                       channel=out.Channel(username=v.channel.username, country=v.channel.source_country)
                       if v.channel else None,
                       post_url=p.post_url, published_at=p.published_at, importance=p.importance)


class _Call:
    """Audits one tool call and turns anticipated failures into errors the model can read."""

    def __init__(self, svc: Services, name: str, args: dict):
        self.svc, self.name, self.args = svc, name, args
        self.ctx: UserContext | None = None
        self.started = time.monotonic()

    async def user(self) -> UserContext:
        try:
            self.ctx = await resolve_user(self.svc.settings, self.svc.token(), self.svc.repos.users)
        except IdentityError as e:
            await self.done(outcome="error", error=str(e))
            raise ToolError(str(e)) from None
        return self.ctx

    async def done(self, items: int = 0, labels: list[str | None] | None = None, outcome: str = "ok",
                   error: str | None = None) -> None:
        c = self.ctx
        await self.svc.audit.tool_call(
            name=self.name, user_id=c.user_id if c else None, trust=c.trust if c else None,
            channel=c.channel if c else None, args=self.args, items_returned=items,
            max_sensitivity=sensitivity.highest(labels) if labels else None, outcome=outcome, error=error,
            duration_ms=int((time.monotonic() - self.started) * 1000))

    async def run(self, fn: Callable[[UserContext], Awaitable]):
        ctx = await self.user()
        try:
            return await fn(ctx)
        except DomainError as e:
            await self.done(outcome="error", error=str(e))
            raise ToolError(str(e)) from None
        except Exception as e:
            await self.done(outcome="error", error=f"{type(e).__name__}: {e}")
            raise


def register_tools(mcp: MCPServer, svc: Services) -> None:

    @mcp.tool(title="Today's news digest", annotations=READ_ONLY)
    async def get_digest(
        side: Annotated[Literal["ua", "ru", "both"], Field(
            description="ua: Ukrainian sources; ru: Russian sources (claims, not facts); both: both")] = "both",
        date: Annotated[str | None, Field(
            description="Kyiv date YYYY-MM-DD; omit for the latest digest")] = None,
    ) -> out.DigestResult:
        """Get the user's daily news digest: sections ordered by the user's interests, every item with its
        source links (source_urls) and post ids (source_post_ids).

        Present items from Ukrainian sources as reported news and items with is_russian_source_claim=true as
        claims of Russian channels ("Russian channels claim..."), never as facts. Cite source_urls. After
        showing the digest, offer short follow-ups: a shorter version, more detail on item N (use get_post
        with its source_post_ids), or the sources."""
        call = _Call(svc, "get_digest", {"side": side, "date": date})

        async def body(ctx: UserContext) -> out.DigestResult:
            res = await digests_domain.get_digests(svc.repos, ctx, side, date)
            result = out.DigestResult(date=res.date, missing_sides=res.missing_sides, digests=[
                out.DigestOut(
                    side=v.digest.side, side_note=v.side_note, digest_id=v.digest.id,
                    created_at=v.digest.created_at.isoformat(), period_start=v.digest.period_start,
                    period_end=v.digest.period_end, hidden_items=v.hidden_items,
                    sections=[out.DigestSectionOut(category=s.category, title=s.title, items=[
                        out.DigestItemOut(n=i.n, text=i.text, source_urls=i.source_urls,
                                          source_post_ids=i.source_post_ids,
                                          is_russian_source_claim=v.digest.side == "ru") for i in items])
                        for s, items in v.sections])
                for v in res.digests])
            if not result.digests:
                result.note = "No digest found for this date. Say so; do not invent news."
            n = sum(len(s.items) for d in result.digests for s in d.sections)
            await call.done(items=n, labels=[v.digest.sensitivity for v in res.digests])
            return result
        return await call.run(body)

    @mcp.tool(title="Search news posts", annotations=READ_ONLY)
    async def search_posts(
        query: Annotated[str, Field(min_length=2, max_length=300,
                                    description="Key words, e.g. 'Brave1 гранти РЕБ' (Ukrainian or Russian)")],
        side: Annotated[Literal["ua", "ru", "all"], Field(description="Source side")] = "all",
        category: Annotated[str | None, Field(
            description="Optional category key, e.g. front, defense_tech, investments, ru_front")] = None,
        days: Annotated[int, Field(ge=1, le=30, description="How many past days to search")] = 7,
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> out.SearchResult:
        """Search the news base (summaries of Telegram posts from Ukrainian and Russian channels) by key words.

        Answer only from the results and cite each fact's post_url. If nothing relevant comes back, say
        "I don't have that in the data" (you may retry once with other words or more days). Results with
        is_russian_source_claim=true are claims of Russian sources, not facts."""
        call = _Call(svc, "search_posts", {"query": query, "side": side, "category": category,
                                           "days": days, "limit": limit})

        async def body(ctx: UserContext) -> out.SearchResult:
            views, words = await posts_domain.search(svc.repos, ctx, query, side, category, days, limit)
            note = None
            if not words:
                note = "The query has no searchable words. Use 2-4 specific key words."
            elif not views:
                note = "Nothing found. Say you don't have this in the data, or retry with other words or more days."
            await call.done(items=len(views), labels=[v.post.sensitivity for v in views])
            return out.SearchResult(query=query, search_words=words, days=days,
                                    results=[_post_out(v) for v in views], note=note)
        return await call.run(body)

    @mcp.tool(title="Get one post", annotations=READ_ONLY)
    async def get_post(
        post_id: Annotated[str, Field(description="A post_id / source_post_ids value from another tool")],
    ) -> out.PostOut:
        """Get one news post (summary, tags, channel, link) by id. Cite its post_url."""
        call = _Call(svc, "get_post", {"post_id": post_id})

        async def body(ctx: UserContext) -> out.PostOut:
            view = await posts_domain.get_post(svc.repos, ctx, post_id)
            await call.done(items=1, labels=[view.post.sensitivity])
            return _post_out(view)
        return await call.run(body)

    @mcp.tool(title="The user's interests", annotations=READ_ONLY)
    async def get_profile() -> out.ProfileOut:
        """The user's goals, today's chosen categories and strongest interests (categories and tags by score).
        Use it to explain why something matters to this user; don't recite it unasked."""
        call = _Call(svc, "get_profile", {})

        async def body(ctx: UserContext) -> out.ProfileOut:
            p = await profile_domain.get_profile(svc.repos, ctx)
            await call.done(items=len(p.goals) + len(p.top_categories) + len(p.top_tags))
            return out.ProfileOut(
                categories_today=p.categories_today, goals=p.goals,
                top_categories=[out.CategoryScore(side=s, category=c, score=v) for s, c, v in p.top_categories],
                top_tags=[out.TagScore(tag=t, score=v) for t, v in p.top_tags])
        return await call.run(body)

    @mcp.tool(title="Rate a post",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
    async def record_feedback(
        post_id: Annotated[str, Field(description="The rated post's id (post_id or a source_post_ids value)")],
        reaction: Annotated[Literal["like", "dislike", "skip"], Field(
            description="like: useful; dislike: not interesting; skip: no opinion")],
        text: Annotated[str | None, Field(max_length=2000,
                                          description="The user's own words about why, if they gave any")] = None,
    ) -> out.FeedbackOut:
        """Save the user's rating of a news post; it tunes which news they see first. Call it only when the
        user clearly rates a specific post, never on your own judgement. A new rating of the same post
        replaces the previous one."""
        call = _Call(svc, "record_feedback", {"post_id": post_id, "reaction": reaction, "text": text})

        async def body(ctx: UserContext) -> out.FeedbackOut:
            r = await feedback_domain.record_feedback(svc.repos, ctx, post_id, reaction, text, svc.mcp_channels)
            await call.done(items=1)
            return out.FeedbackOut(post_id=r.post_id, reaction=r.reaction, replaced_previous=r.replaced_previous,
                                   score_changes=r.score_changes)
        return await call.run(body)
