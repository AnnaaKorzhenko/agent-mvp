import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

class Config:
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "deftech_intel")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    DIRECTOR_CHAT_ID: int = int(os.getenv("DIRECTOR_CHAT_ID", "0"))
    SCRAPE_INTERVAL_MINUTES: int = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "30"))

    # Initial target sources for MVP
    TARGET_TG_CHANNELS: list[str] = [
        "milinfolive",
        "serhii_flash",
        "kiber_boroshno"
    ]

    TARGET_RSS_FEEDS: list[str] = [
        "https://militarnyi.com/feed/",
        "https://def-or.com/feed/"
    ]

config = Config()

_mongo_client: AsyncIOMotorClient | None = None

def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = AsyncIOMotorClient(config.MONGO_URI)
    return _mongo_client[config.MONGO_DB_NAME]