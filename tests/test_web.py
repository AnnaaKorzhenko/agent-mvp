"""Our web app (docs/WEB_PLAN.md, step 5.1): the Auth0 login with a local RSA key standing in for the tenant, the
signed session, the same users record as Claude's connector, the read API over the tool handlers, and the guards."""
import copy
import itertools
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx2
import jwt
import pytest
from bson import ObjectId
from cryptography.hazmat.primitives.asymmetric import rsa

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.tools.base import Services
from brain.web import session
from brain.web.app import create_app
from brain.web.auth import Auth0Login
from tests.conftest import make_settings
from tests.fixtures.sample_data import USERS, load_samples

ISS = "https://tenant.example.auth0.com/"
BASE = "https://app.example.com"
CLIENT = "web-client-id"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_dbs = itertools.count()
DEV_UID = 820_000_001
DIRECTOR = ObjectId("6ae0d78217f7db881103fcf9")


class LocalKeys:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=KEY.public_key())


def id_token(nonce, sub="google-oauth2|director", aud=CLIENT, **claims):
    now = int(time.time())
    return jwt.encode({"iss": ISS, "sub": sub, "aud": aud, "iat": now, "exp": now + 600, "nonce": nonce,
                       "email": "director@example.com", "name": "Директор", **claims}, KEY, algorithm="RS256")


class FakeAuth0:
    """The tenant's /oauth/token: answers a code with the ID token prepared for it."""

    def __init__(self):
        self.tokens: dict[str, str] = {}

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        form = parse_qs(request.content.decode())
        token = self.tokens.get(form["code"][0])
        assert form["client_secret"] == ["s3cret"] and form["redirect_uri"] == [f"{BASE}/auth/callback"]
        return httpx2.Response(200, json={"id_token": token}) if token else httpx2.Response(403, json={})


@pytest.fixture
async def db(test_db):
    d = test_db.client[f"{test_db.name}_w{next(_dbs)}"]
    await load_samples(d)
    yield d
    await test_db.client.drop_database(d.name)


def web(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE, follow_redirects=False)


@pytest.fixture
def oauth_app(db):
    settings = make_settings(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL=BASE, WEB_CLIENT_ID=CLIENT,
                             WEB_CLIENT_SECRET="s3cret", WEB_SESSION_SECRET="x" * 48, TRUST_LEVEL="cloud")
    svc = Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None)
    auth0 = FakeAuth0()
    login = Auth0Login(settings, keys=LocalKeys(), http=httpx2.AsyncClient(transport=httpx2.MockTransport(auth0.handler)))
    return create_app(settings, svc, login), auth0


async def sign_in(c, auth0, **claims):
    r = await c.get("/login", params={"next": "/#digests"})
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.headers["location"].startswith(f"{ISS}authorize?") and q["client_id"] == [CLIENT]
    assert q["redirect_uri"] == [f"{BASE}/auth/callback"] and q["scope"] == ["openid profile email"]
    assert q["audience"] == [f"{ISS}userinfo"]                  # not the MCP API (the tenant's default)
    auth0.tokens["code-1"] = id_token(q["nonce"][0], **claims)
    return await c.get("/auth/callback", params={"code": "code-1", "state": q["state"][0]})


async def test_the_api_needs_a_session_and_writes_need_the_header(oauth_app):
    app, _ = oauth_app
    async with web(app) as c:
        assert (await c.get("/api/health")).json() == {"ok": True}
        assert (await c.get("/api/digest")).status_code == 401
        assert (await c.get("/api/me", headers={"cookie": f"{session.SESSION_COOKIE}=forged.value"})).status_code == 401
        assert (await c.post("/api/chat")).status_code == 403                       # no X-Brain: refused first
        r = await c.get("/")
        assert r.status_code == 200 and "Data Digest" in r.text                    # the UI itself is public


