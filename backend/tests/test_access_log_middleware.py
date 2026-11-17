"""Tests for AccessLogMiddleware + validation/HTTPException logging.

Regression guard for the "invisible 4xx" bug: uvicorn's access log is muted and
FastAPI's default exception handlers log nothing, so handled 4xx responses
(401/403/404/422, …) previously produced NO log line in XTM One at all — a 422
for an invalid body was visible only in the fronting nginx access log. This
middleware emits exactly one structured access line per HTTP request (with the
final status, duration, and — via the exception handlers — the failure reason),
so every request entry/exit is now observable from the application's own logs.
"""

import asyncio
import logging

import pytest
import structlog
from starlette.testclient import TestClient

from app.main import AccessLogMiddleware, application


class _Capture(logging.Handler):
    """Capture ``app.access`` records together with the merged structlog context."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[tuple[int, str, dict]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            (
                record.levelno,
                record.getMessage(),
                dict(structlog.contextvars.get_contextvars()),
            )
        )


@pytest.fixture
def cap():
    """Attach a capture handler to ``app.access`` for the duration of one test."""
    handler = _Capture()
    lg = logging.getLogger("app.access")
    prev_level = lg.level
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        lg.removeHandler(handler)
        lg.setLevel(prev_level)


class _FakeRoute:
    """Stand-in for a FastAPI APIRoute: carries the templated ``path_format``
    that the router records on ``scope["route"]`` for a matched request."""

    def __init__(self, path_format: str) -> None:
        self.path_format = path_format
        self.path = path_format


def _make_app(status: int = 200, route_template: str | None = None):
    async def _app(scope, receive, send):
        if route_template is not None:
            # The FastAPI router records the matched route on the scope in place.
            scope["route"] = _FakeRoute(route_template)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return _app


async def _drive(
    app=None,
    *,
    status: int = 200,
    path: str = "/api/v1/agents",
    method: str = "GET",
    scope_type: str = "http",
    route_template: str | None = None,
    headers: list | None = None,
    client: tuple | None = ("10.0.0.1", 5555),
) -> None:
    """Run one request through ``AccessLogMiddleware`` wrapping ``app``."""
    inner = app if app is not None else _make_app(status, route_template)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    scope = {
        "type": scope_type,
        "path": path,
        "method": method,
        "headers": headers or [],
        "client": client,
    }
    await AccessLogMiddleware(inner)(scope, receive, send)


def _ctx(cap: _Capture) -> dict:
    return cap.records[-1][2]


@pytest.mark.parametrize(
    "status, level",
    [
        (200, logging.INFO),
        (422, logging.WARNING),
        (503, logging.WARNING),  # retryable back-pressure, not a server fault
        (500, logging.ERROR),  # a genuine server fault still alerts
    ],
)
async def test_status_maps_to_log_level(cap, status, level):
    await _drive(status=status)
    lvl, msg, ctx = cap.records[0]
    assert msg == "http_request"
    assert lvl == level
    assert ctx["http_status"] == status


async def test_access_line_carries_request_fields(cap):
    await _drive(status=200, path="/api/v1/agents")
    ctx = _ctx(cap)
    assert ctx["http_method"] == "GET"
    assert ctx["http_path"] == "/api/v1/agents"
    assert ctx["client_ip"] == "10.0.0.1"
    assert "duration_ms" in ctx


@pytest.mark.parametrize(
    "path, status, level",
    [
        ("/api/health", 200, logging.DEBUG),  # successful probe is quieted
        ("/metrics", 200, logging.DEBUG),
        ("/api/health", 500, logging.ERROR),  # a failing probe is NOT quieted
        ("/api/health/ready", 200, logging.DEBUG),  # readiness sub-path is quieted
        ("/api/v1/agents", 200, logging.INFO),  # a non-quiet path stays INFO
        # Sibling routes that merely share a prefix must NOT be quieted (the
        # match is on a path boundary, not a raw string prefix).
        ("/api/healthz", 200, logging.INFO),
        ("/metrics-export", 200, logging.INFO),
    ],
)
async def test_quiet_path_levels(cap, path, status, level):
    await _drive(status=status, path=path)
    assert cap.records[0][0] == level


async def test_websocket_scope_is_passed_through_untouched(cap):
    seen: list = []

    async def _ws(scope, receive, send):
        seen.append(scope["type"])

    await _drive(app=_ws, scope_type="websocket")
    assert seen == ["websocket"]
    assert cap.records == []  # no access line for non-HTTP scopes


async def test_raised_exception_logged_at_error_then_reraised(cap):
    async def _boom(scope, receive, send):
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        await _drive(app=_boom)
    lvl, _msg, ctx = cap.records[0]
    assert lvl == logging.ERROR
    assert ctx["http_status"] == 500


async def test_cancelled_request_is_logged_and_reraised(cap):
    """A client disconnect (CancelledError) must produce a WARNING line, not vanish."""

    async def _hang(scope, receive, send):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _drive(app=_hang)
    lvl, _msg, ctx = cap.records[0]
    assert lvl == logging.WARNING
    assert ctx["http_status"] == 499  # response never started
    assert ctx["client_disconnected"] is True


async def test_cancelled_after_5xx_sent_is_error_not_warning(cap):
    """A real 5xx that was already sent then cancelled mid-body stays ERROR — a
    server fault must not be masked as a client-disconnect WARNING."""

    async def _sent_500_then_cancel(scope, receive, send):
        await send({"type": "http.response.start", "status": 500, "headers": []})
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _drive(app=_sent_500_then_cancel)
    lvl, _msg, ctx = cap.records[0]
    assert ctx["http_status"] == 500
    assert lvl == logging.ERROR


async def test_error_after_response_started_logs_sent_status(cap):
    """A streaming body that fails mid-flight logs the status already sent (at ERROR)."""

    async def _stream_then_boom(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("mid-stream boom")

    with pytest.raises(RuntimeError):
        await _drive(app=_stream_then_boom)
    lvl, _msg, ctx = cap.records[0]
    assert ctx["http_status"] == 200  # the status actually sent to the client
    assert lvl == logging.ERROR  # but flagged as an error


async def test_forwarded_for_first_hop_captured_when_proxy_trusted(cap, monkeypatch):
    # X-Forwarded-For is honored only when the deployment opts in via
    # settings.trust_proxy_headers (same gate as rate-limiting / audit).
    from app.main import settings

    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    await _drive(headers=[(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")])
    ctx = _ctx(cap)
    assert ctx["forwarded_for"] == "203.0.113.7"  # first hop only
    assert ctx["client_ip"] == "10.0.0.1"


@pytest.mark.parametrize(
    "strict, raw, logged",
    [
        # By default the hop is logged as the proxy wrote it, as before.
        (False, b"[2001:DB8::1]:443, 10.0.0.1", "[2001:DB8::1]:443"),
        (True, b"[2001:DB8::1]:443, 10.0.0.1", "2001:db8::1"),
        (False, b"<script>, 10.0.0.1", None),
        (True, b"<script>, 10.0.0.1", None),
    ],
)
async def test_forwarded_for_is_logged_in_the_form_request_client_ip_uses(cap, monkeypatch, strict, raw, logged):
    # Under the flag the first hop is whatever the client sent: only an IP
    # address reaches the log line, in the form request_client_ip() uses.
    from app.main import settings

    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "client_ip_strict", strict)
    await _drive(headers=[(b"x-forwarded-for", raw)])
    ctx = _ctx(cap)
    assert ctx.get("forwarded_for") == logged
    assert ctx["client_ip"] == "10.0.0.1"


@pytest.mark.parametrize(
    "strict, client, logged",
    [
        (False, ("::ffff:10.0.0.1", 5555), "::ffff:10.0.0.1"),
        (True, ("::ffff:10.0.0.1", 5555), "10.0.0.1"),
        # FORWARDED_ALLOW_IPS="*": uvicorn copied the header in unchecked.
        (False, ("x" * 200, 0), None),
        (True, ("x" * 200, 0), None),
        (False, None, None),
    ],
)
async def test_client_ip_is_logged_in_the_form_request_client_ip_uses(cap, monkeypatch, strict, client, logged):
    from app.main import settings

    monkeypatch.setattr(settings, "client_ip_strict", strict)
    await _drive(client=client)
    assert _ctx(cap)["client_ip"] == logged


async def test_forwarded_for_ignored_when_proxy_untrusted(cap, monkeypatch):
    # Default deployment is directly addressable: a spoofable XFF header must
    # NOT poison the logged client identity.
    from app.main import settings

    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    await _drive(headers=[(b"x-forwarded-for", b"1.2.3.4")])
    ctx = _ctx(cap)
    assert "forwarded_for" not in ctx
    assert ctx["client_ip"] == "10.0.0.1"


async def test_context_does_not_leak_between_requests(cap):
    await _drive(status=200)
    # After the middleware emits and cleans up, no request-scoped field remains.
    leaked = structlog.contextvars.get_contextvars()
    for key in ("http_status", "http_path", "http_method", "validation_errors"):
        assert key not in leaked


@pytest.mark.parametrize(
    "concrete_path, template",
    [
        (
            "/api/v1/invitations/validate/SECRET_TOKEN_123",
            "/api/v1/invitations/validate/{token}",
        ),
        (
            "/api/v1/public/chat/WEB_TOK_XYZ/config",
            "/api/v1/public/chat/{web_token}/config",
        ),
        ("/mcp/oauth/authorize/STATE_ABC", "/mcp/oauth/authorize/{state}"),
        # Two params sharing the same value are unambiguous — the template comes
        # straight from the router, not from matching values in the URL.
        ("/api/v1/users/1/posts/1", "/api/v1/users/{user_id}/posts/{post_id}"),
        # A ``{path:path}`` value spanning several segments.
        ("/api/hooks/agent-9/foo/bar/baz", "/api/hooks/{agent_id}/{path}"),
    ],
)
async def test_matched_route_logs_router_template_not_value(cap, concrete_path, template):
    # A matched route logs the router's http.route template, so no concrete path
    # segment (secret or not) from the request URL is ever emitted.
    await _drive(path=concrete_path, route_template=template)
    ctx = _ctx(cap)
    assert ctx["http_path"] == template
    # The concrete URL (and its secret tail) never appears anywhere on the line.
    assert concrete_path not in str(ctx)


@pytest.mark.parametrize(
    "concrete_path, template",
    [
        (
            "/api/v1/invitations/validate/SECRET_TOKEN_123",
            "/api/v1/invitations/validate/{token}",
        ),
        (
            "/api/v1/public/chat/WEB_TOK_XYZ/config",
            "/api/v1/public/chat/{web_token}/config",
        ),
    ],
)
async def test_shed_request_rematches_route_template(cap, concrete_path, template):
    # A request shed before routing (429/503) carries no scope["route"], so
    # _safe_path re-matches the raw path against the route table to recover the
    # template — the secret path segment must never be logged even when shed.
    await _drive(path=concrete_path, route_template=None, status=503)
    ctx = _ctx(cap)
    assert ctx["http_path"] == template
    assert concrete_path not in str(ctx)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/tokens/Xy3k9Zq2Lp7Wm4Rt8Bn1Cv6",  # credential-shaped
        "/api/v2/agents",  # wrong-version typo
        "/api/v1/settings/available-embedding-models",  # human-readable slug
    ],
)
async def test_unmatched_route_logs_raw_path(cap, path):
    # No scope["route"] (404 / static-file / Mount / pre-routing disconnect):
    # there is no template, so the raw path is logged verbatim. Defined
    # credential-bearing endpoints always match and are templated, so only
    # typos/scans reach this branch — which the fronting nginx already logs raw.
    await _drive(path=path, route_template=None, status=404)
    assert _ctx(cap)["http_path"] == path


# --- End-to-end via TestClient (no `with` block => app lifespan never starts,
# so no DB is needed; validation/auth failures happen before any DB access). ---


def test_validation_error_reason_reaches_access_line(cap):
    client = TestClient(application, raise_server_exceptions=False)
    resp = client.post("/api/v1/auth/login")  # missing body -> 422 pre-DB
    assert resp.status_code == 422
    access = [r for r in cap.records if r[1] == "http_request"]
    assert access, "expected an http_request access line for the 422"
    ctx = access[-1][2]
    assert ctx["http_status"] == 422
    assert ctx["http_path"] == "/api/v1/auth/login"
    assert ctx.get("validation_errors"), "validation reason must enrich the line"


def test_http_exception_detail_reaches_access_line(cap):
    client = TestClient(application, raise_server_exceptions=False)
    # Auth dependency raises HTTPException(401) before body validation.
    resp = client.post("/api/v1/platform/chat/sessions")
    assert resp.status_code == 401
    access = [r for r in cap.records if r[1] == "http_request"]
    assert access
    ctx = access[-1][2]
    assert ctx["http_status"] == 401
    assert ctx.get("error_detail")


def test_validation_errors_do_not_leak_submitted_secrets(cap):
    """The redacted validation_errors must never contain the submitted payload."""
    client = TestClient(application, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/auth/login", json={"password": "SUPER_SECRET_PW", "x": 1}
    )
    assert resp.status_code == 422
    access = [r for r in cap.records if r[1] == "http_request"]
    assert access
    errors = access[-1][2]["validation_errors"]
    assert errors  # the reason is still present
    for e in errors:  # only type/loc/msg — never the raw input value or ctx blob
        assert set(e).issubset({"type", "loc", "msg"})
    assert "SUPER_SECRET_PW" not in str(errors)


def test_redact_validation_errors_drops_value_echoing_msg():
    """A custom validator's msg can interpolate the submitted value verbatim;
    for value_error/assertion_error types the msg is dropped so it cannot
    re-leak the very payload that stripping `input` was meant to keep out."""
    from pydantic import BaseModel, field_validator

    from app.main import _redact_validation_errors

    class _M(BaseModel):
        capability: str

        @field_validator("capability")
        @classmethod
        def _v(cls, v: str) -> str:
            raise ValueError(f"Unsupported capability '{v}'.")

    try:
        _M(capability="SECRET_LEAKED_VALUE")
    except Exception as exc:
        redacted = _redact_validation_errors(exc.errors())
    assert redacted
    assert "SECRET_LEAKED_VALUE" not in str(redacted)
    # A built-in error type keeps its (static, value-free) msg.
    builtin = _redact_validation_errors(
        [{"type": "missing", "loc": ["body", "x"], "msg": "Field required", "input": {}}]
    )
    assert builtin == [{"type": "missing", "loc": ["body", "x"], "msg": "Field required"}]
