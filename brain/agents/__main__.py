"""brain/.venv/bin/python -m brain.agents: the agents' worker (brain/agents/worker.py)."""
import asyncio

from brain.agents.worker import main

if __name__ == "__main__":
    asyncio.run(main())
