"""Shows what the scout finds and would propose. Writes nothing.

    brain/.venv/bin/python -m brain.scripts.scout_preview [--db News] [--review [--backend anthropic]
                                                           [--model claude-haiku-5-5]] [--user ID]
    brain/.venv/bin/python -m brain.scripts.scout_preview --web [--search N] [--model …] [--user ID]

(no options)  the rising names in the last 48 h of posts (code only, free)
--review      also asks the model which are worth following, and how to name them (one call; --backend /
              --model, else SCOUT_BACKEND / SCOUT_MODEL, else the learning job's backend)
--user ID     with --review or --search: what that user would be offered, and why the rest isn't
--web         the web scout's areas, from the profiles of the users served in the last 30 days (free)
--search N    with --web: searches the first N areas (at most 5 searches each: billed per search, plus tokens)
"""
from __future__ import annotations

import argparse
import asyncio

from brain.agents import scout as scout_agent
from brain.agents import scout_web
from brain.agents.runtime import Runtime
from brain.config import get_settings
from brain.db.client import close_client, get_client
from brain.db.repositories import Repos, _public_id, user_key
from brain.domain import relevance, topics, trends
from brain.domain.identity import UserContext


async def web(db, settings, search: int, user: str | None) -> None:
    from datetime import datetime, timezone
    w = scout_web.web_scout(db, settings, Runtime(db))
    now = datetime.now(timezone.utc)
    areas = await w.areas(now)
    print(f"database {db.name}: {len(areas)} area(s) (at most SCOUT_WEB_AREAS={settings.scout_web_areas})")
    for a in areas:
        print(f"  {a['users']} user(s): {' · '.join(a['phrases'])}")
    if not search:
        return
    print(f"\nsearching {min(search, len(areas))} area(s) with model {w.backend.model}…")
    kept, dropped, searched, calls = await w.search_areas(areas[:search], now, scout_web.SEARCHES_PER_AREA * search)
    print(f"{sum(c.web_searches for c in calls)} searches, {sum(c.input_tokens + c.output_tokens for c in calls)} "
          f"tokens; {len(kept)} found, {dropped} dropped (unverified sources or already known)")
    for k in kept:
        print(f"  {k['name']} [{k['kind']}]: {k['what']}")
        for src in k["sources"]:
            print(f"      {src['date']} {src['url']}")
    if user:
        await _for_user(db, user, kept)


async def _for_user(db, user: str, kept: list[dict]) -> None:
    uid = int(user) if user.isdigit() else user
    repos = Repos(db)
    ctx = UserContext(_public_id(user_key(uid)), "cloud", "agent", "internal")
    prefs = await relevance.load(repos, ctx)
    followed = await topics.followed(repos, ctx)
    records = [q["q"] for rec in (repos.sources.KEYWORDS, repos.sources.PROFILE_KEYWORDS)
               for q in await repos.sources.get_keyword_queries(rec) or []]
    print(f"\nfor user {uid} (threshold {scout_agent.FIT_MIN}):")
    for k in kept:
        if scout_agent.covered(k, prefs, followed, records):
            print(f"  covered already: {k['name']}")
            continue
        r = scout_agent.fit(k, prefs)
        mark = "PROPOSE" if r.score >= scout_agent.FIT_MIN else "skip   "
        print(f"  {mark} {r.score:4.2f} {k['name']}: {'; '.join(r.why) or 'nothing in their profile'}")


async def main(db_name: str | None, do_review: bool, user: str | None, backend: str | None = None,
               model: str | None = None, do_web: bool = False, search: int = 0) -> None:
    settings = get_settings()
    if backend:
        settings = settings.model_copy(update={"scout_backend": backend})
    if model:
        settings = settings.model_copy(update={"scout_model": model, "scout_web_model": model})
    db = get_client(settings)[db_name or settings.mongo_db]
    if do_web:
        await web(db, settings, search, user)
        await close_client()
        return
    found = await trends.scan(db)
    print(f"database {db.name}: {len(found)} rising name(s) in the last {trends.WINDOW_HOURS} h")
    for c in found:
        rise = "new" if c.rise == float("inf") else f"×{c.rise:.0f}"
        print(f"  {c.posts:3} posts, {c.channels:2} channels, {rise:>5}, {c.sides}: {', '.join(c.terms[:4])}")
    if not do_review:
        await close_client()
        return
    s = scout_agent.scout(db, settings, Runtime(db))
    print(f"\nreviewing with backend {s.backend.name}" + (f", model {settings.scout_model}" if settings.scout_model
                                                          and s.backend.name == "anthropic" else ""))
    kept, dropped, calls = await scout_agent.review(s.backend, found, None)
    used = sum(c.input_tokens + c.output_tokens for c in calls)
    print(f"\nmodel {calls[-1].model if calls else '-'}: kept {len(kept)}, dropped {len(found) - len(kept)} "
          f"({used} tokens)")
    for k in kept:
        print(f"  {k['name']} [{k['kind']}]: {k['what']}\n      search: {k['web_queries']}")
    if user:
        await _for_user(db, user, kept)
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--user")
    ap.add_argument("--backend", choices=["anthropic", "local"])
    ap.add_argument("--model", help="anthropic backend: the model, e.g. claude-haiku-5-5")
    ap.add_argument("--web", action="store_true")
    ap.add_argument("--search", type=int, default=0)
    a = ap.parse_args()
    asyncio.run(main(a.db, a.review, a.user, a.backend, a.model, a.web, a.search))
