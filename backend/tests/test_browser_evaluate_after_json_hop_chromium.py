"""The first evaluate after a same-origin navigate reads the document the navigate answered, in a real Chromium.

UX run cdecbc8a (9 Nov 2026, lab-light): a navigate to a JSON route (``/api/v1/health``), then a navigate to a
single-page route answered that route while the next ``evaluate`` read ``location.pathname`` of the JSON page; and a
``/login#token=`` navigate after another path answered while the next ``evaluate`` still read ``/login``. A local
HTTP server on 127.0.0.1 serves the three kinds of response; the sequence runs three times in a row.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it and sets
``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool
from app.services.builtin_tools.web import build_browser_tool
from app.tools.base import ToolCall

pytestmark = pytest.mark.chromium

KEY = "test:evaluate-after-json-hop"

SPA = b"""<!doctype html><html class="light"><head><title>Lab</title></head><body><div id="root"></div>
<script>
// A single-page shell: it routes on the client, and its sign-in callback reads the token from the fragment,
// stores it and moves to / with history.replaceState, as the lab's /login#token= does.
if (location.pathname === '/login' && location.hash.startsWith('#token=')) {
  localStorage.setItem('copilot_token', location.hash.slice(7));
  history.replaceState(null, '', '/');
}
document.getElementById('root').textContent = 'route ' + location.pathname;
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/api/v1/health"):
            body, kind = b'{"status":"ok"}', "application/json"
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
                await asyncio.sleep(0.05)  # the DNS read of the outbound URL check: the navigate yields here
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


async def _pathname() -> str:
    return (await browser_tool.browser_evaluate(KEY, "() => location.pathname")).rsplit("\n", 1)[-1]


@pytest.mark.parametrize("turn", range(3))
async def test_an_evaluate_after_a_json_route_reads_the_single_page_route(session, origin, turn):
    await browser_tool.browser_navigate(KEY, f"{origin}/api/v1/health")
    answer = await browser_tool.browser_navigate(KEY, f"{origin}/flows/fleets?vis=all")

    assert answer.startswith(f"Navigated to: {origin}/flows/fleets?vis=all\n")
    assert await _pathname() == "/flows/fleets"
    assert (await browser_tool.browser_evaluate(KEY, "() => document.documentElement.className")).endswith("light")


@pytest.mark.parametrize("turn", range(3))
async def test_an_evaluate_after_a_sign_in_fragment_reads_the_signed_in_page(session, origin, turn):
    await browser_tool.browser_navigate(KEY, f"{origin}/api/v1/health")
    await browser_tool.browser_navigate(KEY, f"{origin}/login#token=t{turn}")

    assert await _pathname() == "/"
    assert (await browser_tool.browser_evaluate(KEY, "() => localStorage.getItem('copilot_token')")).endswith(
        f"t{turn}"
    )


@pytest.mark.parametrize("turn", range(3))
async def test_a_navigate_and_an_evaluate_called_in_one_turn_run_in_that_order(session, origin, turn):
    # The agent loop runs a turn's tool calls concurrently; the evaluate used to take the page while the navigate
    # was still checking its URL and read the JSON page the navigate was leaving.
    _defs, executor = build_browser_tool(KEY)
    await browser_tool.browser_navigate(KEY, f"{origin}/api/v1/health")

    def call(n: int, arguments: dict):
        return executor.execute_tool(ToolCall(id=f"call-{n}", name="browser", arguments=arguments))

    navigated, evaluated = await asyncio.gather(
        call(1, {"action": "navigate", "url": f"{origin}/flows/fleets?vis=all"}),
        call(2, {"action": "evaluate", "expression": "() => location.pathname"}),
    )

    assert f"Navigated to: {origin}/flows/fleets?vis=all" in str(getattr(navigated, "content", navigated))
    assert str(getattr(evaluated, "content", evaluated)).rstrip().endswith("/flows/fleets")


@pytest.mark.parametrize("turn", range(3))
async def test_a_sign_in_fragment_on_a_page_already_at_login_reads_the_signed_in_page(session, origin, turn):
    await browser_tool.browser_navigate(KEY, f"{origin}/login")
    await browser_tool.browser_navigate(KEY, f"{origin}/login#token=u{turn}")

    assert await _pathname() == "/"
