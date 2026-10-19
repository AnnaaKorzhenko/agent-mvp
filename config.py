# config.py
import os
from dotenv import load_dotenv

load_dotenv()

def get_env_int(key: str, default: int = 0) -> int:
    val = os.getenv(key, "").strip() if os.getenv(key) else ""
    try:
        return int(val) if val else default
    except ValueError:
        return default

def get_env_str(key: str, default: str = "") -> str:
    return (os.getenv(key) or "").strip() or default

class Config:
    MONGO_URI: str = get_env_str("MONGO_URI")
    MONGO_DB_NAME: str = get_env_str("MONGO_DB_NAME", "News-data")
    TELEGRAM_BOT_TOKEN: str = get_env_str("TELEGRAM_BOT_TOKEN")
    DIRECTOR_CHAT_ID: int = get_env_int("DIRECTOR_CHAT_ID", 0)
    TIMEZONE: str = get_env_str("TIMEZONE", "Europe/Kyiv")
    DAILY_PUSH_HOUR: int = get_env_int("DAILY_PUSH_HOUR", 8)
    DAILY_PUSH_MINUTE: int = get_env_int("DAILY_PUSH_MINUTE", 0)

    # Used only by the external scraping/summarization pipeline, not by the Railway bot.
    GROQ_API_KEY: str = get_env_str("GROQ_API_KEY")
    GROQ_MODEL: str = get_env_str("GROQ_MODEL", "openai/gpt-oss-120b")
    SCRAPE_INTERVAL_MINUTES: int = get_env_int("SCRAPE_INTERVAL_MINUTES", 30)

    def validate(self) -> None:
        missing = [k for k in ("MONGO_URI", "TELEGRAM_BOT_TOKEN") if not getattr(self, k)]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

config = Config()
