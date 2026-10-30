"""Shows what the advisor sees for one user and what it would advise. Writes nothing.

    brain/.venv/bin/python -m brain.scripts.advisor_preview --user ID [--db News] [--advise [--backend anthropic]
                                                                                        [--model claude-haiku-5-5]]

(default)   the user's signals this week: busier topics and goals, new findings, quiet ones (code only, free)
--advise    also the model's advice for the signals with news (one call)
"""
from __future__ import annotations

import argparse
import asyncio
import json

from brain.agents import advisor as advisor_agent
from brain.agents.runtime import Runtime
from brain.config import get_settings
from brain.db.client import close_client, get_client
from brain.db.repositories import Repos, _public_id, user_key
from brain.domain import advice
from brain.domain.identity import UserContext


async def main(db_name: str | None, user: str, do_advise: bool, backend: str | None, model: str | None) -> None:
    settings = get_settings()
    if backend:
        settings = settings.model_copy(update={"scout_backend": backend})
    if model:
        settings = settings.model_copy(update={"advisor_model": model})
    db = get_client(settings)[db_name or settings.mongo_db]
    uid = int(user) if user.isdigit() else user
    ctx = UserContext(_public_id(user_key(uid)), "cloud", "agent", "internal")
    sigs = await advice.signals(Repos(db), ctx)
    print(f"database {db.name}, user {uid}: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s.kind:10} {s.subject[:60]}  {s.facts}  steps {s.steps}  ({len(s.items)} items)")
        for i in s.items[:3]:
            print(f"      {i['day']} {'RU ' if i['claim'] else ''}{i['text'][:110]}")
    news = [s for s in sigs if s.kind in advice.NEEDS_MODEL]
    if do_advise and news:
        a = advisor_agent.advisor(db, settings, Runtime(db))
        payload = {"goals": [g.text for g in ((await Repos(db).users.get_profile(ctx.user_id)).goals) if g.active],
                   "signals": [{"signal": s.key, "kind": s.kind, "about": s.subject, "facts": s.facts,
                                "steps": s.steps, "items": s.items} for s in news]}
        call = await a.backend.complete([{"role": "user", "content": "Data (JSON):\n" + json.dumps(
            payload, ensure_ascii=False)}], system=advisor_agent.SYSTEM, max_tokens=advisor_agent.MAX_OUTPUT,
            sensitivity="internal", user_id=str(ctx.user_id))
        print(f"\nmodel {call.model}: {call.input_tokens} + {call.output_tokens} tokens")
        for x in advisor_agent._parse(call.text).advice:
            print(f"  [{x.step}] {x.title}\n      {x.text}\n      {x.source_urls}")
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--user", required=True)
    ap.add_argument("--advise", action="store_true")
    ap.add_argument("--backend", choices=["anthropic", "local"])
    ap.add_argument("--model")
    a = ap.parse_args()
    asyncio.run(main(a.db, a.user, a.advise, a.backend, a.model))
