"""The MCP Apps widget: served as a ui:// resource, bound to get_digest, self-contained, valid JavaScript."""
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from mcp import Client

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.mcp_server import widgets
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings

BUNDLE_SHA256 = "5bc0452b9994217df506cb505af20e97e766700513afb7d0b45d255785bb051f"   # = SOURCE.md
NODE = shutil.which("node") or next(Path.home().glob(".local/opt/node-*/bin/node"), None)


@pytest.fixture
def server(test_db):
    svc = Services(settings=make_settings(DEV_USER_ID=1), repos=Repos(test_db), audit=AuditLog(test_db),
                   token=lambda: None)
    return create_server(svc)


def test_vendored_bundle_is_the_pinned_file():
    assert hashlib.sha256(widgets.EXT_APPS.read_bytes()).hexdigest() == BUNDLE_SHA256
    assert BUNDLE_SHA256 in (widgets.EXT_APPS.parent / "SOURCE.md").read_text()


def test_widget_html_is_self_contained():
    html = widgets.widget_html("digest.html")
    assert widgets.PLACEHOLDER not in html and "globalThis.__mcpExtApps={" in html
    assert "App:" in widgets.ext_apps_script() and not re.search(r"\bexport\{", widgets.ext_apps_script()[-3000:])
    assert not re.search(r"<script[^>]+src=", html) and "<link" not in html     # nothing loaded from outside
    assert "innerHTML" not in html                                                # data is set as text only


async def test_resource_and_tool_binding(server):
    async with Client(server) as c:
        assert "io.modelcontextprotocol/ui" in (c.server_capabilities.extensions or {})
        tool = {t.name: t for t in (await c.list_tools()).tools}["get_digest"]
        meta = tool.meta or {}
        assert meta["ui"]["resourceUri"] == widgets.DIGEST_UI and meta["ui/resourceUri"] == widgets.DIGEST_UI
        listed = {str(r.uri): r for r in (await c.list_resources()).resources}
        assert listed[widgets.DIGEST_UI].mime_type == "text/html;profile=mcp-app"
        read = await c.read_resource(widgets.DIGEST_UI)
        [content] = read.contents
        assert content.mime_type == "text/html;profile=mcp-app" and "Зведення" in content.text
        assert (content.meta or {}).get("ui", {}).get("domain") is None                # see widgets.py docstring


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_widget_scripts_parse(tmp_path):
    html = widgets.widget_html("digest.html")
    scripts = re.findall(r'<script type="module">(.*?)</script>', html, flags=re.S)
    assert len(scripts) == 2
    for i, code in enumerate(scripts):
        f = tmp_path / f"s{i}.mjs"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([str(NODE), "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