async def test_login_finds_the_same_user_as_claude_and_reads_go_through_the_tools(oauth_app, db):
    app, auth0 = oauth_app
    doc = copy.deepcopy(USERS[0])            # the director, already signed in through Claude's connector
    doc.update({"_id": DIRECTOR, "goals": [{"text": "стежити за НРК", "active": True, "source": "user"}], "feedback": [], "insights": [],
                "identities": [{"provider": ISS, "subject": "google-oauth2|director"}]})
    await db.users.insert_one(doc)
    async with web(app) as c:
        r = await sign_in(c, auth0)
        assert r.status_code == 303 and r.headers["location"] == "/#digests"
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
        c.cookies.set(session.SESSION_COOKIE, r.cookies[session.SESSION_COOKIE])
        me = (await c.get("/api/me")).json()
        assert me == {"name": "Директор", "email": "director@example.com", "picture": None, "dev": False}
        assert (await c.get("/api/profile")).json()["goals"] == ["стежити за НРК"]
        digest = (await c.get("/api/digest")).json()
        assert digest["digests"] and digest["shown_in_widget"]
        posts = (await c.get("/api/posts", params={"q": "дрони", "days": 30})).json()
        assert "results" in posts
        assert (await c.get("/api/posts", params={"q": "д"})).json()["results"] == []      # too short: no call
        assert (await c.get("/api/posts/not-an-id")).status_code == 400
    assert await db.users.count_documents({}) == len(USERS) + 1                  # no second record for him
    [conv] = await db.conversations.find({"user_id": DIRECTOR}).to_list(None)
    assert conv["channel"] == "web" and conv["mcp_session_id"].startswith("web:")
    tools = [m["tool"] for m in conv["messages"] if m["role"] == "tool"]
    assert tools == ["get_profile", "get_digest", "search_posts", "get_post"]
    vias = {m["via"] for m in conv["messages"] if m["role"] == "user"}
    assert vias == {"widget", "search_query"}                  # page views are button presses; the query is his


async def test_a_bad_login_is_refused(oauth_app, db):
    app, auth0 = oauth_app
    async with web(app) as c:
        r = await c.get("/login")
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert (await c.get("/auth/callback", params={"code": "c", "state": "other"})).status_code == 400
        auth0.tokens["c2"] = id_token("wrong-nonce")
        assert (await c.get("/auth/callback", params={"code": "c2", "state": q["state"][0]})).status_code == 400
        auth0.tokens["c3"] = id_token(q["nonce"][0], aud="someone-else")
        assert (await c.get("/auth/callback", params={"code": "c3", "state": q["state"][0]})).status_code == 400
    assert await db.audit_log.count_documents({"event": "auth_failed"}) == 3


async def test_only_allowed_emails_get_in(db):
    settings = make_settings(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL=BASE, WEB_CLIENT_ID=CLIENT, WEB_CLIENT_SECRET="s3cret",
                             WEB_SESSION_SECRET="x" * 48, ALLOWED_EMAILS=["boss@example.com"])
    auth0 = FakeAuth0()
    login = Auth0Login(settings, keys=LocalKeys(), http=httpx2.AsyncClient(transport=httpx2.MockTransport(auth0.handler)))
    app = create_app(settings, Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None),
                     login)
    async with web(app) as c:
        r = await sign_in(c, auth0)
    assert r.status_code == 403 and session.SESSION_COOKIE not in r.cookies


async def test_dev_mode_needs_no_login(db):
    doc = copy.deepcopy(USERS[0])
    doc.update({"_id": DEV_UID, "goals": [{"text": "ціль", "active": True, "source": "user"}], "feedback": [], "insights": []})
    await db.users.insert_one(doc)
    settings = make_settings(DEV_USER_ID=DEV_UID)
    app = create_app(settings, Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=lambda: None))
    async with web(app) as c:
        assert (await c.get("/api/me")).json()["dev"] is True
        assert (await c.get("/api/profile")).json()["goals"] == ["ціль"]
        assert (await c.get("/login", params={"next": "//evil.example"})).headers["location"] == "/"


def test_signed_values_expire_and_cant_be_changed():
    v = session.sign({"sub": "a"}, b"k", 60, now=1000)
    assert session.verify(v, b"k", now=1030) == {"sub": "a"}
    assert session.verify(v, b"k", now=1061) is None
    assert session.verify(v, b"other", now=1030) is None
    body, mac = v.split(".")
    assert session.verify(body[:-2] + "xx." + mac, b"k", now=1030) is None
