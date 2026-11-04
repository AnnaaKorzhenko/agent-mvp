"""The chat (Аналітик) of our web app (docs/WEB_PLAN.md §3): Claude with the brain's tools, streamed.

One user message is one turn:
1. the earlier messages of this chat come back from `conversations` (the last WEB_CHAT_HISTORY, text only);
2. the user's message is logged as it is (role "user", via "chat");
3. rounds: the model is called through the Anthropic backend (gated by sensitivity, every round in audit_log),
   streaming its text out; the tools it asks for run in-process through the same handlers as Claude's connector
   (brain/tools), each logged as a role "tool" message linked to the user's message; at most WEB_CHAT_MAX_ROUNDS;
4. the whole reply is logged (role "assistant", with the model, tokens, rounds, tools, latency, stop reason), also
   when the stream is cut off.

A chat is a conversations session `web:chat:<id>` (channel "web"); a user's tokens per UTC day are capped from
those logs. log_turn isn't offered: this chat sees the messages itself. Signals aren't asked of the model either;
what the user states lasting is saved through save_insight / set_goal as the system prompt says.

Tokens (every round resends the prefix: the tools, then the system prompt):
- the tools' schemas are compacted (no pydantic titles, no `anyOf [X, null]`), and the few descriptions written
  for Claude's widget cards have a web version (WEB_DESCRIPTIONS);
- the tools and the static system prompt sit behind a 1-hour cache breakpoint, shared by every user and chat; the
  rest of the turn is cached for 5 minutes (the backend's top-level cache_control);
- the web is searched by web_research: a small model (WEB_CHAT_RESEARCH_MODEL) runs the searches and hands back a
  short brief with the links it found, so the search results never enter the chat's context.
"""
from __future__ import annotations

import json
import logging
import re
import time
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import anyio
from bson import ObjectId
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from brain.audit.conversations import CallRecord, Said
from brain.db.repositories import user_key
from brain.domain import insights as insights_domain
from brain.domain.identity import UserContext
from brain.integrations.backends import AnthropicBackend
from brain.tools import catalog
from brain.tools.base import Origin, Services
from brain.tools.catalog import ToolSpec

logger = logging.getLogger("brain.web.chat")

SESSION_PREFIX = "web:chat:"
NOT_OFFERED = {"log_turn"}
MAX_TOKENS = 16000                # one round's output, thinking included
MAX_SEARCHES = 5                  # web searches per web_research call
MAX_TOOL_RESULT = 60_000          # characters of one tool result sent to the model
MAX_SOURCES = 15                  # pages listed in a web_research result
PREFIX_CACHE = {"type": "ephemeral", "ttl": "1h"}     # the tools and the static system prompt

# The descriptions of the MCP tools mention Claude's widget cards and log_turn; for this chat, shorter versions.
# The arguments' descriptions stay the MCP ones (brain/mcp_server/tools.py).
WEB_DESCRIPTIONS = {
    "get_digest": (
        "Get the user's daily news digest: sections ordered by the user's interests, every item with its "
        "source_urls and source_post_ids. If the user follows topics, the first digest (side \"topics\") holds "
        "today's news on them: lead with it. Show items with their ref numbers (#1, #2, …); items with "
        "is_russian_source_claim=true are claims of Russian channels, never facts; cite source_urls."),
    "search_posts": (
        "Search the news base (summaries of Telegram posts from Ukrainian and Russian channels) by key words. "
        "Answer only from the results and cite each fact's post_url; if nothing fits, say so. Search again (once, "
        "other words or more days) only if the list is empty. is_russian_source_claim=true: claims of Russian "
        "sources. Show ref numbers (#1, #2, …)."),
    "record_feedback": (
        "Save the user's rating of a news item, or of your own answer or suggestion, when they clearly rate it in "
        "words (the app also has rating buttons). \"The third one\" is ref 3 of the latest digest or search "
        "result: pass its ids (digest items: digest_id + n as item_n; posts: post_id); if unclear, ask; never "
        "invent ids. Reasons from their words, text verbatim. Your own answer or suggestion: target_type answer / "
        "suggestion with a one-line about. A comment on an earlier rating: its feedback_id, or the item's ids with "
        "text and no reaction. Several ratings: several calls. Tell the user the confirmation."),
    "save_insight": (
        "Save something the user asks you to remember («запам'ятай») or clearly wants kept: knowledge from their "
        "work, judgements, how they want analysis done, people and organizations. Keep text verbatim, in their "
        "language. A lasting interest they state («мене цікавлять морські дрони») is kind interest: it becomes web "
        "searches for the daily interests digest. One specific thing to follow for a while: track_topic."),
    "deep_dive": (
        "Start a deep dive when the user wants more on a news item or thing («Глибше про #N», «більше про це»): "
        "returns what the news base has (the item, its source posts, related posts, the user's earlier findings) "
        "and the next steps. Pass the item's ids when you have them. The base rarely has much more on one "
        "incident: then call web_research, and save what it found with save_findings."),
}

