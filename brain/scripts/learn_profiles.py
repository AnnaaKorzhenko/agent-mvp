"""Runs the learning job: updates users.learned from each user's new messages, ratings and insights.

    brain/.venv/bin/python -m brain.scripts.learn_profiles [--db News_dev] [--user ID] [--backend anthropic|local]
                                                           [--since 2026-10-29] [--show-payload | --dry-run]

--show-payload  prints what would be sent, without calling the model (free)
--dry-run       calls the model and prints the new profile, without saving it
(neither)       calls the model, saves users.learned and proposes the goals it found
--since DATE    reprocesses activity from that UTC date (default: since the last run), e.g. after a fix of what
                the job sees; the previous profile is still merged in

The backend comes from MODEL_BACKEND unless --backend is given; `anthropic` needs ANTHROPIC_API_KEY and uses
ANTHROPIC_MODEL. A cloud backend gets `public` / `internal` data only. Token use is printed per user and in total;
every call is also in audit_log (event model_call).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from brain.audit.log import AuditLog
from brain.config import get_settings
from brain.db.client import close_client, get_client
from brain.db.repositories import Repos
from brain.integrations.backends import get_backend
from brain.learning import profile


def _user_id(raw: str):
    return int(raw) if raw.lstrip("-").isdigit() else raw


async def main(db_name: str | None, user: str | None, backend_name: str | None, show_payload: bool,
               dry_run: bool, since: datetime | None = None) -> None:
    settings = get_settings()
    if backend_name:
        settings = settings.model_copy(update={"model_backend": backend_name})
    db = get_client(settings)[db_name or settings.mongo_db]
    backend = get_backend(settings, AuditLog(db))
    users = [_user_id(user)] if user else await profile.users_with_activity(db)
    print(f"database {db.name}, backend {backend.name} (may receive up to '{backend.max_label}'), "
          f"{len(users)} user(s)")
    total_in = total_out = 0
    for uid in users:
        act = await profile.collect(db, uid, backend.max_label, since)
        head = (f"\nuser {uid}: {len(act.chat)} messages, {len(act.feedback)} ratings, {len(act.insights)} "
                f"insights new; {act.withheld} withheld above '{backend.max_label}'; payload {act.size()} chars, "
                f"label {act.label}")
        print(head)
        if act.empty:
            print("  nothing new: skipped")
            continue
        if show_payload:
            print(json.dumps(act.payload(), ensure_ascii=False, indent=1, default=str))
            continue
        try:
            outcome = await profile.learn(backend, act)
        except Exception as e:             # noqa: BLE001 - one user's failure doesn't stop the others
            print(f"  failed: {type(e).__name__}: {e}")
            continue
        used_in = sum(c.input_tokens for c in outcome.calls)
        used_out = sum(c.output_tokens for c in outcome.calls)
        total_in, total_out = total_in + used_in, total_out + used_out
        print(f"  model {outcome.calls[-1].model}: {used_in} input + {used_out} output tokens "
              f"in {len(outcome.calls)} call(s)")
        print(json.dumps(outcome.profile.model_dump(), ensure_ascii=False, indent=1))
        if dry_run:
            print("  dry run: not saved")
        else:
            await profile.save(db, act, outcome)
            await profile.after_save(Repos(db), act, outcome, profile_keywords=settings.profile_keywords)
            print("  saved to users.learned (goal candidates proposed)")
    if total_in or total_out:
        print(f"\ntotal: {total_in} input + {total_out} output tokens")
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--user")
    ap.add_argument("--backend", choices=["anthropic", "local"])
    ap.add_argument("--since", type=lambda d: datetime.fromisoformat(d).replace(tzinfo=timezone.utc))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--show-payload", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.db, a.user, a.backend, a.show_payload, a.dry_run, a.since))
