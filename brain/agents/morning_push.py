"""The morning digest as a Web Push (docs/WEB_PLAN.md step 5.6): the browsers a user subscribed in the web app
(users.push_subscriptions) are told once a day that today's digest is ready.

Agent on the runtime: «morning_push», scope global, every EVERY. Between START and END (Kyiv) it pushes to every
subscribed user whose digest is ready by the morning delivery's rule (digests.morning_ready: both sides before
noon). Once per digest and user: its own mark users.morning_digest.web_push, claimed before sending, so Claude's
delivery (morning_digest.key, get_morning_digest) still delivers it in full. The push says no news, only counts:
«Ранковий дайджест готовий: N пунктів, M для вас» (M: the «⭐» items, for-you and followed topics). A subscription
the push service answers 404/410 for is gone (the browser dropped it) and is deleted; any other failure is
counted and the others still get theirs.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta
from typing import Awaitable, Callable

from pymongo.asynchronous.database import AsyncDatabase

from brain.agents import common
from brain.agents.runs import GLOBAL
from brain.agents.runtime import Report, Runtime
from brain.db.repositories import Repos
from brain.domain import digests

logger = logging.getLogger("brain.agents.morning_push")

AGENT = "morning_push"
EVERY = timedelta(minutes=30)
START, END = time(8, 0), time(11, 30)     # Kyiv: no push at night, and none after the morning
TITLE = "Data Digest"
TAG = "morning-digest"                    # sw.js: a new push replaces the old notification
TTL = 6 * 3600                            # a push the browser didn't get in time is dropped, not shown at night
GONE = (404, 410)


Sender = Callable[[dict, str], Awaitable[int]]      # (subscription {endpoint, keys}, JSON payload) -> HTTP status


def webpush_sender(settings) -> Sender:
    """Sends with pywebpush (blocking: in a thread), signed with the VAPID pair."""
    from pywebpush import WebPushException, webpush

    private = settings.web_push_private_key.get_secret_value()
    claims = {"sub": settings.web_push_contact} if settings.web_push_contact else {}

    def send_now(sub: dict, data: str) -> int:
        try:
            response = webpush({"endpoint": sub["endpoint"], "keys": sub["keys"]}, data,
                               vapid_private_key=private, vapid_claims=dict(claims), ttl=TTL)
        except WebPushException as e:
            if e.response is None:
                raise
            return e.response.status_code
        return response.status_code

    async def send(sub: dict, data: str) -> int:
        return await asyncio.to_thread(send_now, sub, data)
    return send


def _items(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} пункт"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} пункти"
    return f"{n} пунктів"


def message(res: digests.DigestResultView) -> dict:
    """The push: counts only, never a headline (it shows on a lock screen)."""
    total = mine = 0
    for view in res.digests:
        for section, items in view.sections:
            total += len(items)
            if view.kind is not None or section.category == digests.FOR_YOU.category:
                mine += len(items)
    return {"title": TITLE, "body": f"Ранковий дайджест готовий: {_items(total)}, {mine} для вас", "tag": TAG}


class MorningPush:
    def __init__(self, db: AsyncDatabase, runtime: Runtime, send: Sender, every: timedelta = EVERY):
        self.repos = Repos(db)
        self.send = send
        runtime.register(AGENT, self.push)
        runtime.schedule(AGENT, GLOBAL, every)

    async def push(self, scope, trigger: str, now: datetime | None = None) -> Report:
        now = (now or datetime.now(digests.KYIV)).astimezone(digests.KYIV)
        if not START <= now.time() < END:
            return Report("outside the morning window")
        users = sent = gone = failed = 0
        for user_id, subs in await self.repos.users.with_push_subscriptions():
            try:
                counts = await self._user(user_id, subs, now)
            except Exception as e:             # noqa: BLE001 - one user's failure never stops the others
                logger.error(f"{AGENT} {user_id}: {type(e).__name__}: {e}")
                failed += 1
                continue
            if counts:
                users, sent, gone, failed = users + 1, sent + counts[0], gone + counts[1], failed + counts[2]
        return Report(f"{users} pushed: {sent} sent, {gone} gone, {failed} failed")

    async def _user(self, user_id, subs: list[dict], now: datetime) -> tuple[int, int, int] | None:
        """(sent, gone, failed) for the user's browsers, or None if there is nothing new to push."""
        ctx = common.agent_ctx(user_id)
        res, key = await digests.morning_ready(self.repos, ctx, now)
        if key is None or not await self.repos.users.claim_morning_push(ctx.user_id, key, now.date().isoformat()):
            return None
        data = json.dumps(message(res), ensure_ascii=False)
        sent = gone = failed = 0
        for sub in subs:
            try:
                status = await self.send(sub, data)
            except Exception as e:             # noqa: BLE001 - the user's other browsers still get it
                logger.warning(f"{AGENT} {user_id}: push failed: {type(e).__name__}: {e}")
                failed += 1
                continue
            if status in GONE:
                await self.repos.users.delete_push_subscription(ctx.user_id, sub["endpoint"])
                gone += 1
            elif 200 <= status < 300:
                sent += 1
            else:
                logger.warning(f"{AGENT} {user_id}: push service answered {status}")
                failed += 1
        return sent, gone, failed


def enabled(settings) -> bool:
    return bool(settings.morning_push and settings.web_push_public_key and settings.web_push_private_key)


def morning_push(db: AsyncDatabase, settings, runtime: Runtime, send: Sender | None = None) -> MorningPush:
    return MorningPush(db, runtime, send or webpush_sender(settings))
