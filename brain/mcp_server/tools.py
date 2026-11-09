"""Thin MCP tool wrappers: each tool's signature and description (the MCP schema), calling its handler in
brain/tools/handlers.py with where the call comes from.

No business logic here. user_id never comes from tool arguments.
"""
from __future__ import annotations

import logging
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from brain.mcp_server import schemas as out
from brain.mcp_server import widgets
from brain.tools import catalog, handlers
from brain.tools.catalog import ToolSpec
from brain.tools.base import Origin, Services, clean_message  # noqa: F401 - Services: imported from here

logger = logging.getLogger("brain.tools")


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


def origin_of(mcp_ctx: Context | None) -> Origin:
    sid, client = _session(mcp_ctx)
    return Origin(sid, client, lambda: widgets.client_shows_widgets(mcp_ctx))


async def _run(svc: Services, name: str, mcp_ctx: Context | None, params: dict):
    """params: the tool function's locals() at its first line, i.e. its arguments (and svc, from the closure)."""
    args = {k: v for k, v in params.items() if k not in ("mcp_ctx", "svc")}
    return await handlers.run(svc, name, origin_of(mcp_ctx), args)


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# On every tool: the message that led to the call, and what it reveals about the user. Required, so every message
# that reaches News brain is stored and analysed (docs/CONVERSATIONS_PLAN.md §4c).
UserMessage = Annotated[str, Field(min_length=1, max_length=8000, description=(
    "The user's latest chat message, verbatim and complete: the message that led to this call"))]
Signals = Annotated[list[out.Signal] | None, Field(max_length=10, description=(
    "Durable things that message reveals about the user: goals, interests, preferences (how they want answers or "
    "news), facts about their work and people, feedback, things they already know; in their own words. Leave "
    "empty for plain requests, and when you already passed them for this message in another call"))]


# search_posts texts for the two search modes (brain/domain/posts.py). The keyword ones are the original texts;
# SEMANTIC_SEARCH=false (or no VOYAGE_API_KEY) brings them back, so both modes can be tested.
QUERY_KEYWORDS = "Key words, e.g. 'Brave1 гранти РЕБ' (Ukrainian or Russian)"
SEARCH_QUERY_DESC = QUERY_KEYWORDS                          # set by register_tools for the current mode
QUERY_SEMANTIC = ("What to find, as a full phrase in your own words (any language): expand abbreviations "
                  "(РЕБ → «засоби радіоелектронної боротьби (РЕБ)»), add names of systems, companies, units. "
                  "The search goes by meaning and by words, e.g. 'нові російські засоби радіоелектронної боротьби "
                  "(РЕБ), комплекси придушення'")
SEARCH_DOC_SEMANTIC = """Search the news base (summaries of Telegram, web and X posts from Ukrainian and Russian sources) \
by meaning and by words at once, the results reranked by relevance.

Write the query as a full phrase in your own words, not a list of key words: expand abbreviations, add the names of \
systems, companies and units. A question with several parts or about both sides: one search per part (e.g. side ua \
and side ru). An overview or comparison question ("що нового", "порівняй", "чим…"): limit 15. Answer only from the results and cite each fact's post_url. If the results don't answer the question, \
say "I don't have that in the data"; search again (once, other words or more days) only if the result list is \
empty, because every search shows the user another set of result cards. Results with is_russian_source_claim=true \
are claims of Russian sources, not facts. When you list results, show their ref numbers (#1, #2, …). If the result \
says shown_in_widget, the user already sees the results as cards with links: answer in your own words citing #N, \
without listing the posts again."""


