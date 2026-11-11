"""One handler per tool: what the tool does, whoever calls it (MCP, our web chat). `run()` is the entry point.

A handler gets the Call (services, origin, logging) and the tool's own arguments (user_message and signals are
the Call's), resolves the user through the Call and returns the tool's result model, or a CallToolResult for the
tools whose text for the model differs from their structured result.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from bson import ObjectId
from mcp.types import CallToolResult, TextContent

from brain.db.repositories import user_key
from brain.domain import analysis as analysis_domain
from brain.domain import corrections as corrections_domain
from brain.domain import digests as digests_domain
from brain.domain import feedback as feedback_domain
from brain.domain import goals as goals_domain
from brain.domain import insights as insights_domain
from brain.domain import posts as posts_domain
from brain.domain import profile as profile_domain
from brain.domain import proposals as proposals_domain
from brain.domain import topics as topics_domain
from brain.domain.identity import UserContext
from brain.mcp_server import schemas as out
from brain.mcp_server import widgets
from brain.tools.base import Call, Origin, Services, common_args, log_args, proposal_out

logger = logging.getLogger("brain.tools")


# ---- results ----------------------------------------------------------------------------------------------

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


async def _renewals(svc: Services, ctx: UserContext, result: out.DigestResult) -> None:
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


async def _proposals(svc: Services, ctx: UserContext, result: out.DigestResult) -> None:
    """Adds the open proposals (at most proposals_domain.SHOW_MAX). Never breaks the digest."""
    try:
        result.proposals = [proposal_out(p) for p in await proposals_domain.open_for(svc.repos, ctx)]
    except Exception as e:                 # noqa: BLE001
        logger.error(f"proposals failed: {type(e).__name__}: {e}")


async def _analysis_due(svc: Services, ctx: UserContext) -> out.AnalysisDue | None:
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
    if result.proposals:
        text += ("\nThe system's suggestions for the user" + (" (on screen as cards with Так / Не цікаво buttons: "
                 "mention them in one line at most)" if result.shown_in_widget else
                 " (mention each in one line; on the user's answer call answer_proposal)") + ": " + "; ".join(
                     f"{p.proposal_id} {p.kind} «{p.title}» ({p.why})" + (f" sources: {' '.join(p.links)}"
                                                                           if p.links else "")
                     for p in result.proposals))
    due = getattr(result, "analysis_due", None)
    if due:
        text += (f"\nThe {due.period}ly analysis for {due.label} is due: after this digest, call get_analysis with "
                 f"start_date {due.start_date} and end_date {due.end_date}, write it and save it with save_report.")
    return text


async def _mark_seen(svc: Services, ctx: UserContext, res: digests_domain.DigestResultView) -> None:
    """Remembers today's returned items as shown (users.digest_seen). Never breaks the digest."""
    try:
        await digests_domain.mark_seen(svc.repos, ctx, res)
    except Exception as e:                 # noqa: BLE001
        logger.error(f"marking digest items seen failed: {type(e).__name__}: {e}")


async def _digest_done(call: Call, res: digests_domain.DigestResultView, result: out.DigestResult) -> None:
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


# ---- the handlers -----------------------------------------------------------------------------------------

async def get_digest(call: Call, side: str = "both", date: str | None = None, as_text: bool = False,
                     unseen_only: bool = False):
    svc = call.svc
    widget = not as_text and call.origin.widgets()

    async def body(ctx: UserContext) -> CallToolResult:
        day = digests_domain.unseen_day(date) if unseen_only else date
        await topics_domain.sync_if_due(svc.repos)
        res = await digests_domain.get_digests(svc.repos, ctx, side, day)
        found = bool(res.digests)
        if unseen_only:
            await digests_domain.drop_seen(svc.repos, ctx, res)
        result = _digest_out(res)
        if not found:
            result.note = "No digest found for this date. Say so; do not invent news."
        elif not result.digests:
            result.note = digests_domain.ALL_SEEN_NOTE
        result.shown_in_widget = widget and bool(result.digests)
        await _renewals(svc, ctx, result)
        await _proposals(svc, ctx, result)
        await _mark_seen(svc, ctx, res)
        await _digest_done(call, res, result)
        return CallToolResult(content=[TextContent(type="text", text=_model_text(result))],
                              structured_content=result.model_dump(mode="json"))
    return await call.run(body)


async def get_morning_digest(call: Call):
    svc = call.svc
    widget = call.origin.widgets()

    async def body(ctx: UserContext) -> CallToolResult:
        await topics_domain.sync_if_due(svc.repos)
        m = await digests_domain.morning_digest(svc.repos, ctx)
        if m.status != "new":
            result = out.MorningDigestResult(
                status=m.status, date=m.result.date if m.result else None, digests=[],
                missing_sides=m.result.missing_sides if m.result else [],
                note=("Today's digest isn't complete yet." if m.status == "not_ready"
                      else "Today's digest was already delivered. You may offer to show what the user hasn't "
                           "seen of it yet (get_digest with unseen_only=true).") + " Don't present any news.")
            await call.done(items=0)
            return CallToolResult(content=[TextContent(type="text", text=result.model_dump_json())],
                                  structured_content=result.model_dump(mode="json"))
        result = _digest_out(m.result, out.MorningDigestResult, status="new")
        result.shown_in_widget = widget and bool(result.digests)
        await _renewals(svc, ctx, result)
        await _proposals(svc, ctx, result)
        result.analysis_due = await _analysis_due(svc, ctx)
        await _mark_seen(svc, ctx, m.result)
        await _digest_done(call, m.result, result)
        return CallToolResult(content=[TextContent(type="text", text=_model_text(result, "status: new\n"))],
                              structured_content=result.model_dump(mode="json"))
    return await call.run(body)


