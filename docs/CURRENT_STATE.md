# Current state of the repository

Written 2026-10-26 on branch `mcp-server`, which starts from `main` / `prod` at `399d7b5`. The facts below come from reading the code and from read-only queries against the `News` database.

## 1. Modules

| File | What it does | Runs where |
|---|---|---|
| `db.py` | Motor client (`MONGO_URI`, `tz_aware=True`), `db = client["News"]`, plus `sources_collection`, `posts_collection` and `digests_collection` | Both the pipeline and the bot |
| `config.py` | `Config` class read from env. Mostly unused by the bot. `MONGO_DB_NAME` defaults to `News-data`, but `db.py` hard-codes `News`. | Both |
| `scraper.py` | Scrapes `t.me/s/<channel>` web pages and inserts `posts` with `raw_text` | Kaggle pipeline |
| `cleaning.py` | Text cleaning, ad detection and channel footers for the summarizer | Kaggle pipeline |
| `summarizer.py` | **Local model client.** It builds `AsyncOpenAI(base_url=LLM_BASE_URL or http://localhost:11434/v1)` with `LLM_MODEL` (default `qwen2.5:7b-instruct`, through Ollama). Its entry points are `analyze_text(text, side, sem)` and `analyze_pending_posts()`. It writes `summary`, `category`, `importance`, `tags`, `source_side`, `processed` and `prompt_version` to `posts`. | Kaggle pipeline |
| `digest.py` | Builds `digests` per period and side, using the **same client** (`from summarizer import CATEGORIES, MODEL, client`). Entry points: `build_digest(period, side, …)` and `get_latest_digest(period, side)`. | Kaggle pipeline |
| `cleanup.py` | Retention: drops `raw_text`/`clean_text` after 7 days, deletes junk and old posts, keeps digests for 365 days | Kaggle pipeline |
| `analyst.py` | The bot's question answering. It has its **own** OpenAI client (`ASSISTANT_LLM_*`, currently gpt-5-nano) and a tool loop with `search_news`, `get_digest` and `get_trends` (regex over `posts.summary` and `tags`). Entry point: `answer(user_id, question, reply_to)`. | Railway bot |
| `user_store.py` | Bot data layer: users, feedback, **interest scoring**, goals, and chat messages in `conversations.messages` | Railway bot |
| `main.py` | aiogram 3 bot: delivery (all at once, or one at a time with 👍/👎/⏭), comments, the morning picker, push loop, goals, health server | Railway bot (`Procfile: worker: python main.py`) |
| `question_bank.yaml` | Feedback and onboarding questions. Not read by any code yet. | – |
| `questions.jsonl` | 20 evaluation questions (`id`, `type`, `window_days`, `question`, `good_answer_notes`, `red_flags`) | – |
| `test_pipeline.py` | **Broken**: imports `scrapers.*` and `bot.telegram_bot`, which no longer exist | – |
| `requirements.txt` / `requirements-pipeline.txt` | Bot dependencies / pipeline dependencies. The pipeline list includes `fastembed`, which no code uses yet. | – |

## 2. Ownership

The task assigns the local-model flow (model runtime and client, summarizer, analyst, related prompts) to the teammate. Git history doesn't settle this cleanly, because the old `scrapers/` layout was moved to the root by one person on 2026-10-20. That makes the "last author" of most pipeline files misleading. Ownership below is therefore by role, confirmed where git agrees.

**Owned by the local-model track (do not edit, move, rename or reformat):**
- `summarizer.py`: the local model client and summary prompts
- `digest.py`: digest prompts and build, uses the local model
- `analyst.py`: the analyst. The teammate made the last 3 of its 4 commits.
- `requirements-pipeline.txt`

**To confirm with you (shared or unclear):**
- `cleaning.py`, `scraper.py`, `cleanup.py`: pipeline files, not model code
- `db.py`: shared by both tracks; the teammate made the last commit (renaming collections)
- `question_bank.yaml`, `questions.jsonl`: the MCP eval will **read** `questions.jsonl` only

**Owned by this track (bot, MCP, memory):** `main.py`, `user_store.py`, `Procfile`, `requirements.txt`, and the new `brain/`, `tests/` and `docs/`.

## 3. Data flow

```
Kaggle:  scraper.py → posts(raw_text) → summarizer.py (Qwen via Ollama) → posts(summary, tags, …)
         digest.py (Qwen) → digests          cleanup.py → strips raw_text after 7 days
Railway: main.py reads digests → Telegram → users.feedback, users.interests (user_store.py)
         analyst.py (gpt-5-nano) reads posts/digests/users → conversations.messages
```

**MongoDB connection**
- `db.py` creates one Motor client and the bot imports it (`user_store.py: from db import db`).
- The secret comes from `.env` / env through `python-dotenv`.

