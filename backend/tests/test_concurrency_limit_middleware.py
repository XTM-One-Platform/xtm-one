"""Tests for ConcurrencyLimitMiddleware.

Uvicorn's ``--limit-concurrency`` 503s *every* request once its protocol-level
cap is hit — including Kubernetes liveness/readiness probes, which then fail and
can trigger a pod restart during a mere traffic burst. The middleware moves the
real admission control in-app so that:

* health-check and non-HTTP (WebSocket/lifespan) traffic always bypass it, and
* overflow is shed instantly with ``429 Too Many Requests`` (+ ``Retry-After``)
  instead of being queued or blanket-503'd.
"""

import asyncio
import contextlib

import pytest

from app.main import ConcurrencyLimitMiddleware, application


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _call(
    mw,
    *,
    scope_type="http",
    path="/api/v1/agents",
    method="GET",
    origin=None,
    preflight=False,
) -> list[dict]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    headers = []
    if origin:
        headers.append((b"origin", origin.encode()))
    if preflight:
        # The header Starlette's CORSMiddleware keys off to treat an OPTIONS
        # request as a real CORS preflight.
        headers.append((b"access-control-request-method", b"POST"))
    scope = {"type": scope_type, "path": path, "headers": headers, "method": method}
    await mw(scope, receive, send)
    return sent


def _status(sent: list[dict]) -> int | None:
    for m in sent:
        if m.get("type") == "http.response.start":
            return m["status"]
    return None


def _headers(sent: list[dict]) -> dict[str, str]:
    for m in sent:
        if m.get("type") == "http.response.start":
            return {k.decode().lower(): v.decode() for k, v in m.get("headers", [])}
    return {}


async def test_passes_through_under_limit():
    mw = ConcurrencyLimitMiddleware(_ok_app, limit=2)
    sent = await _call(mw)
    assert _status(sent) == 200


async def test_sheds_overflow_with_429_then_recovers():
    gate = asyncio.Event()
    entered = asyncio.Semaphore(0)

    async def blocking_app(scope, receive, send):
        entered.release()
        await gate.wait()
        await _ok_app(scope, receive, send)

    mw = ConcurrencyLimitMiddleware(blocking_app, limit=2)

    # Fill the limit with two in-flight requests.
    t1 = asyncio.create_task(_call(mw))
    t2 = asyncio.create_task(_call(mw))
    await entered.acquire()
    await entered.acquire()

    # The third request is shed immediately (the app is never entered for it).
    over = await _call(mw)
    assert _status(over) == 429
    assert _headers(over).get("retry-after") == "1"

    # Release the held requests — they complete normally.
    gate.set()
    s1 = await t1
    s2 = await t2
    assert _status(s1) == 200
    assert _status(s2) == 200

    # Permits were released, so a fresh request is admitted again.
    assert _status(await _call(mw)) == 200


async def test_health_probes_bypass_even_when_full():
    gate = asyncio.Event()
    entered = asyncio.Semaphore(0)

    async def app(scope, receive, send):
        # GET /api/health is now short-circuited by the middleware itself and
        # never reaches this inner app.  /api/health/* sub-paths (e.g.
        # /api/health/ready) still bypass the concurrency limiter and reach
        # here, so answer them quickly.
        if scope["path"].startswith("/api/health"):
            await _ok_app(scope, receive, send)
            return
        entered.release()
        await gate.wait()
        await _ok_app(scope, receive, send)

    mw = ConcurrencyLimitMiddleware(app, limit=1)

    # Saturate the single permit with a held non-health request.
    held = asyncio.create_task(_call(mw, path="/api/v1/agents"))
    await entered.acquire()

    # Liveness is short-circuited by the middleware — never shed with 429.
    assert _status(await _call(mw, path="/api/health")) == 200
    # Readiness bypasses the limiter and passes through to the inner app.
    assert _status(await _call(mw, path="/api/health/ready")) == 200

    gate.set()
    assert _status(await held) == 200