def register_tools(mcp: MCPServer, svc: Services) -> dict[str, ToolSpec]:
    """Registers every tool on `mcp`; returns their specs (brain/tools/catalog.py) for other callers."""
    specs: dict[str, ToolSpec] = {}

    def tool(**kw):
        def register(fn):
            mcp.tool(**kw)(fn)
            specs[fn.__name__] = catalog.spec_of(fn, **kw)
            return fn
        return register

    @tool(title="Today's news digest", annotations=READ_ONLY, meta=widgets.tool_meta(widgets.DIGEST_UI))
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
        return await _run(svc, "get_digest", mcp_ctx, locals())

    @tool(title="Morning digest, if new", meta=widgets.tool_meta(widgets.DIGEST_UI),
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
        return await _run(svc, "get_morning_digest", mcp_ctx, locals())

    from brain.domain import semantic as _semantic          # which search mode: the texts follow it
    global SEARCH_QUERY_DESC                                # module-level: annotations are resolved from globals
    semantic_on = _semantic.enabled()
    SEARCH_QUERY_DESC = QUERY_SEMANTIC if semantic_on else QUERY_KEYWORDS

    @tool(title="Search news posts", annotations=READ_ONLY, meta=widgets.tool_meta(widgets.SEARCH_UI),
          **({"description": SEARCH_DOC_SEMANTIC} if semantic_on else {}))   # else: the docstring below (keywords)
    async def search_posts(
        mcp_ctx: Context,
        user_message: UserMessage,
        query: Annotated[str, Field(min_length=2, max_length=300, description=SEARCH_QUERY_DESC)],
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
        return await _run(svc, "search_posts", mcp_ctx, locals())

    @tool(title="Get one post", annotations=READ_ONLY)
    async def get_post(
        mcp_ctx: Context,
        user_message: UserMessage,
        post_id: Annotated[str, Field(description="A post_id / source_post_ids value from another tool")],
        full_text: Annotated[bool, Field(description=(
            "Only when the user asks for the text of a Russian post (source_side ru): its Telegram link doesn't "
            "open in Ukraine. Ignored for other posts"))] = False,
        signals: Signals = None,
    ) -> out.PostDetail:
        """Get one news post (summary, tags, channel, link) by id. Cite its post_url. With full_text=true, a
        Russian post also comes with its own text: present it as the channel's claims, not as facts."""
        return await _run(svc, "get_post", mcp_ctx, locals())

    @tool(title="The user's interests", annotations=READ_ONLY)
    async def get_profile(
        mcp_ctx: Context,
        user_message: UserMessage,
        signals: Signals = None,
    ) -> out.ProfileOut:
        """The user's goals, today's chosen categories, strongest interests (categories and tags by score) and
        what they told you about themselves (about_user). Use it when they ask about their goals, interests or
        profile, and to explain why something matters to them; don't recite it unasked."""
        return await _run(svc, "get_profile", mcp_ctx, locals())

    @tool(title="Log the user's message",
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
        return await _run(svc, "log_turn", mcp_ctx, locals())

    @tool(title="Remember something the user shared",
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
        verbatim, in the user's language. A lasting interest the user states in any words («мене цікавлять морські
        дрони», «хочу більше про РЕБ», «слідкуй за супутниковим зв'язком загалом») is kind interest here: within
        minutes it becomes web searches for the daily interests digest. (A specific thing to follow for a while is
        track_topic; things only detected in ordinary messages go through log_turn signals.)"""
        return await _run(svc, "save_insight", mcp_ctx, locals())

    @tool(title="Recall what the user shared", annotations=READ_ONLY)
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
        return await _run(svc, "get_insights", mcp_ctx, locals())

    @tool(title="Rate news or an answer",
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
        return await _run(svc, "record_feedback", mcp_ctx, locals())

    @tool(title="Answer a suggestion",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                          openWorldHint=False))
    async def answer_proposal(
        mcp_ctx: Context,
        user_message: UserMessage,
        proposal_id: Annotated[str, Field(description=(
            "The proposal_id from a digest's proposals, a proposal_to_mention, or get_proposals"))],
        answer: Annotated[Literal["accept", "dismiss"], Field(description=(
            "accept: the user said yes (adds the goal / follows the topic); dismiss: not interested"))],
        reason: Annotated[str | None, Field(max_length=500, description=(
            "The user's own words about why, verbatim, if they gave any («не пропонуй мені про кораблі»): the "
            "system learns what not to suggest from them"))] = None,
        signals: Signals = None,
    ) -> out.ProposalAnswer:
        """Answer one of the system's suggestions when the user says yes or no to it, in any words («так, додай»,
        «давай ту пораду про Рассвет», «ні, це не моє»). A yes does what the suggestion says (adds the goal,
        follows or stops a topic); either answer teaches the system what to suggest. If the result has `ask`, the
        yes was a request (a deep dive, an analysis): carry it out now, as if the user had typed it. Never answer
        for the user: only on their clear yes or no. Confirm `done` in one line."""
        return await _run(svc, "answer_proposal", mcp_ctx, locals())

    @tool(title="The system's open suggestions", annotations=READ_ONLY)
    async def get_proposals(
        mcp_ctx: Context,
        user_message: UserMessage,
        signals: Signals = None,
    ) -> out.ProposalsResult:
        """The system's open suggestions (goals it noticed, topics to follow, advice on what changed), newest
        first: when the user asks about them in any words («що ти там пропонував?», «які в тебе поради?», «а що
        з тією темою, яку ти радив?») or answers one you can't find in this conversation. Then answer_proposal."""
        return await _run(svc, "get_proposals", mcp_ctx, locals())

    @tool(title="How the system suggests things",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
    async def set_agent_prefs(
        mcp_ctx: Context,
        user_message: UserMessage,
        cards: Annotated[int | None, Field(ge=0, le=3, description=(
            "Suggestion cards per digest, 0-3 (default 3): «менше пропозицій» 1, «не показуй пропозиції» 0"))] = None,
        turn_off: Annotated[list[Literal["goal", "topic", "advice"]] | None, Field(description=(
            "Kinds the user doesn't want: goal (suggested goals), topic (new things to follow), advice (what "
            "changed and what to do): «не пропонуй мені теми», «вимкни поради»"))] = None,
        turn_on: Annotated[list[Literal["goal", "topic", "advice"]] | None, Field(description=(
            "Kinds to bring back: «знову показуй поради»"))] = None,
        mentions: Annotated[bool | None, Field(description=(
            "false: no suggestion in ordinary chat answers («не згадуй пропозиції в розмові»); true: back on"))] = None,
        signals: Signals = None,
    ) -> out.AgentPrefsOut:
        """When the user says how much or what kind of suggestions they want, in any words: «менше пропозицій»,
        «не пропонуй мені нові теми», «вимкни поради», «не згадуй це в розмові», «поверни все як було». Pass only
        what they changed. Kinds turned off are no longer even made. Confirm the new settings in one line."""
        return await _run(svc, "set_agent_prefs", mcp_ctx, locals())

    @tool(title="Correct what the system inferred",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
    async def correct_profile(
        mcp_ctx: Context,
        user_message: UserMessage,
        not_interested: Annotated[str, Field(max_length=120, description=(
            "What the user says doesn't interest them (any more), in their words, short: «космос», «кораблі», "
            "«новини про Рассвет»"))],
        signals: Signals = None,
    ) -> out.CorrectionResult:
        """When the user says something the system thinks about them is wrong, in any words: «мене це не
        цікавить», «я вже цим не займаюсь», «чому ти вирішив, що мені цікаві кораблі?», «не підсовуй мені це».
        Removes the matching inferred interests and current intents at once, pushes such news down, closes open
        suggestions about it and remembers it. Topics the user follows are their own choice: if
        followed_matching is not empty, ask whether to stop following them too (track_topic stop)."""
        return await _run(svc, "correct_profile", mcp_ctx, locals())

    @tool(title="Add or remove a goal",
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
        return await _run(svc, "set_goal", mcp_ctx, locals())

    @tool(title="Dig deeper into a news item", annotations=READ_ONLY)
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
        информации", "Глибше про #N", a comment asking for details): returns what the news base already has
        (the item, its source posts, related posts, the user's earlier findings on it) and the next steps. The
        news base rarely has much more on one incident: after this call, search the web yourself, then save what
        you found with save_findings. Map "#N" to the latest digest or search result's ref and pass its ids."""
        return await _run(svc, "deep_dive", mcp_ctx, locals())

    @tool(title="Save what a deep dive found",
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
        return await _run(svc, "save_findings", mcp_ctx, locals())

    @tool(title="Follow a topic in future news",
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
        return await _run(svc, "track_topic", mcp_ctx, locals())

    @tool(title="Analysis of a week or a month", annotations=READ_ONLY)
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
        return await _run(svc, "get_analysis", mcp_ctx, locals())

    @tool(title="Save a written analysis",
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
        return await _run(svc, "save_report", mcp_ctx, locals())

    return specs
