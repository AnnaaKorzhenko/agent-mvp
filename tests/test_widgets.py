"""The MCP Apps widget: served as a ui:// resource, bound to get_digest, self-contained, valid JavaScript."""
import copy
import hashlib
import itertools
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.extension import ClientExtension

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.mcp_server import widgets
from brain.mcp_server.server import create_server
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import DIGEST_UA, USERS

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


PAGES = ("digest.html", "search.html")


@pytest.mark.parametrize("page", PAGES)
def test_widget_html_is_self_contained(page):
    html = widgets.widget_html(page)
    assert "/*__" not in html and "globalThis.__mcpExtApps={" in html and "function createWidget" in html
    assert "App:" in widgets.ext_apps_script() and not re.search(r"\bexport\{", widgets.ext_apps_script()[-3000:])
    assert not re.search(r"<script[^>]+src=", html) and "<link" not in html     # nothing loaded from outside
    assert "innerHTML" not in html                                                # data is set as text only


async def test_resource_and_tool_binding(server):
    async with Client(server) as c:
        assert "io.modelcontextprotocol/ui" in (c.server_capabilities.extensions or {})
        tools = {t.name: t for t in (await c.list_tools()).tools}
        listed = {str(r.uri): r for r in (await c.list_resources()).resources}
        for name, uri, title in (("get_digest", widgets.DIGEST_UI, "Зведення"),
                                 ("search_posts", widgets.SEARCH_UI, "Пошук")):
            meta = tools[name].meta or {}
            assert meta["ui"]["resourceUri"] == uri and meta["ui/resourceUri"] == uri
            assert listed[uri].mime_type == "text/html;profile=mcp-app"
            [content] = (await c.read_resource(uri)).contents
            assert content.mime_type == "text/html;profile=mcp-app" and f"<title>{title}</title>" in content.text
            assert (content.meta or {}).get("ui", {}).get("domain") is None            # see widgets.py docstring
        assert not (tools["record_feedback"].meta or {}).get("ui")                    # no widget of its own


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("page", PAGES)
def test_widget_scripts_parse(tmp_path, page):
    html = widgets.widget_html(page)
    scripts = re.findall(r'<script type="module">(.*?)</script>', html, flags=re.S)
    assert len(scripts) == 2
    for i, code in enumerate(scripts):
        f = tmp_path / f"s{i}.mjs"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([str(NODE), "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


class AppsClient(ClientExtension):
    """A client that renders MCP Apps, as Claude Desktop does."""
    identifier = "io.modelcontextprotocol/ui"

    def settings(self) -> dict:
        return {"mimeTypes": ["text/html;profile=mcp-app"]}


_uids = itertools.count(700_000_001)


@pytest.fixture
async def digest_server(test_db):
    uid = next(_uids)
    await test_db.users.insert_one({**copy.deepcopy(USERS[0]), "_id": uid})
    svc = Services(settings=make_settings(DEV_USER_ID=uid), repos=Repos(test_db), audit=AuditLog(test_db),
                   token=lambda: None)
    return create_server(svc)


async def test_digest_with_widget_gives_the_model_only_an_index(digest_server):
    async with Client(digest_server, extensions=[AppsClient()]) as c:
        r = await c.call_tool("get_digest", {})
    assert r.structured_content["shown_in_widget"] is True
    sections = r.structured_content["digests"][0]["sections"]
    assert sections and sections[0]["items"][0]["text"]                        # the widget still gets everything
    text = r.content[0].text
    assert "Do NOT repeat" in text and "#1 UA" in text and "#3 RU" in text
    assert "Морські дрони уразили катер." in text                              # short texts kept for follow-ups
    assert "source_urls" not in text                                           # not the full JSON


async def test_digest_as_text_or_without_widgets_is_complete(digest_server):
    async with Client(digest_server, extensions=[AppsClient()]) as c:
        r = await c.call_tool("get_digest", {"as_text": True})
    assert r.structured_content["shown_in_widget"] is False and "source_urls" in r.content[0].text
    async with Client(digest_server) as c:                                     # e.g. Claude Code
        r = await c.call_tool("get_digest", {})
    assert r.structured_content["shown_in_widget"] is False and "source_urls" in r.content[0].text


async def test_search_with_widget_tells_the_model_not_to_list_posts(digest_server):
    async with Client(digest_server, extensions=[AppsClient()]) as c:
        r = await c.call_tool("search_posts", {"query": "дрони", "days": 30})
    assert r.structured_content["shown_in_widget"] is True and r.structured_content["results"]
    text = r.content[0].text
    assert text.startswith("The user already sees these results") and "do NOT list" in text
    assert "summary" in text                                                   # summaries kept: Claude answers from them
    async with Client(digest_server) as c:
        r = await c.call_tool("search_posts", {"query": "дрони", "days": 30})
    assert r.structured_content["shown_in_widget"] is False and r.content[0].text.startswith("{")


async def test_follow_up_is_audited_without_a_made_up_target(digest_server, test_db):
    async with Client(digest_server) as c:
        r = await c.call_tool("record_feedback", {"target_type": "digest_item", "digest_id": str(DIGEST_UA),
                                                  "item_n": 1, "reaction": "useful"})
        fid = r.structured_content["feedback_id"]
        f = await c.call_tool("record_feedback", {"feedback_id": fid, "text": "дякую"})
    assert f.structured_content["target_type"] == "digest_item"
    entry = await test_db.audit_log.find_one({"name": "record_feedback", "args.feedback_id": fid})
    assert entry["args"]["target_type"] is None


def test_widgets_hide_when_the_result_is_not_for_cards():
    js = widgets.widget_html("digest.html")
    assert "data?.shown_in_widget === false" in js and "document.body.hidden = true" in js
