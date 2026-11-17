"""WS2: platform-managed JWT re-mint on a mid-run 401.

The OpenAEV integration authenticates with a short-lived (10 min) cross-platform
JWT. A long autonomous run outlives that TTL, after which every REST call 401s -
including the run's own terminal status write - so the run can neither progress
nor settle itself. WS2 binds a JWT re-mint callback onto the shared provider
headers so ``_api`` routes each call through an ``AutoRefreshClient``: a 401/403
re-mints the JWT and retries once, mutating the shared headers so every later
call recovers too.

These tests pin the ``_common._api`` seam end to end (through the real
``AutoRefreshClient`` and ``safe_request``) with the inner ``httpx`` transport
patched, so no socket is opened.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.openaev import _common
from app.integrations.providers.openaev._common import (
    _JWT_PROACTIVE_REFRESH_SKEW_SECONDS,
    _api,
    _bearer_seconds_remaining,
    _ensure_fresh_token,
    _headers,
)


def _resp(status: int, url: str, *, method: str = "POST") -> httpx.Response:
    return httpx.Response(status, request=httpx.Request(method, url))


def _jwt(seconds_from_now: float) -> str:
    """A signature-less JWT whose ``exp`` is *seconds_from_now* from now.

    ``_bearer_seconds_remaining`` only peeks the ``exp`` claim (never verifies the
    signature), so a real key is unnecessary; the ``sig`` segment is a placeholder
    to keep the three-part shape.
    """

    def _seg(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    header = _seg({"alg": "none", "typ": "JWT"})
    payload = _seg({"exp": int(time.time() + seconds_from_now)})
    return f"{header}.{payload}.sig"


@pytest.mark.asyncio
async def test_refreshable_headers_remint_on_401_and_retry() -> None:
    """A 401 re-mints the JWT via the callback and retries once with the new token.

    The shared headers dict is mutated in place, so the recovered token is what
    every subsequent call on the tool-set uses (no second re-mint needed).
    """
    base_url = "https://demo.openaev.io"
    seen_auth: list[str | None] = []
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return "fresh-jwt"

    headers = _headers("stale-jwt", refresh_fn=_remint)

    call = {"n": 0}

    async def _fake_request(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        seen_auth.append((kwargs.get("headers") or {}).get("Authorization"))
        call["n"] += 1
        # First physical request 401s (expired token); the retry (after the
        # re-mint) succeeds.
        return _resp(401 if call["n"] == 1 else 200, url)

    with patch.object(httpx.AsyncClient, "request", _fake_request):
        resp, err = await _api(
            base_url, headers, "post", "/api/foo", "do a thing", json={"x": 1}
        )

    assert err is None, "the retry after re-mint should succeed"
    assert resp is not None and resp.status_code == 200
    assert remints["n"] == 1, "exactly one re-mint on the single 401"
    assert call["n"] == 2, "the original call plus one retry"
    assert seen_auth == ["Bearer stale-jwt", "Bearer fresh-jwt"], (
        "the retry must carry the freshly minted token"
    )
    # The shared headers dict is mutated in place so later calls recover too.
    assert headers["Authorization"] == "Bearer fresh-jwt"


@pytest.mark.asyncio
async def test_static_headers_do_not_retry_on_401() -> None:
    """Without a refresh callback, a 401 is surfaced once (original behaviour).

    A static-API-key integration passes no callback, so ``_api`` must keep using
    a plain client and never retry - a 401 is a genuine auth failure, not a
    recoverable expiry.
    """
    base_url = "https://demo.openaev.io"
    headers = _headers("static-key")  # no refresh_fn
    assert getattr(headers, "refresh_fn", "MISSING") is None

    call = {"n": 0}

    async def _fake_request(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        call["n"] += 1
        return _resp(401, url)

    with patch.object(httpx.AsyncClient, "request", _fake_request):
        _, err = await _api(base_url, headers, "get", "/api/foo", "read a thing")

    assert err is not None, "a 401 with no refresh callback is a hard failure"
    assert call["n"] == 1, "no retry without a refresh callback"


@pytest.mark.asyncio
async def test_remint_returning_none_surfaces_original_401() -> None:
    """If the re-mint yields nothing, the original 401 is surfaced, not masked."""
    base_url = "https://demo.openaev.io"

    async def _remint_fail() -> None:
        return None

    headers = _headers("stale-jwt", refresh_fn=_remint_fail)

    call = {"n": 0}

    async def _fake_request(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        call["n"] += 1
        return _resp(401, url)

    with patch.object(httpx.AsyncClient, "request", _fake_request):
        _, err = await _api(base_url, headers, "get", "/api/foo", "read a thing")

    assert err is not None
    assert call["n"] == 1, "a failed re-mint must not loop; one physical call only"


@pytest.mark.asyncio
async def test_injected_client_is_used_verbatim() -> None:
    """An explicitly injected client is honoured (caller owns its lifecycle)."""

    class _StubClient:
        def __init__(self) -> None:
            self.calls = 0

        async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
            self.calls += 1
            return _resp(200, url)

    stub = _StubClient()
    headers = _headers("tok", refresh_fn=None)
    resp, err = await _api(
        "https://demo.openaev.io",
        headers,
        "get",
        "/api/foo",
        "read",
        client=stub,  # type: ignore[arg-type]
    )
    assert err is None
    assert resp is not None and resp.status_code == 200
    assert stub.calls == 1


def test_common_exports_refreshable_headers() -> None:
    """``_RefreshableHeaders`` is exported so tests / callers can reference it."""
    assert "_RefreshableHeaders" in _common.__all__


# ---------------------------------------------------------------------------
# Proactive (pre-expiry) JWT re-mint: the common-case complement to the 401/403
# reactive backstop above. A 10-min JWT outlived by a long autonomous run must be
# re-minted BEFORE it expires so the peer never has to 401/403 a burst of
# concurrent callbacks (the "too many permission denied / forbidden" storm).
# ---------------------------------------------------------------------------


def test_bearer_seconds_remaining_reads_exp_without_verifying() -> None:
    """A Bearer JWT's ``exp`` is read; a static / malformed token yields ``None``."""
    fresh = _headers(_jwt(600))
    remaining = _bearer_seconds_remaining(fresh)
    assert remaining is not None and 500 < remaining <= 600

    # A static API key is not a JWT -> no known expiry -> proactive refresh is
    # skipped (only the reactive backstop applies).
    assert _bearer_seconds_remaining(_headers("static-api-key")) is None
    # A three-segment value whose payload is not valid base64 JSON is ignored.
    assert _bearer_seconds_remaining(_headers("a.b.c")) is None


