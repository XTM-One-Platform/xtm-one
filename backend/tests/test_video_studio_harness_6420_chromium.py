"""The Chromium half of the Video Studio harness fixes, on loopback fixtures in a real headless Chromium.

* a ``role=menuitem`` step clicks the ``menuitemcheckbox`` a menu draws (the
  session's step 13, "Layered layout (by entity category)"), its own role
  winning when both are there, and a ``button`` step reaches a ``menuitem``
  only by its exact, unique name;
* a ``type`` step into a password field its selector does not name is
  refused before a key is typed;
* the browser tool's ``evaluate`` stops a script that blocks the page past its
  timeout, and the session keeps its login cookie through it and through a
  browser that died.

Skipped where Playwright's Chromium is not installed; the ``Browser tool
(Chromium)`` CI job installs it and sets ``REQUIRE_CHROMIUM=1``, which turns
the skip into a failure.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest

from app.services import browser_tool, web_fetch
from app.services.video_studio import capture
from app.services.video_studio.spec import TakeScript

pytestmark = pytest.mark.chromium

MENU = """<!doctype html><html><body style="font: 16px sans-serif; margin: 40px">
<div role="menu" aria-label="Layout">
  <div role="menuitemcheckbox" aria-checked="false" tabindex="0" id="layered" style="padding: 8px"
    onclick="window.clicked = 'layered'">Layered layout (by entity category)</div>
  <div role="menuitem" tabindex="0" id="tree" style="padding: 8px" onclick="window.clicked = 'tree-item'">Tree</div>
  <div role="menuitemcheckbox" aria-checked="false" tabindex="0" id="tree-check" style="padding: 8px"
    onclick="window.clicked = 'tree-check'">Tree</div>
  <div role="menuitem" tabindex="0" id="export" style="padding: 8px" onclick="window.clicked = 'export'">Export</div>
  <div role="menuitem" tabindex="0" style="padding: 8px" onclick="window.clicked = 'export-png'">Export as PNG</div>
  <div role="menuitem" tabindex="0" id="fit" style="padding: 8px" onclick="window.clicked = 'fit'">Fit graph</div>
