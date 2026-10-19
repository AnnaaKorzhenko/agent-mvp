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

class Config:
    MONGO_URI: str = os.getenv("MONGO_URI", "")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    DIRECTOR_CHAT_ID: int = get_env_int("DIRECTOR_CHAT_ID", 0)
    SCRAPE_INTERVAL_MINUTES: int = get_env_int("SCRAPE_INTERVAL_MINUTES", 30)

config = Config()