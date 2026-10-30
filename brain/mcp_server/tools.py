"""Thin MCP tool wrappers: resolve the caller -> call domain/ -> typed result -> conversation log.

No business logic here. user_id never comes from tool arguments.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Awaitable, Callable, Literal

from bson import ObjectId
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from brain.audit.conversations import WIDGET_PREFIX, CallRecord, ConversationLog
from brain.audit.log import AuditLog
from brain.config import Settings
from brain.db.repositories import Repos, user_key
from brain.domain import analysis as analysis_domain
from brain.domain import digests as digests_domain
from brain.domain import feedback as feedback_domain
from brain.domain import goals as goals_domain
from brain.domain import insights as insights_domain
from brain.domain import posts as posts_domain
from brain.domain import profile as profile_domain
from brain.domain import sensitivity
from brain.domain import topics as topics_domain
from brain.domain.errors import DomainError
from brain.domain.identity import IdentityError, UserContext, VerifiedToken, resolve_user
from brain.mcp_server import schemas as out
from brain.mcp_server import widgets

TokenProvider = Callable[[], VerifiedToken | None]
logger = logging.getLogger("brain.tools")


@dataclass
class Services:
    settings: Settings
    repos: Repos
    audit: AuditLog               # security events only
    token: TokenProvider          # the verified OAuth token (None in dev mode)
    conversations: ConversationLog | None = field(default=None)
    learner: Any = None           # brain.learning.auto.Learner when LEARNING_AUTO is on: set_goal nudges it

    def __post_init__(self) -> None:
        if self.conversations is None:
            self.conversations = ConversationLog(self.repos.db, self.audit,
                                                 self.settings.conversation_idle_minutes,
                                                 self.settings.conversation_max_messages)

    @property
    def mcp_channels(self) -> list[str]:
        return sorted(set(self.settings.channel_by_client.values()) | {"dev", "unknown"})


def _session(mcp_ctx: Context | None) -> tuple[str | None, dict | None]:
    """(MCP session id, client {name, version}) of the current request; (None, None) without HTTP."""
    try:
        rc = mcp_ctx.request_context
    except Exception:              # outside a request
        return None, None
    request = getattr(rc, "request", None)
    sid = request.headers.get("mcp-session-id") if request is not None and hasattr(request, "headers") else None
    params = getattr(getattr(rc, "session", None), "client_params", None)
    info = getattr(params, "client_info", None)
    client = {"name": info.name, "version": info.version} if info else None
    return sid, client


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# On every tool: the message that led to the call, and what it reveals about the user. Required, so every message
# that reaches News brain is stored and analysed (docs/CONVERSATIONS_PLAN.md §4c).
UserMessage = Annotated[str, Field(min_length=1, max_length=8000, description=(
    "The user's latest chat message, verbatim and complete: the message that led to this call"))]
Signals = Annotated[list[out.Signal] | None, Field(max_length=10, description=(
    "Durable things that message reveals about the user: goals, interests, preferences (how they want answers or "
    "news), facts about their work and people, feedback, things they already know; in their own words. Leave "
    "empty for plain requests, and when you already passed them for this message in another call"))]


_FENCE = re.compile(r"^```[\w-]*\s*\n(.*?)\n?```\s*$", re.S)


def clean_message(text: str) -> str:
    """The message as the user typed it: clients sometimes wrap it in a ```markdown code fence."""
    text = text.strip()
    match = _FENCE.match(text)
    return match.group(1).strip() if match else text


def _common(user_message: str | None, signals: list[out.Signal] | None) -> dict:
    # No message only on record_feedback from a widget card opened before cards sent one: a button press.
    message = clean_message(user_message) if user_message is not None else f"{WIDGET_PREFIX} (кнопка в картці)"
    return {"user_message": message, **({"signals": [sig.model_dump() for sig in signals]} if signals else {})}


ALWAYS_ABOUT = {"get_digest", "get_profile"}


@dataclass
class Memory:
    """What _Call.remember() did with the call's signals."""
    saved: list[ObjectId] = field(default_factory=list)
    duplicates: int = 0
    goals_to_offer: list[str] = field(default_factory=list)
    about_user: dict | None = None


def _digest_out(res: digests_domain.DigestResultView, cls: type[out.DigestResult] = out.DigestResult,
                **extra) -> out.DigestResult:
    refs = itertools.count(1)       # one numbering across both sides: «третя» is unambiguous
    return cls(date=res.date, missing_sides=res.missing_sides, **extra, digests=[
        out.DigestOut(
            side=v.side, side_note=v.side_note, digest_id=v.digest.id,
            created_at=v.digest.created_at.isoformat(), period_start=v.digest.period_start,
            period_end=v.digest.period_end, hidden_items=v.hidden_items,
            sections=[out.DigestSectionOut(category=s.category, title=s.title, items=[
                out.DigestItemOut(ref=next(refs), n=v.number(i), text=i.text, source_urls=i.source_urls,
                                  source_post_ids=i.source_post_ids, is_russian_source_claim=v.claim(i),
                                  why=v.why(i).why, relevance=v.why(i).score) for i in items])
                for s, items in v.sections])
        for v in res.digests])


async def _renewals(svc: "Services", ctx: UserContext, result: out.DigestResult) -> None:
    """Adds the followed topics to offer for renewal. Never breaks the digest."""
    try:
        result.topics_to_renew = [
            out.TopicRenewal(name=r.topic.name, expires_at=topics_domain.until_text(r.topic),
                             expired=r.days_left < 0, days_left=r.days_left)
            for r in await topics_domain.to_renew(svc.repos, ctx)]
    except Exception as e:                 # noqa: BLE001
        logger.error(f"renewal check failed: {type(e).__name__}: {e}")


def _period_out(p: analysis_domain.Period) -> out.AnalysisPeriod:
    label = analysis_domain.month_name(p) or f"{p.start:%d.%m}–{p.end:%d.%m.%Y}"
    return out.AnalysisPeriod(start_date=p.start.isoformat(), end_date=p.end.isoformat(), days=p.days, label=label)


async def _analysis_due(svc: "Services", ctx: UserContext) -> out.AnalysisDue | None:
    """Monday / the 1st: the scheduled analysis, if not written yet. Never breaks the digest."""
    try:
        if not (d := analysis_domain.due()):
            return None
        kind, period = d
        if await analysis_domain.stored(svc.repos, ctx, period, None):
            return None
        p = _period_out(period)
        return out.AnalysisDue(period=kind, start_date=p.start_date, end_date=p.end_date, label=p.label)
    except Exception as e:                 # noqa: BLE001
        logger.error(f"analysis_due failed: {type(e).__name__}: {e}")
        return None


def _model_text(result: out.DigestResult, prefix: str = "") -> str:
    """What the model reads: a short index when the cards are on screen, else the full digest; then what to do
    after it (renewals, a due analysis), spelled out either way."""
    text = prefix + (widgets.digest_index(result) if result.shown_in_widget else result.model_dump_json())
    if result.topics_to_renew:
        text += "\nFollowed topics about to expire (ask once, briefly, whether to keep following; on yes call " \
                "track_topic follow with the name): " + "; ".join(
                    f"«{r.name}» " + ("expired " if r.expired else "until ") + r.expires_at
                    for r in result.topics_to_renew)
    due = getattr(result, "analysis_due", None)
    if due:
        text += (f"\nThe {due.period}ly analysis for {due.label} is due: after this digest, call get_analysis with "
                 f"start_date {due.start_date} and end_date {due.end_date}, write it and save it with save_report.")
    return text


async def _digest_done(call: "_Call", res: digests_domain.DigestResultView, result: out.DigestResult) -> None:
    n = sum(len(s.items) for d in result.digests for s in d.sections)
    await call.done(items=n, labels=[v.digest.sensitivity for v in res.digests],
                    digest_ids=[d.digest_id for d in result.digests], widget=result.shown_in_widget,
                    hidden=sum(d.hidden_items for d in result.digests))


def _post_out(v: posts_domain.PostView) -> out.PostOut:
    p = v.post
    return out.PostOut(post_id=p.id, summary=p.summary, tags=p.tags, category=p.category,
                       source_side=p.source_side, is_russian_source_claim=v.is_russian_source_claim,
                       channel=out.Channel(username=v.channel.username, country=v.channel.source_country)
                       if v.channel else None,
                       post_url=p.post_url, published_at=p.published_at, importance=p.importance, why=v.why)


def _topic_out(t, findings: bool = False) -> out.TopicOut:
    return out.TopicOut(
        name=t.name, status=topics_domain.status_of(t),
        expires_at=topics_domain.utc(t.expires_at).isoformat() if t.expires_at else None,
        goal=t.goal, specific=t.specific, broader=t.broader, exclude=t.exclude,
        web_queries=topics_domain.web_queries(t), tags=t.tags,
        findings_count=t.findings_count if t.findings_count is not None else len(t.findings),
        findings=[out.FindingOut(title=f.title, url=f.url, source=f.source, date=f.date, summary=f.summary,
                                 status=f.status) for f in t.findings] if findings else [])


DEEP_DIVE_STEPS = (
    "1) Tell the user what the news base has (subject, source posts, related posts, earlier findings), briefly. "
    "2) Search the web now: several searches, in Ukrainian, Russian and English, by the names, aliases and "
    "technical terms; prefer primary sources (developers, officials, OSINT analysts, defense media); skip what "
    "earlier findings already cover. 3) Answer with a dossier: what it is, specifications, who makes or uses it, "
    "timeline, how it matters for the user's goals, confirmed versus claimed (Russian sources are claims), what "
    "is still unknown; cite a link for every fact. 4) Call save_findings with what you found (one entry per "
    "source, exact URLs). 5) If the topic isn't active yet, ask once whether to follow it in future digests, and "
    "on yes call track_topic.")


class _Call:
    """Logs one tool call to the user's conversation and turns anticipated failures into errors the model can
    read. Security events (access denied, items withheld) go to audit_log."""

    def __init__(self, svc: Services, name: str, args: dict, mcp_ctx: Context | None = None):
        self.svc, self.name, self.args = svc, name, args
        self.ctx: UserContext | None = None
        self.started = time.monotonic()
        self.session_id, self.client = _session(mcp_ctx)
        self.record = CallRecord(tool=name, args=args)
        self.conversation_id = None
        self.memory = Memory()

    async def user(self) -> UserContext:
        try:
            self.ctx = await resolve_user(self.svc.settings, self.svc.token(), self.svc.repos.users)
        except IdentityError as e:
            await self.svc.audit.event("access_denied", mcp_session_id=self.session_id, tool=self.name,
                                       reason=str(e))
            raise ToolError(str(e)) from None
        try:
            await self.svc.repos.users.touch(self.ctx.user_id)
        except Exception as e:             # noqa: BLE001 - last_seen must never break a tool
            logger.error(f"last_seen update failed: {type(e).__name__}: {e}")
        return self.ctx

    async def done(self, items: int = 0, labels: list[str | None] | None = None, outcome: str = "ok",
                   error: str | None = None, post_ids: list[str] | None = None,
                   digest_ids: list[str] | None = None, widget: bool = False, hidden: int = 0) -> None:
        """Called once the result (or error) is known. Never raises."""
        c = self.ctx
        if c is None:
            return
        r = self.record
        r.ok, r.error, r.count, r.widget = outcome == "ok", error, items, widget
        r.post_ids, r.digest_ids = [str(p) for p in post_ids or []], [str(d) for d in digest_ids or []]
        r.duration_ms = int((time.monotonic() - self.started) * 1000)
        if hidden:
            await self.svc.audit.event("sensitivity_filtered", user_id=c.user_id, channel=c.channel,
                                       mcp_session_id=self.session_id, tool=self.name, count=hidden,
                                       max_allowed=c.max_sensitivity)
        self.conversation_id = await self.svc.conversations.log(
            user_id=user_key(c.user_id), channel=c.channel, mcp_session_id=self.session_id, client=self.client,
            record=r)

    async def remember(self, ctx: UserContext) -> None:
        """Saves the call's signals as detected insights and, on the first call of a conversation, reads what's
        known about the user. A failure here never breaks the tool."""
        try:
            first = not await self.svc.conversations.active(user_key(ctx.user_id), ctx.channel, self.session_id)
            chat_id = self.record.word_ids.setdefault("chat", ObjectId()) if self.record.chat() else None
            goals = []
            for sig in self.args.get("signals") or []:
                try:
                    r = await insights_domain.save_insight(
                        self.svc.repos, ctx, sig["text"], label=insights_domain.PRIVATE if sig.get("private") else None,
                        message_id=chat_id, kind=sig["kind"], origin="detected")
                except DomainError:
                    continue
                if r.duplicate:
                    self.memory.duplicates += 1
                    continue
                self.memory.saved.append(r.id)
                if sig["kind"] == "goal":
                    goals.append(sig["text"])
            if goals:
                active = {g.lower() for g in (await profile_domain.get_profile(self.svc.repos, ctx)).goals}
                self.memory.goals_to_offer = [g for g in goals if g.lower() not in active]
            # The digest and the profile usually open a chat, and a new chat can't be told from the last one
            # without a session id: they always bring the profile; other tools only after a quiet spell.
            if first or self.name in ALWAYS_ABOUT:
                self.memory.about_user = await insights_domain.about_user(self.svc.repos, ctx) or None
        except Exception as e:             # noqa: BLE001 - memory must never break a tool
            logger.error(f"remember() failed in {self.name}: {type(e).__name__}: {e}")

    async def _link(self, ctx: UserContext) -> None:
        if self.conversation_id is None:
            return
        for insight_id in self.memory.saved:
            await self.svc.repos.users.link_insight(ctx.user_id, insight_id, self.conversation_id,
                                                    self.record.word_ids.get("chat"))

    def _attach(self, result):
        m = self.memory
        if not (m.goals_to_offer or m.about_user):
            return result
        if isinstance(result, CallToolResult):
            extra = {"goals_to_offer": m.goals_to_offer, "about_user": m.about_user}
            if isinstance(result.structured_content, dict):
                result.structured_content.update(extra)
            for block in result.content:
                if isinstance(block, TextContent):
                    block.text += "\nAbout the user (adapt to it): " + json.dumps(extra, ensure_ascii=False)
                    break
            return result
        if isinstance(result, out.MemoryOut):
            result.goals_to_offer = m.goals_to_offer
            if not result.about_user:
                result.about_user = m.about_user
        return result

    async def run(self, fn: Callable[[UserContext], Awaitable]):
        ctx = await self.user()
        try:
            await self.remember(ctx)
            result = await fn(ctx)
        except DomainError as e:
            await self.done(outcome="error", error=str(e))
            raise ToolError(str(e)) from None
        except Exception as e:
            await self.done(outcome="error", error=f"{type(e).__name__}: {e}")
            raise
        try:
            await self._link(ctx)
        except Exception as e:             # noqa: BLE001
            logger.error(f"linking insights failed in {self.name}: {type(e).__name__}: {e}")
        return self._attach(result)


def register_tools(mcp: MCPServer, svc: Services) -> None:

    @mcp.tool(title="Today's news digest", annotations=READ_ONLY, meta=widgets.tool_meta(widgets.DIGEST_UI))
    async def get_digest(
        mcp_ctx: Context,
        user_message: UserMessage,
        side: Annotated[Literal["ua", "ru", "topics", "both"], Field(
            description="ua: Ukrainian sources; ru: Russian sources (claims, not facts); topics: only news on the "
                        "topics the user follows; both: all of it (the default)")] = "both",
        date: Annotated[str | None, Field(
            description="Kyiv date YYYY-MM-DD; omit for the latest digest")] = None,
        as_text: Annotated[bool, Field(description=(
            "true only if the user asks for the digest as text (e.g. the cards didn't show up)"))] = False,
        signals: Signals = None,
    ) -> Annotated[CallToolResult, out.DigestResult]:
        """Get the user's daily news digest: sections ordered by the user's interests, every item with its
        source links (source_urls) and post ids (source_post_ids). If the user follows topics (track_topic), the
        first digest (side "topics") holds today's news on them, one section per topic: lead with it.

        If the result says shown_in_widget, the user already sees the digest as interactive cards with rating
        buttons: do not repeat, list or summarize it; reply with one short sentence at most, unless the user
        asked something specific. Otherwise present it in text: items from Ukrainian sources as reported news,
        items with is_russian_source_claim=true as claims of Russian channels ("Russian channels claim..."),
        never as facts; cite source_urls; show every item with its ref number (#1, #2, …) so the user can refer
        to it and rate it. Offer short follow-ups: more detail on item N (get_post with its source_post_ids),
        a shorter version, or the sources."""
        call = _Call(svc, "get_digest", {"side": side, "date": date, "as_text": as_text,
                                         **_common(user_message, signals)}, mcp_ctx)
        widget = not as_text and widgets.client_shows_widgets(mcp_ctx)

        async def body(ctx: UserContext) -> CallToolResult:
            await topics_domain.sync_if_due(svc.repos)
            res = await digests_domain.get_digests(svc.repos, ctx, side, date)
            result = _digest_out(res)
            if not result.digests:
                result.note = "No digest found for this date. Say so; do not invent news."
            result.shown_in_widget = widget and bool(result.digests)
            await _renewals(svc, ctx, result)
            await _digest_done(call, res, result)
            return CallToolResult(content=[TextContent(type="text", text=_model_text(result))],
                                  structured_content=result.model_dump(mode="json"))
        return await call.run(body)

    @mcp.tool(title="Morning digest, if new", meta=widgets.tool_meta(widgets.DIGEST_UI),
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                          openWorldHint=False))
    async def get_morning_digest(
        mcp_ctx: Context,
        user_message: UserMessage,
        signals: Signals = None,
    ) -> Annotated[CallToolResult, out.MorningDigestResult]:
        """For a scheduled morning delivery: today's digest, only if it is new for this user. status new: present
        it (it's marked delivered now); not_ready: today's digest isn't complete yet, say nothing about news;
        already_sent: it was delivered before, don't present it again. For a user asking for the digest in
        chat, use get_digest instead. Present a new digest as get_digest describes (cards when shown_in_widget,
        else text with #numbers, links, Russian items as claims)."""
        call = _Call(svc, "get_morning_digest", _common(user_message, signals), mcp_ctx)
        widget = widgets.client_shows_widgets(mcp_ctx)

        async def body(ctx: UserContext) -> CallToolResult:
            await topics_domain.sync_if_due(svc.repos)
            m = await digests_domain.morning_digest(svc.repos, ctx)
            if m.status != "new":
                result = out.MorningDigestResult(
                    status=m.status, date=m.result.date if m.result else None, digests=[],
                    missing_sides=m.result.missing_sides if m.result else [],
                    note=("Today's digest isn't complete yet." if m.status == "not_ready"
                          else "Today's digest was already delivered.") + " Don't present any news.")
                await call.done(items=0)
                return CallToolResult(content=[TextContent(type="text", text=result.model_dump_json())],
                                      structured_content=result.model_dump(mode="json"))
            result = _digest_out(m.result, out.MorningDigestResult, status="new")
            result.shown_in_widget = widget and bool(result.digests)
            await _renewals(svc, ctx, result)
            result.analysis_due = await _analysis_due(svc, ctx)
            await _digest_done(call, m.result, result)
            return CallToolResult(content=[TextContent(type="text", text=_model_text(result, "status: new\n"))],
                                  structured_content=result.model_dump(mode="json"))
        return await call.run(body)

    @mcp.tool(title="Search news posts", annotations=READ_ONLY, meta=widgets.tool_meta(widgets.SEARCH_UI))
    async def search_posts(
        mcp_ctx: Context,
        user_message: UserMessage,
        query: Annotated[str, Field(min_length=2, max_length=300,
                                    description="Key words, e.g. 'Brave1 гранти РЕБ' (Ukrainian or Russian)")],
        side: Annotated[Literal["ua", "ru", "all"], Field(description="Source side")] = "all",
        category: Annotated[str | None, Field(
            description="Optional category key, e.g. front, defense_tech, investments, ru_front")] = None,
        days: Annotated[int, Field(ge=1, le=30, description="How many past days to search")] = 7,
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
        signals: Signals = None,
    ) -> Annotated[CallToolResult, out.SearchResult]:
        """Search the news base (summaries of Telegram posts from Ukrainian and Russian channels) by key words.

        Answer only from the results and cite each fact's post_url. If the results don't answer the question,
        say "I don't have that in the data"; search again (once, other words or more days) only if the result
        list is empty, because every search shows the user another set of result cards. Results with
        is_russian_source_claim=true are claims of Russian sources, not facts. When you list results, show their
        ref numbers (#1, #2, …). If the result says shown_in_widget, the user already sees the results as cards
        with links: answer in your own words citing #N, without listing the posts again."""
        call = _Call(svc, "search_posts", {"query": query, "side": side, "category": category,
                                           "days": days, "limit": limit, **_common(user_message, signals)}, mcp_ctx)

        widget = widgets.client_shows_widgets(mcp_ctx)

        async def body(ctx: UserContext) -> CallToolResult:
            views, words = await posts_domain.search(svc.repos, ctx, query, side, category, days, limit)
            note = None
            if not words:
                note = "The query has no searchable words. Use 2-4 specific key words."
            elif not views:
                note = "Nothing found. Say you don't have this in the data, or retry with other words or more days."
            await call.done(items=len(views), labels=[v.post.sensitivity for v in views],
                            post_ids=[v.post.id for v in views], widget=widget and bool(views))
            results = [_post_out(v).model_copy(update={"ref": i}) for i, v in enumerate(views, 1)]
            result = out.SearchResult(query=query, search_words=words, days=days, results=results, note=note,
                                      shown_in_widget=widget and bool(results))
            # The model still needs the summaries to answer; with cards on screen it is told not to list them.
            text = result.model_dump_json()
            if result.shown_in_widget:
                text = widgets.SEARCH_NOTE.format(n=len(results)) + text
            return CallToolResult(content=[TextContent(type="text", text=text)],
                                  structured_content=result.model_dump(mode="json"))
        return await call.run(body)

    @mcp.tool(title="Get one post", annotations=READ_ONLY)
    async def get_post(
        mcp_ctx: Context,
        user_message: UserMessage,
        post_id: Annotated[str, Field(description="A post_id / source_post_ids value from another tool")],
        signals: Signals = None,
    ) -> out.PostDetail:
        """Get one news post (summary, tags, channel, link) by id. Cite its post_url."""
        call = _Call(svc, "get_post", {"post_id": post_id, **_common(user_message, signals)}, mcp_ctx)

        async def body(ctx: UserContext) -> out.PostDetail:
            view = await posts_domain.get_post(svc.repos, ctx, post_id)
            await call.done(items=1, labels=[view.post.sensitivity], post_ids=[view.post.id])
            return out.PostDetail(**_post_out(view).model_dump())
        return await call.run(body)

    @mcp.tool(title="The user's interests", annotations=READ_ONLY)
    async def get_profile(
        mcp_ctx: Context,
        user_message: UserMessage,
        signals: Signals = None,
    ) -> out.ProfileOut:
        """The user's goals, today's chosen categories, strongest interests (categories and tags by score) and
        what they told you about themselves (about_user). Use it when they ask about their goals, interests or
        profile, and to explain why something matters to them; don't recite it unasked."""
        call = _Call(svc, "get_profile", _common(user_message, signals), mcp_ctx)

        async def body(ctx: UserContext) -> out.ProfileOut:
            p = await profile_domain.get_profile(svc.repos, ctx)
            await call.done(items=len(p.goals) + len(p.top_categories) + len(p.top_tags))
            return out.ProfileOut(
                categories_today=p.categories_today, goals=p.goals,
                top_categories=[out.CategoryScore(side=s, category=c, score=v) for s, c, v in p.top_categories],
                top_tags=[out.TagScore(tag=t, score=v) for t, v in p.top_tags],
                insights=out.InsightCount(saved=p.insights_saved, visible_here=p.insights_visible),
                topics=[_topic_out(t) for t in await topics_domain.list_topics(svc.repos, ctx)],
                about_user=await insights_domain.about_user(svc.repos, ctx))
        return await call.run(body)

    @mcp.tool(title="Log the user's message",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                          openWorldHint=False))
    async def log_turn(
        mcp_ctx: Context,
        user_message: UserMessage,
        previous_reply: Annotated[str | None, Field(max_length=300, description=(
            "One line: what your previous reply in this chat said or did (omit on the first message)"))] = None,
        signals: Signals = None,
    ) -> out.TurnLogged:
        """Record the user's message in their News brain when no other News brain tool fits it (thanks,
        corrections, opinions, things about themselves, small follow-ups). Every other News brain tool already
        records the message it was called for, so don't call log_turn in addition to them. Pass the message
        verbatim; put anything durable it reveals into `signals`, in the user's own words."""
        args = {"previous_reply": previous_reply, **_common(user_message, signals)}
        call = _Call(svc, "log_turn", args, mcp_ctx)

        async def body(ctx: UserContext) -> out.TurnLogged:
            await call.done(items=len(call.memory.saved))
            return out.TurnLogged(saved=len(call.memory.saved), duplicates=call.memory.duplicates)
        return await call.run(body)

    @mcp.tool(title="Remember something the user shared",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                          openWorldHint=False))
    async def save_insight(
        mcp_ctx: Context,
        user_message: UserMessage,
        text: Annotated[str, Field(min_length=1, max_length=4000, description=(
            "What the user shared, verbatim and in the user's language"))],
        kind: Annotated[Literal["fact", "goal", "interest", "preference", "feedback", "known", "other"], Field(
            description="What it is: see log_turn's signal kinds; fact for knowledge about their work")] = "fact",
        area: Annotated[str | None, Field(max_length=60, description=(
            "Optional short area, e.g. 'інвестиції', 'робота', 'люди', 'як аналізувати'"))] = None,
        tags: Annotated[list[str] | None, Field(max_length=10, description="Optional short tags")] = None,
        sensitivity: Annotated[Literal["public", "internal", "confidential", "restricted"] | None, Field(
            description="Default internal (you can recall it later). confidential only if the user says it's "
                        "private: then only their local client can read it")] = None,
        signals: Signals = None,
    ) -> out.InsightSaved:
        """Save something the user explicitly asks you to remember ("запам'ятай", "remember") or clearly wants
        kept: knowledge from their work, judgements, how they want analysis done, people and organizations. This
        is the user's News brain memory: save it here even if you also keep your own memory. Keep `text`
        verbatim, in the user's language. (Things detected in ordinary messages go through log_turn signals.)"""
        args = {"text": text, "kind": kind, "area": area, "tags": tags, "sensitivity": sensitivity}
        call = _Call(svc, "save_insight", {**args, **_common(user_message, signals)}, mcp_ctx)
        message_id = call.record.word_ids.setdefault("insight", ObjectId())   # the logged user message

        async def body(ctx: UserContext) -> out.InsightSaved:
            saved = await insights_domain.save_insight(svc.repos, ctx, text, area, tags, sensitivity, message_id,
                                                       kind=kind)
            await call.done(items=0 if saved.duplicate else 1)
            if call.conversation_id is not None and saved.id is not None:
                await svc.repos.users.link_insight(ctx.user_id, saved.id, call.conversation_id)
            note = ("" if saved.visible_here else " (приватно: у цьому чаті я не зможу його прочитати)")
            return out.InsightSaved(insight_id=str(saved.id) if saved.id else "", sensitivity=saved.sensitivity,
                                    visible_here=saved.visible_here,
                                    confirmation=("Це вже збережено" if saved.duplicate else "Запам'ятав") + note)
        return await call.run(body)

    @mcp.tool(title="Recall what the user shared", annotations=READ_ONLY)
    async def get_insights(
        mcp_ctx: Context,
        user_message: UserMessage,
        query: Annotated[str | None, Field(max_length=300, description="Optional key words")] = None,
        kind: Annotated[Literal["fact", "goal", "interest", "preference", "feedback", "known", "other"] | None,
                        Field(description="Optional: only this kind")] = None,
        area: Annotated[str | None, Field(max_length=60, description="Optional area, as saved")] = None,
        signals: Signals = None,
    ) -> out.InsightsResult:
        """Recall what the user told you about themselves (saved with save_insight or detected by log_turn),
        newest first or best match for `query`. Use it when the user asks what you remember about them
        ("що ти про мене пам'ятаєш") and when their own context would make an answer better. Only insights this
        connection may read come back; `hidden` counts the others."""
        call = _Call(svc, "get_insights", {"query": query, "kind": kind, "area": area,
                                           **_common(user_message, signals)}, mcp_ctx)

        async def body(ctx: UserContext) -> out.InsightsResult:
            r = await insights_domain.get_insights(svc.repos, ctx, query, area, kind=kind)
            await call.done(items=len(r.insights), labels=[i.sensitivity for i in r.insights], hidden=r.hidden)
            note = None
            if not r.insights:
                note = ("Nothing this connection may read" + (f" ({r.hidden} private)" if r.hidden else "")
                        + ". Say so; don't guess what the user saved.")
            return out.InsightsResult(hidden=r.hidden, note=note, insights=[
                out.InsightOut(insight_id=i.id, text=i.text, area=i.area, tags=i.tags, sensitivity=i.sensitivity,
                               created_at=i.created_at.isoformat() if i.created_at else None) for i in r.insights])
        return await call.run(body)

    @mcp.tool(title="Rate news or an answer",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
    async def record_feedback(
        mcp_ctx: Context,
        # Optional here only: the rating cards call this tool directly (older cards sent no message). Claude
        # should always pass it, as with every other tool.
        user_message: Annotated[str | None, Field(max_length=8000, description=(
            "The user's latest chat message, verbatim and complete: the message that led to this call. "
            "Always pass it"))] = None,
        reaction: Annotated[Literal["useful", "not_useful", "neutral", "like", "dislike", "skip"] | None, Field(
            description="useful (👍), not_useful (👎) or neutral (50/50). Omit only to add a comment to an earlier "
                        "rating (feedback_id, or the rated item's ids with text)")] = None,
        target_type: Annotated[Literal["post", "digest_item", "answer", "suggestion"] | None, Field(
            description="post (the default): a post from search_posts/get_post; digest_item: an item of "
                        "get_digest; answer / suggestion: your own answer, analysis or suggestion. "
                        "Omit with feedback_id")] = None,
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
        signals: Signals = None,
    ) -> out.FeedbackOut:
        """Save the user's rating of a news item, or of your own answer or suggestion; news ratings tune which
        news they see first.

        Call it only when the user clearly rates something, never on your own judgement. Map "the third one"
        to ref 3 of the most recent get_digest or search_posts result and pass that item's ids (digest items:
        digest_id + n as item_n; posts: post_id); if it is unclear which item is meant, ask. Never invent ids.
        Take reasons from the user's words and keep text verbatim. For your own answer or suggestion use
        target_type answer / suggestion with a one-line `about` and the post ids you cited. Several ratings in
        one message mean several calls. A comment that follows a rating goes to that rating's feedback_id; if
        you don't know it (e.g. the user rated with the cards' buttons), pass the item's ids and the text without
        a reaction, and it is added to the user's earlier rating of that item.
        Rating the same post or digest item again replaces the earlier rating. Tell the user the
        confirmation."""
        args = {"target_type": target_type, "reaction": reaction, "post_id": post_id, "digest_id": digest_id,
                "item_n": item_n, "about": about, "cited_post_ids": cited_post_ids, "reasons": reasons,
                "text": text, "feedback_id": feedback_id}
        call = _Call(svc, "record_feedback", {**args, **_common(user_message, signals)}, mcp_ctx)

        async def body(ctx: UserContext) -> out.FeedbackOut:
            r = await feedback_domain.record_feedback(svc.repos, ctx, mcp_channels=svc.mcp_channels, **args)
            await call.done(items=1)
            return out.FeedbackOut(feedback_id=r.feedback_id, target_type=r.target_type, reaction=r.reaction,
                                   replaced_previous=r.replaced_previous, score_changes=r.score_changes,
                                   confirmation=r.confirmation,
                                   offer_deep_dive=r.target_type in ("post", "digest_item")
                                   and topics_domain.wants_more(text, reasons))
        return await call.run(body)

    @mcp.tool(title="Add or remove a goal",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
    async def set_goal(
        mcp_ctx: Context,
        user_message: UserMessage,
        action: Annotated[Literal["add", "remove"], Field(description="add a new goal, or remove an existing one")],
        text: Annotated[str, Field(min_length=1, max_length=300, description=(
            "add: the goal in the user's words, e.g. 'стежити за розвитком НРК'; "
            "remove: the goal's text exactly as get_profile returns it"))],
        signals: Signals = None,
    ) -> out.GoalsOut:
        """Add or remove one of the user's goals (what they most want to achieve; any number). Goals shape which
        news matter to them. Call it only when the user explicitly asks to set, change or drop a goal, never on
        your own judgement. To change a goal, remove the old one and add the new one."""
        call = _Call(svc, "set_goal", {"action": action, "text": text, **_common(user_message, signals)},
                     mcp_ctx)

        async def body(ctx: UserContext) -> out.GoalsOut:
            if action == "add":
                r = await goals_domain.add_goal(svc.repos, ctx, text)
                result = out.GoalsOut(goals=r.goals, added=r.changed)
                if svc.learner is not None:                # map the new goal to keywords soon (auto.py)
                    svc.learner.nudge(user_key(ctx.user_id))
            else:
                r = await goals_domain.remove_goal(svc.repos, ctx, text)
                result = out.GoalsOut(goals=r.goals, removed=r.changed)
            await call.done(items=1)
            return result
        return await call.run(body)

    @mcp.tool(title="Dig deeper into a news item", annotations=READ_ONLY)
    async def deep_dive(
        mcp_ctx: Context,
        user_message: UserMessage,
        digest_id: Annotated[str | None, Field(description="A digest item: its digest_id")] = None,
        item_n: Annotated[int | None, Field(description="A digest item: its n (not ref)")] = None,
        post_id: Annotated[str | None, Field(description="Or a post: its post_id")] = None,
        subject: Annotated[str | None, Field(max_length=600, description=(
            "Or, with no item, what to dig into in words, e.g. «Спектр-К»"))] = None,
        query: Annotated[str | None, Field(max_length=120, description=(
            "The thing's name to search the news base for (e.g. «Спектр-К»); default: names quoted in the item"))]
        = None,
        name: Annotated[str | None, Field(max_length=80, description=(
            "The topic's short name if the user already follows it"))] = None,
        signals: Signals = None,
    ) -> out.DeepDiveResult:
        """Start a deep dive when the user wants more on a news item or thing ("більше про це", "надо больше
        информации", "🔎 Глибше про #N", a comment asking for details): returns what the news base already has
        (the item, its source posts, related posts, the user's earlier findings on it) and the next steps. The
        news base rarely has much more on one incident: after this call, search the web yourself, then save what
        you found with save_findings. Map "#N" to the latest digest or search result's ref and pass its ids."""
        args = {"digest_id": digest_id, "item_n": item_n, "post_id": post_id, "subject": subject, "query": query,
                "name": name}
        call = _Call(svc, "deep_dive", {**args, **_common(user_message, signals)}, mcp_ctx)

        async def body(ctx: UserContext) -> out.DeepDiveResult:
            seed = await topics_domain.deep_dive(svc.repos, ctx, digest_id=digest_id, item_n=item_n,
                                                 post_id=post_id, text=subject, query=query, name=name)
            views = seed.source_posts + seed.related_posts
            await call.done(items=len(views), labels=[v.post.sensitivity for v in views],
                            post_ids=[v.post.id for v in views])
            sub = seed.subject
            return out.DeepDiveResult(
                subject=out.DeepDiveSubject(text=sub.text, source_urls=sub.source_urls,
                                            is_russian_source_claim=sub.is_russian_source_claim,
                                            digest_id=sub.digest_id, item_n=sub.item_n, post_id=sub.post_id),
                source_posts=[_post_out(v) for v in seed.source_posts],
                related_posts=[_post_out(v).model_copy(update={"ref": i}) for i, v in enumerate(seed.related_posts, 1)],
                searched_for=seed.searched_for, goals=seed.goals,
                topic=_topic_out(seed.topic, findings=True) if seed.topic else None, next_steps=DEEP_DIVE_STEPS)
        return await call.run(body)

    @mcp.tool(title="Save what a deep dive found",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
    async def save_findings(
        mcp_ctx: Context,
        user_message: UserMessage,
        topic: Annotated[str, Field(min_length=2, max_length=80, description=(
            "The topic's short name, the thing itself, e.g. «Спектр-К»; the same name as before for the same thing"))],
        findings: Annotated[list[out.FindingIn], Field(min_length=1, max_length=15, description=(
            "What you found on the web: one entry per source, with its exact URL"))],
        digest_id: Annotated[str | None, Field(description="The item the dive started from: digest_id")] = None,
        item_n: Annotated[int | None, Field(description="… and its n")] = None,
        post_id: Annotated[str | None, Field(description="Or the post it started from")] = None,
        signals: Signals = None,
    ) -> out.TopicSaved:
        """Save what you found on the web during a deep dive, on the user's topic (created if new; not followed
        until the user agrees, see track_topic). Saved findings come back in the next deep_dive and feed the
        weekly and monthly analysis. Only real search results with their exact URLs; never invent sources."""
        call = _Call(svc, "save_findings", {"topic": topic, "findings": [f.model_dump() for f in findings],
                                            "digest_id": digest_id, "item_n": item_n, "post_id": post_id,
                                            **_common(user_message, signals)}, mcp_ctx)

        async def body(ctx: UserContext) -> out.TopicSaved:
            r = await topics_domain.save_findings(svc.repos, ctx, topic, [f.model_dump() for f in findings],
                                                  digest_id=digest_id, item_n=item_n, post_id=post_id)
            await call.done(items=r.added_findings)
            note = f" (вже були: {r.duplicate_findings})" if r.duplicate_findings else ""
            return out.TopicSaved(topic=_topic_out(r.topic), created=r.created, added_findings=r.added_findings,
                                  duplicate_findings=r.duplicate_findings,
                                  confirmation=f"Зберіг знахідки по «{r.topic.name}»: {r.added_findings}{note}")
        return await call.run(body)

    @mcp.tool(title="Follow a topic in future news",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
    async def track_topic(
        mcp_ctx: Context,
        user_message: UserMessage,
        name: Annotated[str, Field(min_length=2, max_length=80, description="The topic's short name, e.g. «Спектр-К»")],
        action: Annotated[Literal["follow", "stop"], Field(description=(
            "follow: start or renew following it; stop: stop following it"))] = "follow",
        specific: Annotated[list[str] | None, Field(max_length=20, description=(
            "follow: names and aliases as Ukrainian, Russian and English news write them (e.g. «Рассвет», "
            "«Rassvet», «Бюро 1440»), for recognising news on it; short is fine here. If the topic is an area "
            "rather than a named thing (e.g. «оптоволоконні FPV»), its key phrases are its names"))] = None,
        web_queries: Annotated[list[str] | None, Field(max_length=12, description=(
            "follow: what the news pipeline searches the web for daily: 3-8 phrases of 2-6 words, each finding "
            "THIS thing and nothing else, so a name that is also an everyday word or another company gets what "
            "it is, and the name itself in double quotes for an exact match (e.g. '\"Рассвет\" Бюро 1440 "
            "спутники', 'спутниковая связь \"Рассвет\" Геран', '\"Рассвет-3\"', '\"Rassvet\" satellite "
            "constellation Bureau 1440', 'супутники \"Рассвет\" Бюро 1440'). The server quotes the name if you "
            "don't. In Russian (searched in Russian sources), Ukrainian (Ukrainian sources) and English "
            "(worldwide). Always pass them when following"))] = None,
        broader: Annotated[list[str] | None, Field(max_length=20, description=(
            "follow: short phrases for the thing's area, Ukrainian and Russian forms (e.g. «захист відеоканалу "
            "від РЕБ», «видеоприемник FPV»): they lift news on it that doesn't name it; never searched on the web"))]
        = None,
        exclude: Annotated[list[str] | None, Field(max_length=20, description=(
            "follow: words that mark a look-alike that is NOT this topic (other products with a similar name)"))]
        = None,
        tags: Annotated[list[str] | None, Field(max_length=4, description=(
            "follow: 1-4 tags from the fixed tag list that its news would carry"))] = None,
        goal: Annotated[str | None, Field(max_length=300, description="follow: the user's goal it serves, as "
                                                                       "get_profile returns it")] = None,
        days: Annotated[int, Field(ge=1, le=180, description="follow: for how many days (default 30)")] = 30,
        digest_id: Annotated[str | None, Field(description="The item it started from: digest_id")] = None,
        item_n: Annotated[int | None, Field(description="… and its n")] = None,
        post_id: Annotated[str | None, Field(description="Or the post it started from")] = None,
        signals: Signals = None,
    ) -> out.TopicSaved:
        """Follow a specific thing in the user's future digests and searches (or stop following it). Call it only
        when the user agrees or asks ("так, відстежуй", "стеж за цим"), never on your own judgement. After a deep
        dive you know the aliases: pass them in specific, the web searches in web_queries, the area in broader,
        look-alikes in exclude."""
        args = {"name": name, "action": action, "specific": specific, "web_queries": web_queries,
                "broader": broader, "exclude": exclude, "tags": tags, "goal": goal, "days": days,
                "digest_id": digest_id, "item_n": item_n, "post_id": post_id}
        call = _Call(svc, "track_topic", {**args, **_common(user_message, signals)}, mcp_ctx)

        async def body(ctx: UserContext) -> out.TopicSaved:
            if action == "stop":
                r = await topics_domain.stop_topic(svc.repos, ctx, name)
                confirmation = f"Більше не відстежую «{r.topic.name}» (знахідки збережено)"
            else:
                r = await topics_domain.track_topic(svc.repos, ctx, name, specific=specific, broader=broader,
                                                    exclude=exclude, tags=tags, goal=goal, days=days,
                                                    digest_id=digest_id, item_n=item_n, post_id=post_id,
                                                    queries=web_queries)
                confirmation = f"Відстежую «{r.topic.name}» до {topics_domain.until_text(r.topic)}"
            await call.done(items=1)
            follow = action == "follow"
            missing = topics_domain.missing_regions(topics_domain.web_queries(r.topic)) if follow else []
            return out.TopicSaved(topic=_topic_out(r.topic), created=r.created, unknown_tags=r.unknown_tags,
                                  missing_regions=missing,
                                  not_searched=topics_domain.not_searched(r.topic) if follow else [],
                                  confirmation=confirmation)
        return await call.run(body)

    @mcp.tool(title="Analysis of a week or a month", annotations=READ_ONLY)
    async def get_analysis(
        mcp_ctx: Context,
        user_message: UserMessage,
        period: Annotated[Literal["week", "month"], Field(description=(
            "week: the last 7 days, month: the last 30 days (ending end_date, default today)"))] = "week",
        start_date: Annotated[str | None, Field(description="Exact first day YYYY-MM-DD (with end_date)")] = None,
        end_date: Annotated[str | None, Field(description="Last day YYYY-MM-DD (Kyiv); default today")] = None,
        focus: Annotated[str | None, Field(max_length=120, description=(
            "Optional: only one goal, tracked topic or thing, e.g. «Спектр-К», «НРК»"))] = None,
        refresh: Annotated[bool, Field(description=(
            "true only if the user asks for a fresh analysis of a period already analysed"))] = False,
        signals: Signals = None,
    ) -> out.AnalysisResult:
        """Analysis of everything found in a week or a month, for this user: not a list of news but what changed,
        trends with numbers, claims versus confirmations, why it matters for their goals and topics, what to watch.
        Use it when the user asks for a weekly / monthly analysis, review, trends or "що змінилося за тиждень",
        and when a morning digest says analysis_due. If the report exists, present it (status stored). Otherwise
        (status to_write) the result is a pack of the period's data: write the analysis from it as next_steps
        says, then save it with save_report."""
        args = {"period": period, "start_date": start_date, "end_date": end_date, "focus": focus,
                "refresh": refresh}
        call = _Call(svc, "get_analysis", {**args, **_common(user_message, signals)}, mcp_ctx)

        async def body(ctx: UserContext) -> out.AnalysisResult:
            p = analysis_domain.period_for(period, start_date, end_date)
            report = None if refresh else await analysis_domain.stored(svc.repos, ctx, p, focus)
            if report is not None:
                await call.done(items=1)
                return out.AnalysisResult(status="stored", period=_period_out(p), focus=focus, title=report.title,
                                          highlights=report.highlights, report=report.text,
                                          written_at=report.created_at.isoformat() if report.created_at else None)
            pack = await analysis_domain.build(svc.repos, ctx, p, focus)
            await call.done(items=len(pack.items))
            status = "to_write" if pack.digests_used else "empty"
            return out.AnalysisResult(
                status=status, period=_period_out(p), previous_period=_period_out(pack.previous), focus=focus,
                goals=pack.goals, tracked_topics=pack.tracked, user_summary=pack.summary, counts=pack.counts,
                selected_from=pack.selected_from, findings=pack.findings,
                items=[out.AnalysisItem(id=i.id, date=i.date, side=i.side, is_russian_source_claim=i.claim,
                                        category=i.category, text=i.text, source_urls=i.source_urls, why=i.why,
                                        goals=i.goals, topics=i.topics) for i in pack.items],
                next_steps=analysis_domain.WRITE_STEPS if status == "to_write"
                else "There are no digests in this period: say so; don't write an analysis.")
        return await call.run(body)

    @mcp.tool(title="Save a written analysis",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                          openWorldHint=False))
    async def save_report(
        mcp_ctx: Context,
        user_message: UserMessage,
        start_date: Annotated[str, Field(description="The analysed period's first day, as get_analysis returned")],
        end_date: Annotated[str, Field(description="… and its last day")],
        title: Annotated[str, Field(min_length=3, max_length=200, description="Short title, e.g. «Тиждень "
                                                                               "23.10–29.10: РЕБ і НРК»")],
        highlights: Annotated[list[str], Field(min_length=1, max_length=7, description=(
            "3-7 one-line key points, most important first"))],
        report: Annotated[str, Field(min_length=200, max_length=20000, description=(
            "The full analysis as you presented it (markdown), with its source links"))],
        focus: Annotated[str | None, Field(max_length=120, description="The focus passed to get_analysis, if any")]
        = None,
        signals: Signals = None,
    ) -> out.ReportSaved:
        """Save the analysis you wrote from get_analysis, so it is kept, comes back next time without rewriting, and
        feeds later analyses. Call it once per analysis, right after presenting it."""
        call = _Call(svc, "save_report", {"start_date": start_date, "end_date": end_date, "focus": focus,
                                          "title": title, "highlights": highlights, "report_chars": len(report),
                                          **_common(user_message, signals)}, mcp_ctx)

        async def body(ctx: UserContext) -> out.ReportSaved:
            p = analysis_domain.period_for("week", start_date, end_date)
            r = await analysis_domain.save_report(svc.repos, ctx, p, focus, title, highlights, report)
            await call.done(items=1)
            return out.ReportSaved(period=_period_out(p), focus=focus, title=r.title,
                                   confirmation=f"Аналіз за {_period_out(p).label} збережено")
        return await call.run(body)
