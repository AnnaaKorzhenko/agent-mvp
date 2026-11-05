"""The agents' worker as its own service: the learner's watch loop and the runtime's queue, nothing else.

Run (Render: a Background Worker with the MCP service's env):
    brain/.venv/bin/python -m brain.agents
Stops cleanly on SIGTERM/SIGINT: the loops are cancelled between awaits, and a run cut short is taken again
once its lease in agent_runs expires (brain/agents/runs.py).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from brain.agents.setup import Agents, build_agents
from brain.config import get_settings
from brain.db.client import close_client, get_db

logger = logging.getLogger("brain.agents")


async def serve(agents: Agents, stop: asyncio.Event) -> None:
    """Runs the agents' loops until `stop` is set, then cancels them and waits for them to end."""
    tasks = []
    if agents.learner is not None:
        tasks.append(asyncio.create_task(agents.learner.watch(), name="learner.watch"))
    if agents.runtime is not None:
        tasks.append(asyncio.create_task(agents.runtime.work(), name="runtime.work"))
    if not tasks:                               # still waits: an exiting worker would only be restarted
        logger.warning("agents worker: no agent is on in the settings, nothing to run")
    stopping = asyncio.create_task(stop.wait())
    try:
        while not stop.is_set():
            done, _ = await asyncio.wait([stopping, *tasks], return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t is not stopping:           # the loops never return; a crash is logged, the rest go on
                    tasks.remove(t)
                    if not t.cancelled() and t.exception() is not None:
                        logger.error(f"agents worker: {t.get_name()} stopped: {t.exception()!r}")
    finally:
        for t in [stopping, *tasks]:
            t.cancel()
        for t in [stopping, *tasks]:
            with contextlib.suppress(asyncio.CancelledError):
                await t


async def run(agents: Agents) -> None:
    """serve() until SIGTERM (Render's stop) or SIGINT (Ctrl+C)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)
    try:
        await serve(agents, stop)
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
    logger.info("agents worker: stopped")


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    agents = build_agents(get_db(settings), settings)
    logger.info(f"agents worker: db={settings.mongo_db} agents="
                f"{', '.join(sorted(agents.runtime.agents)) if agents.runtime else 'none'}")
    try:
        await run(agents)
    finally:
        await close_client()
