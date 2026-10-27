# brain: the MCP server

The single door to the news brain for Claude (and later ChatGPT and a local chat).
Design: `docs/MCP_PLAN.md`. Data it depends on: `docs/DATA_CONTRACT.md`.

## Setup (Python 3.12 via uv)

```bash
uv venv brain/.venv --python 3.12
uv pip install --python brain/.venv/bin/python -r brain/requirements-dev.txt
```

Settings come from env or the repo's `.env` (see `.env.example`).

## Tests

```bash
brain/.venv/bin/pytest tests -m "not contract"    # throwaway News_test_* database, dropped afterwards
CONTRACT_MONGO_URI=... brain/.venv/bin/pytest tests/contract -m contract    # real data, read-only
```

## Run locally (sample data only)

```bash
brain/.venv/bin/python -m brain.scripts.load_dev_data --db News_dev --reset
MONGO_DB=News_dev DEV_USER_ID=111000111 TRUST_LEVEL=cloud brain/.venv/bin/python -m brain.mcp_server
```

- MCP endpoint: `http://127.0.0.1:8000/mcp` (streamable HTTP)
- Liveness: `http://127.0.0.1:8000/health`
- Dev mode (no OAuth) refuses to start on a non-local `PUBLIC_BASE_URL`.

To test with real news instead, copy `posts`, `sources` and `digests` from `News` (read-only) into `News_dev`. Users and `audit_log` in `News_dev` are kept. Rerun it before a test session, since the pipeline writes to `News`:

```bash
brain/.venv/bin/python -m brain.scripts.copy_to_dev --dry-run
brain/.venv/bin/python -m brain.scripts.copy_to_dev
```

**MCP Inspector** (needs Node.js 22+):

```bash
npx @modelcontextprotocol/inspector
```

Then choose "Streamable HTTP" with the URL `http://127.0.0.1:8000/mcp`.

## Tools (v0, no LLM calls)

| Tool | What it returns |
|---|---|
| `get_digest(side, date)` | The daily digest(s) in the user's order, with links and post ids. Russian items are flagged as claims. |
| `search_posts(query, side, category, days, limit)` | Ranked posts: summary, tags, channel, `post_url` |
| `get_post(post_id)` | One post |
| `get_profile()` | Goals, today's categories, top categories and tags. Never feedback. |
| `record_feedback(post_id, reaction, text)` | Saves a like / dislike / skip in the bot's feedback shape and updates scores |
| `set_goal(action, text)` | Adds or removes a goal with the bot's rules (max 5 active, no duplicates; removing keeps history) |

Every call is written to `audit_log`. Run `python -m brain.scripts.setup_indexes` once per database for the TTL index.

## Eval baseline

```bash
brain/.venv/bin/python -m brain.eval.run_questions --db News     # read-only; results in brain/eval/results/
```

Mark the CSV's `relevant` column by hand. The marked file is the baseline for comparing backends later.
