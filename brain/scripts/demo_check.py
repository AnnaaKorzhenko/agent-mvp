"""Before a demo: are the web app and the MCP server up and wired as they should be? Reads only; writes nothing,
needs no keys and no sign-in (the signed-in parts are checked by the 401 they give without one).

    brain/.venv/bin/python -m brain.scripts.demo_check --web https://WEB.onrender.com --mcp https://MCP.onrender.com

One line per check, «ok» or what was wrong; exit code 1 if anything failed. A free Render service sleeps: the first
request may take a minute, hence the long timeout.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

import httpx2

TIMEOUT = 90.0
# The screens a demo walks through, by their data-screen (the labels may be reworded)
SCREENS = ("chat", "digests", "proposals", "search", "map", "profile")


@dataclass
class Check:
    name: str
    problem: str | None = None           # None: ok

    def line(self) -> str:
        return f"{'ok  ' if self.problem is None else 'FAIL'} {self.name}" + (f": {self.problem}" if self.problem else "")


async def _get(c: httpx2.AsyncClient, url: str, **kw) -> httpx2.Response | str:
    try:
        return await c.request(kw.pop("method", "GET"), url, **kw)
    except httpx2.HTTPError as e:
        return f"{type(e).__name__}: {e}"


def _expect(name: str, r, status: int, **more) -> Check:
    if isinstance(r, str):
        return Check(name, r)
    if r.status_code != status:
        return Check(name, f"HTTP {r.status_code}, expected {status}")
    for what, ok in more.items():
        if not ok(r):
            return Check(name, what.replace("_", " "))
    return Check(name)


async def web_checks(c: httpx2.AsyncClient, base: str) -> list[Check]:
    base = base.rstrip("/")
    health, page, sw, manifest, me = await asyncio.gather(
        _get(c, f"{base}/api/health"), _get(c, f"{base}/"), _get(c, f"{base}/sw.js"),
        _get(c, f"{base}/manifest.webmanifest"), _get(c, f"{base}/api/me"))
    ui = _expect("web: the UI and its screens", page, 200)
    if ui.problem is None and (missing := [s for s in SCREENS if f'data-screen="{s}"' not in page.text]):
        ui.problem = f"no screen {', '.join(missing)}"
    return [
        _expect("web: /api/health", health, 200),
        ui,
        _expect("web: /sw.js (push)", sw, 200, not_javascript=lambda r: "javascript" in r.headers.get("content-type", "")),
        _expect("web: the manifest", manifest, 200,
                not_a_manifest=lambda r: "manifest+json" in r.headers.get("content-type", "")),
        _expect("web: /api/me needs a sign-in", me, 401),
    ]


async def mcp_checks(c: httpx2.AsyncClient, base: str) -> list[Check]:
    base = base.rstrip("/")
    url = f"{base}/mcp"
    health, call, meta = await asyncio.gather(
        _get(c, f"{base}/health"), _get(c, url, method="POST"),
        _get(c, f"{base}/.well-known/oauth-protected-resource/mcp"))

    def names_url(r) -> bool:
        try:
            return r.json().get("resource") == url and bool(r.json().get("authorization_servers"))
        except ValueError:
            return False
    return [
        _expect("mcp: /health", health, 200, not_ok=lambda r: r.text.strip() == "ok"),
        _expect("mcp: /mcp needs a sign-in", call, 401,
                no_WWW_Authenticate=lambda r: "resource_metadata" in r.headers.get("www-authenticate", "")),
        _expect("mcp: the OAuth metadata names the MCP URL", meta, 200, wrong_resource_or_no_issuer=names_url),
    ]


async def run(web: str | None, mcp: str | None, transport: httpx2.AsyncBaseTransport | None = None) -> list[Check]:
    async with httpx2.AsyncClient(timeout=TIMEOUT, follow_redirects=True, transport=transport) as c:
        parts = await asyncio.gather(web_checks(c, web) if web else asyncio.sleep(0, []),
                                     mcp_checks(c, mcp) if mcp else asyncio.sleep(0, []))
    return [check for part in parts for check in part]


def main(argv: list[str] | None = None, transport: httpx2.AsyncBaseTransport | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--web", help="the web app's URL, e.g. https://second-brain-b8at.onrender.com")
    ap.add_argument("--mcp", help="the MCP server's URL (without /mcp), e.g. https://agent-mvp-1-jh24.onrender.com")
    args = ap.parse_args(argv)
    if not args.web and not args.mcp:
        ap.error("give --web, --mcp or both")
    checks = asyncio.run(run(args.web, args.mcp, transport))
    for check in checks:
        print(check.line())
    failed = sum(check.problem is not None for check in checks)
    print(f"\n{len(checks) - failed} of {len(checks)} ok" + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
