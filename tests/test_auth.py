"""OAuth end to end, with a local RSA key standing in for Auth0's JWKS.

The server runs under uvicorn on a free port and is called over real HTTP:
401 + metadata without a token, tools with a valid token, rejection of bad tokens, and the conversation log
per MCP session (real streamable HTTP sessions only exist here).
"""
import socket
import threading
import time
from types import SimpleNamespace

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pymongo import AsyncMongoClient

from brain.audit.conversations import ensure_conversation_indexes
from brain.audit.log import AuditLog
from brain.db.repositories import Repos, ensure_user_indexes
from brain.mcp_server.auth import Auth0TokenVerifier
from brain.mcp_server.server import create_server, transport_security
from brain.mcp_server.tools import Services
from tests.conftest import make_settings
from tests.fixtures.sample_data import P_DRONES

ISS = "https://tenant.example.auth0.com/"
NS = "https://news-brain/"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class LocalKeys:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=KEY.public_key())


def token(aud, *, sub="google-oauth2|alice", iss=ISS, exp_in=600, key=KEY, **claims):
    now = int(time.time())
    payload = {"iss": iss, "sub": sub, "aud": [aud, f"{ISS}userinfo"], "iat": now, "exp": now + exp_in,
               "azp": "claude-client-id", "scope": "openid offline_access", **claims}
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test"})


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(mongo_uri, test_db_name, test_db):
    """uvicorn in a thread; its Mongo client is created and used only on the server's own event loop."""
    import uvicorn
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    settings = make_settings(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL=base, MONGO_DB=test_db_name, TRUST_LEVEL="cloud")
    db = AsyncMongoClient(mongo_uri, tz_aware=True)[test_db_name]
    from brain.mcp_server.auth import token_provider
    svc = Services(settings=settings, repos=Repos(db), audit=AuditLog(db), token=token_provider(settings))
    mcp = create_server(svc, verifier=Auth0TokenVerifier(ISS, settings.resource_url, keys=LocalKeys(),
                                                         on_event=svc.audit.event))
    app = mcp.streamable_http_app(transport_security=transport_security(settings))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield SimpleNamespace(base=base, resource=settings.resource_url)
    srv.should_exit = True
    thread.join(timeout=5)


async def session(server, bearer, *calls):
    """Several tool calls over one MCP connection (one Mcp-Session-Id)."""
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {bearer}"}, timeout=20) as http:
        async with Client(streamable_http_client(server.resource, http_client=http)) as c:
            return [await c.call_tool(tool, {"user_message": f"повідомлення для {tool}", **args})
                    for tool, args in calls]


async def call(server, bearer, tool, args):
    return (await session(server, bearer, (tool, args)))[0]


