"""Web Push: the settings, the VAPID key script and the web app's subscriptions."""
from __future__ import annotations

import base64
import itertools

import httpx2
import pytest
from py_vapid import Vapid

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.scripts import vapid_keys
from brain.tools.base import Services
from brain.web.app import create_app
from tests.conftest import make_settings

DEV_UID = 820_000_004
OTHER_UID = 820_000_005
KEYS = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
        "auth": "tBHItJI5svbpez7KI4CCXg"}
_dbs = itertools.count()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_settings_default_off():
    s = make_settings()
    assert s.morning_push is False
    assert s.web_push_public_key is None and s.web_push_private_key is None and s.web_push_contact is None


def test_settings_private_key_is_secret():
    s = make_settings(WEB_PUSH_PUBLIC_KEY="pub", WEB_PUSH_PRIVATE_KEY="s3cr3t-vapid",
                      WEB_PUSH_CONTACT="mailto:a@example.com", MORNING_PUSH="true")
    assert s.morning_push is True and s.web_push_public_key == "pub"
    assert s.web_push_private_key.get_secret_value() == "s3cr3t-vapid"
    assert "s3cr3t-vapid" not in repr(s)


def test_new_pair_is_a_working_vapid_pair():
    public, private = vapid_keys.new_pair()
    raw_public = _unb64(public)
    assert len(raw_public) == 65 and raw_public[0] == 4 and len(_unb64(private)) == 32
    assert "=" not in public + private
    # pywebpush loads the private key this way; its public key must be the one we print
    v = Vapid.from_string(private)
    assert v.public_key.public_numbers().x.to_bytes(32, "big") == raw_public[1:33]
    assert vapid_keys.new_pair() != (public, private)


def test_script_prints_and_writes_nothing(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    vapid_keys.main()
    lines = capsys.readouterr().out.splitlines()
    assert [l.split("=", 1)[0] for l in lines] == ["WEB_PUSH_PUBLIC_KEY", "WEB_PUSH_PRIVATE_KEY"]
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
async def db(test_db):
    d = test_db.client[f"{test_db.name}_p{next(_dbs)}"]
    other = {"endpoint": "https://push.example.com/other", "keys": KEYS}
    await d.users.insert_many([{"_id": DEV_UID, "goals": [], "topics": []},
                               {"_id": OTHER_UID, "goals": [], "topics": [], "push_subscriptions": [other]}])
    yield d
    await test_db.client.drop_database(d.name)


def app_with(db, **env):
    settings = make_settings(DEV_USER_ID=DEV_UID, **env)
    return create_app(settings, Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None))


def web(app, csrf=True):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8001",
                              headers={"X-Brain": "1"} if csrf else {})


def sub(n: int) -> dict:
    return {"endpoint": f"https://push.example.com/s{n}", "keys": KEYS}


async def subs(db, uid=DEV_UID) -> list[dict]:
    return (await db.users.find_one({"_id": uid})).get("push_subscriptions", [])


async def test_key_is_null_and_subscribe_refused_without_keys(db):
    async with web(app_with(db)) as c:
        assert (await c.get("/api/push/key")).json() == {"key": None}
        assert (await c.post("/api/push/subscribe", json=sub(1))).status_code == 503
    assert await subs(db) == []


async def test_subscribe_one_per_endpoint_at_most_five(db):
    async with web(app_with(db, WEB_PUSH_PUBLIC_KEY="BPub")) as c:
        assert (await c.get("/api/push/key")).json() == {"key": "BPub"}
        assert (await c.post("/api/push/subscribe", json=sub(1))).json() == {"subscribed": True}
        await c.post("/api/push/subscribe", json=sub(1))                        # the same browser again
        [s] = await subs(db)
        assert s["endpoint"] == sub(1)["endpoint"] and s["keys"] == KEYS and s["created_at"]
        for n in range(2, 8):
            assert (await c.post("/api/push/subscribe", json=sub(n))).status_code == 200
        assert [s["endpoint"] for s in await subs(db)] == [sub(n)["endpoint"] for n in range(3, 8)]
    assert len(await subs(db, OTHER_UID)) == 1                                   # the session's user only


async def test_unsubscribe_removes_only_the_users_own(db):
    async with web(app_with(db, WEB_PUSH_PUBLIC_KEY="BPub")) as c:
        await c.post("/api/push/subscribe", json=sub(1))
        r = await c.post("/api/push/unsubscribe", json={"endpoint": "https://push.example.com/other"})
        assert r.json() == {"subscribed": False, "removed": False}
        r = await c.post("/api/push/unsubscribe", json={"endpoint": sub(1)["endpoint"]})
        assert r.json() == {"subscribed": False, "removed": True}
    assert await subs(db) == [] and len(await subs(db, OTHER_UID)) == 1


async def test_csrf_header_and_valid_subscription_required(db):
    app = app_with(db, WEB_PUSH_PUBLIC_KEY="BPub")
    async with web(app, csrf=False) as c:
        assert (await c.post("/api/push/subscribe", json=sub(1))).status_code == 403
        assert (await c.post("/api/push/unsubscribe", json={"endpoint": sub(1)["endpoint"]})).status_code == 403
    async with web(app) as c:
        bad = [{"endpoint": "http://push.example.com/x", "keys": KEYS},
               {"endpoint": sub(1)["endpoint"], "keys": {"p256dh": "not base64!", "auth": KEYS["auth"]}},
               {"endpoint": sub(1)["endpoint"]}]
        for body in bad:
            assert (await c.post("/api/push/subscribe", json=body)).status_code == 422
    assert await subs(db) == []


async def test_a_user_id_in_the_body_is_ignored_and_long_endpoints_refused(db):
    async with web(app_with(db, WEB_PUSH_PUBLIC_KEY="BPub")) as c:
        await c.post("/api/push/subscribe", json={**sub(1), "user_id": OTHER_UID})
        long = {"endpoint": "https://push.example.com/" + "x" * 1000, "keys": KEYS}
        assert (await c.post("/api/push/subscribe", json=long)).status_code == 422
    assert [s["endpoint"] for s in await subs(db)] == [sub(1)["endpoint"]]
    assert len(await subs(db, OTHER_UID)) == 1


async def test_service_worker_and_manifest_are_served_with_their_types(db):
    async with web(app_with(db), csrf=False) as c:
        sw = await c.get("/sw.js")
        assert sw.status_code == 200 and sw.headers["content-type"].startswith("text/javascript")
        assert sw.headers["cache-control"] == "no-cache"
        assert "notificationclick" in sw.text and "#digests" in sw.text
        m = await c.get("/manifest.webmanifest")
        assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
        assert m.json()["start_url"] == "/#digests"
        assert (await c.get("/icon.svg")).headers["content-type"].startswith("image/svg+xml")
        assert 'rel="manifest" href="/manifest.webmanifest"' in (await c.get("/")).text
