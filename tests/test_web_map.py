"""The teammate's «Інвестиції» screen (data-digest-web/frontend, commit «map»), tested as it is: what the web app
serves for it, and the digest data its jump to «Інвестиції та фінансування» relies on. There is no JS runner here,
so the jump's pattern (INVEST_RE) is read out of app.js and compiled in Python."""
from __future__ import annotations

import ast
import copy
import itertools
import re
from datetime import datetime
from pathlib import Path

import httpx2
import pytest
from bson import ObjectId

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.domain import digests, sensitivity, topics
from brain.domain.identity import UserContext
from brain.tools.base import Services
from brain.web.app import FRONTEND, create_app
from tests.conftest import make_settings
from tests.fixtures.sample_data import POSTS, USERS

ROOT = Path(__file__).resolve().parents[1]
MAP_SRC = "https://defense-valley.news/embed/map/"
_ids = itertools.count(976_000_001)


def app_js() -> str:
    return (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")


def invest_re() -> re.Pattern:
    m = re.search(r"const INVEST_RE = /(.+?)/([a-z]*);", app_js())
    assert m, "INVEST_RE is no longer a regex literal in app.js"
    return re.compile(m.group(1), re.I if "i" in m.group(2) else 0)


def is_invest(section: dict) -> bool:
    """app.js isInvest: the category or the title matches."""
    rx = invest_re()
    return bool(rx.search(section.get("category") or "") or rx.search(section.get("title") or ""))


def pipeline_titles() -> dict[str, dict[str, str]]:
    """digest.py's CATEGORY_TITLES, read from the file (the teammate's pipeline: not imported)."""
    tree = ast.parse((ROOT / "digest.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "CATEGORY_TITLES" for t in n.targets))
    return ast.literal_eval(node.value)


# ---- what the web app serves ------------------------------------------------------------------------------

@pytest.fixture
async def ui(test_db):
    settings = make_settings(DEV_USER_ID=next(_ids))
    app = create_app(settings, Services(settings=settings, repos=Repos(test_db), audit=AuditLog(test_db),
                                        token=lambda: None))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8001") as c:
        yield c


async def test_the_menu_has_the_investments_screen(ui):
    r = await ui.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    button = re.search(r'<button class="nav-item" data-screen="map">(.*?)</button>', r.text, re.S)
    assert button and "Інвестиції" in button.group(1)


async def test_the_map_screen_is_served_with_a_sandboxed_lazy_embed(ui):
    r = await ui.get("/js/app.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    js = r.text
    assert "function renderMap()" in js
    assert re.search(r"map:\s*renderMap\b", js), "the map screen is not among render()'s views"
    assert f"const MAP_SRC = '{MAP_SRC}'" in js
    frame = re.search(r"<iframe\b(.*?)></iframe>", js[js.index("function renderMap()"):], re.S)
    assert frame and "${MAP_SRC}" in frame.group(1)
    assert 'loading="lazy"' in frame.group(1) and re.search(r'sandbox="[^"]*allow-scripts', frame.group(1))
    assert "data-invest-digest" in js and "data-invest" in js        # the button and the section it jumps to
    css = await ui.get("/css/styles.css")
    assert css.status_code == 200 and ".embed-frame" in css.text


async def test_nothing_the_app_sends_blocks_the_embed(ui):
    """The map is another site's page in an iframe: a CSP whose frame-src leaves it out would leave the screen
    blank. None is sent today; one added later must allow defense-valley.news. (Our X-Frame-Options: DENY is fine:
    it stops others from framing us, not us from framing the map.)"""
    for path in ("/", "/js/app.js", "/api/health"):
        csp = (await ui.get(path)).headers.get("content-security-policy")
        if csp:
            frame_src = re.search(r"(?:frame-src|child-src|default-src)([^;]*)", csp)
            assert frame_src and "defense-valley.news" in frame_src.group(1), csp


# ---- the data the jump needs ------------------------------------------------------------------------------

def test_the_jump_pattern_finds_the_investments_section_and_no_other():
    titles = pipeline_titles()
    hits = [(side, cat) for side, cats in titles.items() for cat, title in cats.items()
            if is_invest({"category": cat, "title": title})]
    assert hits == [("ua", "investments")]
    assert is_invest({"category": "investments", "title": None})                      # by the category alone
    assert is_invest({"category": "x", "title": "💰 Інвестиції та фінансування"})       # by the title alone
    for category in ("for_you", "topic:brave1", "topics"):
        assert not is_invest({"category": category, "title": "⭐ Для вас"})


@pytest.fixture
async def world(test_db):
    db = test_db.client[f"{test_db.name}_m{next(_ids)}"]
    uid = next(_ids)
    user = {**copy.deepcopy(USERS[0]), "_id": uid, "goals": [], "feedback": [], "interests": {}}
    await db.users.insert_one(user)
    await db.posts.insert_many(copy.deepcopy(POSTS))
    yield db, uid
    await test_db.client.drop_database(db.name)


async def add_digest(db, sections) -> str:
    _id = ObjectId()
    await db.digests.insert_one({"_id": _id, "period": "day", "trigger": "scheduled", "side": "ua",
                                 "created_at": datetime.now(digests.KYIV), "sections": sections})
    return str(_id)


INVEST = {"category": "investments", "title": "💰 Інвестиції та фінансування", "items": [
    {"text": "Brave1 видав гранти на РЕБ.", "source_urls": ["https://t.me/a/1"], "source_post_ids": [], "n": 2},
    {"text": "Фонд залучив 20 млн євро на дрони.", "source_urls": ["https://t.me/a/2"], "source_post_ids": [], "n": 3}]}
FRONT = {"category": "front", "title": "⚔️ Фронт", "items": [
    {"text": "Бої під Покровськом.", "source_urls": ["https://t.me/a/3"], "source_post_ids": [], "n": 1}]}


def web(db, uid):
    settings = make_settings(DEV_USER_ID=uid, TRUST_LEVEL="cloud")
    app = create_app(settings, Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None))
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8001")


def invest_sections(body: dict) -> list[dict]:
    return [s for d in body["digests"] for s in d["sections"] if is_invest(s)]


async def test_the_digest_api_keeps_the_sections_category_and_title(world):
    db, uid = world
    await add_digest(db, [FRONT, copy.deepcopy(INVEST)])
    async with web(db, uid) as w:
        body = (await w.get("/api/digest", params={"side": "ua"})).json()
    [section] = invest_sections(body)
    assert section["category"] == "investments" and section["title"] == INVEST["title"]
    assert [i["n"] for i in section["items"]] == [2, 3]


async def test_the_section_stays_when_for_you_takes_one_of_its_items(world):
    db, uid = world
    await add_digest(db, [FRONT, copy.deepcopy(INVEST)])
    await topics.track_topic(Repos(db), UserContext(uid, "cloud", "web", sensitivity.max_for("cloud")), "Brave1")
    async with web(db, uid) as w:
        body = (await w.get("/api/digest", params={"side": "ua"})).json()
    sections = [s for d in body["digests"] for s in d["sections"]]
    for_you = next(s for s in sections if s["category"] == digests.FOR_YOU.category)
    assert [i["n"] for i in for_you["items"]] == [2] and not is_invest(for_you)
    [section] = invest_sections(body)
    assert section["category"] == "investments" and [i["n"] for i in section["items"]] == [3]


async def test_unseen_only_keeps_the_section_while_it_has_unseen_items(world):
    db, uid = world
    digest_id = await add_digest(db, [FRONT, copy.deepcopy(INVEST)])
    day = datetime.now(digests.KYIV).date().isoformat()
    await Repos(db).users.mark_digest_seen(uid, day, [digests.seen_key(digest_id, 1), digests.seen_key(digest_id, 2)],
                                           digests.SEEN_MAX)
    async with web(db, uid) as w:
        body = (await w.get("/api/digest", params={"side": "ua", "unseen_only": "1"})).json()
        [section] = invest_sections(body)
        assert section["category"] == "investments" and [i["n"] for i in section["items"]] == [3]
        assert invest_sections((await w.get("/api/digest", params={"side": "ua", "unseen_only": "1"})).json()) == []


async def test_a_digest_without_investments_gives_the_jump_nothing_to_find(world):
    db, uid = world
    await add_digest(db, [FRONT])
    async with web(db, uid) as w:
        body = (await w.get("/api/digest", params={"side": "ua"})).json()
    assert body["digests"] and invest_sections(body) == []