@pytest.mark.asyncio
async def test_ensure_fresh_token_remints_when_near_expiry() -> None:
    """A token inside the skew window is re-minted once and updated in place."""
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return _jwt(600)  # a comfortably fresh replacement

    # Just inside the refresh window.
    headers = _headers(_jwt(_JWT_PROACTIVE_REFRESH_SKEW_SECONDS - 10), refresh_fn=_remint)
    await _ensure_fresh_token(headers, _remint)

    assert remints["n"] == 1
    # The shared dict is mutated in place, so every later call sees the new token.
    remaining = _bearer_seconds_remaining(headers)
    assert remaining is not None and remaining > _JWT_PROACTIVE_REFRESH_SKEW_SECONDS


@pytest.mark.asyncio
async def test_ensure_fresh_token_noop_when_far_from_expiry() -> None:
    """A token comfortably in date is left untouched (no needless re-mint)."""
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return _jwt(600)

    headers = _headers(_jwt(_JWT_PROACTIVE_REFRESH_SKEW_SECONDS + 300), refresh_fn=_remint)
    before = headers["Authorization"]
    await _ensure_fresh_token(headers, _remint)

    assert remints["n"] == 0, "a token far from expiry must not be re-minted"
    assert headers["Authorization"] == before


@pytest.mark.asyncio
async def test_ensure_fresh_token_coalesces_concurrent_callers() -> None:
    """A gather() burst on an expiring token mints exactly ONE fresh token.

    This is the whole point of the proactive path: without coalescing, each of the
    N concurrent orchestrator callbacks would re-mint on the same expiry window.
    """
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        await asyncio.sleep(0)  # force overlap: yield while holding the lock
        return _jwt(600)

    headers = _headers(_jwt(_JWT_PROACTIVE_REFRESH_SKEW_SECONDS - 10), refresh_fn=_remint)

    await asyncio.gather(*(_ensure_fresh_token(headers, _remint) for _ in range(8)))

    assert remints["n"] == 1, "concurrent callers must coalesce to a single re-mint"


@pytest.mark.asyncio
async def test_ensure_fresh_token_swallows_remint_failure() -> None:
    """A proactive re-mint failure never fails the call; token is left as-is."""

    async def _remint_boom() -> str:
        raise RuntimeError("mint service down")

    headers = _headers(_jwt(_JWT_PROACTIVE_REFRESH_SKEW_SECONDS - 10), refresh_fn=_remint_boom)
    before = headers["Authorization"]
    await _ensure_fresh_token(headers, _remint_boom)  # must not raise

    assert headers["Authorization"] == before


