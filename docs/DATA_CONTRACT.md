# Data contract: what the MCP server reads and writes

This is the agreement between the **local-model track** (pipeline: writes `posts`, `digests`, `sources`) and the **MCP track** (reads them; reads and writes parts of `users`).

`tests/contract/test_data_contract.py` **parses the tables below** and checks real recent documents against them. Changing a row here changes the test.

How to read the tables:
- **Field:** dotted path. `[]` means "every element of this list".
- **Type:**
  - `str`, `int`, `float`, `bool`, `datetime`, `objectid`, `list`, `dict`, `any`;
  - `a\|b` means either type is allowed (escaped, because `|` separates the table's columns);
  - a trailing `?` means `null` is also allowed.
- **Required:**
  - `yes`: missing on any checked document is reported as a contract break.
  - `no`: optional; the server falls back to a default.
- **Access:** `R` read by the MCP server, `W` written by it, `RW` both.

Notes:
- Fields not listed are ignored: the server's read models drop unknown fields.
- Date strings are ISO-8601 with an offset, e.g. `2026-10-26T08:15:00+00:00`, and are compared as strings. See request #3 in `REQUESTS_TO_LOCAL_MODEL_TRACK.md`.

## posts

Written by the pipeline. The MCP server **never writes** posts.

| Field | Type | Required | Access | Meaning |
|---|---|---|---|---|
| `_id` | objectid | yes | R | Post id; `post_id` in MCP tools is this, as a hex string |
| `post_url` | str | yes | R | Link to the original Telegram post; what answers cite |
| `channel_id` | objectid | yes | R | → `sources._id` |
| `published_at` | str | yes | R | When the channel published it (ISO-8601 string) |
| `processed` | bool | yes | R | `true` once summarised; unprocessed posts are never returned |
| `summary` | str | yes | R | The model summary; the only text the server returns |
| `source_side` | str | yes | R | `ua` or `ru`. `ru` items are claims of Russian sources |
| `category` | str | yes | R | Category key (`front`, `defense_tech`, … / `ru_front`, …) |
| `tags` | list | yes | R | Topic tags (strings) |
| `importance` | int | yes | R | 1–5 |
| `is_ad` | bool | yes | R | Ads are excluded |
| `views` | int? | no | R | Telegram views |
| `is_duplicate` | bool | no | R | Excluded when `true` (not written yet) |
| `text_removed` | bool | no | R | Raw text was deleted by retention; informational |
| `sensitivity` | str | no | R | `public` / `internal` / `confidential` / `restricted`; missing = `internal` |

## sources

| Field | Type | Required | Access | Meaning |
|---|---|---|---|---|
| `_id` | objectid | yes | R | Channel id |
| `username` | str | yes | R | Telegram username, shown as the channel name |
| `platform` | str | yes | R | Currently always `telegram` |
| `source_country` | str | yes | R | `UA` or `RU` |
| `category` | str? | no | R | Optional channel category (often `null`) |

## digests

Shared, not per user.

| Field | Type | Required | Access | Meaning |
|---|---|---|---|---|
| `_id` | objectid | yes | R | Digest id |
| `period` | str | yes | R | `day` or `week` (v0 reads `day`) |
| `side` | str | yes | R | `ua` or `ru` |
| `trigger` | str | yes | R | `scheduled` (preferred) or `on_demand` |
| `created_at` | datetime | yes | R | Build time; used to pick "latest" and "on date" (Kyiv) |
| `period_start` | str | no | R | Period start (ISO string) |
| `period_end` | str | no | R | Period end (ISO string) |
| `sections` | list | yes | R | Ordered sections |
| `sections[].category` | str | yes | R | Category key |
| `sections[].title` | str | no | R | Section title |
| `sections[].items` | list | yes | R | Items of the section |
| `sections[].items[].text` | str | yes | R | Item text |
| `sections[].items[].source_urls` | list | yes | R | Links of the source posts |
| `sections[].items[].source_post_ids` | list | yes | R | → `posts._id` (may point to posts already deleted by retention) |
| `sections[].items[].n` | int | no | R | Item number in the digest |
| `sensitivity` | str | no | R | As for posts; missing = `internal` |

## users

Shared by the bot and the MCP server.
- The MCP server reads a **projection** (never `feedback` in reads).
- It writes feedback entries and scores.
- It **creates new records for people who sign in through OAuth** (Claude). Telegram users are not changed or linked.

Such a record is
`{_id: ObjectId, source: "claude", email, created_at, last_seen, identities: [{provider: <issuer>, subject: <sub>, created_at}], onboarded: false, daily_push: false, interests: {category_scores: {ua: {}, ru: {}}, tag_scores: {}}, goals: [], feedback: []}`.
The bot ignores these records: they have no Telegram id, and `onboarded` and `daily_push` are false.

| Field | Type | Required | Access | Meaning |
|---|---|---|---|---|
| `_id` | int\|objectid | yes | RW | Telegram user id (int) for bot users; ObjectId for users created for an OAuth login |
| `source` | str | no | W | **New:** `claude` / `chatgpt` / … on records the MCP server created |
| `email` | str? | no | W | **New:** sign-in email from the Auth0 Action, on records the MCP server created |
| `interests.category_scores` | dict | no | RW | `{ua: {category: score}, ru: {…}}`, updated through `brain/domain/scoring.py` |
| `interests.tag_scores` | dict | no | RW | `{tag: score}`, updated through `brain/domain/scoring.py` |
| `daily.date` | str | no | R | Kyiv date of the morning picker (`YYYY-MM-DD`) |
| `daily.selected` | dict | no | R | Today's chosen categories `{ua: [], ru: []}`; ordering uses them only if `daily.date` is today |
| `goals` | list | no | RW | Goals, `$push`ed by `set_goal` in the bot's shape (max 5 active, no duplicates) |
| `goals[].text` | str | yes | RW | Goal text, one line, at most 300 characters |
| `goals[].active` | bool | yes | RW | Only active goals are returned; removing a goal sets `false` (history stays) |
| `goals[].source` | str | no | W | `user`, as the bot writes it |
| `goals[].created_at` | datetime | no | W | When the goal was added |
| `goals[].removed_at` | datetime | no | W | When the goal was removed |
| `goals[].channel` | str | no | W | **New:** the MCP client that wrote the goal. Missing = written by the bot |
| `feedback` | list | no | W | Reaction log, `$push`ed. Read only to replace the server's own earlier entry for the same post |
| `feedback[].post_id` | objectid? | no | RW | Post the reaction is about (`null` in very old bot entries) |
| `feedback[].reaction` | str | yes | RW | `like` / `dislike` / `skip` |
| `feedback[].channel` | str | no | RW | **New:** `claude` / `chatgpt` / `local` / `telegram`. Missing = written by the bot (`telegram`) |
| `identities` | list | no | RW | **New:** OAuth logins of this user; unique index `identity_unique` |
| `identities[].provider` | str | yes | RW | The OAuth issuer, e.g. `https://<tenant>.auth0.com/` |
| `identities[].subject` | str | yes | RW | The token's `sub` (a Google or Auth0 user id) |

A feedback entry written by the MCP server has the bot's shape:
`{post_id, reaction, text, digest_id: null, item_n: null, side, category, tags, at, channel}`.

## audit_log (new, owned by the MCP server)

Approved 2026-10-26. A TTL index on `at` (`AUDIT_TTL_DAYS`, default 180) deletes old entries.

| Field | Type | Required | Access | Meaning |
|---|---|---|---|---|
| `at` | datetime | yes | W | Time of the call |
| `kind` | str | yes | W | `tool` or `model` |
| `user_id` | int? | no | W | Resolved user |
| `trust` | str | no | W | `cloud` / `local` |
| `channel` | str | no | W | Client channel |
| `name` | str | yes | W | Tool name, or `backend:model` |
| `args` | dict | no | W | Arguments with secrets redacted |
| `items_returned` | int | no | W | Number of items in the result |
| `max_sensitivity` | str | no | W | Highest label returned (tool) or sent (model) |
| `tokens` | dict | no | W | `{input, output}` for model calls |
| `outcome` | str | yes | W | `ok` / `error` / `refused` |
| `error` | str | no | W | Short error text |

## conversations

Not used by the MCP server.
