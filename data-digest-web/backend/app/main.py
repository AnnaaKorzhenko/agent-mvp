import base64
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import get_db
from .routes import chat, digest, posts, profile

app = FastAPI(title="Data Digest API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Якщо задані APP_USER і APP_PASSWORD — пускаємо лише з логіном."""
    if not (settings.APP_USER and settings.APP_PASSWORD) or request.url.path == "/api/health":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
            if secrets.compare_digest(user, settings.APP_USER) and secrets.compare_digest(pwd, settings.APP_PASSWORD):
                return await call_next(request)
        except Exception:
            pass
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Data Digest"'})


for r in (digest.router, posts.router, chat.router, profile.router):
    app.include_router(r, prefix="/api")


@app.get("/api/health")
async def health():
    await get_db().command("ping")
    return {"ok": True, "db": settings.DB_NAME}


# Інтерфейс віддається з цього ж сервера: http://localhost:8000/
frontend = (Path(__file__).resolve().parent.parent / settings.FRONTEND_DIR).resolve()
if frontend.is_dir():
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
