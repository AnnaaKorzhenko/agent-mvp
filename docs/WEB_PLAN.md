# Phase 5: our own UI and chat (the agent core)

Plan written 2026-11-02. Work on the `ui-migration` branch.

**Decisions (the user, 2026-11-02):**
- Her frontend: **edit in place**; her backend is retired.
- New dependencies (FastAPI, authlib, sse-starlette): **OK**.
- Chat model: **Sonnet 5.5** by default.
- «Агенти» page: **only us** for now.
- Push channel: **Web Push**.
- The Claude connector: **stays** until the web is very good and reliable; maybe dropped then, not soon.

## Step 5.0 as built (2026-11-02, commit «Tools split»)

- **`brain/tools/`** holds the tools independent of MCP:
  - `base.py`: `Services`, `Origin` (session id, client, whether it draws cards; asked once, only by the tools
    with cards) and `Call` (was `_Call`: resolve the user, remember the signals, log to `conversations`).
  - `handlers.py`: one handler per tool, `(call, **its own args)`, and `run(svc, name, origin, args)`.
  - `catalog.py`: `build(svc)` gives each tool's `ToolSpec` (name, description, annotations, pydantic argument
    model, JSON schema without `user_message` / `signals`); `call(svc, spec, args, user_message, origin)`
    validates like MCP and runs the handler.
- **`brain/mcp_server/tools.py`** (1,054 → ~490 lines) keeps only the signatures and docstrings, the one place
  they're written. Each body is `_run(svc, name, mcp_ctx, locals())`; `register_tools` returns the specs.
- **Unchanged for Claude:** the 19 tools' MCP definitions (`list_tools`) are byte-identical to before. The 263
  earlier tests pass as they were; `tests/test_tools_core.py` adds 3.

## Step 5.1 as built (2026-11-02, deployed 2026-11-02: https://second-brain-b8at.onrender.com)

- **`brain/web/`**: `python -m brain.web` (port 8001 locally, `$PORT` on Render).
  - `app.py`: FastAPI. `/login` → Auth0 → `/auth/callback` → session cookie; `/logout`; `/api/health`, `/api/me`,
    `/api/digest?side=`, `/api/posts?q=&side=&days=`, `/api/posts/{id}`, `/api/profile`; `/api/chat` answers 503
    until 5.2. Everything else: the UI's files from `data-digest-web/frontend`.
  - **Every read runs a tool handler** (`brain/tools/catalog.call`) as the signed-in user, so it is filtered by
    sensitivity and logged to `conversations` like Claude's calls: channel `web`, session `web:<login id>`,
    page views as button presses (`via: widget`), a search query as the user's words (`via: search_query`).
  - `auth.py`: the Auth0 code flow by hand (httpx2 + PyJWT, already installed; no authlib, which would add
    httpx). The ID token is checked: issuer, audience = our client id, expiry, nonce. Tokens aren't kept.
  - `session.py`: the session is a signed (HMAC-SHA256), httpOnly, Secure, SameSite=Lax cookie holding only who
    the user is (issuer, subject, email, name, picture, login id), for `WEB_SESSION_DAYS`.
  - **Guards:** writes to `/api/*` need the header `X-Brain: 1`; `nosniff`, `X-Frame-Options: DENY`, the API is
    never cached, and the UI's files are revalidated on every load. A login is checked against `ALLOWED_EMAILS`
    and audited (`web_login`, `auth_failed`, `access_denied`).
  - **The same person as in Claude:** the same Auth0 tenant gives the same `sub`, so the login finds the users
    record Claude's connector created (`identities`); a first-time person gets a new one (`AUTO_CREATE_USERS`).
  - Dev mode: with `DEV_USER_ID` and no `OAUTH_ISSUER`, no login; local URL only.
