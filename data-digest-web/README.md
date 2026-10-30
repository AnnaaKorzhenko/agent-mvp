# Data Digest — веб-інтерфейс + API

```
frontend/   інтерфейс (HTML/CSS/JS)
backend/    FastAPI: читає MongoDB, відповідає через LLM, віддає інтерфейс
```

Інтерфейс відкривається з того ж сервера, тож окремо запускати його не треба.

## 1. Запуск локально (Mac)

```bash
cd data-digest-web/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env .env      # відкрий .env і впиши MONGODB_URI та LLM_API_KEY
```

## 2. Налаштувати поля під свою базу (один раз)

```bash
python inspect_db.py
```

Скрипт лише читає базу і показує колекції, поля з прикладами значень і списки категорій та платформ.
За його виводом поправ у `app/config.py`:

- `COLLECTIONS` — як у тебе називаються колекції постів, сюжетів і користувачів;
- `POST_FIELDS`, `STORY_FIELDS`, `USER_FIELDS` — назви полів (вкладені пишуться через крапку, напр. `meta.channel`);
- `CATEGORY_MAP` — які значення `category` з бази відповідають трьом категоріям інтерфейсу;
- `PLATFORM_MAP`, `LANG_MAP` — те саме для платформ і мов.

Для початку можна поставити `DB_NAME=test-data`.

## 3. Старт

```bash
uvicorn app.main:app --reload --port 8000
```

Відкрий **http://localhost:8000**. Перевірка підключення до бази: http://localhost:8000/api/health

## Що робить кожен ендпоінт

| Ендпоінт | Звідки бере дані |
|---|---|
| `GET /api/digest?period=day\|week&category=` | колекція сюжетів за 1 або 7 днів; якщо сюжетів ще немає — показує пости |
| `GET /api/posts?q=&platform=&lang=` | пошук по `summary`, `text` і назві джерела |
| `POST /api/chat` | знаходить релевантні пости за 14 днів, віддає їх LLM, повертає відповідь з посиланнями на джерела |
| `GET/PUT /api/profile` | профіль у колекції користувачів (id з `WEB_USER_ID`) |

## Локальна модель замість OpenAI

У `.env`:
```
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:14b
```

## Деплой на Railway

1. Новий сервіс з папки `data-digest-web` (у корені вже є `requirements.txt` і `Procfile`, Root Directory лишай порожнім).
2. Start command підтягнеться з `Procfile`: `cd backend && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Змінні середовища — ті самі, що в `.env`. **Обов'язково** задай `APP_USER` і `APP_PASSWORD`, інакше інтерфейс і дані будуть відкриті всім, хто знайде адресу.
4. У MongoDB Atlas → Network Access дозволь підключення з Railway.

## Що варто знати

- Дати в базі мають бути типу `datetime`, а не рядком. Якщо `inspect_db.py` показує `str`, фільтр за періодом нічого не знайде. Скажи — переробимо.
- Логін поки один на всіх (HTTP Basic), а профіль у вебі один (`WEB_USER_ID`). Персональні акаунти — наступний крок.
- «Нещодавні чати» в бічній панелі поки статичні; збереження історії чатів — теж наступний крок.
