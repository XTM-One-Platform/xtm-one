"""The browser tool's redirect guard against a real headless Chromium.

Loopback servers only, no network. Chromium resolves every ``*.localhost``
name to the loopback address itself, so ``a.localhost`` and ``b.localhost``
stand in for two public sites: the policy double allows those two names and
hands every other URL to the real outbound URL policy, which refuses
``localhost`` (after DNS) and ``127.0.0.1``. The route filter lets
``localhost`` through (it resolves nothing), so a refusal of that hop is the
guard's.

Skipped where Playwright's Chromium is not installed; the ``Browser tool
(Chromium)`` CI job installs it and sets ``REQUIRE_CHROMIUM=1``, which turns
a missing browser into a failure: a skipped check there is a green build that
checked nothing.
"""

from __future__ import annotations

import asyncio
import base64
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.pinned_request import REFUSED_MESSAGE
from app.services import browser_tool, web_fetch
from app.services.builtin_tools import build_browser_tool
from app.tools.base import ToolCall

pytestmark = pytest.mark.chromium

STAND_INS = {"a.localhost", "b.localhost"}
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
#: A service worker that fetches the URL a page posts to it and answers how it went.
_SERVICE_WORKER = b"""
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('message', event => {
  fetch(event.data, {mode: 'no-cors'}).then(() => event.source.postMessage('fetched'), () => event.source.postMessage('failed'));
});
"""


class _Handler(BaseHTTPRequestHandler):
    """A redirecting site: ``/r?to=`` answers 302, ``/chainN`` counts down, ``/loopN`` never ends."""

    hits: list[str]

    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, body: bytes = b"", *, content_type: str = "text/html", location: str = "") -> None:
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.ico":
            return self._send(404)
        self.hits.append(f"{self.headers.get('Host')}{self.path}")
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        path = parsed.path
        if path == "/r":
            return self._send(302, location=query["to"])
        if path.startswith("/loop"):
            return self._send(302, location=f"/loop{int(path[5:] or 0) + 1}")
        if path.startswith("/chain") and int(path[6:] or 0) > 0:
            return self._send(302, location=f"/chain{int(path[6:]) - 1}")
        if path.endswith(".png"):
            return self._send(200, _PNG, content_type="image/png")
        if path == "/slow-me":
            time.sleep(0.3)  # a session check the page awaits before it stores its token
            return self._send(200, b"{}", content_type="application/json")
        if path == "/worker.js":
            return self._send(200, _SERVICE_WORKER, content_type="application/javascript")
        pages = {
            "/with-image": f"<img src='{query.get('src')}'>",
            "/with-frame": f"<iframe src='{query.get('src')}'></iframe>",
            "/with-fetch": (
                f"<script>fetch('{query.get('src')}', {{mode: 'no-cors'}})"
                ".then(() => document.title = 'fetched', () => document.title = 'failed')</script>"
            ),
            "/with-link": f"<a id='next' href='{query.get('href')}'>next</a>",
            # Reads its fragment at load only, as an app that consumes a callback fragment on mount.
            "/reads-fragment": "<script>document.title = location.hash || 'none'</script>",
            # Signs in from its fragment after its load and a fetch, then moves itself, as the web app's /login does.
            "/signs-in": (
                "<script>addEventListener('load', () => setTimeout(() => {"
                " const token = location.hash.slice(1); if (!token) return;"
                " fetch('/slow-me').then(() => { localStorage.setItem('tok', token);"
                " history.replaceState(null, '', '/home'); }); }, 100));</script>"
            ),
        }
        body = pages.get(path, f"page {path}")
        return self._send(200, f"<html><head><title>{path}</title></head><body>{body}</body></html>".encode())


class _Server(ThreadingHTTPServer):
    # The default backlog of 5 refuses part of a burst of parallel connections (Windows).
    request_queue_size = 128
    daemon_threads = True


