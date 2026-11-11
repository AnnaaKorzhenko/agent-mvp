"""brain/scripts/demo_check.py against the real web app and MCP server, in process (no network): each URL's host is
routed to its ASGI app."""
from __future__ import annotations

import httpx2
import pytest

from brain.audit.log import AuditLog
from brain.db.repositories import Repos
from brain.mcp_server.auth import Auth0TokenVerifier
from brain.mcp_server.server import create_server, transport_security
from brain.mcp_server.tools import Services
from brain.scripts import demo_check
from brain.web.app import create_app
from brain.web.auth import Auth0Login
from tests.conftest import make_settings

WEB = "http://web.localhost"
MCP = "http://127.0.0.1:8765"
ISS = "https://tenant.example.auth0.com/"


class NoKeys:
    def get_signing_key_from_jwt(self, token):          # no call here carries a token
        raise AssertionError("demo_check must not send a token")


def _no_auth0(request):
    raise AssertionError("demo_check must not reach Auth0")


class ByHost(httpx2.AsyncBaseTransport):
    def __init__(self, apps: dict[str, object]):
        self.apps = {host: httpx2.ASGITransport(app=app) for host, app in apps.items()}
        self.seen: list[tuple[str, str]] = []

    async def handle_async_request(self, request):
        self.seen.append((request.method, str(request.url)))
        return await self.apps[request.url.netloc.decode()].handle_async_request(request)


@pytest.fixture
def apps(test_db):
    web_settings = make_settings(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL=WEB, WEB_CLIENT_ID="web-client",
                                 WEB_CLIENT_SECRET="s3cret", WEB_SESSION_SECRET="x" * 48, TRUST_LEVEL="cloud")
    login = Auth0Login(web_settings, keys=NoKeys(), http=httpx2.AsyncClient(transport=httpx2.MockTransport(_no_auth0)))
    web = create_app(web_settings, Services(settings=web_settings, repos=Repos(test_db), audit=AuditLog(test_db),
                                            token=lambda: None), login)
    settings = make_settings(OAUTH_ISSUER=ISS, PUBLIC_BASE_URL=MCP, TRUST_LEVEL="cloud")
    svc = Services(settings=settings, repos=Repos(test_db), audit=AuditLog(test_db), token=lambda: None)
    mcp = create_server(svc, verifier=Auth0TokenVerifier(ISS, settings.resource_url, keys=NoKeys(),
                                                         on_event=svc.audit.event))
    return {"web.localhost": web,
            "127.0.0.1:8765": mcp.streamable_http_app(transport_security=transport_security(settings))}


def test_all_checks_pass_on_a_healthy_pair(apps, capsys):
    transport = ByHost(apps)
    assert demo_check.main(["--web", WEB, "--mcp", MCP], transport=transport) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and out.count("ok  ") == 9 and "9 of 9 ok" in out
    assert {m for m, _ in transport.seen} == {"GET", "POST"}
    assert [u for m, u in transport.seen if m == "POST"] == [f"{MCP}/mcp"]          # the one unauthenticated call


def test_a_missing_screen_and_a_wrong_mcp_url_fail_with_exit_1(apps, capsys, monkeypatch):
    monkeypatch.setattr(demo_check, "SCREENS", (*demo_check.SCREENS, "nowhere"))
    apps["127.0.0.1:8766"] = apps["127.0.0.1:8765"]       # the server answers, but its metadata names :8765
    assert demo_check.main(["--web", WEB, "--mcp", "http://127.0.0.1:8766"], transport=ByHost(apps)) == 1
    out = capsys.readouterr().out
    assert "FAIL web: the UI and its screens: no screen nowhere" in out
    assert "FAIL mcp: the OAuth metadata names the MCP URL" in out and "7 of 9 ok, 2 failed" in out


def test_an_unreachable_service_is_a_failure_not_a_crash(capsys):
    def down(request):
        raise httpx2.ConnectError("connection refused", request=request)
    assert demo_check.main(["--mcp", MCP], transport=httpx2.MockTransport(down)) == 1
    out = capsys.readouterr().out
    assert out.count("FAIL") == 3 and "ConnectError" in out


def test_it_needs_a_url():
    with pytest.raises(SystemExit):
        demo_check.main([])
