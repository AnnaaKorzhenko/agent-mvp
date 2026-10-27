"""Thin MCP tool wrappers: resolve the caller -> call domain/ -> typed result -> audit.

No business logic here. user_id never comes from tool arguments.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from brain.audit.log import AuditLog
from brain.config import Settings
from brain.db.repositories import Repos
from brain.domain import digests as digests_domain
from brain.domain import feedback as feedback_domain
from brain.domain import goals as goals_domain
from brain.domain import posts as posts_domain
from brain.domain import profile as profile_domain
from brain.domain import sensitivity
from brain.domain.errors import DomainError
from brain.domain.identity import IdentityError, UserContext, VerifiedToken, resolve_user
from brain.mcp_server import schemas as out
from brain.mcp_server import widgets

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

    @mcp.tool(title="Today's news digest", annotations=READ_ONLY, meta=widgets.tool_meta(widgets.DIGEST_UI))
    async def get_digest(
        mcp_ctx: Context,
        side: Annotated[Literal["ua", "ru", "both"], Field(
            description="ua: Ukrainian sources; ru: Russian sources (claims, not facts); both: both")] = "both",
        date: Annotated[str | None, Field(
            description="Kyiv date YYYY-MM-DD; omit for the latest digest")] = None,
        as_text: Annotated[bool, Field(description=(
            "true only if the user asks for the digest as text (e.g. the cards didn't show up)"))] = False,
    ) -> Annotated[CallToolResult, out.DigestResult]:
        """Get the user's daily news digest: sections ordered by the user's interests, every item with its
        source links (source_urls) and post ids (source_post_ids).

        If the result says shown_in_widget, the user already sees the digest as interactive cards with rating
        buttons: do not repeat, list or summarize it; reply with one short sentence at most, unless the user
        asked something specific. Otherwise present it in text: items from Ukrainian sources as reported news,
        items with is_russian_source_claim=true as claims of Russian channels ("Russian channels claim..."),
        never as facts; cite source_urls; show every item with its ref number (#1, #2, …) so the user can refer
        to it and rate it. Offer short follow-ups: more detail on item N (get_post with its source_post_ids),
        a shorter version, or the sources."""
        call = _Call(svc, "get_digest", {"side": side, "date": date, "as_text": as_text})
        widget = not as_text and widgets.client_shows_widgets(mcp_ctx)

        async def body(ctx: UserContext) -> CallToolResult:
            res = await digests_domain.get_digests(svc.repos, ctx, side, date)
            refs = itertools.count(1)       # one numbering across both sides: «третя» is unambiguous
            result = out.DigestResult(date=res.date, missing_sides=res.missing_sides, digests=[
                out.DigestOut(
                    side=v.digest.side, side_note=v.side_note, digest_id=v.digest.id,
                    created_at=v.digest.created_at.isoformat(), period_start=v.digest.period_start,
                    period_end=v.digest.period_end, hidden_items=v.hidden_items,
                    sections=[out.DigestSectionOut(category=s.category, title=s.title, items=[
                        out.DigestItemOut(ref=next(refs), n=v.number(i), text=i.text, source_urls=i.source_urls,
                                          source_post_ids=i.source_post_ids,
                                          is_russian_source_claim=v.digest.side == "ru") for i in items])
                        for s, items in v.sections])
                for v in res.digests])
            if not result.digests:
                result.note = "No digest found for this date. Say so; do not invent news."
            result.shown_in_widget = widget and bool(result.digests)
            n = sum(len(s.items) for d in result.digests for s in d.sections)
            await call.done(items=n, labels=[v.digest.sensitivity for v in res.digests])
            # What the model reads: a short index when the cards are on screen, else the full digest.
            text = widgets.digest_index(result) if result.shown_in_widget else result.model_dump_json()
            return CallToolResult(content=[TextContent(type="text", text=text)],
                                  structured_content=result.model_dump(mode="json"))
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
        is_russian_source_claim=true are claims of Russian sources, not facts. When you list results, show their
        ref numbers (#1, #2, …)."""
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
            results = [_post_out(v).model_copy(update={"ref": i}) for i, v in enumerate(views, 1)]
            return out.SearchResult(query=query, search_words=words, days=days, results=results, note=note)
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

    @mcp.tool(title="Rate news or an answer",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
    async def record_feedback(
        reaction: Annotated[Literal["useful", "not_useful", "neutral", "like", "dislike", "skip"] | None, Field(
            description="useful (👍), not_useful (👎) or neutral (50/50). Omit only with feedback_id")] = None,
        target_type: Annotated[Literal["post", "digest_item", "answer", "suggestion"], Field(
            description="post: a post from search_posts/get_post; digest_item: an item of get_digest; "
                        "answer / suggestion: your own answer, analysis or suggestion")] = "post",
        post_id: Annotated[str | None, Field(description="post: the post's post_id")] = None,
        digest_id: Annotated[str | None, Field(description="digest_item: the item's digest_id")] = None,
        item_n: Annotated[int | None, Field(description="digest_item: the item's n (not ref)")] = None,
        about: Annotated[str | None, Field(max_length=200, description=(
            "answer / suggestion: one line saying what was rated, e.g. 'аналіз інвестицій у НРК'"))] = None,
        cited_post_ids: Annotated[list[str] | None, Field(max_length=20, description=(
            "answer / suggestion: post ids the rated answer relied on, from the tools' results"))] = None,
        reasons: Annotated[list[Literal["already_known", "not_relevant", "inaccurate", "too_long", "too_short",
                                        "want_more", "other"]] | None, Field(
            description="Reasons taken from the user's words, if any")] = None,
        text: Annotated[str | None, Field(max_length=2000,
                                          description="The user's own words, verbatim, if they gave any")] = None,
        feedback_id: Annotated[str | None, Field(description=(
            "To add a comment or reasons to an earlier rating: its feedback_id. Scores don't change"))] = None,
    ) -> out.FeedbackOut:
        """Save the user's rating of a news item, or of your own answer or suggestion; news ratings tune which
        news they see first.

        Call it only when the user clearly rates something, never on your own judgement. Map "the third one"
        to ref 3 of the most recent get_digest or search_posts result and pass that item's ids (digest items:
        digest_id + n as item_n; posts: post_id); if it is unclear which item is meant, ask. Never invent ids.
        Take reasons from the user's words and keep text verbatim. For your own answer or suggestion use
        target_type answer / suggestion with a one-line `about` and the post ids you cited. Several ratings in
        one message mean several calls. A comment that follows a rating goes to that rating's feedback_id.
        Rating the same post or digest item again replaces the earlier rating. Tell the user the
        confirmation."""
        args = {"target_type": target_type, "reaction": reaction, "post_id": post_id, "digest_id": digest_id,
                "item_n": item_n, "about": about, "cited_post_ids": cited_post_ids, "reasons": reasons,
                "text": text, "feedback_id": feedback_id}
        call = _Call(svc, "record_feedback", args)

        async def body(ctx: UserContext) -> out.FeedbackOut:
            r = await feedback_domain.record_feedback(svc.repos, ctx, mcp_channels=svc.mcp_channels, **args)
            await call.done(items=1)
            return out.FeedbackOut(feedback_id=r.feedback_id, target_type=r.target_type, reaction=r.reaction,
                                   replaced_previous=r.replaced_previous, score_changes=r.score_changes,
                                   confirmation=r.confirmation)
        return await call.run(body)

    @mcp.tool(title="Add or remove a goal",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
    async def set_goal(
        action: Annotated[Literal["add", "remove"], Field(description="add a new goal, or remove an existing one")],
        text: Annotated[str, Field(min_length=1, max_length=300, description=(
            "add: the goal in the user's words, e.g. 'стежити за розвитком НРК'; "
            "remove: the goal's text exactly as get_profile returns it"))],
    ) -> out.GoalsOut:
        """Add or remove one of the user's goals (what they most want to achieve; at most 5). Goals shape which
        news matter to them. Call it only when the user explicitly asks to set, change or drop a goal, never on
        your own judgement. To change a goal, remove the old one and add the new one."""
        call = _Call(svc, "set_goal", {"action": action, "text": text})

        async def body(ctx: UserContext) -> out.GoalsOut:
            if action == "add":
                r = await goals_domain.add_goal(svc.repos, ctx, text)
                result = out.GoalsOut(goals=r.goals, added=r.changed)
            else:
                r = await goals_domain.remove_goal(svc.repos, ctx, text)
                result = out.GoalsOut(goals=r.goals, removed=r.changed)
            await call.done(items=1)
            return result
        return await call.run(body)