@pytest.mark.asyncio
async def test_api_sends_freshly_minted_token_without_a_401() -> None:
    """``_api`` re-mints BEFORE sending, so the first physical request is fresh.

    The reactive path needs a 401 to recover; the proactive path means a
    near-expiry token is swapped out first, so the peer sees only a valid token
    and never has to 401/403 - exactly one physical call, carrying the new token.
    """
    base_url = "https://demo.openaev.io"
    remints = {"n": 0}
    seen_auth: list[str | None] = []

    async def _remint() -> str:
        remints["n"] += 1
        return _jwt(600)

    headers = _headers(_jwt(_JWT_PROACTIVE_REFRESH_SKEW_SECONDS - 10), refresh_fn=_remint)

    call = {"n": 0}

    async def _fake_request(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        seen_auth.append((kwargs.get("headers") or {}).get("Authorization"))
        call["n"] += 1
        return _resp(200, url)

    with patch.object(httpx.AsyncClient, "request", _fake_request):
        resp, err = await _api(
            base_url, headers, "post", "/api/foo", "do a thing", json={"x": 1}
        )

    assert err is None
    assert resp is not None and resp.status_code == 200
    assert remints["n"] == 1, "one proactive re-mint before sending"
    assert call["n"] == 1, "no 401 round-trip needed - a single physical call"
    assert seen_auth == ["Bearer " + headers["Authorization"].split(" ", 1)[1]], (
        "the only physical request must already carry the freshly minted token"
    )


@pytest.mark.asyncio
async def test_api_does_not_remint_a_healthy_token() -> None:
    """A token far from expiry drives a normal single call with no re-mint."""
    base_url = "https://demo.openaev.io"
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return _jwt(600)

    headers = _headers(_jwt(_JWT_PROACTIVE_REFRESH_SKEW_SECONDS + 300), refresh_fn=_remint)

    call = {"n": 0}

    async def _fake_request(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        call["n"] += 1
        return _resp(200, url)

    with patch.object(httpx.AsyncClient, "request", _fake_request):
        _, err = await _api(base_url, headers, "get", "/api/foo", "read a thing")

    assert err is None
    assert remints["n"] == 0, "a healthy token must not be proactively re-minted"
    assert call["n"] == 1


@pytest.mark.asyncio
async def test_retry_after_remint_does_not_replay_the_401_session_cookie() -> None:
    """The refreshed retry must re-authenticate as a cookie-less bearer request.

    OpenAEV exempts a bearer request WITHOUT cookies from CSRF, but its 401 (the
    expired-JWT response) opens a session and returns ``Set-Cookie``.
    ``AutoRefreshClient`` reuses ONE httpx client across the 401 and its retry, so
    without clearing the jar the retry would replay that ``JSESSIONID``, lose the
    bearer-without-cookies CSRF exemption, and come back 403 - the exact failure
    that stopped a long autonomous run from recording events / authoring steps once
    its 10-minute JWT expired mid-run. The retry must therefore send no ``Cookie``.

    Uses a real httpx client behind a ``MockTransport`` (not a patched ``request``)
    so httpx's actual cookie jar - the thing under test - is exercised end to end.
    """
    base_url = "https://demo.openaev.io"
    seen: list[dict[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "cookie": request.headers.get("cookie"),
                "auth": request.headers.get("authorization"),
            }
        )
        if len(seen) == 1:
            # The expired-JWT 401: OpenAEV's request cache opens a session here and
            # returns its id, which httpx stores in the shared client's cookie jar.
            return httpx.Response(
                401, headers={"Set-Cookie": "JSESSIONID=sess-1; Path=/; HttpOnly"}
            )
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        # AutoRefreshClient builds its client with a `limits=` for the default
        # transport; swap in the mock transport instead (limits is then moot).
        kwargs.pop("limits", None)
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    async def _remint() -> str:
        return "fresh-jwt"

    headers = _headers("stale-jwt", refresh_fn=_remint)

    with patch("app.integrations.http.httpx.AsyncClient", side_effect=_factory):
        resp, err = await _api(
            base_url, headers, "post", "/api/foo", "do a thing", json={"x": 1}
        )

    assert err is None, "the retry after re-mint should succeed"
    assert resp is not None and resp.status_code == 200
    assert len(seen) == 2, "the original 401 plus exactly one retry"
    assert seen[0]["cookie"] is None, "the first request carries no cookie"
    assert seen[1]["cookie"] is None, (
        "the retry must NOT replay the JSESSIONID the 401 set, or it loses the "
        "bearer-without-cookies CSRF exemption and comes back 403"
    )
    assert seen[1]["auth"] == "Bearer fresh-jwt", (
        "the retry still carries the freshly minted token"
    )
