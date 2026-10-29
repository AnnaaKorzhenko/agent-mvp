"""The MCP server: one door to the brain. Streamable HTTP at /mcp, liveness at /health.

Run locally (dev user, sample data):
    brain/.venv/bin/python -m brain.mcp_server
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from brain.audit.log import AuditLog
from brain.config import Settings, get_settings
from brain.db.client import get_db
from brain.db.repositories import Repos
from brain.mcp_server.auth import auth_settings, token_provider, token_verifier
from brain.mcp_server.widgets import apps_extension
from brain.learning.auto import learner
from brain.mcp_server.tools import Services, register_tools

INSTRUCTIONS = """\
You are connected to the user's private news brain: daily digests and summaries of Telegram posts from \
Ukrainian and Russian channels about the war and the defense sector, plus the user's own interests and goals.

Rules:
- For anything about the news or the user's data, answer only from these tools' results. Cite the post_url \
or source_urls for every fact.
- If the tools return nothing relevant, say "I don't have that in the data" instead of guessing. Do not fill \
gaps from general knowledge without saying clearly that it is not from the data.
- Items with is_russian_source_claim=true come from Russian channels: write "Russian channels claim ...", never \
present them as facts.
- After showing a digest, offer short follow-ups: a shorter version, more detail on item N, or the sources.
- Reply in the user's language; Ukrainian by default.
- If a tool result says the user already sees it as interactive cards (shown_in_widget), don't repeat, list \
or summarize it; one short sentence at most, unless the user asked something specific.
- Show digest and search items with their ref numbers (#1, #2, …) when you present them in text.
- Call record_feedback only when the user clearly rates something: a news item, or your own answer or \
suggestion. "The third one" is ref 3 of the most recent digest or search result; if it's unclear which item \
is meant, ask. Never invent ids. One message with several ratings means several calls; a comment that \
follows a rating goes to its feedback_id.
- Call set_goal only when the user explicitly asks to set, change or drop a goal.
- When the user wants more on an item or thing ("більше про це", "надо больше информации по этому", "🔎 Глибше \
про #N", a rating comment asking for details, or record_feedback returns offer_deep_dive): call deep_dive, then \
search the web yourself (the news base rarely has more on one incident), answer with a sourced dossier, save \
what you found with save_findings, and ask once whether to follow the topic (track_topic). Web results are not \
from the news base: say where each fact comes from.
- Every News brain tool takes user_message (the user's latest message, verbatim) and signals (durable things it \
reveals: goals, interests, preferences, facts about their work, feedback, things they already know, in their \
words). For a message that needs no other News brain tool (thanks, corrections, opinions, things about \
themselves), call log_turn with it.
- When a result has about_user, adapt to it for the whole conversation: follow their preferences, skip what they \
already know, connect news to their goals and interests. When it has goals_to_offer, ask once whether to add them.
- Digests and search results are already ordered by the user's goals and interests; each item's `why` says why it \
matters to them. Lead with items whose `why` names a goal. In every answer, summary or analysis, connect the \
findings to the user's goals and interests and say which goal they serve; leave out what they already know or avoid. \
If nothing matches their goals, say so briefly instead of padding.
- News brain is the user's memory here. For "запам'ятай" use save_insight; for "що ти про мене пам'ятаєш" or \
questions about their goals and interests use get_insights and get_profile, even if you also have your own memory.
"""

logger = logging.getLogger("brain.mcp")


def create_server(svc: Services, verifier: TokenVerifier | None = None) -> MCPServer:
    """With OAUTH_ISSUER set, every request needs a valid Auth0 access token (401 + metadata otherwise)."""
    oauth = {}
    if svc.settings.oauth_issuer:
        oauth = {"token_verifier": verifier or token_verifier(svc.settings, svc.audit.event),
                 "auth": auth_settings(svc.settings)}
    mcp = MCPServer("brain", title="News brain", instructions=INSTRUCTIONS, version="0.4.0",
                    log_level=svc.settings.log_level.upper(), extensions=[apps_extension()], **oauth)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> PlainTextResponse:      # never authenticated: returns no data
        return PlainTextResponse("ok")

    register_tools(mcp, svc)
    return mcp


def build_services(settings: Settings) -> Services:
    if not settings.dev_mode and not settings.oauth_issuer:
        raise SystemExit("Refusing to start: set OAUTH_ISSUER + PUBLIC_BASE_URL (Auth0), "
                         "or DEV_USER_ID for local development on 127.0.0.1.")
    db = get_db(settings)
    return Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=token_provider(settings),
                    learner=learner(db, settings) if settings.learning_auto else None)


def transport_security(settings: Settings) -> TransportSecuritySettings | None:
    if settings.allowed_hosts:
        return TransportSecuritySettings(allowed_hosts=settings.allowed_hosts)
    return None          # the SDK's default: DNS-rebinding protection, localhost only


def create_app():
    """ASGI app for uvicorn: `uvicorn brain.mcp_server.server:create_app --factory`."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    svc = build_services(settings)
    mcp = create_server(svc)
    logger.info(f"MCP server: db={settings.mongo_db} trust={settings.trust_level} "
                + (f"dev user {settings.dev_user_id}" if settings.dev_mode
                   else f"OAuth issuer {settings.oauth_issuer}, resource {settings.resource_url}"))
    app = mcp.streamable_http_app(transport_security=transport_security(settings))
    if svc.learner is not None:
        _start_with(app, svc.learner.watch)
    return app


def _start_with(app, task_factory) -> None:
    """Runs a background task for the app's lifetime, around the SDK's own lifespan (its session manager)."""
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        task = asyncio.create_task(task_factory())
        try:
            async with inner(a) as state:
                yield state
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.router.lifespan_context = lifespan
