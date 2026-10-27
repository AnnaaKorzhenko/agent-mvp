"""MCP Apps widgets: interactive views Claude renders under a tool's answer (docs/FEEDBACK_PLAN.md §4).

Each widget is one self-contained HTML file in ui/, served as a `ui://` resource. The ext-apps `App` client
(vendored, see ui/vendor/.../SOURCE.md) is inlined, so the sandbox needs no external domains (no `csp`).
`ui.domain` is not set: the widget reaches the server only through the host (callServerTool), which uses the
connector's own sign-in, so it needs no stable origin of its own.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from mcp.server.apps import Apps

UI_DIR = Path(__file__).parent / "ui"
EXT_APPS = UI_DIR / "vendor" / "ext-apps-1.7.5" / "app-with-deps.js"
PLACEHOLDER = "/*__EXT_APPS__*/"

DIGEST_UI = "ui://news-brain/digest.html"


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
    if html.count(PLACEHOLDER) != 1:
        raise RuntimeError(f"{name}: expected one {PLACEHOLDER}")
    return html.replace(PLACEHOLDER, ext_apps_script())


def apps_extension() -> Apps:
    """The MCP Apps extension with every widget resource; pass it to MCPServer(extensions=[...])."""
    apps = Apps()
    apps.add_html_resource(DIGEST_UI, widget_html("digest.html"), name="digest", title="Зведення",
                           description="Today's digest with 👍 / 👎 / 50/50 buttons", prefers_border=False)
    return apps
