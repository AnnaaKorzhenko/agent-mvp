"""The agents' worker as its own service: the learner's watch loop and the runtime's queue, nothing else.

Run (Render: a Background Worker with the MCP service's env):
    brain/.venv/bin/python -m brain.agents
Stops cleanly on SIGTERM/SIGINT: the loops are cancelled between awaits, and a run cut short is taken again
once its lease in agent_runs expires (brain/agents/runs.py). While it runs it writes a heartbeat there every
HEARTBEAT_EVERY, which the agents console shows (the MCP service, when it runs the agents itself, writes none).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket

from brain.agents.runs import HEARTBEAT_EVERY, AgentRuns
from brain.agents.setup import Agents, build_agents
from brain.config import get_settings
from brain.db.client import close_client, get_db

logger = logging.getLogger("brain.agents")


async def beat(runs: AgentRuns, every: float = HEARTBEAT_EVERY.total_seconds()) -> None:
    """The heartbeat loop: a failed write is logged and tried again next time, it never stops the worker."""
    host, pid = socket.gethostname(), os.getpid()
    while True:
        try:
            await runs.beat(host, pid)
        except Exception as e:                  # noqa: BLE001 - the agents matter more than the marker
            logger.error(f"agents worker: heartbeat failed: {type(e).__name__}: {e}")
        await asyncio.sleep(every)


async def serve(agents: Agents, stop: asyncio.Event, heartbeat: AgentRuns | None = None) -> None:
    """Runs the agents' loops (and the heartbeat, if given) until `stop` is set, then cancels them and waits for
    them to end."""
    tasks = []
    if heartbeat is not None:                   # also with no agent on: the console shows the worker is up
        tasks.append(asyncio.create_task(beat(heartbeat), name="heartbeat"))
    if agents.learner is not None:
        tasks.append(asyncio.create_task(agents.learner.watch(), name="learner.watch"))
    if agents.runtime is not None:
        tasks.append(asyncio.create_task(agents.runtime.work(), name="runtime.work"))
    if agents.learner is None and agents.runtime is None:     # still waits: an exiting worker would only restart
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


async def run(agents: Agents, heartbeat: AgentRuns | None = None) -> None:
    """serve() until SIGTERM (Render's stop) or SIGINT (Ctrl+C)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)
    try:
        await serve(agents, stop, heartbeat)
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
    logger.info("agents worker: stopped")


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = get_db(settings)
    agents = build_agents(db, settings)
    logger.info(f"agents worker: db={settings.mongo_db} agents="
                f"{', '.join(sorted(agents.runtime.agents)) if agents.runtime else 'none'}")
    try:
        await run(agents, AgentRuns(db))
    finally:
        await close_client()
