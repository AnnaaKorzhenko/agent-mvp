from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .config import COLLECTIONS, settings

_client: AsyncIOMotorClient | None = None


def get_db() -> AsyncIOMotorDatabase:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.MONGODB_URI, tz_aware=True)
    return _client[settings.DB_NAME]


def set_client(client) -> None:
    """Для тестів: підмінити клієнт (напр. mongomock)."""
    global _client
    _client = client


def col(name: str):
    return get_db()[COLLECTIONS[name]]