async def search_posts(call: Call, query: str, side: str = "all", category: str | None = None, days: int = 7,
                       limit: int = 10):
    svc = call.svc
    widget = call.origin.widgets()

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


async def get_post(call: Call, post_id: str, full_text: bool = False) -> out.PostDetail:
    async def body(ctx: UserContext) -> out.PostDetail:
        view = await posts_domain.get_post(call.svc.repos, ctx, post_id)
        text = await posts_domain.full_text(call.svc.repos, view) if full_text else None
        await call.done(items=1, labels=[view.post.sensitivity], post_ids=[view.post.id])
        return out.PostDetail(**_post_out(view).model_dump(), full_text=text.text if text else None,
                              full_text_note=text.note if text else None)
    return await call.run(body)


async def get_profile(call: Call) -> out.ProfileOut:
    svc = call.svc

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


async def log_turn(call: Call, previous_reply: str | None = None) -> out.TurnLogged:
    async def body(ctx: UserContext) -> out.TurnLogged:
        await call.done(items=len(call.memory.saved))
        return out.TurnLogged(saved=len(call.memory.saved), duplicates=call.memory.duplicates)
    return await call.run(body)


async def save_insight(call: Call, text: str, kind: str = "fact", area: str | None = None,
                       tags: list[str] | None = None, sensitivity: str | None = None) -> out.InsightSaved:
    svc = call.svc
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


async def get_insights(call: Call, query: str | None = None, kind: str | None = None,
                       area: str | None = None) -> out.InsightsResult:
    async def body(ctx: UserContext) -> out.InsightsResult:
        r = await insights_domain.get_insights(call.svc.repos, ctx, query, area, kind=kind)
        await call.done(items=len(r.insights), labels=[i.sensitivity for i in r.insights], hidden=r.hidden)
        note = None
        if not r.insights:
            note = ("Nothing this connection may read" + (f" ({r.hidden} private)" if r.hidden else "")
                    + ". Say so; don't guess what the user saved.")
        return out.InsightsResult(hidden=r.hidden, note=note, insights=[
            out.InsightOut(insight_id=i.id, text=i.text, area=i.area, tags=i.tags, sensitivity=i.sensitivity,
                           created_at=i.created_at.isoformat() if i.created_at else None) for i in r.insights])
    return await call.run(body)


async def record_feedback(call: Call, **args) -> out.FeedbackOut:
    svc = call.svc

    async def body(ctx: UserContext) -> out.FeedbackOut:
        r = await feedback_domain.record_feedback(svc.repos, ctx, mcp_channels=svc.mcp_channels, **args)
        await call.done(items=1)
        return out.FeedbackOut(feedback_id=r.feedback_id, target_type=r.target_type, reaction=r.reaction,
                               replaced_previous=r.replaced_previous, score_changes=r.score_changes,
                               confirmation=r.confirmation,
                               offer_deep_dive=r.target_type in ("post", "digest_item")
                               and topics_domain.wants_more(args.get("text"), args.get("reasons")))
    return await call.run(body)


async def answer_proposal(call: Call, proposal_id: str, answer: str, reason: str | None = None) -> out.ProposalAnswer:
    svc = call.svc

    async def body(ctx: UserContext) -> out.ProposalAnswer:
        r = await proposals_domain.answer(svc.repos, ctx, proposal_id, answer == "accept",
                                          mcp_channels=svc.mcp_channels, reason=reason)
        await call.done(items=1)
        return out.ProposalAnswer(proposal_id=proposal_id, accepted=r.accepted, done=r.done, ask=r.ask)
    return await call.run(body)


async def get_proposals(call: Call) -> out.ProposalsResult:
    async def body(ctx: UserContext) -> out.ProposalsResult:
        found = await proposals_domain.list_open(call.svc.repos, ctx)
        await call.done(items=len(found))
        return out.ProposalsResult(proposals=[proposal_out(p) for p in found])
    return await call.run(body)


async def set_agent_prefs(call: Call, **args) -> out.AgentPrefsOut:
    async def body(ctx: UserContext) -> out.AgentPrefsOut:
        p = await proposals_domain.set_prefs(call.svc.repos, ctx, **args)
        await call.done(items=1)
        return out.AgentPrefsOut(cards=p.cards, off=p.off, mentions=p.mentions)
    return await call.run(body)


