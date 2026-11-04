"""What every tool call shares: the services, where the call comes from, and Call, which resolves the caller,
remembers what the message reveals and logs the call to the user's conversation.

No business logic here. user_id never comes from tool arguments.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bson import ObjectId
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from brain.audit.conversations import WIDGET_PREFIX, CallRecord, ConversationLog
from brain.audit.log import AuditLog
from brain.config import Settings
from brain.db.repositories import Repos, user_key
from brain.domain import insights as insights_domain
from brain.domain import profile as profile_domain
from brain.domain import proposals as proposals_domain
from brain.domain.errors import DomainError
from brain.domain.identity import IdentityError, UserContext, VerifiedToken, resolve_user
from brain.mcp_server import schemas as out

TokenProvider = Callable[[], VerifiedToken | None]
logger = logging.getLogger("brain.tools")


@dataclass
class Services:
    settings: Settings
    repos: Repos
    audit: AuditLog               # security events only
    token: TokenProvider          # the verified OAuth token (None in dev mode)
    conversations: ConversationLog | None = field(default=None)
    learner: Any = None           # brain.agents.learner.Learner when LEARNING_AUTO is on: set_goal nudges it
    agents: Any = None            # brain.agents.runtime.Runtime when an agent is on: its worker runs queued work

    def __post_init__(self) -> None:
        if self.conversations is None:
            self.conversations = ConversationLog(self.repos.db, self.audit,
                                                 self.settings.conversation_idle_minutes,
                                                 self.settings.conversation_max_messages)

    @property
    def mcp_channels(self) -> list[str]:
        return sorted(set(self.settings.channel_by_client.values()) | {"dev", "unknown"})


@dataclass
class Origin:
    """Where a call comes from: its session (an MCP session id, later a web chat), the client, and whether the
    client draws our cards (asked only by the tools that have cards, and only once)."""
    session_id: str | None = None
    client: dict | None = None
    shows_widgets: Callable[[], bool] = lambda: False
    _widgets: bool | None = field(default=None, repr=False)

    def widgets(self) -> bool:
        if self._widgets is None:
            self._widgets = self.shows_widgets()
        return self._widgets


_FENCE = re.compile(r"^```[\w-]*\s*\n(.*?)\n?```\s*$", re.S)


def clean_message(text: str) -> str:
    """The message as the user typed it: clients sometimes wrap it in a ```markdown code fence."""
    text = text.strip()
    match = _FENCE.match(text)
    return match.group(1).strip() if match else text


def common_args(user_message: str | None, signals: list | None) -> dict:
    # No message only on record_feedback from a widget card opened before cards sent one: a button press.
    message = clean_message(user_message) if user_message is not None else f"{WIDGET_PREFIX} (кнопка в картці)"
    return {"user_message": message, **({"signals": [_plain(sig) for sig in signals]} if signals else {})}


def _plain(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def log_args(args: dict) -> dict:
    """A tool's own arguments as logged: models as plain dicts."""
    return {k: [_plain(v) for v in value] if isinstance(value, list) else _plain(value) for k, value in args.items()}


ALWAYS_ABOUT = {"get_digest", "get_profile"}
# Tools whose results carry the suggestions themselves, or answer one: no extra one to mention.
NO_MENTION = {"get_digest", "get_morning_digest", "get_proposals", "answer_proposal"}

PROPOSAL_YES = {"set_goal": "додати ціль", "track_topic": "відстежувати тему", "stop_topic": "припинити"}


def _yes(p) -> str:
    """The yes button's words: the action's own label (advice steps carry one), else by tool."""
    if p.action is None:
        return "так"
    return p.action.args.get("label") or PROPOSAL_YES.get(p.action.tool, "так")


def proposal_out(p) -> out.ProposalOut:
    return out.ProposalOut(proposal_id=p.id, kind=p.kind, title=p.title, why=p.why, links=p.links, yes=_yes(p),
                           ask=p.action.args.get("message") if p.action and p.action.tool == "chat" else None)


@dataclass
class Memory:
    """What Call.remember() did with the call's signals."""
    saved: list[ObjectId] = field(default_factory=list)
    duplicates: int = 0
    about_user: dict | None = None
    proposal: Any = None              # one open suggestion to mention (ProposalDoc): once a day, or a goal just said


class Call:
    """Logs one tool call to the user's conversation and turns anticipated failures into errors the model can
    read. Security events (access denied, items withheld) go to audit_log."""

    def __init__(self, svc: Services, name: str, args: dict, origin: Origin | None = None):
        self.svc, self.name, self.args = svc, name, args
        self.origin = origin or Origin()
        self.ctx: UserContext | None = None
        self.started = time.monotonic()
        self.session_id, self.client = self.origin.session_id, self.origin.client
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
            just_said = None
            if goals:                         # a goal the user said: one suggestion, asked once, with one button
                active = {g.lower() for g in (await profile_domain.get_profile(self.svc.repos, ctx)).goals}
                for text in [g for g in goals if g.lower() not in active]:
                    if await proposals_domain.propose(
                            self.svc.repos, ctx.user_id, kind="goal", title=text, agent="chat",
                            why="ви щойно про це сказали", action={"tool": "set_goal", "args": {"text": text}},
                            priority=proposals_domain.JUST_SAID) == "ok" and just_said is None:
                        just_said = next((p.id for p in await self.svc.repos.users.get_proposals(ctx.user_id)
                                          if p.key == proposals_domain.key_of("goal", text)), None)
            # The digest and the profile usually open a chat, and a new chat can't be told from the last one
            # without a session id: they always bring the profile; other tools only after a quiet spell.
            if first or self.name in ALWAYS_ABOUT:
                self.memory.about_user = await insights_domain.about_user(self.svc.repos, ctx) or None
            if self.name not in NO_MENTION or just_said:
                self.memory.proposal = await proposals_domain.to_mention(self.svc.repos, ctx, just_said=just_said)
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
        if not (m.about_user or m.proposal):
            return result
        mention = proposal_out(m.proposal) if m.proposal else None
        if isinstance(result, CallToolResult):
            extra = {"about_user": m.about_user, **({"proposal_to_mention": mention.model_dump()} if mention else {})}
            if isinstance(result.structured_content, dict):
                result.structured_content.update(extra)
            for block in result.content:
                if isinstance(block, TextContent):
                    block.text += "\nAbout the user (adapt to it): " + json.dumps(extra, ensure_ascii=False)
                    break
            return result
        if isinstance(result, out.MemoryOut):
            result.proposal_to_mention = mention
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