@pytest.mark.parametrize("limit", [5, 0])
async def test_liveness_short_circuits_inner_app(limit):
    """GET /api/health must be answered by the middleware itself — the inner app
    must NOT be called, so inner-stack pressure (DB pool, router dispatch) can
    never stall the liveness probe. This holds even with the limiter disabled
    (limit=0), so liveness never depends on inner-app pressure or config."""
    inner_called = {"n": 0}

    async def counting_app(scope, receive, send):
        inner_called["n"] += 1
        await _ok_app(scope, receive, send)

    mw = ConcurrencyLimitMiddleware(counting_app, limit=limit)
    assert _status(await _call(mw, path="/api/health", method="GET")) == 200
    assert inner_called["n"] == 0, "inner app must not be reached for GET /api/health"


async def test_liveness_body_and_content_type():
    """The short-circuited liveness response must match the route contract:
    body ``{"status":"ok"}``, ``Content-Type: application/json``, and the
    ``X-Robots-Tag`` the app applies to every response. The outermost
    short-circuit bypasses ``NoIndexMiddleware``, so the header must be set here;
    assert it so the contract cannot regress silently."""
    import json

    from app.main import ROBOTS_DIRECTIVES

    mw = ConcurrencyLimitMiddleware(_ok_app, limit=5)
    sent = await _call(mw, path="/api/health", method="GET")

    body = b"".join(m.get("body", b"") for m in sent if m.get("type") == "http.response.body")
    assert json.loads(body) == {"status": "ok"}

    h = _headers(sent)
    assert h.get("content-type", "").startswith("application/json")
    assert h.get("x-robots-tag") == ROBOTS_DIRECTIVES


async def test_liveness_head_short_circuits():
    """HEAD /api/health is short-circuited (no inner-app call) and, per HTTP
    semantics (RFC 9110), carries no response body."""
    inner_called = {"n": 0}

    async def counting_app(scope, receive, send):
        inner_called["n"] += 1
        await _ok_app(scope, receive, send)

    mw = ConcurrencyLimitMiddleware(counting_app, limit=5)
    sent = await _call(mw, path="/api/health", method="HEAD")
    assert _status(sent) == 200
    assert inner_called["n"] == 0, "inner app must not be reached for HEAD /api/health"
    body = b"".join(
        m.get("body", b"") for m in sent if m.get("type") == "http.response.body"
    )
    assert body == b"", "HEAD response must not include a body"


@pytest.mark.parametrize("method", ["POST", "DELETE", "PATCH"])
async def test_liveness_non_get_passes_through(method):
    """POST/DELETE/PATCH /api/health are NOT short-circuited: they pass through
    to the inner app (the FastAPI router), which returns 405 Method Not Allowed
    for a non-GET method on a GET-only route. The fake inner app below mirrors
    that 405 so the test verifies both the pass-through and the resulting
    status, not merely that the inner app was reached."""
    inner_called = {"n": 0}

    async def router_app(scope, receive, send):
        inner_called["n"] += 1
        # Mirror the router's 405 for a non-GET method on @application.get(...).
        await send({"type": "http.response.start", "status": 405, "headers": []})
        await send(
            {"type": "http.response.body", "body": b'{"detail":"Method Not Allowed"}'}
        )

    mw = ConcurrencyLimitMiddleware(router_app, limit=5)
    sent = await _call(mw, path="/api/health", method=method)
    assert inner_called["n"] == 1, f"{method} /api/health must reach the inner app"
    assert _status(sent) == 405, f"{method} /api/health must yield the router's 405"


async def test_readiness_still_passes_through():
    """/api/health/ready must NOT be short-circuited — it continues to the
    inner app so the deep dependency checks actually run."""
    inner_called = {"n": 0}

    async def counting_app(scope, receive, send):
        inner_called["n"] += 1
        await _ok_app(scope, receive, send)

    mw = ConcurrencyLimitMiddleware(counting_app, limit=5)
    await _call(mw, path="/api/health/ready", method="GET")
    assert inner_called["n"] == 1, "/api/health/ready must reach the inner app"


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
async def test_non_http_scope_bypasses_limit(scope_type):
    calls = {"n": 0}

    async def app(scope, receive, send):
        calls["n"] += 1

    mw = ConcurrencyLimitMiddleware(app, limit=1)
    await _call(mw, scope_type=scope_type, path="/api/v1/chat/ws")
    assert calls["n"] == 1


