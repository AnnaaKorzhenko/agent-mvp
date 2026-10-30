"""
Налаштування бекенду.

1) Секрети й адреси — у змінних середовища / файлі .env (див. .env).
2) Назви колекцій і полів у MongoDB — у словниках нижче.
   Запусти `python inspect_db.py`, подивись реальні назви полів у своїй базі
   і виправ значення праворуч. Ліва частина (ключі) — те, що очікує фронт, її не чіпай.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    MONGODB_URI: str = "mongodb://localhost:27017"
    DB_NAME: str = "News"                      # тестова база: test-data

    # LLM аналітика. Працює з OpenAI або з будь-яким OpenAI-сумісним сервером
    # (Ollama: LLM_BASE_URL=http://localhost:11434/v1, LLM_API_KEY=ollama, LLM_MODEL=qwen2.5:14b)
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str | None = None
    LLM_MODEL: str = "gpt-5-mini"
    CHAT_CONTEXT_DAYS: int = 14                # з якого періоду брати пости для відповіді
    CHAT_CONTEXT_POSTS: int = 30               # скільки постів давати моделі

    # Профіль у вебі прив'язується до цього користувача бота (поки немає логіну)
    WEB_USER_ID: str = "web"

    # Захист: якщо задано — браузер попросить логін/пароль (HTTP Basic)
    APP_USER: str = ""
    APP_PASSWORD: str = ""

    CORS_ORIGINS: str = "*"                    # через кому, напр. http://localhost:5173
    FRONTEND_DIR: str = "../frontend"          # звідки віддавати інтерфейс
    TIMEZONE: str = "Europe/Kyiv"


settings = Settings()


# ---------- Колекції ----------
COLLECTIONS = {
    "posts": "posts",          # оброблені пости (TG, X, web)
    "stories": "stories",      # сюжети (групи постів про одну подію)
    "users": "users",          # профілі користувачів бота
}

# ---------- Поля постів ----------
POST_FIELDS = {
    "platform": "platform",    # telegram / x / web
    "source": "channel",       # назва каналу / акаунта / сайту
    "lang": "lang",            # uk / ru / en
    "date": "date",            # datetime
    "summary": "summary",      # LLM-підсумок
    "text": "text",            # оригінальний текст (для пошуку)
    "category": "category",
    "relevance": "relevance",  # число (0–1 або 1–10) чи рядок high/medium/low
    "url": "url",
}

# ---------- Поля сюжетів ----------
STORY_FIELDS = {
    "title": "title",
    "summary": "summary",
    "category": "category",
    "side": "side",            # UA / RU / Світ (якщо немає — визначимо за мовою)
    "lang": "lang",
    "relevance": "relevance",
    "date": "date",            # дата сюжету / останнього поста
    "posts_count": "posts_count",
    "post_ids": "post_ids",    # якщо posts_count немає — порахуємо len(post_ids)
}

# ---------- Поля профілю ----------
USER_FIELDS = {
    "id": "user_id",
    "interests": "interests",
    "schedule": "schedule",
    "sides": "sides",
}

# Категорії фронту -> значення поля category у базі (можна кілька на одну)
CATEGORY_MAP = {
    "tech": ["defense_tech", "Оборонні технології"],
    "front": ["front_innovation", "Інновації на фронті"],
    "invest": ["defense_investment", "Оборонні інвестиції"],
}

# Платформи фронту -> значення поля platform у базі (без урахування регістру)
PLATFORM_MAP = {
    "Telegram": ["telegram", "tg"],
    "X": ["x", "twitter"],
    "Web": ["web", "site"],
}

# Мови фронту -> значення поля lang у базі
LANG_MAP = {
    "UA": ["uk", "ua"],
    "RU": ["ru"],
    "EN": ["en"],
}
