# Connect Claude to the news brain (Auth0 + Render)

Result: the MCP server runs on Render at `https://<service>.onrender.com/mcp`, people sign in with Auth0, and Claude (web, desktop, mobile) uses it as a custom connector.

The steps below use `<service>` for the Render service name and `<tenant>` for the Auth0 tenant domain, e.g. `news-brain.eu.auth0.com`. Do them in this order, because the Render URL is needed in Auth0.

## 0. Before you start

- **Render's free tier gives 750 instance hours a month per workspace**, and a free service that's kept awake uses about 730.
  - Pinging both the bot and this server would use about 1,490 hours. Render would then **suspend all free services** in the workspace until the next month.
  - Pick one of the hosting options in `MCP_PLAN.md` §6 first.
- `users` records for Claude users are created automatically on first sign-in.
  - Existing Telegram users are not changed or linked.
  - Each new record has `source: "claude"`, `onboarded: false` and `daily_push: false`, so the bot never messages it.

## 1. Render: create the service, so its URL is known

1. Dashboard → **New → Web Service** → this GitHub repository.
2. Choose the branch with `brain/` (`mcp-server`, or `main` after merging).
3. Settings:

   | Setting | Value |
   |---|---|
   | Runtime | Python 3 |
   | Build command | `pip install -r brain/requirements.txt` |
   | Start command | `python -m brain.mcp_server --host 0.0.0.0` |
   | Health check path | `/health` |
   | Instance type | as decided in step 0 |

4. Don't deploy yet. Note the URL: `https://<service>.onrender.com`.

(A Docker build also works: `deploy/mcp/Dockerfile`, with the root directory as the build context.)

## 2. Auth0

Auth0's free plan covers this setup: one app for Claude, entered by hand. We **don't** use open Dynamic Client Registration, which lets anyone create apps in the tenant and which Auth0 itself advises against in production.

1. **Create the tenant** (region EU). Note the domain `<tenant>`. The issuer is `https://<tenant>/`, with the trailing slash.

2. **Create an API**: Applications → APIs → Create API.
   - Name: `News brain MCP`.
   - Identifier: **exactly** `https://<service>.onrender.com/mcp`.
   - Signing algorithm: RS256.
   - Then, under Settings: **Allow Offline Access = on**, so Claude gets refresh tokens.

3. **Make tokens carry this audience.** Claude sends `resource=<MCP URL>`; Auth0 traditionally expects `audience`.
   - Settings → General → API Authorization Settings → **Default Audience** = `https://<service>.onrender.com/mcp`.
   - If your plan shows it: Settings → Advanced → **Resource Parameter Compatibility Profile = on**.

4. **Create the app for Claude**: Applications → Create Application.
   - Name `Claude`, type **Native**. Claude signs in as a public client with PKCE and no secret.
   - **Allowed Callback URLs**: `https://claude.ai/api/mcp/auth_callback, https://claude.com/api/mcp/auth_callback`.
   - Advanced Settings → Grant Types: **Authorization Code** and **Refresh Token**.
   - Refresh Token Rotation: **on**.
   - Note the **Client ID**.

5. **Sign-in methods**: Authentication → Database (Username-Password) and/or Social → Google. Enable them for the `Claude` app. To keep the brain private, do one of these:
   - Database: **Disable Sign Ups**, and create each user under User Management → Users.
   - Or allow Google, and list the permitted emails in `ALLOWED_EMAILS` (step 3).

6. **Post-login Action**, which adds the client name and email to the access token:
   - Actions → Library → Create Action → Build from scratch, trigger *Login / Post Login*:

     ```js
     exports.onExecutePostLogin = async (event, api) => {
       const ns = "https://news-brain/";
       api.accessToken.setCustomClaim(ns + "client_name", event.client.name);   // "Claude" -> channel "claude"
       if (event.user.email) api.accessToken.setCustomClaim(ns + "email", event.user.email);
     };
     ```

   - Click **Deploy**, then go to Actions → Triggers → post-login and drag the Action into the flow.

## 3. Render: environment and first deploy

Set these in the Render service's **Environment** (never in git):