async def test_limit_zero_disables_shedding():
    mw = ConcurrencyLimitMiddleware(_ok_app, limit=0)
    assert _status(await _call(mw)) == 200


async def test_active_count_released_after_exception():
    """A handler error must still release the permit (no leak)."""

    async def boom_app(scope, receive, send):
        raise RuntimeError("handler boom")

    mw = ConcurrencyLimitMiddleware(boom_app, limit=1)

    for _ in range(3):
        with contextlib.suppress(RuntimeError):
            await _call(mw)
    # If the permit leaked, _active would be >= limit and the next request
    # would be shed. It isn't, because the finally always decrements.
    assert mw._active == 0



# ---------------------------------------------------------------------------
# The web chat's SSE streams: counted while they authenticate and set up,
# released once their response starts.
# ---------------------------------------------------------------------------

_SEND_STREAM = ("POST", "/api/v1/web/chat/messages")
_EVENTS_STREAM = ("GET", "/api/v1/web/chat/conversations/c0ffee/events")


@pytest.mark.parametrize(("method", "path"), [_SEND_STREAM, _EVENTS_STREAM])
async def test_a_web_chat_stream_setting_up_is_shed_past_the_limit(method, path):
    """A flood of sends or event streams with bad tokens reaches no
    authentication past the limit: each is shed like any request."""
    reached = {"n": 0}

    async def app(scope, receive, send):
        reached["n"] += 1
        await _ok_app(scope, receive, send)

    mw = ConcurrencyLimitMiddleware(app, limit=1)
    mw._active = 1

    sent = await _call(mw, path=path, method=method)

    assert _status(sent) == 429
    assert reached["n"] == 0


@pytest.mark.parametrize(("method", "path"), [_SEND_STREAM, _EVENTS_STREAM])
async def test_a_streaming_web_chat_stream_holds_no_slot(method, path):
    streaming = asyncio.Event()
    finish = asyncio.Event()

    async def stream_app(scope, receive, send):
        if scope["path"] != path:
            await _ok_app(scope, receive, send)
            return
        # Authentication and setup run here, the slot held.
        assert mw._active == 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        streaming.set()
        await finish.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    mw = ConcurrencyLimitMiddleware(stream_app, limit=1)
    stream = asyncio.create_task(_call(mw, path=path, method=method))
    await asyncio.wait_for(streaming.wait(), 2)

    assert mw._active == 0
    assert _status(await _call(mw)) == 200

    finish.set()
    assert _status(await stream) == 200
    assert mw._active == 0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/web/chat/messages"),
        ("POST", "/api/v1/web/chat/conversations/c0ffee/events"),
        ("POST", "/api/v1/web/chat/messages/steer"),
    ],
)
async def test_other_web_chat_requests_hold_their_slot_to_the_end(method, path):
    started = asyncio.Event()
    finish = asyncio.Event()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        started.set()
        await finish.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    mw = ConcurrencyLimitMiddleware(app, limit=1)
    held = asyncio.create_task(_call(mw, path=path, method=method))
    await asyncio.wait_for(started.wait(), 2)

    assert mw._active == 1
    finish.set()
    await held
    assert mw._active == 0


def test_middleware_is_registered_outermost():
    """BasePathMiddleware is added last → the true outermost layer: it
    answers nothing, only normalises the request path, and must run before
    any layer that reads it. AccessLogMiddleware sits just inside it, so it
    still observes the final wire status of every request. Then
    GracefulShutdownDrainMiddleware, which absorbs a
    bounded-drain cancellation wherever in the stack it was delivered; then
    ConcurrencyLimitMiddleware, the outermost *functional* layer that sheds
    overflow before any real work. Starlette runs the last-added middleware
    first, so user_middleware[0] is the outermost."""
    from app.core.base_path import BasePathMiddleware
    from app.main import AccessLogMiddleware, GracefulShutdownDrainMiddleware

    classes = [m.cls for m in application.user_middleware]
    assert ConcurrencyLimitMiddleware in classes
    assert classes[0] is BasePathMiddleware
    assert classes[1] is AccessLogMiddleware
    assert classes[2] is GracefulShutdownDrainMiddleware
    assert classes[3] is ConcurrencyLimitMiddleware


