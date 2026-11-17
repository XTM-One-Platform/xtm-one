"""``network`` in a real Chromium: offline fails new requests and fires ``offline``, online gives them back (#11230).

A local HTTP server streams a chunked response that never ends (the shape of an SSE events stream). With
``offline=True`` ``navigator.onLine`` is false, ``offline`` fires and a new fetch fails; with ``offline=False``
the page receives ``online`` and a new fetch answers again. Chromium's offline emulation does not cut a connection
already open (the stream keeps reading): the tool's answer says so, and a page's reconnect logic reacts to the
``offline`` event.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job
installs it and sets ``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool

pytestmark = pytest.mark.chromium

KEY = "agent:conv"


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """``/stream`` sends a chunk every 100 ms until the client goes; anything else answers ``ok``."""
    request = await reader.readuntil(b"\r\n\r\n")
    path = request.split(b" ", 2)[1]
    try:
        if path == b"/stream":
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            while True:
                writer.write(b"6\r\ndata:\n\r\n")
                await writer.drain()
                await asyncio.sleep(0.1)
        body = b"<!doctype html><title>net</title>" if path == b"/" else b"ok"
        kind = b"text/html" if path == b"/" else b"text/plain"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: "
            + kind
            + b"\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


@pytest.fixture
async def session(monkeypatch) -> AsyncIterator[tuple[browser_tool.BrowserSession, str]]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - Playwright is a dev dependency
        pytest.skip("Playwright is not installed")
    monkeypatch.setattr(browser_tool, "_kept_states", {})
    monkeypatch.setattr(browser_tool, "_kept_pages", {})
    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
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
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(origin + "/")
                held = browser_tool.BrowserSession(pw=None, browser=browser, context=context, page=page)
                monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=held))
                yield held, origin
            finally:
                await browser.close()
    finally:
        server.close()


OPEN_STREAM_JS = """() => {
  window.__events = [];
  addEventListener('offline', () => __events.push('offline'));
  addEventListener('online', () => __events.push('online'));
  window.__stream = 'open';
  fetch('/stream').then(async (r) => {
    const reader = r.body.getReader();
    try { for (;;) { const { done } = await reader.read(); if (done) { __stream = 'done'; return; } } }
    catch (e) { __stream = 'errored'; }
  }).catch(() => { __stream = 'refused'; });
}"""
FETCH_JS = "() => fetch('/ping').then(r => r.text()).catch(e => 'failed: ' + e.message)"


async def test_offline_fails_new_requests_and_fires_the_events_until_back_online(session):
    held, _origin = session
    await held.page.evaluate(OPEN_STREAM_JS)
    await held.page.wait_for_timeout(300)
    assert await held.page.evaluate("() => __stream") == "open"

    await browser_tool.browser_network(KEY, True)

    assert await held.page.evaluate("() => navigator.onLine") is False
    assert (await held.page.evaluate(FETCH_JS)).startswith("failed:")
    assert await held.page.evaluate("() => __events") == ["offline"]

    await browser_tool.browser_network(KEY, False)

    assert await held.page.evaluate("() => navigator.onLine") is True
    assert await held.page.evaluate(FETCH_JS) == "ok"
    assert await held.page.evaluate("() => __events") == ["offline", "online"]
