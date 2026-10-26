# MCP server plan ("brain")

Status: **approved 2026-10-26 with the decisions below. Phase 2 (foundation) and phase 3 (MCP server v0, local) done.**

Phase 3 notes:
- Search finds words that start a word (Unicode-aware; MongoDB patterns need `(*UCP)`). Stems are at most 6 letters, and a stop list drops question words.
- `brain/eval/run_questions.py` is the baseline (results are git-ignored).
- MCP Inspector wasn't run, because Node.js isn't installed. The same protocol checks ran through the SDK's own HTTP client against the running server.
See `CURRENT_STATE.md` for the facts this plan is based on.

## Decisions (2026-10-26)

| # | Decision |
|---|---|
| Python | `uv` installed; `brain/.venv` uses Python 3.12. |
| `audit_log` | Approved, with a TTL index. |
| Text index on `posts` | **Not approved.** `search_posts` uses case-insensitive pattern matching on `summary` and `tags` (as the bot's analyst does) and ranks in Python. No index is created on the pipeline's collection. |
| Tests | A throwaway `News_test_<random>` database on the same cluster, dropped after each run. |
| MCP feedback entries | Approved as in §0.5. |
| Ownership | `cleaning.py`, `scraper.py`, `cleanup.py` and `db.py` belong to the local-model track. `question_bank.yaml` is ours. |
| Dependencies | Approved. `brain/` uses PyMongo's async API (`AsyncMongoClient`). Motor stays only in the bot. |
| Anthropic fallback | Default model `claude-opus-5-5` (`ANTHROPIC_MODEL`). Refusal fallbacks are on (`fallbacks: "default"`). `temperature` is not sent, because current models reject it. |

## 0. Conflicts and decisions needed before phase 2

| # | What the task says | What the repo or the docs say | Proposal |
|---|---|---|---|
| 1 | Use "FastMCP" | MCP Python SDK **v2.3.0** (current on PyPI) renamed it to `MCPServer` (`from mcp.server import MCPServer`). The old import path is removed. Serving options (`host`, `port`, `transport_security`) moved to `run()` / `streamable_http_app()`. | Use `mcp>=2.3,<3` and `MCPServer`. |
| 2 | Python not specified | `mcp` needs Python ≥ 3.10. This Mac has **only Python 3.9.6**, and no Homebrew, `uv` or Docker. | `brain/` targets Python 3.12 (in the Docker image). For local dev you install Python 3.12 from python.org, or allow me to install `uv`. |
| 3 | Bot imports scoring from `brain/` | The bot runs on Railway from the repo root with `requirements.txt`. | `brain/domain/scoring.py` is **pure Python with no third-party imports**, and `brain/__init__.py` imports nothing. So the bot needs no new dependency. |
| 4 | `get_digest` ordered by `users.categories` + `category_scores` | `users.categories` is **legacy**: no current code writes or reads it. The bot orders by today's picker choice (`users.daily.selected`, if `daily.date` is today) and then `category_scores`. | Use the same order as the bot, so both interfaces agree. Ignore `categories` unless you want it as a fallback. |
| 5 | `record_feedback(post_id, …)` "in the same shape the bot uses" | Bot entries are keyed by `(digest_id, item_n)`, and `set_reaction` replaces the earlier reaction to the same item. | Same fields, with `digest_id: null`, `item_n: null` and `channel`. `side`, `category` and `tags` come from the post. A new MCP reaction replaces the previous **MCP** reaction to the same `post_id` and reverses its points through `scoring.py`. Telegram entries are never touched. |
| 6 | Map an OAuth login through `users.identities` | Nothing links an OAuth subject to a Telegram id yet. | Phase 4: an admin script `brain/scripts/link_identity.py --user <tg_id> --provider … --subject …` for the current 3 users. A self-service `/link` code in the bot is a later bot change. |
| 7 | OAuth "as required by Claude" | The SDK is **only a resource server**: it verifies tokens and serves RFC 9728 metadata. It has no login and issues no tokens. Claude needs an authorization server with DCR or CIMD, S256 PKCE, refresh, the callback `https://claude.ai/api/mcp/auth_callback` and loopback redirects for Claude Code. | Phase 4: pick an external authorization server (§6). We don't write our own. |
| 8 | Audit log | No collection exists for it. | A new collection, **`audit_log`**, with a TTL index (180 days). Needs your approval (§8). |
| 9 | "the user's role" for local trust | `users` has no role field, and roles aren't in the allowed additions. | For now, `MAX_SENSITIVITY_LOCAL` comes from config (default `restricted`). Roles come later if you approve a field. |
| 10 | Root `Dockerfile` | Railway prefers a root `Dockerfile` over the `Procfile`, so a root Dockerfile would rebuild the **bot** service as the MCP image. | `deploy/mcp/Dockerfile`, with `RAILWAY_DOCKERFILE_PATH` set on a **separate** Railway service. |
| 11 | Text index "on `posts`" | `posts` belongs to the pipeline. MongoDB allows only one text index per collection, and has no Ukrainian stemmer. | Create the index with a script, after you say OK: `{summary: "text", tags: "text"}` with `default_language: "none"` (no stemming, so it's the same for UA and RU). I'll tell the teammate it exists. |

## 1. Folder layout

```
brain/
  __init__.py              # empty: the bot imports brain.domain.scoring without pulling pydantic
  config.py                # pydantic-settings Settings; every env var lives here
  db/
    client.py              # motor client from settings (MONGO_URI, MONGO_DB), separate from the bot's db.py
    models.py              # pydantic read models (extra="ignore", safe defaults)
    repositories.py        # PostsRepo, DigestsRepo, SourcesRepo, UsersRepo: the only place that touches Mongo
  domain/
    scoring.py             # MOVED from user_store.py: POINTS, CHOICE_POINTS, tag_key, points_inc, category_order, top_tags
    sensitivity.py         # labels, trust levels, allowed(), filter_items()
    digests.py             # latest digest per side and date, personalised order, RU labelling
    posts.py               # search (text index + ranking), get by id
    profile.py             # categories, goals, top categories and tags (never feedback)
    feedback.py            # validate post_id, build entry, replace previous MCP reaction, apply scoring
    identity.py            # UserContext(user_id, trust, channel, max_sensitivity); resolution rules
  integrations/
    local_models.py        # the ONLY import of the teammate's code (lazy)
    backends.py            # ModelBackend protocol, LocalPipelineBackend, AnthropicBackend, get_backend()
    embeddings.py          # EmbeddingBackend protocol + a stub (phase 5 / when agreed)
  mcp_server/
    server.py              # MCPServer app, instructions, streamable HTTP app, /health
    tools.py               # thin wrappers: resolve user → domain call → typed result → audit
    schemas.py             # typed tool outputs (pydantic)
    auth.py                # TokenVerifier + identity mapping (phase 4); dev user in phase 3
  audit/
    log.py                 # audit_tool_call(), audit_model_call()
  scripts/
    setup_indexes.py       # phase 3: audit_log TTL index (no index on posts)
    link_identity.py       # phase 4
  eval/
    run_questions.py       # questions.jsonl → search_posts → results CSV / JSONL
  requirements.txt
tests/
  conftest.py              # throwaway DB News_test_<uuid> + fixture loader; dropped after the run
  fixtures/*.json          # realistic sample docs (handwritten, no production data)
  test_scoring.py  test_digest_order.py  test_sensitivity.py  test_feedback.py
  test_backends.py  test_identity.py  test_bot_scoring_smoke.py
  test_mcp_tools.py        # starts the server in memory and calls every tool
  contract/test_data_contract.py   # opt-in: validates recent REAL documents (read-only)
deploy/mcp/Dockerfile
docs/  CURRENT_STATE.md  MCP_PLAN.md  DATA_CONTRACT.md  REQUESTS_TO_LOCAL_MODEL_TRACK.md  CONNECT_CLAUDE.md
.env.example
```

**Rules**
- Tools hold no logic: everything is in `domain/`, so ChatGPT, the local chat and the bot can reuse it.
- Only `repositories.py` talks to Mongo.

## 2. Domain details

**`scoring.py`**
- It contains the functions currently in `user_store.py`, with **identical** behaviour: `POINTS`, `CHOICE_POINTS`, `_key` (renamed `tag_key`), `_points_inc` (renamed `points_inc`), `category_scores`, `top_tags` and `category_order`.
- `user_store.py` keeps its old names as aliases. The bot's only change is the import.
- A golden test proves the old and new functions give the same output on fixtures.

**`sensitivity.py`**
- `LEVELS = public < internal < confidential < restricted`.
- A missing `sensitivity` is treated as `internal`.
- `max_for(trust)`: `cloud` → `internal`; `local` → `settings.MAX_SENSITIVITY_LOCAL`.
- Filtering runs in `domain/` on every item before it reaches a tool:
  - **Digests:** a digest item gets the highest label among its source posts and the digest's own label.
  - **Posts:** filtering is in the query (`sensitivity` ∈ allowed or missing) **and** again in Python, as a double check.
- `get_post` returns the same "not found" for a forbidden post as for a missing one, so a forbidden post doesn't reveal it exists.

**`identity.py`**
- `user_id` never comes from tool arguments.
- In dev mode, `DEV_USER_ID` and `trust=local` come from config. Dev mode refuses to start if `PUBLIC_BASE_URL` is non-local.
- With OAuth, `get_access_token()` gives `(issuer, subject)`, which is matched against `users.identities`. No match means a clear error.
- **Trust level comes from the deployment, not the client.** The Railway service is `TRUST_LEVEL=cloud`. The future local machine runs a second instance with `TRUST_LEVEL=local`, reachable only on localhost or the tunnel.
- `channel` comes from the OAuth `client_id` / `client_name` (`Claude` → `claude`), with a config map for ChatGPT and local.

## 3. MCP tools v0

None of the tools call an LLM. All outputs are typed pydantic models.

| Tool | Returns (`schemas.py`) | Behaviour |
|---|---|---|
| `get_digest(side="both", date=None)` | `DigestResult{date, digests[{side, side_note, created_at, sections[{category, label, items[{n, text, source_urls, source_post_ids, is_russian_source_claim}]}]}]}` | Latest `period=day` digest per side, `scheduled` first and any trigger as a fallback. With `date`, the latest one created that Kyiv day. Sections use the bot's order. RU digests carry `side_note: "Claims of Russian sources, not verified facts"`. Never includes feedback. |
| `search_posts(query, side="all", category=None, days=7, limit=10)` | `SearchResult{query, results[{post_id, summary, tags, category, source_side, channel, post_url, published_at, importance, is_russian_source_claim}], note}` | Pattern search over `summary` and `tags` (query words minus stop words, crude stemming), excluding `is_ad`, `is_duplicate` and unprocessed posts, within `published_at ≥ now−days`. Up to 200 candidates are ranked in Python: `0.6·word-match share + 0.25·recency + 0.15·importance/5`. `limit` ≤ 25 and `days` ≤ 30. |
| `get_post(post_id)` | `Post{… + channel{username, country}}` | Invalid id, missing or forbidden → `ToolError("Post not found")`. |
| `get_profile()` | `Profile{categories_today, goals[], top_categories[{side, category, label, score}], top_tags[{tag, score}]}` | Read with a projection; `feedback` is never loaded. |
| `record_feedback(post_id, reaction, text=None)` | `FeedbackResult{post_id, reaction, replaced_previous, score_changes}` | Rejects a null, invalid or unknown post id (and one hidden by sensitivity). Writes the entry described in §0.5 and updates scores through `scoring.py`. |

**Server instructions** (draft; the final English text goes in `server.py`):
- Answer questions about the user's news only from these tools' results, and cite `post_url` / `source_urls`.
- If the tools return nothing relevant, say "I don't have that in the data". Don't guess or fill gaps from general knowledge without saying so.
- Items with `is_russian_source_claim` are claims of Russian channels: write "Russian channels claim…", never present them as fact.
- After a digest, offer short follow-ups: shorter, more detail on item N, show sources.
- Reply in the user's language, Ukrainian by default.
- Use `record_feedback` only when the user clearly rates a post.

Every tool description repeats the parts relevant to it.

## 4. Integrations: the boundary with the local-model track

**How the local model is called today**
- `summarizer.py` builds `client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)` and `MODEL = LLM_MODEL`. That's Ollama, OpenAI-compatible, `qwen2.5:7b-instruct`.
- `digest.py` reuses the same client.
- There is **no general "complete" entry point**. The public functions are task-specific:
  - `analyze_text(text, side, sem)` returns post JSON;
  - `build_digest(period, side)`;
  - `analyst.answer(user_id, question)`, which writes chat history, so the MCP server must not use it.

**`local_models.py`**, written against a minimal assumed interface (assumptions marked in the code):
- `local_model_info() -> {model, base_url}` reads `summarizer.MODEL` and `summarizer.client.base_url`. It doesn't duplicate her env vars.
- `local_chat(messages, max_tokens, temperature) -> ModelResult` calls `summarizer.client.chat.completions.create(model=summarizer.MODEL, …)`.
  - *Assumption:* the runtime stays OpenAI-compatible.
  - If she adds a stable `complete()` entry point, only this function changes.
- `summarizer` is imported **lazily**, inside the functions. Importing it pulls in her `db.py` and `cleaning.py`; the lazy import keeps that out of server start-up and tests.

**`backends.py`**
- `ModelBackend.complete(messages, system=None, max_tokens=1024, temperature=0.2, sensitivity="internal") -> ModelResult{text, input_tokens, output_tokens, model, backend}`.
- `LocalPipelineBackend` is the default and goes through the adapter.
- `AnthropicBackend` uses the official `anthropic` SDK.
  - It **refuses** any payload whose `sensitivity` is above `internal`, raising `SensitivityError` before any network call.
  - Default model `claude-sonnet-5-5`, which can be changed in config.
- `get_backend()` picks one from `MODEL_BACKEND=local|anthropic`.
- Every call goes through `audit_model_call` (backend, model, tokens, highest sensitivity).
- In v0 nothing calls a backend. These exist for `research_topic` and outward verification later (`TODO(next)`).

**`embeddings.py`**
- Her track has **no embeddings** today. `fastembed` is in `requirements-pipeline.txt` but unused.
- Proposal, to agree with her: `fastembed` with a multilingual model that runs on CPU and handles Ukrainian and Russian, such as `intfloat/multilingual-e5-large`, or `paraphrase-multilingual-MiniLM-L12-v2` as a lighter option.
- It sits behind an `EmbeddingBackend` and runs **only locally**; private text is never sent to an external embedding API.
- Phase 2 ships only the interface and a stub.

## 5. Search

**Phase 3:** pattern search with ranking in Python. No index is created on `posts`; the text index was not approved.

**Phase 5 (semantic)**
- The cluster is Atlas 8.0 and `$listSearchIndexes` works, so **Atlas Vector Search** is the simplest option: no new infrastructure.
- It goes behind a `VectorIndex` interface (`upsert(id, vector)`, `query(vector, filter, k)`).
- `AtlasVectorIndex` comes first. Later, on the local machine, a local alternative can be dropped in (for example `sqlite-vec` or Qdrant).
- Where the vectors live is a request to the teammate: either `posts.embedding` written by her pipeline, or an MCP-side collection (which needs your approval).

## 6. Auth and deployment

**Phase 3 (dev)**
- Runs locally on `http://127.0.0.1:8000/mcp` in dev-user mode, tested with MCP Inspector.
- **Only the test database with fixture data.**

**Phase 4 (OAuth)**
- `auth.py` implements the SDK's `TokenVerifier` (verifying JWTs against the authorization server's JWKS) plus `AuthSettings(issuer_url, resource_server_url=<exact public /mcp URL>, required_scopes)`, with `validate_token_resource=True`.
- The SDK then serves `/.well-known/oauth-protected-resource/mcp` and returns 401 with `WWW-Authenticate: Bearer resource_metadata=…`, which Claude requires.
- Authorization server options. Claude needs DCR or CIMD, S256, refresh rotation and both callbacks.
  - **A.** A hosted identity provider with DCR or CIMD: fastest on Railway.
  - **B.** Self-hosted **Keycloak**, which supports DCR, on the always-on machine. The most private, and it fits the long-term goal.
  - **C.** Our own authorization server. Not recommended: too much security-critical code.
  - Decision needed in phase 4.
- **Not deployed publicly with real data until OAuth works.**

**Docker and hosting**
- `deploy/mcp/Dockerfile`: `python:3.12-slim`, non-root user, installs `brain/requirements.txt`, runs `uvicorn brain.mcp_server.server:app --host 0.0.0.0 --port $PORT --proxy-headers`.
- `TransportSecuritySettings(allowed_hosts=[PUBLIC_HOST])` comes from env.
- `/health` is a custom route with no data.
- The code assumes neither Railway nor the tunnel. All of it comes from env (`PUBLIC_BASE_URL`, `PORT`, `TRUST_LEVEL`).
- Notes for both targets go in `docs/CONNECT_CLAUDE.md`: Railway as a second service, and later the local machine behind a Cloudflare Tunnel or Tailscale Funnel.

**Secrets**
- Env only. `.env.example` lists names with placeholder values.
- Anthropic's egress range (`160.79.104.0/21`) is documented for anyone who wants an allowlist.

## 7. Configuration (`brain/config.py`)

| Variable | Default | What it sets |
|---|---|---|
| `MONGO_URI` | required | Database connection |
| `MONGO_DB` | `News` | Database name |
| `TRUST_LEVEL` | `cloud` | Trust level of this deployment (`cloud` \| `local`) |
| `DEV_USER_ID` | – | Dev-user mode when set and OAuth is off |
| `MAX_SENSITIVITY_LOCAL` | `restricted` | Highest label a local connection may receive |
| `PUBLIC_BASE_URL` | – | Public URL of the server |
| `ALLOWED_HOSTS` | – | Hosts accepted by transport security |
| `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, `OAUTH_REQUIRED_SCOPES` | – | Token verification (phase 4) |
| `MODEL_BACKEND` | `local` | `local` \| `anthropic` |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | – | Anthropic fallback backend |
| `AUDIT_TTL_DAYS` | `180` | How long audit entries are kept |
| `LOG_LEVEL` | – | Logging level |

## 8. Audit

**Where entries go:** `audit_log` (new collection, needs your OK) and also the standard log. Railway logs alone have short retention.

**Entry shape:** `{at, kind: "tool"|"model", user_id, trust, channel, tool, args (redacted: free text kept, never tokens), items_returned, max_sensitivity_returned, duration_ms, error}`.

**Model calls** add `{backend, model, input_tokens, output_tokens, max_sensitivity_sent}`.

**Tested:** a `cloud` backend given a `confidential` payload raises `SensitivityError` and logs `refused`.

## 9. Tests and evaluation

**Test database**
- `pytest` + `pytest-asyncio`.
- The tests create a **throwaway database `News_test_<uuid>`** on `TEST_MONGO_URI` (default: the same cluster as `MONGO_URI`) and drop it afterwards.
- A real MongoDB is needed because `$text` can't be faked.
- Fixtures are handwritten sample documents in the real shapes, including string `published_at`, RU and UA posts, and labelled posts.

**Unit tests**
- scoring (golden comparison with the old `user_store` functions)
- digest order
- sensitivity (*a cloud request never returns `confidential`*)
- feedback validation and replacement
- the backend switch and its refusal rule
- user resolution

**Bot smoke test:** `user_store.set_reaction`, `add_category_choice` and `category_order` run against the test database after the import change, with the same results as before.

**Server test:** an in-memory MCP client lists the tools and calls each one, checking structured content.

**Data contract test** (`tests/contract/`)
- Opt-in with `CONTRACT_MONGO_URI`; **read-only**.
- It reads the latest 200 posts and 10 digests, plus the sources and users they reference, and validates them against strict contract models generated from `DATA_CONTRACT.md`.
- It reports every missing or renamed field with a count. It doesn't stop at the first one.

**Eval:** `brain/eval/run_questions.py` runs every `questions.jsonl` question through `search_posts` (using `window_days`) and writes `results/<date>.jsonl` and a CSV with the top 10 hits per question for manual "relevant y/n" marking. The marked file becomes the baseline. Later the same questions run through each `ModelBackend` for comparison.

## 10. Dependencies (`brain/requirements.txt`; the bot's `requirements.txt` stays unchanged)

`mcp>=2.3,<3` (pulls in starlette, uvicorn, pydantic, pyjwt, httpx), `pydantic-settings`, `motor`, `pymongo`, `python-dotenv`, `anthropic` (phase 2), `openai` (already used by the teammate's client, needed by the adapter at call time).

Dev: `pytest`, `pytest-asyncio`.

`fastembed` comes later, local only, after agreement with the teammate.

## 11. Phases

1. **Read and plan** (this): `CURRENT_STATE.md`, this plan, `REQUESTS_TO_LOCAL_MODEL_TRACK.md`.
2. **Foundation:**
   - `brain/` with config, db, repositories, models;
   - `scoring.py`, with the bot switched to it and the smoke test passing;
   - `DATA_CONTRACT.md` and the contract test;
   - `sensitivity.py`, `identity.py`, `local_models.py`, `backends.py` and `embeddings.py` (stub), with tests (her code mocked);
   - `.env.example`.
3. **MCP server v0:** the five tools, instructions, audit (with a setup script for the `audit_log` indexes), tests, and a local run checked with MCP Inspector.
4. **Deploy and connect:** Dockerfile, a Railway service, OAuth (after the authorization server decision), `link_identity.py`, `CONNECT_CLAUDE.md` with 10 test questions.
5. **Local path:** the `LocalPipelineBackend` connected for real (with the teammate), the local-chat connection doc, embeddings and vector search if agreed, and the eval comparing local and Anthropic.

`TODO(next)` notes go where the out-of-scope items plug in: `research_topic`, the outward-verification gate, tag normalisation and de-duplicating digest items.
