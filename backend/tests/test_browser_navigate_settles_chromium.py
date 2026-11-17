"""A navigate answers only once the page settled, so the next evaluate reads the new document (real Chromium).

UX runs 544d96bd, b26cd84b and 4f7d0136 (14-15 Nov 2026, xtm-one lab): a navigate answered the new URL while the
next ``evaluate`` or ``click`` read the previous document: a single-page route the application only renders after a
fetch, a query string the application rewrites after its load, a ``/login#token=`` callback that stores the token
after a fetch and moves to ``/``. A local HTTP server on 127.0.0.1 serves a shell that routes late, as the web app
does; each case is followed by an ``evaluate`` that must read the new document.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it and sets
``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool

pytestmark = pytest.mark.chromium

KEY = "test:navigate-settles"

# A shell that renders its route only after a session fetch and a client-side delay, then (for the routes that do)
# moves itself with history: /restore pushes the route it "restored", /list writes back its default query string,
# /login#token= stores the token and replaces itself with /.
SPA = b"""<!doctype html><html><head><title>Lab</title></head><body><div id="root">loading</div>
<script>
const render = () => { document.getElementById('root').textContent = 'route ' + location.pathname + location.search; };
fetch('/api/me').then(() => setTimeout(() => {
  if (location.pathname === '/restore') history.pushState(null, '', '/restored/route');
  if (location.pathname === '/list' && !location.search) history.replaceState(null, '', '/list?vis=all');
  if (location.pathname === '/login' && location.hash.startsWith('#token=')) {
    localStorage.setItem('copilot_token', location.hash.slice(7));
    history.replaceState(null, '', '/');
  }
  render();
}, 150));
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/api/me"):
            time.sleep(0.3)  # the session check the shell awaits before it routes
            body, kind = b"{}", "application/json"
        elif self.path.startswith("/moving"):
            body = (
                b"<html><head><title>Moving</title></head><body><script>let n = 0;"
                b" setInterval(() => history.replaceState(null, '', '/moving?tick=' + (++n)), 100);</script></body></html>"
            )
            kind = "text/html"
        elif self.path.startswith("/plain"):
            body, kind = b"<html><head><title>Plain</title></head><body>plain page</body></html>", "text/html"
        else:
            body, kind = SPA, "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return None


@pytest.fixture
def origin() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
async def session(monkeypatch) -> AsyncIterator[browser_tool.BrowserSession]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - Playwright is a dev dependency
        pytest.skip("Playwright is not installed")
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except Exception as exc:
            if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
                raise
            if os.environ.get("REQUIRE_CHROMIUM") == "1":
                pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
            pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
        try:
            context = await browser.new_context(viewport={"width": 800, "height": 600})
            page = await context.new_page()
            held = browser_tool.BrowserSession(pw=None, browser=browser, context=context, page=page)
            held.documents.watch(page)

            async def allow(url: str) -> str:
                return url

            monkeypatch.setattr(browser_tool, "_sessions", {})
            monkeypatch.setattr(browser_tool, "_url_check", lambda _session: allow)
            monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=held))
            monkeypatch.setattr(browser_tool, "_remember_state", AsyncMock(return_value={}))
            monkeypatch.setattr(browser_tool, "_share_landed_page", AsyncMock(return_value=None))
            monkeypatch.setattr(browser_tool, "_keep_after_settle", lambda *_a: None)
            yield held
        finally:
            await browser.close()


async def _read(expression: str) -> str:
    return (await browser_tool.browser_evaluate(KEY, expression)).rsplit("\n", 1)[-1]


async def test_a_spa_push_state_route_is_the_document_the_next_evaluate_reads(session, origin):
    await browser_tool.browser_navigate(KEY, f"{origin}/plain")
    answer = await browser_tool.browser_navigate(KEY, f"{origin}/restore")

    assert answer.startswith(f"Navigated to: {origin}/restored/route\n")
    assert await _read("() => location.pathname") == "/restored/route"
    assert await _read("() => document.getElementById('root').textContent") == "route /restored/route"


async def test_a_query_only_change_is_read_by_the_next_evaluate(session, origin):
    await browser_tool.browser_navigate(KEY, f"{origin}/list?vis=mine")
    answer = await browser_tool.browser_navigate(KEY, f"{origin}/list?vis=team")

    assert answer.startswith(f"Navigated to: {origin}/list?vis=team\n")
    assert await _read("() => location.search") == "?vis=team"
    assert await _read("() => document.getElementById('root').textContent") == "route /list?vis=team"


async def test_a_default_query_the_application_writes_back_is_named_by_the_answer(session, origin):
    answer = await browser_tool.browser_navigate(KEY, f"{origin}/list")

    assert answer.startswith(f"Navigated to: {origin}/list?vis=all\n")
    assert await _read("() => document.getElementById('root').textContent") == "route /list?vis=all"


async def test_a_hash_login_redirect_is_signed_in_at_the_next_evaluate(session, origin):
    await browser_tool.browser_navigate(KEY, f"{origin}/plain")
    answer = await browser_tool.browser_navigate(KEY, f"{origin}/login#token=abc")

    assert answer.startswith(f"Navigated to: {origin}/\n")
    assert await _read("() => location.pathname") == "/"
    assert await _read("() => localStorage.getItem('copilot_token')") == "abc"
    assert await _read("() => document.getElementById('root').textContent") == "route /"


async def test_a_full_navigation_to_another_document_is_read_by_the_next_evaluate(session, origin):
    await browser_tool.browser_navigate(KEY, f"{origin}/restore")
    answer = await browser_tool.browser_navigate(KEY, f"{origin}/plain")

    assert answer.startswith(f"Navigated to: {origin}/plain\nTitle: Plain")
    assert await _read("() => document.body.textContent") == "plain page"


async def test_a_page_that_keeps_moving_is_answered_at_the_bound_with_the_url_it_shows(session, origin, monkeypatch):
    monkeypatch.setattr(browser_tool, "_NAVIGATE_SETTLE_SECONDS", 1.0)
    started = asyncio.get_running_loop().time()
    answer = await browser_tool.browser_navigate(KEY, f"{origin}/moving")

    assert asyncio.get_running_loop().time() - started < 10
    assert answer.startswith(f"Navigated to: {origin}/moving?tick=")
    assert "Note: the page was still changing its URL after 1 s" in answer