# ---------------------------------------------------------------------------
# CORS on the shed 429: the limiter runs outside the CORS
# middlewares, so the shed response must carry CORS headers itself or browser
# clients see an opaque CORS failure instead of a readable 429.
# ---------------------------------------------------------------------------


async def test_shed_response_has_wildcard_cors_for_public_paths():
    mw = ConcurrencyLimitMiddleware(_ok_app, limit=1)
    mw._active = 1  # saturate so the next request is shed
    sent = await _call(
        mw, path="/api/v1/public/chat", origin="https://anything.example.com"
    )
    assert _status(sent) == 429
    h = _headers(sent)
    # Mirrors PublicCORSMiddleware (wide-open widget): * and no credentials.
    assert h.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in h
    assert h.get("retry-after") == "1"
    assert h.get("access-control-expose-headers") == "Retry-After"


async def test_shed_response_echoes_allowed_origin_with_credentials():
    from app.core.config import settings

    allowed = settings.cors_origins[0]
    mw = ConcurrencyLimitMiddleware(_ok_app, limit=1)
    mw._active = 1
    sent = await _call(mw, path="/api/v1/agents", origin=allowed)
    assert _status(sent) == 429
    h = _headers(sent)
    # Mirrors CORSMiddleware: echo the allowed origin + credentials + Vary.
    assert h.get("access-control-allow-origin") == allowed
    assert h.get("access-control-allow-credentials") == "true"
    assert h.get("vary") == "Origin"
    assert h.get("access-control-expose-headers") == "Retry-After"


async def test_shed_response_omits_cors_for_disallowed_origin():
    mw = ConcurrencyLimitMiddleware(_ok_app, limit=1)
    mw._active = 1
    sent = await _call(
        mw, path="/api/v1/agents", origin="https://evil.example.com"
    )
    assert _status(sent) == 429
    # A disallowed origin gets no ACAO — same as the inner CORSMiddleware.
    assert "access-control-allow-origin" not in _headers(sent)


async def test_options_preflight_bypasses_limit_even_when_full():
    """A *true* CORS preflight (OPTIONS + Access-Control-Request-Method) must
    pass through even when saturated, so the browser can send the real request
    (which then gets a CORS-headed 429 if shed)."""
    mw = ConcurrencyLimitMiddleware(_ok_app, limit=1)
    mw._active = 1  # a GET here would be shed with 429
    sent = await _call(
        mw,
        path="/api/v1/agents",
        method="OPTIONS",
        origin="https://x.example.com",
        preflight=True,
    )
    assert _status(sent) == 200  # reached the inner app, not shed


async def test_non_preflight_options_is_shed_when_full():
    """A bare OPTIONS (no Access-Control-Request-Method) is NOT a CORS preflight
    — it must be counted/shed like any other method so the bypass can't be used
    to dodge overload shedding."""
    mw = ConcurrencyLimitMiddleware(_ok_app, limit=1)
    mw._active = 1  # saturated
    sent = await _call(mw, path="/api/v1/agents", method="OPTIONS")
    assert _status(sent) == 429


@pytest.mark.parametrize(
    "path, origin",
    [
        ("/api/v1/agents", None),  # standard shed path
        ("/api/v1/public/chat", "https://w.example.com"),  # public early-return branch
    ],
)
async def test_shed_response_carries_robots_header(path, origin):
    """The limiter is outermost, so a shed 429 bypasses NoIndexMiddleware — it
    must still carry the noindex/nofollow X-Robots-Tag the app applies to every
    response, on both the standard and the public-path (early-return) branches.
    """
    from app.main import ROBOTS_DIRECTIVES

    mw = ConcurrencyLimitMiddleware(_ok_app, limit=1)
    mw._active = 1  # saturate so the next request is shed
    sent = await _call(mw, path=path, origin=origin)
    assert _status(sent) == 429
    assert _headers(sent).get("x-robots-tag") == ROBOTS_DIRECTIVES