- **The UI** (`data-digest-web/frontend`, edited in place):
  - `api.js` calls our API; a 401 sends the browser to `/login`.
  - Дайджести: Усе / Українські / Російські / Ваші теми й інтереси, with «Для вас» first, why each item matters,
    Russian claims marked, source links. Section emoji are stripped (the director's laptop shows grey boxes).
  - Пошук: query, side, period; results with channel, date, why, and a link to the post.
  - Профіль: goals, followed topics, what the system learned (read-only until 5.3).
  - The user block shows the signed-in name, with «Вийти».
  - The chat screen is hers as it was until 5.2.
- **Settings:** `WEB_CLIENT_ID`, `WEB_CLIENT_SECRET`, `WEB_SESSION_SECRET`, `WEB_SESSION_DAYS`, `WEB_FRONTEND_DIR`;
  `CHANNEL_BY_CLIENT` gains `web`. **Dependency:** `fastapi` (+ `annotated-doc`), nothing else changed.
- **Tests:** `tests/test_web.py` (6): the login with a local key in place of the tenant (state, nonce, audience
  refused), the same users record as Claude's, reads logged as `web`, `ALLOWED_EMAILS`, dev mode, the guards,
  the signed cookie. All 272 tests pass.
- **Not done here:** her `data-digest-web/backend` is still in the repo (unused); remove it when she agrees.

### To deploy 5.1 (done; kept for redeploys)

1. **Auth0** → Applications → Create → *Regular Web Application* («News brain web»):
   - Allowed Callback URLs: `https://<web-url>/auth/callback`
   - Allowed Logout URLs: `https://<web-url>/`
   - Allowed Web Origins: `https://<web-url>`
   - Its Connections: the same ones as Claude's connector app (e.g. Google), so the director's account is the same.
2. **Render** → New → Web Service, the same repo, branch `ui-migration`:
   - Build: `pip install -r brain/requirements.txt`; Start: `python -m brain.web --host 0.0.0.0`
   - Health Check Path: `/api/health` (not the MCP service's `/health`)
   - Env: `MONGO_URI`, `MONGO_DB=News`, `TRUST_LEVEL=cloud`, `OAUTH_ISSUER` (as on the MCP service),
     `PUBLIC_BASE_URL=https://<web-url>`, `WEB_CLIENT_ID`, `WEB_CLIENT_SECRET`, `WEB_SESSION_SECRET` (new random),
     `ALLOWED_EMAILS` (as on the MCP service), `FORWARDED_ALLOW_IPS=*`. No `DEV_USER_ID`, no agents' settings.
3. Atlas → Network Access: allow the new service.
4. The login asks for `audience=<issuer>userinfo`: otherwise the tenant's Default Audience (the MCP API) applies
   and Auth0 refuses this app («not authorized to access resource server»). No API access is granted to it.
5. Open `https://<web-url>`, sign in with the same Google account as in Claude: the digest should be his.

## Step 5.2 as built (2026-11-02, not deployed)

- **`brain/web/chat.py`:** the chat loop, by hand (§1.3).
  - One message is one turn: the earlier messages of the chat come back from `conversations` (the last
    `WEB_CHAT_HISTORY`, text only), the user's message is logged, then up to `WEB_CHAT_MAX_ROUNDS` model rounds.
  - Each round streams through `AnthropicBackend.stream` (`brain/integrations/backends.py`): the sensitivity gate
    first, one `model_call` in audit_log per round (also when the user stops it: outcome `cancelled`).
    Sonnet 5.5 (`WEB_CHAT_MODEL`), adaptive thinking, `fallbacks: "default"`, top-level prompt caching (the system
    prompt and the 19 tools, ~17.5k tokens, are read from the cache after the first round).
  - Tools: every catalog tool except `log_turn`, run in-process through `catalog.call` with the user's own token
    (`eager_input_streaming`; inputs validated by the tools' pydantic models, a bad input goes back to the model as
    an error), plus Anthropic's `web_search` for deep dives (`WEB_CHAT_WEB_SEARCH`).
  - The system prompt: the MCP instructions rewritten for a UI (markdown links as sources, no emoji, no
    user_message / signals / widget rules), today's date, the user's name and `about_user`.
- **Logs (`brain/audit/conversations.py`):** a chat is the session `web:chat:<id>`.
  - `Said`: the user's message as typed (`via: chat`), and the whole reply (`role: assistant`,
    `written_by: model`) with `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `rounds`, `tools`,
    `web_searches`, `latency_ms`, `stop_reason` (`end_turn`, `max_rounds`, `refusal`, `cancelled`, `error`).
  - The turn's tool calls are logged as before and link to the user's message (no second copy).
  - `thread`, `sessions` and `assistant_tokens` read them back: the thread, the sidebar's list, the daily budget
    (`WEB_CHAT_TOKENS_PER_DAY` per user and UTC day; cache reads aren't counted).
- **API:** `POST /api/chats` (an id; nothing stored until the first message), `GET /api/chats`,
  `GET /api/chats/{id}`, `POST /api/chats/{id}/messages` (server-sent events: `text`, `tool`, `done`, `error`).
  Without `ANTHROPIC_API_KEY` the chat endpoints answer 503.
- **UI:** the answer streams in with a tool status line («Шукаю в постах…»), a stop button, safe markdown with
  links; «Нещодавні» lists the real chats and opens them.
- **Checked:** 6 chat tests (scripted fake Claude) and the whole suite pass; one real round confirmed the request
  shape; in the browser against a throwaway copy: a search answered with a link to the post, a follow-up used the
  thread, stop left a `cancelled` reply, a chat reopened from the sidebar.
- **To deploy:** on the web service add `ANTHROPIC_API_KEY` (and the `WEB_CHAT_*` settings only to change the
  defaults). Push, and Render redeploys.
- **Not yet:**
  - signals: the learner doesn't read web chat text yet; the model saves what the user states through
    save_insight / set_goal as the prompt says;
  - long threads: only the last `WEB_CHAT_HISTORY` messages are sent, no summary of the earlier ones;
  - deleting a chat (soft delete, then a purge: ask before building the purge);
  - a round stopped mid-stream logs 0 tokens (its usage never arrives), so the budget undercounts it slightly;
  - earlier turns are sent back as text only, so a follow-up about a previous result may search again.

## Step 5.3 as built (2026-11-03, not deployed)

Every button runs one brain tool as the signed-in user (`tool()` in `brain/web/app.py`), exactly as the MCP cards'
buttons do, and is logged as a button press (`[widget] …`, `via: widget`); typed reasons and comments are logged
as the user's words.

| UI | Endpoint | Tool |
|---|---|---|
| «Пропозиції» screen (badge with the open count): «Так, …» / «Ні» with an optional reason | `GET /api/proposals`, `POST /api/proposals/{id}` | `get_proposals`, `answer_proposal` (a yes whose action is `chat` opens a new chat with its request) |
| Profile: add / remove a goal | `POST /api/goals` | `set_goal` |
| Profile: stop a followed topic, renew an expired one for 30 days | `POST /api/topics` | `track_topic` |
| Profile: «Це не про мене» on inferred interests and intents | `POST /api/profile/not-interested` | `correct_profile` (followed topics that match are named, not stopped) |
| Profile: suggestion settings (cards per digest 0-3, kinds, mentions in chat) | `POST /api/prefs`; read in `GET /api/profile` (`agent_prefs`) | `set_agent_prefs` |
| «Корисно» / «Не корисно» on digest items and search results, then an optional «Чому?» | `POST /api/feedback` | `record_feedback` (the comment is added to that rating) |
| The same on the analyst's replies | `POST /api/chats/{id}/messages/{mid}/rating` | `record_feedback` (`answer`, about = the question); the rating is also put on the reply in `conversations` (`rating`), a comment goes to the same feedback |

- **Checked:** 4 tests (`tests/test_web_actions.py`) and the whole suite; in the browser on a throwaway copy: «Так»
  on a topic card followed it with no chat (the step's "done when"), «Ні» with a reason, goal added, topic
  stopped, advice turned off, a digest item rated and commented, a reply rated and the rating still there after
  reopening the chat.
- **To deploy:** nothing new to set; push.
- **Not yet:** ratings already given aren't shown on digest items and posts after a reload (only on replies).

## Chat token optimisation (2026-11-03, not deployed)

Measured on a real «Запитати аналітика» deep dive: 4 rounds, 44.6k input, 4.6k output, 184k cache reads, 4 web
searches (about $0.21). Every round resends a fixed prefix of 17.8k tokens: the tools 10.7k, Anthropic's web search
tool 6.1k, the system prompt 1k.

- **Schemas compacted** (`compact` in `brain/web/chat.py`): pydantic's titles, `anyOf [X, null]` and `default:
  null` dropped; argument names are never touched. The descriptions written for Claude's widget cards have a web
  version (`WEB_DESCRIPTIONS`, 5 tools; a test checks each names a real tool). Argument descriptions stay the MCP ones.
- **web_research** replaces the web search tool: `WEB_CHAT_RESEARCH_MODEL` (Haiku 5.5) runs up to 5 searches
  through `AnthropicBackend.search` and returns a brief of at most ~250 words plus the pages found; a link not
  among the search results loses its URL. Logged as a `web_research` tool call; its tokens are on the reply
  (`research_input_tokens`, `research_output_tokens`) and count toward the daily budget.
- **Shared 1-hour cache**: the tools and the static system prompt sit behind one `ttl: 1h` breakpoint, shared by
  every user and chat; the per-user part (date, name, about_user) and the turn are cached for 5 minutes.
- **Two buttons on digest items**: «Пояснити» (from the news base, no web) and «Глибше, з інтернетом» (the deep
  dive). Both start a new chat and send the item's ids (`item` on the message: not shown, kept in the log), so
  deep_dive finds the item on the first call.

Result: the fixed prefix is 9.4k (from 17.8k). The same kind of deep dive on sample data: 15.6k input, 2.5k output,
32k cache reads on Sonnet, plus 49k input on Haiku at a twentieth of the price (about $0.14, of which $0.05 is the
5 searches). «Пояснити»: 2 rounds, 1.2k input with the prefix cached (about $0.01).

## 0. What exists

- **`data-digest-web/`** (the teammate's UI):
  - `frontend/`: vanilla JS, no build step. Four screens (Аналітик, Дайджести, Пошук по постах, Профіль та
    інтереси), with all data going through `frontend/js/api.js` (5 calls). It has a mock mode.
  - `backend/`: FastAPI + motor + OpenAI client. Its own guesses at the data:
    - a `stories` collection that `News` doesn't have;
    - `users.user_id` / `interests` / `schedule` / `sides` fields that the brain doesn't use;
    - one shared HTTP Basic login and one `WEB_USER_ID` for everyone;
    - a chat that greps 14 days of posts by word stems and asks one OpenAI call;
    - no conversation history.
  - **Its `PUT /api/profile` upserts into `users`**: against `News` it would create a stray user document.
    Don't deploy that backend against production.
- **The brain** (`brain/`): everything the UI needs already exists as domain functions behind the MCP tools:
  - digests with «Для вас», search, posts, profile, goals, topics, insights, feedback, proposals, corrections,
    agent prefs, deep dives, analysis;
  - Auth0 tokens and the `identities` link;
  - `conversations` logging; model backends with the sensitivity gate and audit;
  - the agents with their `agent_runs` queue (leases + unique indexes, so two workers are safe).
- **What we don't control today:** in Claude, the server sees only tool calls. It never sees the user's messages
  or Claude's replies, it can't push anything, and buttons can only paste text into the message box.

## 1. Decisions (recommended)

### 1.1 Frontend: keep hers, replace her backend with the brain's

- Keep `data-digest-web/frontend` and its look. Change `api.js` to call the brain's web API, and add the screens
  and pieces it lacks (§4).
- Retire `data-digest-web/backend`. Every endpoint it has is a weaker copy of a brain tool; keeping two data
  layers over the same `News` would drift.
- The new backend lives in **`brain/web/`**, next to `brain/mcp_server/`, and imports the same `brain.domain`.
- **Ask her first** (her files): whether we edit `data-digest-web/frontend` in place, or she moves it to a new
  home (e.g. `web/`) and keeps working on the design there while we wire the data.

### 1.2 Hosting: a separate Render web service, same repo, same Atlas

| | MCP service (as now) | Web service (new) |
|---|---|---|
| Start | `python -m brain.mcp_server --host 0.0.0.0` | `python -m brain.web --host 0.0.0.0` |
| Serves | Claude's connector (`/mcp`) | the UI (static files) and `/api/*`, with streaming chat |
| Auth | Auth0 access tokens (resource server) | Auth0 login → httpOnly session cookie |
| Agents worker | runs here (as now) | off at first (`AGENTS_WORKER=false`) |

- **Why separate:**
  - a web deploy doesn't restart the MCP server and drop Claude's stateful sessions;
  - each scales on its own;
  - each auth setup stays simple;
  - one breaking doesn't take down the other.
- **Same database:** a user is the same user in Claude and on the web (the same `users` document via
  `identities`), with the same goals, topics, cards and digests.
- **The agents worker** stays in the MCP service until the web is in use. Then it moves to a third, tiny Render
  Background Worker (`python -m brain.agents.worker`), so neither web service carries it. The queue is already
  safe with more than one worker.
- **Region:** the same as the Atlas cluster. Allow the new service's outbound IPs in Atlas Network Access.
- **Domain:** e.g. `app.<ours>`, with Auth0's callback / logout URLs set to it.
- **Cost:** one more Starter instance (~$7/month), plus another later for the worker.

### 1.3 LangGraph / LangChain: neither. Build it by hand, in this repo

What the chat needs is one tool-use loop:

1. send the conversation, the system prompt and the brain's tools to Claude;
2. run the tool calls it asks for;
3. send the results back;
4. repeat until it answers;
5. stream it out.

With the Anthropic SDK (already a dependency) that's ~150-250 lines we own. The rest, which these frameworks
sell, we already have and in a stricter form:

| What LangGraph / LangChain offer | What we have |
|---|---|
| state / checkpoints | `conversations` in Mongo (we choose the shape, it's queryable, it's the training/eval data) |
| background agents, retries, schedules | `brain/agents/runtime.py` + `agent_runs` (leases, budgets per user and day, run log) |
| human-in-the-loop interrupts | proposal cards with direct buttons |
| provider abstraction | `brain/integrations/backends.py`, which also enforces the sensitivity gate (cloud ≤ `internal`) and audits each call. A framework's own model wrappers would go around it. |
| tool definitions | the MCP tools' pydantic schemas (reused, §2) |

- **Costs of a framework:** a large dependency tree, another abstraction to debug, and less control over the
  exact payload (what the user asked for in "complete control").
- **When to reconsider LangGraph:** if we later need long, branching multi-step agent workflows that pause for
  days and resume mid-graph. Even then we'd try it on one agent first.
- **Not Managed Agents / Agent SDK either:**
  - Managed Agents runs the loop on Anthropic's side, which gives up control of the logs and payloads;
  - the Agent SDK is built for agents with a file system and a shell.

## 2. One set of tools for Claude and for our chat

Today each tool's logic is wired into `brain/mcp_server/tools.py` (`register_tools`, `_Call`).

- Refactor into a **tool registry**: each tool is
  `{name, description, input model (pydantic), handler(svc, ctx, args) -> result model}`.
  - `brain/mcp_server` registers every entry as an MCP tool (no behaviour change; the MCP tests must pass as
    they are).
  - `brain/web/chat.py` turns the same entries into Anthropic tool definitions
    (`input_schema = Model.model_json_schema()`) and calls the handlers in-process. No MCP hop.
- **`user_id` comes from the session**, never from the model or the request body (as now).
- **Tools that only make sense in Claude** (`log_turn`'s "tell me what the user said" workaround) aren't offered
  to our chat: our chat sees the messages itself and records them directly.
- **The server instructions** (`INSTRUCTIONS` in `server.py`) become the base of the chat's system prompt, with
  the parts about Claude's limits (pasting into the message box, one mention a day) rewritten for a UI that has
  real buttons.

## 3. The chat (`brain/web/chat.py`)

### 3.1 API

- **`POST /api/chats`** creates a conversation and returns its id.
- **`POST /api/chats/{id}/messages`** with `{content}` streams Server-Sent Events:
  - `text` (deltas);
  - `tool` (name and status, for "Шукаю в постах…");
  - `cards` (digest items, posts, proposals), which the UI draws as components, like today's widgets;
  - `done` (tokens, conversation id).
- **`GET /api/chats`** returns the sidebar's «Нещодавні»: titles and dates, newest first.
- **`GET /api/chats/{id}`** returns the whole thread.

### 3.2 The loop

- **Model:** `WEB_CHAT_MODEL`, default `claude-sonnet-5-5` (quality matters most here). Adaptive thinking, and
  streaming.
- **Calls go through `backends.py`:** add a `stream_with_tools` method there, so every chat call is gated by
  sensitivity and audited like the agents' calls.
- **Tools:** the registry (§2).
- **Limits:**
  - at most 8 tool rounds per message;
  - a token budget per user and day (`WEB_CHAT_TOKENS_PER_DAY`), counted with the agents' usage;
  - a stop button that cancels the stream.
- **Prompt caching** on the system prompt and the tool list (the same for every turn).
- **Long threads:** send the last N turns in full plus a short summary of the earlier ones, kept on the
  conversation.
- **Learning:** the learner gets the real user messages now, not Claude's paraphrase in `log_turn`.
  - The same `nudge()` after a turn with a goal / interest signal.
  - Signals are extracted by the learner from the text, not by the chat model calling `log_turn`.

### 3.3 Conversation logs (full control)

- The same `conversations` collection, `channel: "web"`, one document per chat (`session_key: "web:<chat id>"`).
- What it now stores:
  - the user's messages and the assistant's replies in full;
  - each tool call with its args and result references (as now);
  - the model, tokens, latency and stop reason per reply;
  - 👍 / 👎 on a reply (text buttons, §4).
- **Size:** the current cap (`conversation_max_messages`) starts a new part; the sidebar shows one chat.
- **Privacy:**
  - the user can delete a chat (soft delete, then a purge job; ask before building the purge);
  - logs are never sent to a cloud model above `internal`;
  - `users.feedback` stays out of reads (as now).
- **What this unlocks:**
  - real evals: replay logged questions against prompt and model changes (`brain/eval`);
  - the item 9 self-report;
  - knowing which answers were bad.

## 4. The UI, screen by screen

| Screen | Now | Wire it to |
|---|---|---|
| Аналітик (chat) | one OpenAI call over grepped posts | §3, streaming, with sources as links to posts; the sidebar's «Нещодавні» from `GET /api/chats` |
| Дайджести | the `stories` collection (missing), so it falls back to raw posts | `get_digest` / `get_morning_digest`: the daily digest, «Для вас», «Ваші теми й інтереси», with feedback buttons on each item |
| Пошук по постах | a regex over posts | `search_posts` (with its relevance order); a post opens via `get_post` |
| Профіль та інтереси | 8 fixed chips, saved into fields nothing reads | the real profile: goals (add / close), tracked topics (follow / stop), learned interests with «це не про мене» (`correct_profile`), and agent prefs (how many cards, which kinds, mentions) |
| **Пропозиції** (new) | — | the agents' cards with **direct buttons**: «Так» runs the action at once (`answer_proposal`), «Ні» opens an optional reason box. No pasting into a chat. |
| **Агенти** (new, admin only) | — | `agents_report` as a page: runs per agent, tokens, failures, accept rates per kind; pause or run an agent now; budgets. `users.role: "admin"` gates it. |

- **Header:** the user's name and avatar from Auth0, not the hard-coded «Катерина».
- **Buttons are text** (the director's laptop doesn't show emoji).

## 5. Login

- **Auth0, the same tenant**, with a new *Regular Web Application* (authorization code flow).
  - The backend keeps the tokens.
  - The browser gets only an httpOnly, Secure, SameSite=Lax session cookie.
  - Sessions live in Mongo or a signed cookie; that's a choice inside the plan, not a new collection decision yet.
- The Auth0 `sub` resolves through the same `identities` link as the MCP server. The director logs in with the
  same account he uses for Claude and sees the same profile.
- **Every `/api/*` needs the session**, except `/api/health`. CSRF: a header token on POST/PUT/DELETE.
- **Dependencies:** FastAPI (or plain Starlette, already pulled in by `mcp`), `sse-starlette`, `authlib` for the
  OIDC flow. All small and common. FastAPI and authlib are new: confirm.

## 6. Push delivery (after the chat works)

1. **In-app first:** a badge on «Пропозиції» and the morning digest at the top of «Дайджести», from what the
   agents and the pipeline already write.
2. **Then one push channel**, the user's choice:
   - **Web Push:** works on the laptop and on the phone if the site is installed as an app. No third party.
     Needs a service worker and VAPID keys.
   - **Email:** the digest each morning. Needs a sender (Postmark / SES).
   - **The Telegram bot** already reaches people, but it's her code (`main.py`). We'd hand her an
     endpoint to call, not edit the bot.

## 7. Steps

| Step | What | Done when |
|---|---|---|
| 5.0 ✓ | Tool registry refactor (§2) | MCP tests pass unchanged; one handler per tool |
| 5.1 ✓ | `brain/web` skeleton: Auth0 login, `/api/me`, `/api/health`, static frontend; read endpoints for digest, search, post, profile; `api.js` switched | the director logs in and sees his own digest and «Для вас» |
| 5.2 ✓ | Chat: the loop, streaming, `conversations` with full messages, the sidebar's chat list | a question with a tool call answers with sources; the thread is in Mongo |
| 5.3 ✓ | Direct actions: «Пропозиції», profile editing, feedback buttons on items and replies | «Так» on a card follows the topic without a chat |
| 5.4 | Agents console (admin) | runs, accept rates and budgets visible; an agent can be paused |
| 5.5 | Deploy the web service on Render (§1.2); move the worker out when traffic starts | both services up; the MCP service unaffected by web deploys |
| 5.6 | Push (§6) and evals from the logs | the morning digest reaches the user without them opening anything |

5.0-5.2 is the core; 5.3 is what makes it better than the Claude version.

## 8. Open questions

All answered 2026-11-02 (see the top).