WEB_RESEARCH = {
    "name": "web_research",
    "description": (
        "Search the web (news sites, official pages, analysts) when the news base isn't enough: in a deep dive, or "
        "when the user asks for outside sources. A researcher runs the searches and returns a short brief with "
        "links, and the pages it found (sources). Cite and save only those pages; say these facts are from the "
        "web, not the news base. One call per question; a second only for something clearly missing."),
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string", "minLength": 3, "maxLength": 300, "description": (
            "What to find out, specifically: the thing's names and aliases (Ukrainian, Russian, English) and what "
            "about it")},
        "focus": {"type": "string", "maxLength": 300, "description": (
            "Optional: why it matters to the user (their goal), so the brief keeps to it")},
    }, "required": ["query"]},
}
RESEARCH_SYSTEM = """You research the web for an analyst of the war in Ukraine and the defense sector. Search for what is asked, then write a compact brief in Ukrainian, at most 250 words: the facts you found, each with a markdown link to the page it came from, its date, and who says it (Russian official or media sources are claims, not facts). Say plainly what you could not find or confirm. Cite only pages from your search results."""

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def compact(schema: Any) -> Any:
    """A JSON schema without what the model doesn't need: pydantic's titles, `anyOf [X, null]` (optional is
    already "not required"), `default: null`."""
    if isinstance(schema, list):
        return [compact(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: ({name: compact(sub) for name, sub in v.items()} if k in ("properties", "$defs") and isinstance(v, dict)
               else compact(v))                     # argument names (a finding's "title") are never dropped
           for k, v in schema.items() if k != "title" and not (k == "default" and v is None)}
    alts = out.get("anyOf")
    if isinstance(alts, list) and len(alts) == 2 and {"type": "null"} in alts:
        out.pop("anyOf")
        out = {**next(a for a in alts if a != {"type": "null"}), **out}
    return out


def keep_known_links(text: str, known: set[str]) -> str:
    """A markdown link whose URL isn't among the search results loses its URL (its words stay)."""
    return _LINK.sub(lambda m: m.group(0) if m.group(2) in known else m.group(1), text)

SYSTEM = """\
You are the analyst of the user's private news brain: daily digests and summaries of Telegram posts from \
Ukrainian and Russian channels about the war and the defense sector, plus the user's own goals and interests. \
You talk with the user in a web app; the tools are the brain's.

Rules:
- For anything about the news or the user's data, answer only from the tools' results. Cite every fact with a \
markdown link to its post_url or source_urls: [канал або джерело](url). Never invent links or ids.
- If the tools return nothing relevant, say you don't have that in the data instead of guessing. Do not fill gaps \
from general knowledge without saying clearly that it is not from the data.
- Items with is_russian_source_claim=true come from Russian channels: write "російські канали стверджують…", never \
present them as facts.
- Reply in the user's language; Ukrainian by default. Plain markdown: short paragraphs, lists, bold; no tables, \
no emoji (they don't display on the user's laptop).
- Show digest and search items with their ref numbers (#1, #2, …). "Третя" is ref 3 of the most recent digest \
or search result; if it's unclear which item is meant, ask.
- After a digest, offer short follow-ups: a shorter version, more on item N, the sources.
- Call record_feedback only when the user clearly rates something: a news item, or your own answer or \
suggestion. One message with several ratings means several calls.
- Call set_goal only when the user explicitly asks to set, change or drop a goal. A lasting interest the user \
states («мене цікавить…», an area): save_insight kind interest. One specific thing to follow: track_topic. \
"Запам'ятай…": save_insight. What you know about them: get_insights and get_profile.
- «Поясни #N з бази» (or a plain question about an item): answer from the news base only: deep_dive with the \
item's ids for its source posts, and search_posts if needed; no web_research. A short answer, then offer «Глибше» \
(a deep dive with the web) in one line.
- When the user wants more on an item or thing ("більше про це", "Глибше про #N"): call deep_dive, then \
web_research (the news base rarely has more on one incident), answer with a sourced dossier, save what you found \
with save_findings, and ask once whether to follow the topic (track_topic). Say which facts come from the web and \
which from the news base.
- A user message may end with «[item: digest_id …, item_n …]», added by the app's button: the ids of the item it \
is about, for the tools. item_n is the item's number inside its digest and differs from the #N the user sees; \
never mention these ids or numbers to the user.
- For a weekly or monthly analysis: get_analysis; present a stored report, or write one from the pack (analysis, \
not a list of news; the counts are exact) and save it with save_report.
- A result's proposals are the system's own suggestions; nothing changes until the user says yes. \
proposal_to_mention: one line at the end of your answer. On the user's clear yes or no call answer_proposal; never \
answer one for the user. When the user says how much or what kind of suggestions they want, set_agent_prefs. When \
the user says something the system assumed about them is wrong, correct_profile.
- Digests and search results are already ordered by the user's goals and interests; each item's `why` says why it \
matters to them. Lead with items whose `why` names a goal, connect findings to their goals, and leave out what \
they already know or avoid. If nothing matches their goals, say so briefly instead of padding.
- When a result has about_user, adapt to it for the whole conversation."""


def session_of(chat_id: str) -> str:
    return SESSION_PREFIX + chat_id


def new_chat_id() -> str:
    return str(ObjectId())


def valid_chat_id(chat_id: str) -> bool:
    return ObjectId.is_valid(chat_id) and len(chat_id) == 24


def title_of(text: str | None) -> str:
    t = " ".join((text or "").split())
    return (t[:60] + "…") if len(t) > 60 else (t or "Чат")


def result_text(result: Any) -> str:
    """What the model reads of a tool's result."""
    if isinstance(result, CallToolResult):
        text = "\n".join(b.text for b in result.content if isinstance(b, TextContent))
        text = text or json.dumps(result.structured_content, ensure_ascii=False, default=str)
    else:
        text = result.model_dump_json(exclude_none=True)
    return text if len(text) <= MAX_TOOL_RESULT else text[:MAX_TOOL_RESULT] + "… [cut]"


def echo(content: list) -> list:
    """The assistant's blocks as sent back. After a mid-output fallback, the blocks of the declined attempt
    (thinking, tool calls, searches) before the last `fallback` block are left out; its text stays."""
    kinds = [getattr(b, "type", None) for b in content]
    if "fallback" not in kinds:
        return list(content)
    last = len(kinds) - 1 - kinds[::-1].index("fallback")
    drop = {"thinking", "redacted_thinking", "tool_use", "server_tool_use", "web_search_tool_result"}
    return [b for i, b in enumerate(content) if i > last or kinds[i] not in drop]


def as_turns(messages: list[dict], limit: int) -> list[dict]:
    """Logged chat messages as the model's turns: the last `limit`, alternating, starting with the user's."""
    turns: list[dict] = []
    for m in messages[-limit:]:
        text = (m.get("content") or "").strip()
        if not text:
            continue
        if turns and turns[-1]["role"] == m["role"]:
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": m["role"], "content": text})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def _start_of_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class Reply:
    """The reply as it builds up: what's logged at the end, whatever happens."""
    rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    web_searches: int = 0
    research_input_tokens: int = 0    # web_research's own model calls
    research_output_tokens: int = 0
    model: str | None = None
    stop_reason: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        self.parts: list[str] = []
        self.tools: list[str] = []
        self.started = time.monotonic()

    @property
    def text(self) -> str:
        return "".join(self.parts).strip()

    def count(self, message) -> None:
        u = message.usage
        self.rounds += 1
        self.model = message.model
        self.input_tokens += (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        self.output_tokens += u.output_tokens or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        self.web_searches += getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0

    def meta(self) -> dict:
        return {"model": self.model, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "rounds": self.rounds, "tools": self.tools,
                "web_searches": self.web_searches, "research_input_tokens": self.research_input_tokens,
                "research_output_tokens": self.research_output_tokens,
                "stop_reason": self.stop_reason, "error": self.error,
                "latency_ms": int((time.monotonic() - self.started) * 1000)}


class Chat:
    def __init__(self, svc: Services, specs: dict[str, ToolSpec], backend: AnthropicBackend, client: dict,
                 research: AnthropicBackend | None = None):
        """research: the backend web_research searches with (none: the chat can't search the web)."""
        self.svc, self.specs, self.backend, self.client, self.research = svc, specs, backend, client, research
        self.tools: list[dict] = [{"name": spec.name, "description": WEB_DESCRIPTIONS.get(spec.name, spec.description),
                                   "input_schema": compact(spec.input_schema), "eager_input_streaming": True}
                                  for spec in specs.values() if spec.name not in NOT_OFFERED]
        if research is not None:
            self.tools.append({**WEB_RESEARCH, "eager_input_streaming": True})

    # ---- reading ---------------------------------------------------------------------------------------------

    async def chats(self, ctx: UserContext, limit: int = 30) -> list[dict]:
        rows = await self.svc.conversations.sessions(user_key(ctx.user_id), ctx.channel, SESSION_PREFIX, limit)
        return [{"id": r["session"][len(SESSION_PREFIX):], "title": title_of(r.get("first")),
                 "updated_at": r["updated_at"].isoformat()} for r in rows if r.get("first")]

    async def thread(self, ctx: UserContext, chat_id: str) -> list[dict]:
        msgs = await self.svc.conversations.thread(user_key(ctx.user_id), ctx.channel, session_of(chat_id))
        return [{"id": str(m["id"]), "role": m["role"], "content": m.get("content") or "",
                 "created_at": m["created_at"].isoformat(),
                 **({"stop_reason": m.get("stop_reason"), "rating": (m.get("rating") or {}).get("reaction")}
                    if m["role"] == "assistant" else {})} for m in msgs]

    async def reply(self, ctx: UserContext, chat_id: str, message_id: str) -> tuple[str, dict] | None:
        """One of the chat's replies and the user's message it answered: (question, reply), or None."""
        if not ObjectId.is_valid(message_id):
            return None
        msgs = await self.svc.conversations.thread(user_key(ctx.user_id), ctx.channel, session_of(chat_id))
        question = ""
        for m in msgs:
            if m["role"] == "user":
                question = m.get("content") or ""
            elif str(m["id"]) == message_id:
                return question, m
        return None

    async def rate(self, ctx: UserContext, chat_id: str, message_id: str, reaction: str,
                   feedback_id: str | None, text: str | None = None) -> bool:
        rating = {"reaction": reaction, "feedback_id": feedback_id, "text": text, "at": datetime.now(timezone.utc)}
        return await self.svc.conversations.rate(user_key(ctx.user_id), ctx.channel, session_of(chat_id),
                                                 ObjectId(message_id), rating)

    async def used_today(self, ctx: UserContext) -> int:
        return await self.svc.conversations.assistant_tokens(user_key(ctx.user_id), ctx.channel,
                                                             _start_of_day(datetime.now(timezone.utc)))

    # ---- one turn --------------------------------------------------------------------------------------------

    async def _system(self, ctx: UserContext, name: str | None) -> list[dict]:
        today = datetime.now(timezone.utc).date().isoformat()
        about = None
        try:
            about = await insights_domain.about_user(self.svc.repos, ctx) or None
        except Exception as e:             # noqa: BLE001 - the chat works without it
            logger.error(f"about_user failed: {type(e).__name__}: {e}")
        extra = f"Today is {today} (UTC)." + (f" The user's name: {name}." if name else "")
        if about:
            extra += "\nAbout the user (adapt to it): " + json.dumps(about, ensure_ascii=False, default=str)
        # The static part closes the shared prefix (the tools come before it): cached for an hour, for everyone.
        return [{"type": "text", "text": SYSTEM, "cache_control": PREFIX_CACHE}, {"type": "text", "text": extra}]

    async def _log(self, ctx: UserContext, chat_id: str, said: Said) -> None:
        await self.svc.conversations.log(user_id=user_key(ctx.user_id), channel=ctx.channel,
                                         mcp_session_id=session_of(chat_id), client=self.client, record=said)

    async def _research(self, ctx: UserContext, chat_id: str, block, reply: Reply) -> dict:
        """web_research: the searches run in their own model call; the chat gets the brief and the pages."""
        out: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id}
        args = block.input if isinstance(block.input, dict) else {}
        query, focus = str(args.get("query") or "").strip()[:300], str(args.get("focus") or "").strip()[:300]
        if len(query) < 3:
            return {**out, "is_error": True, "content": "Invalid arguments: query is required."}
        record = CallRecord(tool="web_research", args={"query": query, **({"focus": focus} if focus else {})})
        started = time.monotonic()
        try:
            r = await self.research.search(
                [{"role": "user", "content": query + (f"\n\nWhat matters to the user: {focus}" if focus else "")}],
                RESEARCH_SYSTEM, max_tokens=2000, max_uses=MAX_SEARCHES, sensitivity=ctx.max_sensitivity,
                user_id=ctx.user_id)
        except Exception as e:          # noqa: BLE001 - the model hears the search failed, the turn goes on
            logger.exception("web_research failed")
            record.ok, record.error = False, f"{type(e).__name__}: {e}"
            result = {**out, "is_error": True, "content": "The web search failed; answer from the news base."}
        else:
            reply.research_input_tokens += r.input_tokens
            reply.research_output_tokens += r.output_tokens
            reply.web_searches += r.web_searches
            sources = [{"url": x["url"], "title": x.get("title") or "", "date": x.get("page_age")}
                       for x in r.sources[:MAX_SOURCES]]
            record.count = len(sources)
            result = {**out, "content": json.dumps({
                "brief": keep_known_links(r.text, {x["url"] for x in r.sources}), "sources": sources,
                "searches": r.web_searches}, ensure_ascii=False)}
        record.duration_ms = int((time.monotonic() - started) * 1000)
        await self.svc.conversations.log(user_id=user_key(ctx.user_id), channel=ctx.channel,
                                         mcp_session_id=session_of(chat_id), client=self.client, record=record)
        return result

    async def _run_tool(self, caller: Services, block, user_message: str, origin: Origin) -> dict:
        spec = self.specs.get(block.name)
        out: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id}
        if spec is None or spec.name in NOT_OFFERED:
            return {**out, "is_error": True, "content": f"No tool named {block.name!r}."}
        if not isinstance(block.input, dict):
            return {**out, "is_error": True, "content": json.dumps({"INVALID_JSON": json.dumps(block.input)})}
        try:
            result = await catalog.call(caller, spec, block.input, user_message, origin)
        except ValidationError as e:    # a truncated or mistyped input (eager streaming): the model can retry
            return {**out, "is_error": True, "content": "Invalid arguments: " + json.dumps(
                e.errors(include_url=False, include_input=False), ensure_ascii=False, default=str)}
        except ToolError as e:
            return {**out, "is_error": True, "content": str(e)}
        except Exception as e:          # noqa: BLE001 - the model hears that the tool failed, the turn goes on
            logger.exception(f"tool {block.name} failed")
            return {**out, "is_error": True, "content": f"The tool failed ({type(e).__name__})."}
        return {**out, "content": result_text(result)}

    async def turn(self, caller: Services, ctx: UserContext, chat_id: str, text: str,
                   name: str | None = None, item: dict | None = None) -> AsyncIterator[dict]:
        """Events for the UI: {"type": "text", "text"} deltas, {"type": "tool", "name"} when a tool starts,
        {"type": "done", "message_id", "stop_reason"} or {"type": "error", "detail"}."""
        s = self.svc.settings
        used = await self.used_today(ctx)
        if used >= s.web_chat_tokens_per_day:
            yield {"type": "error", "detail": "Денний ліміт чату вичерпано. Спробуйте завтра."}
            return
        earlier = as_turns(await self.svc.conversations.thread(user_key(ctx.user_id), ctx.channel,
                                                               session_of(chat_id)), s.web_chat_history)
        system = await self._system(ctx, name)
        await self._log(ctx, chat_id, Said("user", text, meta={"item": item} if item else {}))
        # The item a digest button asked about: its ids for the model (the user sees and the log keeps the text).
        prompt = text + (f"\n\n[item: digest_id {item['digest_id']}, item_n {item['item_n']}]" if item else "")
        messages: list[dict] = earlier + [{"role": "user", "content": prompt}]
        origin = Origin(session_of(chat_id), self.client, lambda: False)    # the model writes the answer itself
        reply = Reply()
        finished = False
        try:
            while True:
                if reply.rounds >= s.web_chat_max_rounds:
                    reply.stop_reason = "max_rounds"
                    break
                if reply.parts and not reply.parts[-1].endswith("\n\n"):
                    reply.parts.append("\n\n")
                    yield {"type": "text", "text": "\n\n"}
                final = None
                async with aclosing(self.backend.stream(messages, system, self.tools, MAX_TOKENS,
                                                        sensitivity=ctx.max_sensitivity,
                                                        user_id=ctx.user_id)) as rounds:
                    async for item in rounds:      # closed at once if the user stops: its audit entry is written
                        if isinstance(item, str):
                            reply.parts.append(item)
                            yield {"type": "text", "text": item}
                        else:
                            final = item
                reply.count(final)
                reply.stop_reason = final.stop_reason
                content = echo(final.content)
                if final.stop_reason == "pause_turn":           # a long web search: resume it as it is
                    messages.append({"role": "assistant", "content": content})
                    continue
                calls = [b for b in content if getattr(b, "type", None) == "tool_use"]
                if final.stop_reason in ("refusal", "max_tokens") or not calls:
                    break                     # a cut-off tool input is never run
                messages.append({"role": "assistant", "content": content})
                results = []
                for block in calls:
                    reply.tools.append(block.name)
                    yield {"type": "tool", "name": block.name}
                    if block.name == WEB_RESEARCH["name"] and self.research is not None:
                        results.append(await self._research(ctx, chat_id, block, reply))
                    else:
                        results.append(await self._run_tool(caller, block, text, origin))
                messages.append({"role": "user", "content": results})
            if reply.stop_reason == "refusal" and not reply.text:
                reply.parts = ["Не можу відповісти на це питання."]
                yield {"type": "text", "text": reply.parts[0]}
            elif reply.stop_reason == "max_rounds":
                note = "\n\n_Зупинився: забагато кроків для одного питання. Уточніть, що саме знайти._"
                reply.parts.append(note)
                yield {"type": "text", "text": note}
            finished = True
        except Exception as e:
            reply.error, reply.stop_reason = f"{type(e).__name__}: {e}", "error"
            logger.exception("chat turn failed")
            yield {"type": "error", "detail": "Не вдалося отримати відповідь. Спробуйте ще раз."}
        finally:
            if not finished and reply.stop_reason != "error":
                reply.stop_reason = "cancelled"            # the user stopped it, or the connection dropped
            said = Said("assistant", reply.text, meta=reply.meta())
            with anyio.CancelScope(shield=True):
                await self._log(ctx, chat_id, said)
        if reply.stop_reason != "error":
            yield {"type": "done", "message_id": str(said.message_id), "stop_reason": reply.stop_reason}