async def correct_profile(call: Call, not_interested: str) -> out.CorrectionResult:
    async def body(ctx: UserContext) -> out.CorrectionResult:
        r = await corrections_domain.not_interested(call.svc.repos, ctx, not_interested)
        await call.done(items=len(r.dropped_interests) + len(r.dropped_intents))
        return out.CorrectionResult(not_interested=r.text, dropped_interests=r.dropped_interests,
                                    dropped_intents=r.dropped_intents, closed_proposals=r.closed_proposals,
                                    followed_matching=r.followed_matching)
    return await call.run(body)


async def set_goal(call: Call, action: str, text: str) -> out.GoalsOut:
    svc = call.svc

    async def body(ctx: UserContext) -> out.GoalsOut:
        if action == "add":
            r = await goals_domain.add_goal(svc.repos, ctx, text)
            result = out.GoalsOut(goals=r.goals, added=r.changed)
            if svc.learner is not None:                # map the new goal to keywords soon (agents/learner.py)
                svc.learner.nudge(user_key(ctx.user_id))
        else:
            r = await goals_domain.remove_goal(svc.repos, ctx, text)
            result = out.GoalsOut(goals=r.goals, removed=r.changed)
        await call.done(items=1)
        return result
    return await call.run(body)


async def deep_dive(call: Call, digest_id: str | None = None, item_n: int | None = None, post_id: str | None = None,
                    subject: str | None = None, query: str | None = None, name: str | None = None
                    ) -> out.DeepDiveResult:
    async def body(ctx: UserContext) -> out.DeepDiveResult:
        seed = await topics_domain.deep_dive(call.svc.repos, ctx, digest_id=digest_id, item_n=item_n,
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


async def save_findings(call: Call, topic: str, findings: list, digest_id: str | None = None,
                        item_n: int | None = None, post_id: str | None = None) -> out.TopicSaved:
    async def body(ctx: UserContext) -> out.TopicSaved:
        r = await topics_domain.save_findings(call.svc.repos, ctx, topic, [f.model_dump() for f in findings],
                                              digest_id=digest_id, item_n=item_n, post_id=post_id)
        await call.done(items=r.added_findings)
        note = f" (вже були: {r.duplicate_findings})" if r.duplicate_findings else ""
        return out.TopicSaved(topic=_topic_out(r.topic), created=r.created, added_findings=r.added_findings,
                              duplicate_findings=r.duplicate_findings,
                              confirmation=f"Зберіг знахідки по «{r.topic.name}»: {r.added_findings}{note}")
    return await call.run(body)


async def track_topic(call: Call, name: str, action: str = "follow", specific: list[str] | None = None,
                      web_queries: list[str] | None = None, broader: list[str] | None = None,
                      exclude: list[str] | None = None, tags: list[str] | None = None, goal: str | None = None,
                      days: int = 30, digest_id: str | None = None, item_n: int | None = None,
                      post_id: str | None = None) -> out.TopicSaved:
    svc = call.svc

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


async def get_analysis(call: Call, period: str = "week", start_date: str | None = None, end_date: str | None = None,
                       focus: str | None = None, refresh: bool = False) -> out.AnalysisResult:
    svc = call.svc

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


async def save_report(call: Call, start_date: str, end_date: str, title: str, highlights: list[str], report: str,
                      focus: str | None = None) -> out.ReportSaved:
    async def body(ctx: UserContext) -> out.ReportSaved:
        p = analysis_domain.period_for("week", start_date, end_date)
        r = await analysis_domain.save_report(call.svc.repos, ctx, p, focus, title, highlights, report)
        await call.done(items=1)
        return out.ReportSaved(period=_period_out(p), focus=focus, title=r.title,
                               confirmation=f"Аналіз за {_period_out(p).label} збережено")
    return await call.run(body)


# ---- the registry -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Handler:
    fn: Callable[..., Awaitable[Any]]
    log: Callable[[dict], dict] = log_args     # the tool's own arguments as logged


def _report_log(args: dict) -> dict:             # the report itself is saved by the tool, not logged twice
    return {"start_date": args["start_date"], "end_date": args["end_date"], "focus": args.get("focus"),
            "title": args["title"], "highlights": args["highlights"], "report_chars": len(args["report"])}


HANDLERS: dict[str, Handler] = {fn.__name__: Handler(fn) for fn in (
    get_digest, get_morning_digest, search_posts, get_post, get_profile, log_turn, save_insight, get_insights,
    record_feedback, answer_proposal, get_proposals, set_agent_prefs, correct_profile, set_goal, deep_dive,
    save_findings, track_topic, get_analysis)} | {"save_report": Handler(save_report, _report_log)}

OWN = ("user_message", "signals")                # every tool's: they belong to the Call


async def run(svc: Services, name: str, origin: Origin | None, args: dict):
    """Runs tool `name` for the signed-in user. args: the tool's arguments (validated), with user_message and
    signals. Raises KeyError for an unknown tool, mcp's ToolError for what the model can read."""
    handler = HANDLERS[name]
    own = {k: v for k, v in args.items() if k not in OWN}
    call = Call(svc, name, {**handler.log(own), **common_args(args.get("user_message"), args.get("signals"))},
                origin)
    return await handler.fn(call, **own)
