"""The teammate's companies catalogue on the «Інвестиції» screen (brain/web/companies.py, commit «Sexy companies»),
tested as it is through the web app: /api/companies (search, filters, paging, the filters' lists) and
/api/companies/{slug}, on a defence_companies collection shaped like the live one."""
from __future__ import annotations

import itertools
import re

import httpx2
import pytest

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.tools.base import Services
from brain.web.app import FRONTEND, create_app
from brain.web.auth import Auth0Login
from tests.conftest import make_settings
from tests.test_demo_check import NoKeys, _no_auth0

_ids = itertools.count(978_000_001)


def company(name, country, sector, meta, **more):
    slug = name.lower().replace(" ", "-")
    return {"name": name, "slug": slug, "country": country, "sector": sector, "card_metadata": meta,
            "description": f"{name} builds things.", "location": "Somewhere", "website": f"{slug}.com",
            "profile_url": f"https://www.europedefencetech.com/startups/{slug}", "collection_status": "success",
            "data_name": "internal", "facts": {"website": f"{slug}.com"}, "funding": [{"round": "Seed"}],
            "faq": [], "sources": [], "verification": {}, **more}


COMPANIES = [
    company("Acodyne", "denmark", "drones", "Drones Pre-seed / Jun 2026"),
    company("Brightline", "united-kingdom", "autonomy-ai", "Autonomy & AI Series A / Mar 2026", location="London"),
    company("Cortex EW", "germany", "electronic-warfare", "Electronic Warfare Seed / Jan 2026"),
    company("Delta (Navy)", "germany", "maritime", "Maritime Seed / Feb 2026"),
    company("Echo Failed", "france", "drones", "Drones Seed", collection_status="failed"),
]


@pytest.fixture
async def ui(test_db):
    db = test_db.client[f"{test_db.name}_co{next(_ids)}"]
    await db.defence_companies.insert_many([dict(c) for c in COMPANIES])
    settings = make_settings(DEV_USER_ID=next(_ids))
    app = create_app(settings, Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8001") as c:
        yield c
    await test_db.client.drop_database(db.name)


async def get(ui, **params) -> dict:
    r = await ui.get("/api/companies", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_the_first_page_lists_collected_companies_by_name_with_the_filters(ui):
    body = await get(ui)
    assert body["total"] == 4 and [c["name"] for c in body["items"]] == ["Acodyne", "Brightline", "Cortex EW",
                                                                        "Delta (Navy)"]
    first = body["items"][0]
    assert (first["sector_label"], first["stage"]) == ("Drones", "Pre-seed / Jun 2026")
    assert body["items"][1]["sector_label"] == "Autonomy & AI"
    assert "_id" not in first and "data_name" not in first and "funding" not in first      # a card, not the page
    assert body["countries"] == [{"id": "denmark", "label": "Denmark", "count": 1},
                                 {"id": "germany", "label": "Germany", "count": 2},
                                 {"id": "united-kingdom", "label": "United Kingdom", "count": 1}]
    assert [(s["id"], s["label"]) for s in body["sectors"]] == [
        ("autonomy-ai", "Autonomy & AI"), ("drones", "Drones"), ("electronic-warfare", "Electronic Warfare"),
        ("maritime", "Maritime")]


async def test_search_filters_and_paging(ui):
    assert [c["name"] for c in (await get(ui, country="germany"))["items"]] == ["Cortex EW", "Delta (Navy)"]
    assert [c["name"] for c in (await get(ui, country="germany", sector="maritime"))["items"]] == ["Delta (Navy)"]
    assert [c["name"] for c in (await get(ui, q="london"))["items"]] == ["Brightline"]       # by the city
    assert [c["name"] for c in (await get(ui, q="(navy)"))["items"]] == ["Delta (Navy)"]     # text, not a regex
    assert (await get(ui, q=".*"))["total"] == 0
    page2 = await get(ui, skip=2, limit=2)
    assert page2["total"] == 4 and [c["name"] for c in page2["items"]] == ["Cortex EW", "Delta (Navy)"]
    assert "countries" not in page2 and "sectors" not in page2                            # the lists: page one only
    assert (await ui.get("/api/companies", params={"limit": 61})).status_code == 422


async def test_a_companys_page(ui):
    r = await ui.get("/api/companies/brightline")
    assert r.status_code == 200
    page = r.json()
    assert page["name"] == "Brightline" and page["funding"] == [{"round": "Seed"}] and page["stage"]
    assert "_id" not in page and "data_name" not in page
    assert (await ui.get("/api/companies/echo-failed")).status_code == 404               # not collected
    assert (await ui.get("/api/companies/nobody")).status_code == 404
    assert (await ui.get("/api/companies/-bad")).status_code == 404


async def test_the_catalogue_needs_a_sign_in(test_db):
    settings = make_settings(OAUTH_ISSUER="https://tenant.example.auth0.com/", PUBLIC_BASE_URL="http://web.localhost",
                             WEB_CLIENT_ID="web-client", WEB_CLIENT_SECRET="s3cret", WEB_SESSION_SECRET="x" * 48)
    login = Auth0Login(settings, keys=NoKeys(), http=httpx2.AsyncClient(transport=httpx2.MockTransport(_no_auth0)))
    app = create_app(settings, Services(settings=settings, repos=Repos(test_db), audit=AuditLog(test_db),
                                        token=lambda: None), login)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://web.localhost") as c:
        assert (await c.get("/api/companies")).status_code == 401
        assert (await c.get("/api/companies/acodyne")).status_code == 401


def test_the_screen_loads_the_catalogue():
    js = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert re.search(r"if \(screen === 'map'\) loadCompanies\(\)", js)
    assert "companiesSectionHtml()" in js[js.index("function renderMap()"):]
    assert "getCompanies" in (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")
