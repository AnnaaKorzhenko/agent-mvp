"""The agents' worker in its own service (brain/agents/worker.py, `python -m brain.agents`)."""
import asyncio
import logging
import os
import signal

import pytest

from brain.agents import worker
from brain.agents.runtime import Report, Runtime
from brain.agents.setup import Agents


@pytest.fixture
async def own(test_db):
    """A database of its own: the worker takes any queued run of its agents."""
    db = test_db.client[f"{test_db.name}_worker"]
    yield db
    await test_db.client.drop_database(db.name)


async def until(check, timeout=20.0):
    for _ in range(int(timeout / 0.1)):
        if await check():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("timed out")


def demo_runtime(db, seen):
    rt = Runtime(db)

    async def demo(scope, trigger):
        seen.append((scope, trigger))
        return Report("ok")

    rt.register("demo", demo)
    return rt


async def test_the_worker_runs_a_queued_run_logs_it_and_stops(own, caplog):
    seen = []
    rt = demo_runtime(own, seen)
    await rt.submit("demo", 7, "test")
    stop = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="brain.agents"):
        task = asyncio.create_task(worker.serve(Agents(runtime=rt, learner=None), stop))

        async def finished():
            return await own.agent_runs.count_documents({"agent": "demo", "status": "done"}) == 1
        await until(finished)
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    assert seen == [(7, "test")]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("demo ")]
    assert lines == ["demo 7 (test, queued): ok"]


async def test_sigterm_stops_the_worker_cleanly(own):
    seen = []
    rt = demo_runtime(own, seen)
    task = asyncio.create_task(worker.run(Agents(runtime=rt, learner=None)))
    await asyncio.sleep(0.5)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=10)
    assert task.exception() is None
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL          # the handler is gone with the worker


async def test_with_no_agent_on_it_waits_for_the_stop(caplog):
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="brain.agents"):
        task = asyncio.create_task(worker.serve(Agents(runtime=None, learner=None), stop))
        await asyncio.sleep(0.2)
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    assert any("nothing to run" in r.getMessage() for r in caplog.records)


async def test_main_builds_the_agents_with_the_shared_function(monkeypatch):
    built = []

    def fake_build(db, settings):
        built.append(settings)
        return Agents(runtime=None, learner=None)

    async def fake_run(agents):
        assert agents.runtime is None and agents.learner is None

    async def no_close():
        pass

    monkeypatch.setattr(worker, "build_agents", fake_build)
    monkeypatch.setattr(worker, "get_db", lambda settings: None)
    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(worker, "close_client", no_close)
    await worker.main()
    assert len(built) == 1
