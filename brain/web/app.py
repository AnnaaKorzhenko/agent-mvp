"""Our own web app (docs/WEB_PLAN.md, step 5.1): the UI in data-digest-web/frontend and its API, on the brain.

Run locally (dev user, no login): DEV_USER_ID=… brain/.venv/bin/python -m brain.web
On Render: its own web service, `python -m brain.web --host 0.0.0.0` (the MCP service stays as it is).

- /login, /auth/callback, /logout: Auth0 (brain/web/auth.py); the session is a signed httpOnly cookie.
- /api/*: needs the session (except /api/health); the user always comes from it, never from the request.
  Every read goes through the same tool handlers as Claude's (brain/tools), so it is logged to `conversations`
  (channel "web", one session per browser login) and filtered by sensitivity the same way.
- Anything else: the UI's static files.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import secrets
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, Field, ValidationError

from brain.audit.conversations import WIDGET_PREFIX
from brain.audit.log import AuditLog
from brain.config import Settings, get_settings
from brain.db.client import get_db
from brain.db.repositories import Repos
from brain.domain.identity import IdentityError, UserContext, resolve_user
from brain.integrations.backends import AnthropicBackend
from brain.tools import catalog
from brain.tools.base import Origin, Services
from brain.web import session
from brain.web.chat import Chat, new_chat_id, valid_chat_id
from brain.web.auth import Auth0Login, LoginError, WebUser

logger = logging.getLogger("brain.web")

VERSION = "0.1.0"
FRONTEND = Path(__file__).resolve().parents[2] / "data-digest-web" / "frontend"
LOGIN_MAX_AGE = 600                       # seconds to finish the Auth0 login
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-brain"                   # every write sends it: a cross-site form can't
CLIENT = {"name": "web", "version": VERSION}


class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


def build_services(settings: Settings) -> Services:
    if not settings.dev_mode:
        missing = [n for n, v in (("OAUTH_ISSUER", settings.oauth_issuer), ("PUBLIC_BASE_URL", settings.public_base_url),
                                  ("WEB_CLIENT_ID", settings.web_client_id),
                                  ("WEB_CLIENT_SECRET", settings.web_client_secret),
                                  ("WEB_SESSION_SECRET", settings.web_session_secret)) if not v]
        if missing:
            raise SystemExit(f"Refusing to start: set {', '.join(missing)} (Auth0 login), or DEV_USER_ID for local "
                             "development on 127.0.0.1.")
    db = get_db(settings)
    return Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None)


def _next(path: str | None) -> str:
    """Only our own pages after a login: a relative path, never another site."""
    return path if path and path.startswith("/") and not path.startswith("//") else "/"


def chat_backend(settings: Settings, audit: AuditLog) -> AnthropicBackend | None:
    """Claude for the chat, or None without ANTHROPIC_API_KEY (the chat then answers 503)."""
    if not settings.anthropic_api_key:
        return None
    return AnthropicBackend(audit, settings, model=settings.web_chat_model)


def create_app(settings: Settings | None = None, svc: Services | None = None,
               login: Auth0Login | None = None, backend: AnthropicBackend | None = None) -> FastAPI:
    settings = settings or get_settings()
    svc = svc or build_services(settings)
    login = login or (None if settings.dev_mode else Auth0Login(settings))
    secret = (settings.web_session_secret.get_secret_value().encode() if settings.web_session_secret
              else secrets.token_bytes(32))          # dev mode: sessions last until a restart
    base = (settings.public_base_url or "http://127.0.0.1:8000").rstrip("/")
    callback = f"{base}/auth/callback"
    cookie = {"httponly": True, "samesite": "lax", "secure": base.startswith("https://"), "path": "/"}
    session_age = settings.web_session_days * 86400
    specs = catalog.build(svc)
    backend = backend or chat_backend(settings, svc.audit)
    chat = Chat(svc, specs, backend, CLIENT) if backend else None
    app = FastAPI(title="News brain web", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in SAFE_METHODS \
                and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "Missing the X-Brain header"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        # The API: never cached. The UI's files: revalidated (ETag) on every load, so a deploy shows at once.
        response.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith("/api/")
                                    else "no-cache")
        return response

    # ---- login ------------------------------------------------------------------------------------------

    @app.get("/login")
    async def start_login(next: str | None = None):
        if login is None:                               # dev mode: already "signed in" as the dev user
            return RedirectResponse(_next(next), status_code=303)
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        r = RedirectResponse(login.authorize_url(callback, state, nonce), status_code=303)
        r.set_cookie(session.LOGIN_COOKIE, session.sign({"state": state, "nonce": nonce, "next": _next(next)},
                                                        secret, LOGIN_MAX_AGE), max_age=LOGIN_MAX_AGE, **cookie)
        return r

    @app.get("/auth/callback")
    async def finish_login(request: Request, code: str | None = None, state: str | None = None,
                           error: str | None = None):
        started = session.verify(request.cookies.get(session.LOGIN_COOKIE), secret)
        if login is None or error or not code or not started or not secrets.compare_digest(state or "",
                                                                                            started["state"]):
            await svc.audit.event("auth_failed", reason=f"web login: {error or 'bad state'}")
            return PlainTextResponse("Вхід не вдався. Спробуйте ще раз: /login", status_code=400)
        try:
            claims = await login.identity(code, callback, started["nonce"])
        except LoginError as e:
            await svc.audit.event("auth_failed", reason=f"web login: {e}")
            return PlainTextResponse("Вхід не вдався. Спробуйте ще раз: /login", status_code=400)
        user = WebUser(issuer=claims["iss"], subject=claims["sub"], email=claims.get("email"),
                       name=claims.get("name") or claims.get("nickname"), picture=claims.get("picture"),
                       sid=secrets.token_urlsafe(12))
        try:                    # the users record (created on the first login) and ALLOWED_EMAILS, now
            ctx = await resolve_user(settings, user.token(login.client_id), svc.repos.users)
        except IdentityError as e:
            await svc.audit.event("access_denied", reason=str(e), channel="web")
            return PlainTextResponse(str(e), status_code=403)
        await svc.audit.event("web_login", user_id=ctx.user_id, channel="web")
        r = RedirectResponse(started["next"], status_code=303)
        r.set_cookie(session.SESSION_COOKIE, session.sign(dataclasses.asdict(user), secret, session_age),
                     max_age=session_age, **cookie)
        r.delete_cookie(session.LOGIN_COOKIE, path="/")
        return r

    @app.get("/logout")
    async def logout():
        r = RedirectResponse(login.logout_url(base + "/") if login else "/", status_code=303)
        r.delete_cookie(session.SESSION_COOKIE, path="/")
        return r

    # ---- the API ----------------------------------------------------------------------------------------

    async def web_user(request: Request) -> WebUser:
        if login is None:
            return WebUser(issuer="dev", subject=str(settings.dev_user_id), name="dev", sid="dev")
        data = session.verify(request.cookies.get(session.SESSION_COOKIE), secret)
        if not data:
            raise HTTPException(401, "Not signed in")
        return WebUser(**data)

    def caller(user: WebUser) -> Services:
        """The services as this user: the tools resolve the user from this token, never from arguments."""
        return dataclasses.replace(svc, token=(lambda: None) if login is None
                                   else (lambda: user.token(login.client_id)))

    async def context(user: WebUser) -> UserContext:
        try:
            return await resolve_user(settings, caller(user).token(), svc.repos.users)
        except IdentityError as e:
            raise HTTPException(403, str(e)) from None

    async def tool(user: WebUser, name: str, args: dict, did: str):
        """Runs a brain tool as this user; `did`: what the user did in the UI, logged as a button press."""
        origin = Origin(f"web:{user.sid}", CLIENT, lambda: True)
        try:
            result = await catalog.call(caller(user), specs[name], args, f"{WIDGET_PREFIX} {did}", origin)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_input=False)) from None
        except ToolError as e:
            raise HTTPException(400, str(e)) from None
        if isinstance(result, CallToolResult):
            return result.structured_content
        return result.model_dump(mode="json")

    @app.get("/api/health")
    async def health():                 # never authenticated: returns no data
        return {"ok": True}

    @app.get("/api/me")
    async def me(user: WebUser = Depends(web_user)):
        return {"name": user.name, "email": user.email, "picture": user.picture, "dev": login is None}

    @app.get("/api/digest")
    async def digest(side: Literal["ua", "ru", "topics", "both"] = "both", date: str | None = None,
                     user: WebUser = Depends(web_user)):
        return await tool(user, "get_digest", {"side": side, "date": date}, f"відкрив дайджест ({side})")

    @app.get("/api/posts")
    async def posts(q: str = "", side: Literal["ua", "ru", "all"] = "all", days: int = Query(7, ge=1, le=30),
                    limit: int = Query(25, ge=1, le=25), user: WebUser = Depends(web_user)):
        q = q.strip()
        if len(q) < 2:
            return {"query": q, "results": [], "note": "Введіть щонайменше 2 символи."}
        return await tool(user, "search_posts", {"query": q, "side": side, "days": days, "limit": limit},
                          f"шукає в постах: {q}")

    @app.get("/api/posts/{post_id}")
    async def post(post_id: str, user: WebUser = Depends(web_user)):
        return await tool(user, "get_post", {"post_id": post_id}, "відкрив пост")

    @app.get("/api/profile")
    async def profile(user: WebUser = Depends(web_user)):
        return await tool(user, "get_profile", {}, "відкрив профіль")

    # ---- the chat (brain/web/chat.py) ---------------------------------------------------------------------

    def chat_on() -> Chat:
        if chat is None:
            raise HTTPException(503, "Чат вимкнено: на сервері не задано ANTHROPIC_API_KEY.")
        return chat

    def chat_id_ok(chat_id: str) -> str:
        if not valid_chat_id(chat_id):
            raise HTTPException(404, "Немає такого чату")
        return chat_id

    @app.post("/api/chats")
    async def new_chat(user: WebUser = Depends(web_user)):
        chat_on()
        return {"id": new_chat_id()}            # nothing is stored until its first message

    @app.get("/api/chats")
    async def chats(user: WebUser = Depends(web_user)):
        return {"chats": await chat_on().chats(await context(user))}

    @app.get("/api/chats/{chat_id}")
    async def chat_thread(chat_id: str, user: WebUser = Depends(web_user)):
        messages = await chat_on().thread(await context(user), chat_id_ok(chat_id))
        if not messages:
            raise HTTPException(404, "Немає такого чату")
        return {"id": chat_id, "messages": messages}

    @app.post("/api/chats/{chat_id}/messages")
    async def chat_message(chat_id: str, body: ChatIn, user: WebUser = Depends(web_user)):
        """Server-sent events: one `data: {json}` line per event (brain/web/chat.py Chat.turn)."""
        c, chat_id, ctx = chat_on(), chat_id_ok(chat_id), await context(user)
        text = body.content.strip()
        if not text:
            raise HTTPException(422, "Порожнє повідомлення")

        async def events():
            async for event in c.turn(caller(user), ctx, chat_id, text, user.name):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    frontend = Path(settings.web_frontend_dir) if settings.web_frontend_dir else FRONTEND
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    else:
        logger.warning(f"no UI files at {frontend}: serving the API only")
    return app


def create_default_app() -> FastAPI:
    """ASGI factory for uvicorn: `uvicorn brain.web.app:create_default_app --factory`."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info(f"web: db={settings.mongo_db} trust={settings.trust_level} "
                + (f"dev user {settings.dev_user_id}" if settings.dev_mode else f"Auth0 {settings.oauth_issuer}"))
    return create_app(settings)
