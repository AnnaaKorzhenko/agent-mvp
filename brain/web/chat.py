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
"""
from __future__ import annotations

import json
import logging
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

from brain.audit.conversations import Said
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
MAX_SEARCHES = 5                  # web searches per round (deep dives)
MAX_TOOL_RESULT = 60_000          # characters of one tool result sent to the model

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
- When the user wants more on an item or thing ("більше про це", "Глибше про #N"): call deep_dive, then search the \
web yourself (the news base rarely has more on one incident), answer with a sourced dossier, save what you found \
with save_findings, and ask once whether to follow the topic (track_topic). Say which facts come from the web and \
which from the news base.
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
                "web_searches": self.web_searches, "stop_reason": self.stop_reason, "error": self.error,
                "latency_ms": int((time.monotonic() - self.started) * 1000)}


class Chat:
    def __init__(self, svc: Services, specs: dict[str, ToolSpec], backend: AnthropicBackend, client: dict):
        self.svc, self.specs, self.backend, self.client = svc, specs, backend, client
        s = svc.settings
        self.tools: list[dict] = [{"name": spec.name, "description": spec.description,
                                   "input_schema": spec.input_schema, "eager_input_streaming": True}
                                  for spec in specs.values() if spec.name not in NOT_OFFERED]
        if s.web_chat_web_search:
            self.tools.append({"type": AnthropicBackend.SEARCH_TOOL, "name": "web_search", "max_uses": MAX_SEARCHES})

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
        extra = f"\n\nToday is {today} (UTC)." + (f" The user's name: {name}." if name else "")
        if about:
            extra += "\nAbout the user (adapt to it): " + json.dumps(about, ensure_ascii=False, default=str)
        return [{"type": "text", "text": SYSTEM + extra}]

    async def _log(self, ctx: UserContext, chat_id: str, said: Said) -> None:
        await self.svc.conversations.log(user_id=user_key(ctx.user_id), channel=ctx.channel,
                                         mcp_session_id=session_of(chat_id), client=self.client, record=said)

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
                   name: str | None = None) -> AsyncIterator[dict]:
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
        await self._log(ctx, chat_id, Said("user", text))
        messages: list[dict] = earlier + [{"role": "user", "content": text}]
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