@pytest.fixture
def site() -> Iterator[tuple[int, list[str]]]:
    hits: list[str] = []
    server = _Server(("127.0.0.1", 0), type("Handler", (_Handler,), {"hits": hits}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def policy(monkeypatch) -> None:
    """The real outbound URL policy, except for the two stand-ins for public sites."""

    async def validate(url: str) -> str:
        if urlparse(url).hostname in STAND_INS:
            return url
        return await web_fetch.validate_outbound_url_async(url)

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", validate)


@pytest.fixture
async def session(policy, monkeypatch) -> AsyncIterator[tuple[str, browser_tool.BrowserSession]]:
    # The idle sweep outlives the test's event loop; each test closes its own session.
    monkeypatch.setattr(browser_tool, "_ensure_cleanup_running", AsyncMock())
    key = f"test:{uuid.uuid4().hex}"
    try:
        launched = await browser_tool._launch_session(key)
    except RuntimeError as exc:
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    try:
        yield key, launched
    finally:
        await browser_tool._close_session(key)


def _requested(hits: list[str], path: str) -> list[str]:
    return [hit for hit in hits if urlparse(f"http://{hit}").path == path]


async def test_a_redirect_the_policy_allows_is_followed_as_before(session, site):
    key, _ = session
    port, _hits = site

    result = await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/r?to=http://b.localhost:{port}/landing")

    assert result.startswith(f"Navigated to: http://b.localhost:{port}/landing\nTitle: /landing" + "\nNote: not the requested ")


async def test_a_long_redirect_chain_the_policy_allows_is_followed_as_before(session, site):
    key, _ = session
    port, hits = site

    result = await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/chain12")

    assert result.startswith(f"Navigated to: http://a.localhost:{port}/chain0\nTitle: /chain0" + "\nNote: not the requested ")
    assert len([hit for hit in hits if "/chain" in hit]) == 13


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"], ids=["hostname-refused-after-dns", "literal-ip"])
async def test_a_redirect_to_an_address_the_policy_refuses_is_never_requested(session, site, host):
    key, launched = session
    port, hits = site
    await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/before")
    hop = f"http://{host}:{port}/secret"

    result = await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/r?to={hop}")
    direct = await browser_tool.browser_navigate(key, hop)

    reason = direct.removeprefix("Navigation blocked by URL policy: ")
    assert reason == REFUSED_MESSAGE
    assert result == f"Navigation blocked by URL policy: redirected to {hop}: {reason}"
    assert _requested(hits, "/secret") == []
    assert launched.page.url == f"http://a.localhost:{port}/before"  # the page stayed where it was
    assert "page /before" in await browser_tool.browser_get_content(key)


async def test_a_redirect_refused_after_allowed_hops_stops_at_the_refused_one(session, site):
    key, _ = session
    port, hits = site
    hop = f"http://localhost:{port}/secret"
    second = f"http://b.localhost:{port}/r?to={hop}"

    result = await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/r?to={second}")

    assert result == f"Navigation blocked by URL policy: redirected to {hop}: {REFUSED_MESSAGE}"
    assert [urlparse(f"http://{hit}").hostname for hit in hits] == ["a.localhost", "b.localhost"]


async def test_a_redirect_loop_is_stopped_by_chromium_as_before(session, site):
    key, _ = session
    port, hits = site
    _defs, executor = build_browser_tool(key)

    result = await executor.execute_tool(
        ToolCall(id="call-1", name="browser", arguments={"action": "navigate", "url": f"http://a.localhost:{port}/loop0"})
    )

    assert result.is_error is True
    assert "net::ERR_TOO_MANY_REDIRECTS" in result.content
    # Chromium's own limit, unchanged by the guard: the first request and 20 redirects at most.
    assert 1 < len(hits) <= 21


async def test_a_click_through_a_refused_redirect_stays_on_the_page_and_says_why(session, site):
    key, launched = session
    port, hits = site
    hop = f"http://localhost:{port}/secret"
    page_url = f"http://a.localhost:{port}/with-link?href=/r?to={hop}"
    await browser_tool.browser_navigate(key, page_url)
    reason = (await browser_tool.browser_navigate(key, hop)).removeprefix("Navigation blocked by URL policy: ")

    result = await browser_tool.browser_click(key, "#next")

    assert result == (
        f"Clicked '#next'. Current page: {page_url} — /with-link\n"
        f"Navigation blocked by URL policy: redirected to {hop}: {reason}"
    )
    assert _requested(hits, "/secret") == []
    assert launched.page.url == page_url


@pytest.mark.parametrize("page", ["with-image", "with-frame", "with-fetch"])
async def test_a_subresource_redirect_to_a_refused_address_is_never_requested(session, site, page):
    key, launched = session
    port, hits = site
    source = f"http://a.localhost:{port}/r?to=http://localhost:{port}/secret.png"

    await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/{page}?src={source}")
    await launched.page.wait_for_load_state("load")
    if page == "with-fetch":
        await launched.page.wait_for_function("document.title !== '/with-fetch'")
        assert await launched.page.title() == "failed"

    assert len(_requested(hits, "/r")) == 1
    assert _requested(hits, "/secret.png") == []


async def test_a_subresource_redirect_the_policy_allows_still_loads(session, site):
    key, launched = session
    port, hits = site
    source = f"http://a.localhost:{port}/r?to=http://b.localhost:{port}/pixel.png"

    await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/with-image?src={source}")
    await launched.page.wait_for_load_state("load")

    assert await launched.page.evaluate("document.images[0].naturalWidth") == 1
    assert len(_requested(hits, "/pixel.png")) == 1


async def test_a_service_worker_redirect_is_held_to_the_policy_too(session, site):
    key, launched = session
    port, hits = site
    await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/start")
    await launched.page.evaluate("navigator.serviceWorker.register('/worker.js').then(() => navigator.serviceWorker.ready)")
    await launched.page.reload()
    ask = """url => new Promise(resolve => {
        navigator.serviceWorker.onmessage = message => resolve(message.data);
        navigator.serviceWorker.controller.postMessage(url);
    })"""

    allowed = await launched.page.evaluate(ask, f"http://a.localhost:{port}/r?to=http://b.localhost:{port}/allowed")
    refused = await launched.page.evaluate(ask, f"http://a.localhost:{port}/r?to=http://localhost:{port}/secret")

    assert (allowed, refused) == ("fetched", "failed")
    assert len(_requested(hits, "/allowed")) == 1
    assert _requested(hits, "/secret") == []


async def test_a_service_worker_first_url_stays_under_the_route_filter(session, site):
    # Playwright routes a service worker's requests through the context route, so the guard leaves
    # their first URL to the route filter like a page's: an upgrade that stops routing them fails here.
    key, launched = session
    port, hits = site
    await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/start")
    await launched.page.evaluate("navigator.serviceWorker.register('/worker.js').then(() => navigator.serviceWorker.ready)")
    await launched.page.reload()

    refused = await launched.page.evaluate(
        """url => new Promise(resolve => {
            navigator.serviceWorker.onmessage = message => resolve(message.data);
            navigator.serviceWorker.controller.postMessage(url);
        })""",
        f"http://127.0.0.1:{port}/direct-secret",
    )

    assert refused == "failed"
    assert _requested(hits, "/direct-secret") == []


async def test_a_fragment_only_navigate_loads_the_page_again_to_read_it(session, site):
    # A signed-in /login#token= URL opened on a page already at /login (#10458): the page reads it on a load.
    key, launched = session
    port, hits = site
    await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/reads-fragment")

    result = await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/reads-fragment#token=abc")

    assert result.startswith(f"Navigated to: http://a.localhost:{port}/reads-fragment#token=abc\n")
    assert result.endswith(
        "Note: only the fragment differed, so the page was loaded again to read it as a first visit does."
    )
    assert await launched.page.evaluate("document.title") == "#token=abc"
    assert _requested(hits, "/reads-fragment") == [f"a.localhost:{port}/reads-fragment"] * 2


@pytest.mark.parametrize("loop", range(10))
async def test_a_fragment_sign_in_is_read_by_the_next_script(session, site, loop):
    # The page stores its token from the fragment after its load and a fetch, then moves to /home (#10464).
    key, _launched = session
    port, _hits = site
    await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/signs-in")

    result = await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/signs-in#token=abc")

    state = await browser_tool.browser_evaluate(key, "() => [location.href, localStorage.getItem('tok')]")
    assert result.startswith(f"Navigated to: http://a.localhost:{port}/home\n")
    assert f"http://a.localhost:{port}/home" in state and "token=abc" in state


async def test_many_parallel_requests_are_all_released(session, site):
    key, launched = session
    port, _hits = site
    await browser_tool.browser_navigate(key, f"http://a.localhost:{port}/start")

    loaded = await launched.page.evaluate(
        f"""() => Promise.all(Array.from({{length: 40}}, (_, i) =>
            fetch('http://a.localhost:{port}/r?to=http://b.localhost:{port}/n' + i + '.png', {{mode: 'no-cors'}})
              .then(() => 1, () => 0))).then(r => r.reduce((a, b) => a + b, 0))"""
    )

    assert loaded == 40
    await asyncio.sleep(0)
    assert launched.redirects._decisions == set()