| Variable | Value |
|---|---|
| `PYTHON_VERSION` | a 3.12 version, e.g. `3.12.11` |
| `MONGO_URI` | same as the bot |
| `MONGO_DB` | `News` |
| `TRUST_LEVEL` | `cloud` (Claude receives `public` and `internal` only) |
| `PUBLIC_BASE_URL` | `https://<service>.onrender.com` |
| `ALLOWED_HOSTS` | `["<service>.onrender.com"]` |
| `OAUTH_ISSUER` | `https://<tenant>/` |
| `ALLOWED_EMAILS` | `["you@example.com", "teammate@example.com"]` (or leave unset if Auth0 sign-ups are closed) |
| `FORWARDED_ALLOW_IPS` | `*` |
| `LOG_LEVEL` | `INFO` |

**Do not set `DEV_USER_ID`.**

Before the first deploy, create the indexes once, from your machine:

```bash
brain/.venv/bin/python -m brain.scripts.setup_indexes --db News --dry-run
```

```bash
brain/.venv/bin/python -m brain.scripts.setup_indexes --db News
```

This creates `audit_log` (with a 180-day TTL) and a unique index on `users.identities` (one record per login). It touches nothing else.

Then deploy and check:

```bash
curl https://<service>.onrender.com/health
```

```bash
curl -i -X POST https://<service>.onrender.com/mcp
```

- The first should print `ok`.
- The second should return `401`, with `WWW-Authenticate: Bearer resource_metadata="…"`.
- `https://<service>.onrender.com/.well-known/oauth-protected-resource/mcp` should show `resource` = your MCP URL and `authorization_servers` = `["https://<tenant>/"]`.

**Monitor:** if this service is kept awake (step 0), add `https://<service>.onrender.com/health` to the uptime monitor every 10 minutes. `/health` returns no data and needs no sign-in.

## 4. Claude: add the custom connector

1. In Claude: Settings → Connectors → **Add custom connector**.
   - Name: `News brain`.
   - URL: `https://<service>.onrender.com/mcp`.
2. **Advanced settings → OAuth Client ID** = the Client ID of the Auth0 `Claude` app. Leave the client secret empty.
3. Click **Connect**, sign in on the Auth0 page and accept.
4. In a chat, enable the connector (the "+" / tools menu).

After the first call:
- `users` has a new record with `identities[0].subject` = your Auth0 user id and `source: "claude"`;
- `audit_log` shows the calls with `channel: "claude"`.

## 5. Checklist: 10 test questions (ask in Ukrainian)

| # | Ask | Expect |
|---|---|---|
| 1 | «Покажи сьогоднішнє зведення» | `get_digest`: both sides; links cited; RU part framed as claims; offers follow-ups |
| 2 | «Тільки російські джерела за сьогодні» | `get_digest(side=ru)`: «рос. канали заявляють…», never as fact |
| 3 | «Детальніше про пункт 2» | `get_post` with that item's post id; cites its `post_url` |
| 4 | «Що було про Brave1 за тиждень?» | `search_posts`; every fact with a link |
| 5 | «Що нового про морські дрони за останній місяць?» | `search_posts(days=30)` |
| 6 | «Що відомо про квантові комп'ютери в ЗСУ?» | Says the data doesn't have it; no invented facts |
| 7 | «Що росіяни пишуть про Покровськ?» | `search_posts(side=ru)`, claims framing |
| 8 | «Які мої цілі й інтереси?» | `get_profile`; a new Claude user's profile starts empty |
| 9 | After a post is shown: «Ця новина корисна» | `record_feedback(like)`; ask #8 again and that category's score has gone up |
| 10 | «Зведення за 1 січня 2001» | No digest for that date; says so |

Also check:
- an email not in `ALLOWED_EMAILS` is refused;
- disconnecting and reconnecting the connector works;
- after an hour, calls still work (token refresh).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Couldn't reach the MCP server" | The free service is asleep (wait about a minute and retry), or `ALLOWED_HOSTS` doesn't match the host (the server answers 421) |
| Auth0 error "userinfo audience is not allowed…" | Default Audience / the Resource Parameter Compatibility Profile is not set (step 2.3) |
| Every call returns 401 after sign-in | The API identifier ≠ `PUBLIC_BASE_URL + /mcp`, or `OAUTH_ISSUER` lacks the trailing slash |
| "This account is not allowed" | The email is not in `ALLOWED_EMAILS`, or the Action isn't deployed (no email claim) |
| Feedback shows `channel: "unknown"` | The post-login Action isn't in the flow |
