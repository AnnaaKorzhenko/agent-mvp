"""The brain's own MongoDB connection (PyMongo async API), configured from Settings.

Separate from the bot's db.py on purpose: db.py belongs to the local-model track and hard-codes the
database name, which tests cannot change.
"""
from __future__ import annotations

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from brain.config import Settings, get_settings

_client: AsyncMongoClient | None = None


def get_client(settings: Settings | None = None) -> AsyncMongoClient:
    global _client
    if _client is None:
        settings = settings or get_settings()
        _client = AsyncMongoClient(settings.mongo_uri.get_secret_value(), tz_aware=True,
                                   appname="brain-mcp")
    return _client


def get_db(settings: Settings | None = None) -> AsyncDatabase:
    settings = settings or get_settings()
    return get_client(settings)[settings.mongo_db]


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
