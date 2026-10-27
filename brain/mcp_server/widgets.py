"""MCP Apps widgets: interactive views Claude renders under a tool's answer (docs/FEEDBACK_PLAN.md §4).

Each widget is one self-contained HTML file in ui/, served as a `ui://` resource. The ext-apps `App` client
(vendored, see ui/vendor/.../SOURCE.md) is inlined, so the sandbox needs no external domains (no `csp`).
`ui.domain` is not set: the widget reaches the server only through the host (callServerTool), which uses the
connector's own sign-in, so it needs no stable origin of its own.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

from mcp.server.apps import Apps, client_supports_apps
from mcp.server.mcpserver import Context

from brain.mcp_server import schemas as out

logger = logging.getLogger("brain.widgets")

UI_DIR = Path(__file__).parent / "ui"
EXT_APPS = UI_DIR / "vendor" / "ext-apps-1.7.5" / "app-with-deps.js"
PLACEHOLDER = "/*__EXT_APPS__*/"
COMMON = {"/*__COMMON_CSS__*/": UI_DIR / "common.css", "/*__COMMON_JS__*/": UI_DIR / "common.js"}

DIGEST_UI = "ui://news-brain/digest.html"
SEARCH_UI = "ui://news-brain/search.html"


def tool_meta(resource_uri: str) -> dict:
    """A tool's `_meta` binding it to a widget: the spec's nested key plus the legacy flat key, as the
    TypeScript helper (registerAppTool) writes both."""
    return {"ui": {"resourceUri": resource_uri}, "ui/resourceUri": resource_uri}


@lru_cache
def ext_apps_script() -> str:
    """The bundle as a classic statement list: its single trailing `export{a as B,…}` becomes
    `globalThis.__mcpExtApps = {B: a, …}`, which the widget's own module script reads."""
    code = EXT_APPS.read_text(encoding="utf-8")
    match = re.search(r"export\{([^}]*)\};?\s*$", code)
    if not match or "</script" in code.lower():
        raise RuntimeError(f"Unexpected bundle format: {EXT_APPS}")
    pairs = []
    for part in match.group(1).split(","):
        local, _, exported = part.strip().partition(" as ")
        pairs.append(f"{(exported or local).strip()}:{local.strip()}")
    return code[:match.start()] + "globalThis.__mcpExtApps={" + ",".join(pairs) + "};"


@lru_cache
def widget_html(name: str) -> str:
    html = (UI_DIR / name).read_text(encoding="utf-8")
    parts = {PLACEHOLDER: ext_apps_script(), **{k: f.read_text(encoding="utf-8") for k, f in COMMON.items()}}
    for placeholder, text in parts.items():
        if html.count(placeholder) != 1:
            raise RuntimeError(f"{name}: expected one {placeholder}")
        html = html.replace(placeholder, text)
    return html


def client_shows_widgets(ctx: Context | None) -> bool:
    """Whether this connection's client declared MCP Apps support (it renders our ui:// widgets)."""
    try:
        shows = ctx is not None and client_supports_apps(ctx)
        params = ctx.request_context.session.client_params if ctx is not None else None
    except Exception:           # outside a request, or a transport without a session
        shows, params = False, None
    client = getattr(getattr(params, "client_info", None), "name", None)
    logger.info(f"client {client!r}: widgets {'yes' if shows else 'no'}")
    return shows


def _short(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"


def digest_index(result: out.DigestResult) -> str:
    """What the model reads when the digest is on screen as cards: a rule not to repeat it, and an index
    (ref -> ids, opening words) so follow-ups and chat ratings («третя некорисна») still work."""
    items = [(d, s, i) for d in result.digests for s in d.sections for i in s.items]
    lines = [
        f"The user already sees this digest ({result.date}) as interactive cards #1–#{len(items)} with source "
        "links and 👍 / 👎 / 50/50 buttons. Do NOT repeat, list or summarize the digest in your reply. Reply "
        "with one short sentence at most (e.g. offer more detail on an item by its number), unless the user "
        "asked something specific. If the user asks for the digest as text, call get_digest with as_text=true.",
        "Ratings made with the buttons are already saved; they reach you as context, not as messages.",
        "Index (ref · side · category · digest_id/n · source post ids · opening words). Items marked RU are "
        "claims of Russian channels, not facts:",
    ]
    for d, s, i in items:
        lines.append(f"#{i.ref} {'RU' if d.side == 'ru' else 'UA'} · {s.category} · {d.digest_id}/{i.n} · "
                     f"{','.join(i.source_post_ids[:3])} · «{_short(i.text)}»")
    for d in result.digests:
        if d.hidden_items:
            lines.append(f"{d.hidden_items} {d.side} item(s) withheld for this connection.")
    if result.missing_sides:
        lines.append("No digest for: " + ", ".join(result.missing_sides))
    return "\n".join(lines)


SEARCH_NOTE = (
    "The user already sees these results as interactive cards #1–#{n} (channel, date, summary, link, and "
    "👍 / 👎 / 50/50 buttons). Answer the user's question from them in your own words, citing items as #N; do "
    "NOT list the posts one by one or repeat their links. Ratings made with the buttons are already saved; they "
    "reach you as context, not as messages.\n")


def apps_extension() -> Apps:
    """The MCP Apps extension with every widget resource; pass it to MCPServer(extensions=[...])."""
    apps = Apps()
    apps.add_html_resource(DIGEST_UI, widget_html("digest.html"), name="digest", title="Зведення",
                           description="Today's digest with 👍 / 👎 / 50/50 buttons", prefers_border=False)
    apps.add_html_resource(SEARCH_UI, widget_html("search.html"), name="search", title="Пошук",
                           description="Search results with 👍 / 👎 / 50/50 buttons", prefers_border=False)
    return apps