async def test_unauthenticated_gets_401_with_metadata_pointer(server):
    async with httpx2.AsyncClient() as http:
        r = await http.post(server.resource, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401
        assert "resource_metadata=" in r.headers["www-authenticate"]
        meta = (await http.get(f"{server.base}/.well-known/oauth-protected-resource/mcp")).json()
        assert meta["resource"] == server.resource and meta["authorization_servers"] == [ISS]
        assert (await http.get(f"{server.base}/health")).text == "ok"


async def test_valid_token_creates_a_new_user_and_tools_work(server, test_db):
    await ensure_user_indexes(test_db)
    good = token(server.resource, **{f"{NS}client_name": "Claude", f"{NS}email": "alice@example.com"})
    before = await test_db.users.count_documents({})
    r = await call(server, good, "get_profile", {})
    assert not r.is_error, r.content
    assert r.structured_content["goals"] == [] and r.structured_content["top_tags"] == []
    user = await test_db.users.find_one({"identities.subject": "google-oauth2|alice"})
    assert user["identities"][0]["provider"] == ISS and user["source"] == "claude"
    assert user["email"] == "alice@example.com" and user["daily_push"] is False
    assert await test_db.users.count_documents({}) == before + 1

    fb = await call(server, good, "record_feedback", {"post_id": str(P_DRONES), "reaction": "like"})
    assert not fb.is_error
    user = await test_db.users.find_one({"_id": user["_id"]})
    assert user["feedback"][-1]["channel"] == "claude"
    assert user["interests"]["category_scores"]["ua"]["defense_tech"] == 1
    assert await test_db.users.count_documents({}) == before + 1          # same user on the second call
    # Activity is in conversations, not in audit_log. This protocol version has no Mcp-Session-Id, so calls
    # within the idle window share one document.
    [doc] = await test_db.conversations.find({"user_id": user["_id"]}).to_list(None)
    assert doc["channel"] == "claude" and doc["mcp_session_id"] is None
    assert [m["tool"] for m in doc["messages"] if m["role"] == "tool"] == ["get_profile", "record_feedback"]
    assert not await test_db.audit_log.find_one({"name": "record_feedback"})
    accepted = await test_db.audit_log.count_documents({"event": "auth_token_accepted",
                                                        "details.subject": "google-oauth2|alice"})
    assert accepted == 1                                                  # once per token, not per request


async def test_calls_in_a_row_are_one_conversation(server, test_db):
    await ensure_conversation_indexes(test_db)
    bob = token(server.resource, sub="auth0|bob", **{f"{NS}client_name": "Claude"})
    results = await session(server, bob,
                            ("search_posts", {"query": "морські дрони", "days": 30}),
                            ("record_feedback", {"post_id": str(P_DRONES), "reaction": "useful",
                                                 "text": "корисно, дякую"}),
                            ("get_post", {"post_id": str(P_DRONES)}))
    assert not any(r.is_error for r in results)
    user = await test_db.users.find_one({"identities.subject": "auth0|bob"})
    [doc] = await test_db.conversations.find({"user_id": user["_id"]}).to_list(None)
    assert doc["client"]["name"] and doc["part"] == 1 and doc["session_key"].startswith("anon:")
    roles = [(m["role"], m.get("tool") or m.get("via")) for m in doc["messages"]]
    assert roles == [("tool", "search_posts"), ("user", "chat"), ("user", "search_query"),
                     ("tool", "record_feedback"), ("user", "chat"), ("user", "feedback_text"),
                     ("tool", "get_post"), ("user", "chat")]
    assert doc["message_count"] == 8
    search, said, query = doc["messages"][:3]
    assert said["content"] == "повідомлення для search_posts" and said["tool_message_id"] == search["id"]
    assert query["content"] == "морські дрони" and query["tool_message_id"] == search["id"]
    assert "user_message" not in search["args"]                            # stored once, as its own message
    assert search["result"]["ok"] and str(P_DRONES) in search["result"]["post_ids"]
    assert doc["messages"][5]["content"] == "корисно, дякую"

    await call(server, bob, "get_profile", {})                  # another connection, inside the idle window
    assert await test_db.conversations.count_documents({"user_id": user["_id"]}) == 1
    assert not await test_db.audit_log.find_one({"kind": "tool"})


@pytest.mark.parametrize("bad", ["wrong_audience", "wrong_issuer", "expired", "wrong_key", "garbage"])
async def test_bad_tokens_are_rejected(server, bad):
    t = {"wrong_audience": lambda: token("https://other.example/mcp"),
         "wrong_issuer": lambda: token(server.resource, iss="https://evil.auth0.com/"),
         "expired": lambda: token(server.resource, exp_in=-3600),
         "wrong_key": lambda: token(server.resource, key=OTHER_KEY),
         "garbage": lambda: "not-a-jwt"}[bad]()
    async with httpx2.AsyncClient() as http:
        r = await http.post(server.resource, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            headers={"Authorization": f"Bearer {t}",
                                     "Accept": "application/json, text/event-stream"})
        assert r.status_code == 401


async def test_rejected_tokens_are_security_events(server, test_db):
    async with httpx2.AsyncClient() as http:
        await http.post(server.resource, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"Authorization": "Bearer not-a-jwt", "Accept": "application/json, text/event-stream"})
    entry = await test_db.audit_log.find_one({"event": "auth_failed"}, sort=[("at", -1)])
    assert entry["details"]["reason"] == "DecodeError" and set(entry) >= {"at", "event", "user_id", "details"}
