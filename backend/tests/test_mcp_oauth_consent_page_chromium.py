"""The authorization page's script for a product sign-in, in a real headless Chromium.

The page is the one ``_consent_html`` renders, served with the Content-Security-Policy the
platform enforces on it, with and without a URL path prefix. Playwright stands in for the
endpoints it calls and the pages it goes to, so no server and no network are involved
(``*.localhost`` names, every request answered by the route). What is pinned is behaviour, not
script text: a product with an XTM One session is signed in with no click and leaves no history
entry, one without goes to the platform sign-in page, a failure shows a product error (Try again,
or Cancel back to the product with ``error=access_denied``) instead of the MCP consent card, a
session endpoint that keeps refusing stops after one retry and only for that request, and an MCP
client still gets its consent page.

Skipped where Playwright's Chromium is not installed; ``REQUIRE_CHROMIUM=1`` (the CI job of the
``chromium`` marker) turns that into a failure.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import pytest

from app.api.v1 import mcp_oauth
from app.core import csp
from app.core.config import settings

pytestmark = pytest.mark.chromium

XTM = "http://xtm.localhost"
PRODUCT = "http://product.localhost"
CALLBACK = f"{PRODUCT}/callback"
CHALLENGE = "c" * 43


@dataclass
class _Platform:
    """What the stand-in XTM One answers, and what it saw."""

    base: str = ""
    # An HTTP status, or "network" for a request that never gets an answer.
    session_answer: int | str = 200
    hits: list[str] = field(default_factory=list)

    @property
    def root(self) -> str:
        return f"{XTM}{self.base}"

    def authorize_url(self, challenge: str = CHALLENGE) -> str:
        return f"{self.root}/mcp/oauth/authorize?client_id=xtm-platform-opencrq&code_challenge={challenge}"

    def count(self, request: str) -> int:
        return sum(1 for hit in self.hits if hit == request)


@pytest.fixture(params=["", "/xtm-one"], ids=["root", "path-prefix"])
def base(request, monkeypatch) -> str:
    monkeypatch.setattr(settings, "base_path", request.param)
    return request.param


@pytest.fixture
async def browser() -> AsyncIterator[object]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - Playwright is a dev dependency
        pytest.skip("Playwright is not installed")
    async with async_playwright() as playwright:
        try:
            launched = await playwright.chromium.launch()
        except Exception as exc:
            if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
                raise
            if os.environ.get("REQUIRE_CHROMIUM") == "1":
                pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
            pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
        try:
            yield launched
        finally:
            await launched.close()


def _html(*, product_sign_in: bool, challenge: str) -> str:
    return mcp_oauth._consent_html(
        client_name="OpenCRQ",
        client_id="xtm-platform-opencrq",
        redirect_uri=CALLBACK,
        state="s",
        code_challenge=challenge,
        code_challenge_method="S256",
        response_type="code",
        scope="openid email" if product_sign_in else "",
        nonce="n" if product_sign_in else "",
        product_sign_in=product_sign_in,
    )


def _enforced_policy() -> str:
    page = csp.page_policy_for("/mcp/oauth/authorize")
    return csp.build_policy({"type": "http", "headers": [], "path": "/mcp/oauth/authorize"}, page=page)


async def _open(browser, platform: _Platform, *, product_sign_in: bool, token: str | None):
    context = await browser.new_context()
    page = await context.new_page()
    errors: list[str] = []
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)

    async def route(r):
        url = r.request.url
        path = url.split("?")[0]
        platform.hits.append(f"{r.request.method} {path}")
        if path == f"{platform.root}/mcp/oauth/authorize/session":
            if platform.session_answer == "network":
                return await r.abort()
            if platform.session_answer != 200:
                return await r.fulfill(status=int(platform.session_answer), body="{}", content_type="application/json")
            body = json.dumps({"redirect_url": f"{CALLBACK}?code=c&state=s"})
            return await r.fulfill(status=200, body=body, content_type="application/json")
        if path == f"{platform.root}/mcp/oauth/authorize":
            challenge = parse_qs(urlparse(url).query).get("code_challenge", [CHALLENGE])[0]
            return await r.fulfill(
                status=200,
                body=_html(product_sign_in=product_sign_in, challenge=challenge),
                content_type="text/html",
                headers={"Content-Security-Policy": _enforced_policy()},
            )
        if path == f"{platform.root}/api/v1/auth/config":
            return await r.fulfill(status=200, body="{}", content_type="application/json")
        if path == f"{platform.root}/login":
            # The SPA sign-in page: signs the person in, then goes back to the page that sent them.
            script = "localStorage.setItem('copilot_token', 'fresh'); location.href = sessionStorage.getItem('mcp_oauth_return');"
            return await r.fulfill(status=200, body=f"<!doctype html><script>{script}</script>", content_type="text/html")
        if url.startswith(PRODUCT):
            return await r.fulfill(status=200, body="<!doctype html><title>product</title>", content_type="text/html")
        return await r.fulfill(status=404, body="")

    await context.route("**/*", route)
    if token:
        await context.add_init_script(
            f"if (location.origin === {json.dumps(XTM)}) localStorage.setItem('copilot_token', {json.dumps(token)});"
        )
    return page, errors


async def test_a_product_with_a_session_is_signed_in_with_no_click_and_no_history_entry(browser, base):
    platform = _Platform(base=base)
    page, errors = await _open(browser, platform, product_sign_in=True, token="t")
    before = await page.evaluate("history.length")

    await page.goto(platform.authorize_url())
    await page.wait_for_url(f"{CALLBACK}?code=c&state=s")

    assert platform.count(f"POST {platform.root}/mcp/oauth/authorize/session") == 1
    # The authorization page left no entry: Back does not land on it again.
    assert await page.evaluate("history.length") == before + 1
    assert not [e for e in errors if "Content Security Policy" in e]


async def test_a_product_without_a_session_goes_through_the_platform_sign_in(browser, base):
    platform = _Platform(base=base)
    page, _ = await _open(browser, platform, product_sign_in=True, token=None)

    await page.goto(platform.authorize_url())
    await page.wait_for_url(f"{CALLBACK}?code=c&state=s")

    assert platform.count(f"GET {platform.root}/login") == 1
    assert platform.count(f"POST {platform.root}/mcp/oauth/authorize/session") == 1


@pytest.mark.parametrize("answer", [500, "network"])
async def test_a_failed_product_sign_in_shows_a_product_error_not_the_consent_card(browser, answer):
    platform = _Platform(session_answer=answer)
    page, _ = await _open(browser, platform, product_sign_in=True, token="t")

    await page.goto(platform.authorize_url())
    await page.locator("#session-authorize-btn").get_by_text("Try again").wait_for()

    assert await page.locator("#consent-card h2").text_content() == "Sign-in failed"
    assert await page.locator("#js-error").text_content() == "Could not sign you in. Please try again."
    assert not await page.locator("#consent-card .scope-info").is_visible()
    assert not await page.locator("#signing-in").is_visible()

    # Try again calls the session endpoint again; it answers this time.
    platform.session_answer = 200
    await page.locator("#session-authorize-btn").click()
    await page.wait_for_url(f"{CALLBACK}?code=c&state=s")
    assert platform.count(f"POST {platform.root}/mcp/oauth/authorize/session") == 2


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (403, "You cannot sign in to a product while impersonating another user."),
        (400, "Signing in to this product through XTM One is not available."),
    ],
)
async def test_a_refusal_for_good_offers_no_retry(browser, status, message):
    platform = _Platform(session_answer=status)
    page, _ = await _open(browser, platform, product_sign_in=True, token="t")

    await page.goto(platform.authorize_url())
    await page.locator("#js-error").get_by_text(message).wait_for()

    assert not await page.locator("#session-authorize-btn").is_visible()
    assert await page.locator("#session-section").get_by_text("Cancel").is_visible()
    assert platform.count(f"POST {platform.root}/mcp/oauth/authorize/session") == 1


async def test_cancel_after_a_failure_tells_the_product(browser):
    platform = _Platform(session_answer=500)
    page, _ = await _open(browser, platform, product_sign_in=True, token="t")

    await page.goto(platform.authorize_url())
    await page.locator("#session-section").get_by_text("Cancel").click()
    await page.wait_for_url(f"{CALLBACK}?*")

    query = parse_qs(urlparse(page.url).query)
    assert query == {"error": ["access_denied"], "state": ["s"], "iss": [mcp_oauth._base_url()]}


async def test_a_session_endpoint_that_keeps_refusing_stops_after_one_retry(browser):
    platform = _Platform(session_answer=401)
    page, _ = await _open(browser, platform, product_sign_in=True, token="t")

    await page.goto(platform.authorize_url())
    await page.locator("#session-authorize-btn").get_by_text("Try again").wait_for()

    # The stored session was refused, the sign-in page gave a fresh one, refused too: stop.
    assert platform.count(f"GET {platform.root}/login") == 2
    assert platform.count(f"POST {platform.root}/mcp/oauth/authorize/session") == 3


@pytest.mark.parametrize(("left_by", "signed_in"), [("an-earlier-request", True), ("this-request", False)])
async def test_round_trips_count_for_their_own_request_only(browser, left_by, signed_in):
    platform = _Platform()
    page, _ = await _open(browser, platform, product_sign_in=True, token=None)
    this_request = "t" * 43
    # Two round trips to the sign-in page left behind in the tab, by an abandoned sign-in.
    await page.goto(f"{platform.root}/api/v1/auth/config")
    stored = json.dumps({"request": this_request if left_by == "this-request" else "e" * 43, "count": 2})
    await page.evaluate(f"sessionStorage.setItem('mcp_oauth_sign_in_attempts', {json.dumps(stored)})")

    await page.goto(platform.authorize_url(this_request))

    if signed_in:
        await page.wait_for_url(f"{CALLBACK}?code=c&state=s")
        assert platform.count(f"GET {platform.root}/login") == 1
    else:
        await page.locator("#session-authorize-btn").get_by_text("Try again").wait_for()
        assert platform.count(f"GET {platform.root}/login") == 0


async def test_an_mcp_client_still_gets_its_consent_page(browser):
    platform = _Platform()
    page, _ = await _open(browser, platform, product_sign_in=False, token="t")
    before = await page.evaluate("history.length")

    await page.goto(platform.authorize_url())
    await page.locator("#session-authorize-btn").get_by_text("Authorize").wait_for()

    assert await page.locator("#consent-card").is_visible()
    assert await page.get_by_text("Approve access for this application?").is_visible()
    assert platform.count(f"POST {platform.root}/mcp/oauth/authorize/session") == 0

    await page.locator("#session-authorize-btn").click()
    await page.wait_for_url(f"{CALLBACK}?code=c&state=s")
    # A click-through keeps the page in the history, as before.
    assert await page.evaluate("history.length") == before + 2


@pytest.mark.parametrize("status", [400, 403])
async def test_an_mcp_client_refused_keeps_its_consent_page(browser, status):
    """The refusals that end a product sign-in for good leave an MCP client's page as it was."""
    platform = _Platform(session_answer=status)
    page, _ = await _open(browser, platform, product_sign_in=False, token="t")

    await page.goto(platform.authorize_url())
    await page.locator("#session-authorize-btn").click()
    await page.locator("#js-error").get_by_text("Could not complete authorization. Please try again.").wait_for()

    assert await page.locator("#session-authorize-btn").get_by_text("Authorize").is_visible()
    assert await page.locator("#consent-card h2").text_content() == "Authorize Application"