</div>
<input id="secret" type="password" aria-label="Passphrase" style="width: 200px; height: 30px">
</body></html>"""


async def _on_page(run) -> None:
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 900, "height": 600})
                await page.set_content(MENU)
                script = TakeScript.model_validate({"steps": [{"action": "wait", "seconds": 0}], "pace": "fast", "settle_ms": 0})
                recorder = capture._Recorder(SimpleNamespace(page=page), script, viewport=(900, 600), max_seconds=120)
                recorder.t_start = time.time()
                await run(page, recorder)
            finally:
                await browser.close()
    except Exception as exc:
        _skip_without_chromium(exc)


def _skip_without_chromium(exc: Exception) -> None:
    text = str(exc)
    if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
        raise exc
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
    pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


def _step(**fields):
    return TakeScript.model_validate({"steps": [fields]}).steps[0]


async def test_a_menuitem_step_clicks_the_menuitemcheckbox_the_menu_draws():
    async def run(page, recorder):
        await recorder.run_step(13, _step(action="click", role="menuitem", name="Layered layout (by entity category)"))
        assert await page.evaluate("() => window.clicked") == "layered"
        assert recorder.events[-1]["matched_role"] == "menuitemcheckbox"

    await _on_page(run)


async def test_the_steps_own_role_wins_over_its_family():
    async def run(page, recorder):
        await recorder.run_step(0, _step(action="click", role="menuitem", name="Tree", timeout=2))
        assert await page.evaluate("() => window.clicked") == "tree-item"
        assert "matched_role" not in recorder.events[-1]
        await recorder.run_step(1, _step(action="click", role="menuitemcheckbox", name="Tree", timeout=2))
        assert await page.evaluate("() => window.clicked") == "tree-check"

    await _on_page(run)


async def test_a_button_step_reaches_a_menuitem_only_by_its_exact_unique_name():
    async def run(page, recorder):
        await recorder.run_step(0, _step(action="click", role="button", name="Fit graph", timeout=2))
        assert await page.evaluate("() => window.clicked") == "fit"
        assert recorder.events[-1]["matched_role"] == "menuitem"
        # "Export" names two entries by a substring ("Export", "Export as PNG"), one exactly: the exact one.
        await recorder.run_step(1, _step(action="click", role="button", name="Export", timeout=2))
        assert await page.evaluate("() => window.clicked") == "export"
        # A name no entry has exactly is never taken from a family.
        with pytest.raises(Exception, match="Timeout"):
            await recorder.run_step(2, _step(action="click", role="button", name="Export as", timeout=1))

    await _on_page(run)


async def test_a_type_step_into_a_password_field_is_refused_before_a_key_is_typed():
    async def run(page, recorder):
        with pytest.raises(capture.CredentialRefusedError, match="step 4 types into a password field"):
            await recorder.run_step(4, _step(action="type", selector="#secret", text="hunter22"))
        await page.focus("#secret")
        with pytest.raises(capture.CredentialRefusedError):
            await recorder.run_step(5, _step(action="type", text="hunter22"))
        assert await page.input_value("#secret") == ""
        assert not any("hunter22" in str(event) for event in recorder.events)

    await _on_page(run)


# ── The browser tool: a hung script, a dead browser ───────────────────────

STAND_IN = "app.localhost"


class _Site(BaseHTTPRequestHandler):
    """``/login`` sets the session cookie, ``/check`` says whether the request carried it, ``/busy`` blocks on demand."""

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        cookie = "sid=signed-in" in (self.headers.get("Cookie") or "")
        body = {
            "/login": "<title>Signed in</title>signed in",
            "/check": f"<title>check</title>{'carried the login cookie' if cookie else 'signed out'}",
        }.get(path, "<title>page</title>page")
        data = body.encode()
        self.send_response(200)
        if path == "/login":
            self.send_header("Set-Cookie", "sid=signed-in; Path=/; HttpOnly")
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


@pytest.fixture
def site() -> Iterator[int]:
    server = _Server(("127.0.0.1", 0), _Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
async def key(monkeypatch) -> AsyncIterator[str]:
    async def validate(url: str) -> str:
        if urlparse(url).hostname == STAND_IN:
            return url
        return await web_fetch.validate_outbound_url_async(url)

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", validate)
    monkeypatch.setattr(browser_tool, "_ensure_cleanup_running", AsyncMock())
    session_key = f"test:{uuid.uuid4().hex}"
    try:
        await browser_tool._get_or_create_session(session_key)
    except RuntimeError as exc:
        _skip_without_chromium(exc)
    try:
        yield session_key
    finally:
        await browser_tool._close_session(session_key, forget=True)
        browser_tool._notices.pop(session_key, None)


async def test_a_script_blocking_the_page_is_stopped_at_its_timeout_and_the_session_stays_signed_in(key, site):
    await browser_tool.browser_navigate(key, f"http://{STAND_IN}:{site}/login")

    started = time.monotonic()
    with pytest.raises(browser_tool.EvaluateTimeoutError) as stopped:
        await browser_tool.browser_evaluate(key, "() => { const end = Date.now() + 60000; while (Date.now() < end) {} }", 2)
    assert time.monotonic() - started < 15
    assert "did not return within 2 s" in str(stopped.value) and "it was stopped" in str(stopped.value)

    assert "carried the login cookie" in await _checked(key, site)
    assert browser_tool.evaluate_timeout(None) == browser_tool.EVALUATE_TIMEOUT_SECONDS
    assert browser_tool.evaluate_timeout(600) == browser_tool.MAX_EVALUATE_TIMEOUT_SECONDS


async def test_a_browser_that_died_comes_back_with_its_cookies_and_says_so(key, site):
    await browser_tool.browser_navigate(key, f"http://{STAND_IN}:{site}/login")
    await browser_tool._sessions[key].browser.close()

    checked = await _checked(key, site)

    assert "carried the login cookie" in checked
    notice = browser_tool.take_notice(key)
    assert notice is not None and notice.startswith("Session re-created, cookies restored (1)")
    assert browser_tool.take_notice(key) is None


async def test_closing_the_session_signs_it_out(key, site):
    await browser_tool.browser_navigate(key, f"http://{STAND_IN}:{site}/login")
    await browser_tool.browser_close(key)

    assert "signed out" in await _checked(key, site)
    assert browser_tool.take_notice(key) is None


async def _checked(key: str, port: int) -> str:
    await browser_tool.browser_navigate(key, f"http://{STAND_IN}:{port}/check")
    return await browser_tool.browser_get_content(key)