**Interest scoring (`user_store.py`)**
- `POINTS = {like: +1, dislike: −1, skip: 0}`, plus `CHOICE_POINTS = +1` for every category chosen in the morning picker.
- `_points_inc(entry, sign)` builds `$inc` for `interests.category_scores.<side>.<category>` and `interests.tag_scores.<tag>`. Tag keys are cleaned by `_key()`, which replaces `.` with `·` and strips a leading `$`.
- `set_reaction()` replaces any earlier reaction to the same digest item and reverses its points.
- `category_scores()`, `top_tags()` and `category_order()` read the scores.

**Feedback write (`set_reaction`)**
- It `$push`es `{post_id: source_post_ids[0], reaction, text: "", digest_id, item_n, side, category, tags[:10], at}`, capped at the last 1,000 entries.
- Comments are added later through `set_comment()`: `text`, `text_at`, `comment_status`.
- Entries are keyed by **`(digest_id, item_n)`**, not by `post_id`.

**Digest read (`main.py`)**
- `latest_daily_docs()` takes the latest `{period: "day", side, trigger: "scheduled"}` per side.
- `_get_digest()` falls back to any trigger.
- `ordered_items()` orders sections by today's picker choice (`users.daily.selected`) first, then by `category_scores`.

**Configuration and secrets**
- Values come from env through `os.environ`, `os.getenv` and `load_dotenv()`, which is called in `db.py` and `config.py`.
- `.env` is git-ignored.
- There is no `.env.example`.

**Deployment**
- Railway runs the bot from the `Procfile`. There is no Dockerfile.
- A keep-alive HTTP server starts when `PORT` is set.
- The Kaggle pipeline runs outside this repo's deployment.

## 4. Database facts (read-only check, 2026-10-26)

**Deployment**
- MongoDB Atlas 8.0.34 (`mongodb+srv`, enterprise modules).
- `$listSearchIndexes` works and currently returns no indexes, so Atlas Search and Vector Search are available on this cluster.

**Document counts**

| Collection | Documents |
|---|---|
| `sources` | 40 |
| `posts` | 4,365 |
| `digests` | 16 |
| `users` | 3 |
| `conversations` | 3 |

**`posts`**
- `published_at` and `created_at` are **ISO strings**, not dates. `analyzed_at` is a datetime.
- `post_id` is a string (the Telegram message id). `channel_id` is an ObjectId.
- `raw_text` is still present on 1,874 posts. The rest have `text_removed: true`.
- No post has an embedding.
- There is **no text index**.

**`digests`**
- `period` is `day` or `week`, `trigger` is `scheduled` or `on_demand`, `side` is `ua` or `ru`.
- Items are `{text, source_urls[], source_post_ids[], n}`.
- `created_at` is a datetime; `period_start` and `period_end` are strings.

**`sources`**
- `source_country` is `UA` or `RU`, and `platform` is always `telegram`.
- `category` is null on 11 of 40.
- `created_at` is a string.

**`users`**
- `_id` is the Telegram id (int).
- `categories {ua[], ru[]}` exists but **no current code writes or reads it**. It is a legacy field.
- No `identities` field exists anywhere, and no post or digest has `sensitivity`.

## 5. Fragile points

1. **Date types:** `published_at` is a string, so date filters are string comparisons. That works only while every writer keeps the same ISO format and timezone.
2. **Two different model clients:** the summarizer and digest use `summarizer.client` (Ollama/Qwen); the analyst uses its own OpenAI client. There is no shared model interface yet.
3. **Feedback identity:** feedback is keyed by digest item, not post. `feedback_report()` groups by `(digest_id, item_n)`.
4. **Stray indexes on `sources`:** it has the four `posts` indexes (`published_at/importance`, `processed`, …), left from an earlier collection-name swap. They're harmless but show that collection names have moved before.
5. **`db.py` can't be configured:** the database name `News` is hard-coded, while `config.MONGO_DB_NAME` says `News-data`. Tests can't point at a test database without patching.
6. **Python versions:** the only local interpreter is Python 3.9.6 (`.venv`), and there is no `uv` or Docker installed. The bot code uses `str | None` annotations, which only work on 3.10+ unless `from __future__ import annotations` is added, so Railway must be running 3.10 or newer.
7. **A root Dockerfile would hijack the bot's Railway build:** Railway prefers a root `Dockerfile` over the `Procfile`. The MCP image must live elsewhere, for example `deploy/mcp/Dockerfile` with `RAILWAY_DOCKERFILE_PATH`.
8. **No tests:** there are no automated bot tests in the repo, and `test_pipeline.py` is broken.
9. **Retention:** `cleanup.py` deletes posts after 14 to 180 days depending on importance, and digests after 365 days. Old `source_post_ids` in digests may point to posts that no longer exist.
