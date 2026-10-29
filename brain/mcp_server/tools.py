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
from typing import Annotated, Awaitable, Callable, Literal

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
from brain.domain import digests as digests_domain
from brain.domain import feedback as feedback_domain
from brain.domain import goals as goals_domain
from brain.domain import insights as insights_domain
from brain.domain import posts as posts_domain
from brain.domain import profile as profile_domain
from brain.domain import sensitivity
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
            side=v.digest.side, side_note=v.side_note, digest_id=v.digest.id,
            created_at=v.digest.created_at.isoformat(), period_start=v.digest.period_start,
            period_end=v.digest.period_end, hidden_items=v.hidden_items,
            sections=[out.DigestSectionOut(category=s.category, title=s.title, items=[
                out.DigestItemOut(ref=next(refs), n=v.number(i), text=i.text, source_urls=i.source_urls,
                                  source_post_ids=i.source_post_ids, is_russian_source_claim=v.digest.side == "ru",
                                  why=v.why(i).why, relevance=v.why(i).score) for i in items])
                for s, items in v.sections])
        for v in res.digests])


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
        side: Annotated[Literal["ua", "ru", "both"], Field(
            description="ua: Ukrainian sources; ru: Russian sources (claims, not facts); both: both")] = "both",
        date: Annotated[str | None, Field(
            description="Kyiv date YYYY-MM-DD; omit for the latest digest")] = None,
        as_text: Annotated[bool, Field(description=(
            "true only if the user asks for the digest as text (e.g. the cards didn't show up)"))] = False,
        signals: Signals = None,
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
        call = _Call(svc, "get_digest", {"side": side, "date": date, "as_text": as_text,
                                         **_common(user_message, signals)}, mcp_ctx)
        widget = not as_text and widgets.client_shows_widgets(mcp_ctx)

        async def body(ctx: UserContext) -> CallToolResult:
            res = await digests_domain.get_digests(svc.repos, ctx, side, date)
            result = _digest_out(res)
            if not result.digests:
                result.note = "No digest found for this date. Say so; do not invent news."
            result.shown_in_widget = widget and bool(result.digests)
            await _digest_done(call, res, result)
            # What the model reads: a short index when the cards are on screen, else the full digest.
            text = widgets.digest_index(result) if result.shown_in_widget else result.model_dump_json()
            return CallToolResult(content=[TextContent(type="text", text=text)],
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
            await _digest_done(call, m.result, result)
            text = widgets.digest_index(result) if result.shown_in_widget else result.model_dump_json()
            return CallToolResult(content=[TextContent(type="text", text="status: new\n" + text)],
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
                                   confirmation=r.confirmation)
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
        """Add or remove one of the user's goals (what they most want to achieve; at most 5). Goals shape which
        news matter to them. Call it only when the user explicitly asks to set, change or drop a goal, never on
        your own judgement. To change a goal, remove the old one and add the new one."""
        call = _Call(svc, "set_goal", {"action": action, "text": text, **_common(user_message, signals)},
                     mcp_ctx)

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
