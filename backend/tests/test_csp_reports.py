"""Content-Security-Policy violation reports.

Browsers POST what the policy blocked (or would have blocked) to
``/api/v1/csp-report``, in the ``report-uri`` format and in the Reporting API
format. The endpoint is unauthenticated, so it is bounded (body size, reports
per request, a per-IP rate limit, distinct violations tracked) and it never
logs a URL with its query string or fragment - signed file links and the
export's render page carry a credential there. Reports are aggregated: one
line the first time a violation is seen, one summary per window. The
violations are also counted per UTC day in Redis, for every replica at once,
which is what the security audit reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import app.core.rate_limit as rate_limit
import app.services.csp_reports as csp_reports
from app.api.v1.csp_reports import router as csp_report_router
from app.core.base_path import BasePathMiddleware
from app.core.config import settings
from app.core.csp import CSP_REPORT_PATH
from app.services.csp_reports import (
    KNOWN_DIRECTIVES,
    MAX_REPORT_BODY_BYTES,
    MAX_REPORTS_PER_REQUEST,
    RecentViolations,
    Violation,
    ViolationLog,
    count_violations,
    parse_reports,
    recent_violations,
    scrub_blocked,
    scrub_document,
    scrub_source,
    violation_counter_key,
)

SIGNED = "https://xtm.example.com/api/v1/chat/files/42/public?token=SECRET-TOKEN#frag"
EIGHT_DAYS = 8 * 24 * 60 * 60


class _FakeRedis:
    """Just enough of Redis for ``enforce_rate_limit``'s fixed window and the daily violation counters."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self.pipelines: list[list[tuple]] = []
        self.fail: Exception | None = None
        """Raised by every pipeline (the rate limit keeps answering)."""
        self.hang = False
        """Every pipeline waits forever (the rate limit keeps answering)."""

    async def eval(self, _script, _numkeys, key, _window):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def ttl(self, _key):
        return 30

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple] = []

    def hincrby(self, key, name, amount=1):
        self.commands.append(("hincrby", key, name, amount))
        return self

    def expire(self, key, seconds):
        self.commands.append(("expire", key, seconds))
        return self

    def hgetall(self, key):
        self.commands.append(("hgetall", key))
        return self

    async def execute(self):
        if self.redis.hang:
            await asyncio.Event().wait()
        if self.redis.fail is not None:
            raise self.redis.fail
        self.redis.pipelines.append(self.commands)
        results: list[object] = []
        for command, key, *args in self.commands:
            if command == "hincrby":
                name, amount = args
                row = self.redis.hashes.setdefault(key, {})
                row[name] = str(int(row.get(name, "0")) + amount)
                results.append(int(row[name]))
            elif command == "expire":
                self.redis.ttls[key] = args[0]
                results.append(True)
            else:
                results.append(dict(self.redis.hashes.get(key, {})))
        return results


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()

    async def _client():
        return redis

    monkeypatch.setattr(rate_limit, "redis_client", _client)
    monkeypatch.setattr(csp_reports, "redis_client", _client)
    monkeypatch.setattr(csp_reports, "_counter_failure_logged", False)
    return redis


@pytest.fixture
def violation_log(monkeypatch):
    log = ViolationLog()
    monkeypatch.setattr(csp_reports, "violation_log", log)
    return log


def _client(base_path: str = "") -> TestClient:
    app = FastAPI()
    app.include_router(csp_report_router, prefix=CSP_REPORT_PATH)
    app.add_middleware(BasePathMiddleware, base_path=base_path)
    return TestClient(app)


def _csp_report(**overrides) -> dict:
    report = {
        "document-uri": "https://xtm.example.com/presentations/0b7c43de-5d5b-4d2c-9d3f-1f2a3b4c5d6e/static?render_token=SECRET-TOKEN",
        "referrer": "https://xtm.example.com/chat?c=1#x",
        "violated-directive": "img-src 'self'",
        "effective-directive": "img-src",
        "original-policy": "default-src 'self'; report-uri /api/v1/csp-report",
        "disposition": "report",
        "blocked-uri": SIGNED,
        "source-file": "https://xtm.example.com/assets/index-abc.js?v=SECRET-TOKEN",
        "status-code": 200,
        "script-sample": "SECRET-SAMPLE",
    }
    report.update(overrides)
    return {"csp-report": report}


def _reporting_api(**body_overrides) -> list[dict]:
    body = {
        "documentURL": "https://xtm.example.com/traces?session=SECRET-TOKEN",
        "blockedURL": "wss://evil.example.org:8443/socket?token=SECRET-TOKEN",
        "effectiveDirective": "connect-src",
        "originalPolicy": "default-src 'self'",
        "disposition": "enforce",
        "sourceFile": "chrome-extension://abcdefghijklmnopabcdefghijklmnop/content.js",
        "sample": "SECRET-SAMPLE",
        "statusCode": 200,
    }
    body.update(body_overrides)
    return [
        {"type": "csp-violation", "age": 10, "url": body["documentURL"], "user_agent": "x", "body": body},
        {"type": "deprecation", "age": 1, "url": "https://xtm.example.com/", "body": {"id": "x"}},
    ]


# -- scrubbing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (SIGNED, "https://xtm.example.com"),
        ("https://user:pass@cdn.example.com:8443/a.js?x=1", "https://cdn.example.com:8443"),
        ("wss://Evil.Example.org/socket?token=1", "wss://evil.example.org"),
        ("inline", "inline"),
        ("eval", "eval"),
        ("wasm-eval", "wasm-eval"),
        ("data:image/png;base64,iVBORw0KGgo=", "data:"),
        ("blob:https://xtm.example.com/4f1d-uuid", "blob:"),
        ("chrome-extension://abcdefghijklmnop/inject.js?x=1", "chrome-extension:"),
        ("", "(none)"),
        (None, "(none)"),
        ("/relative/path?token=1", "(relative)"),
        ("https://exa\nmple.com/\x00?a", "https://example.com"),
        ("https://exa\u2028mple.com/\x85", "https://example.com"),
        # A host a reader could take for another ``key=value`` field.
        ("https://evil document=/admin/", "(invalid)"),
    ],
)
def test_scrub_blocked(raw, expected):
    assert scrub_blocked(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://xtm.example.com/chat?c=123#frag", "/chat"),
        (
            "https://xtm.example.com/presentations/0b7c43de-5d5b-4d2c-9d3f-1f2a3b4c5d6e/speaker?x=1",
            "/presentations/:id/speaker",
        ),
        ("https://xtm.example.com/runs/12345", "/runs/:id"),
        ("https://xtm.example.com/xtm-one/files/abcdefghijklmnopqrstuvwxyz0123", "/xtm-one/files/:id"),
        ("https://xtm.example.com/", "/"),
        ("about:srcdoc", "about:"),
        ("", "(none)"),
    ],
)
def test_scrub_document(raw, expected):
    assert scrub_document(raw) == expected


def test_scrub_source_keeps_the_script_path_without_its_query():
    assert scrub_source("https://xtm.example.com/assets/index-abc.js?v=SECRET") == (
        "https://xtm.example.com/assets/index-abc.js"
    )
    assert scrub_source("moz-extension://1234/content.js") == "moz-extension:"
    assert scrub_source(None) == ""


def test_scrub_source_collapses_identifiers_and_refuses_a_spoofed_host():
    # An inline violation names the document itself as its source.
    assert scrub_source("https://xtm.example.com/presentations/0b7c43de-5d5b-4d2c-9d3f-1f2a3b4c5d6e/speaker") == (
        "https://xtm.example.com/presentations/:id/speaker"
    )
    assert scrub_source("https://evil document=/admin/x.js") == "(invalid)"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A crafted page URL cannot fake another ``key=value`` field on the line.
        (
            "https://xtm.example.com/chat disposition=enforce directive=script-src",
            "/chat%20disposition%3Denforce%20directive%3Dscript-src",
        ),
        ('https://xtm.example.com/a"b\\c', "/a%22b%5Cc"),
        # Invisible and bidirectional characters reorder what a reader sees.
        ("https://xtm.example.com/admin\u202euser\u200b", "/admin%E2%80%AEuser%E2%80%8B"),
        # What the browser already percent-encoded is kept as it wrote it.
        ("https://xtm.example.com/caf%C3%A9/a%20b", "/caf%C3%A9/a%20b"),
        ("https://xtm.example.com/a:b@c/(x)~y", "/a:b@c/(x)~y"),
    ],
)
def test_scrub_document_encodes_what_could_forge_a_log_field(raw, expected):
    assert scrub_document(raw) == expected


def test_scrub_source_encodes_what_could_forge_a_log_field():
    assert scrub_source("https://xtm.example.com/assets/x y.js disposition=enforce") == (
        "https://xtm.example.com/assets/x%20y.js%20disposition%3Denforce"
    )


def test_a_forged_report_cannot_add_a_field_to_its_log_line(fake_redis, violation_log, caplog):
    forged = "https://xtm.example.com/chat disposition=enforce directive=script-src blocked=https://evil.example"
    with caplog.at_level(logging.WARNING, logger="app.services.csp_reports"):
        response = _client().post(CSP_REPORT_PATH, json=_csp_report(**{"document-uri": forged}))
    assert response.status_code == 204
    [record] = [r for r in caplog.records if "first seen" in r.getMessage()]
    message = record.getMessage()
    for field in ("directive=", "blocked=", "document=", "disposition="):
        assert message.count(field) == 1, message


# -- parsing -----------------------------------------------------------------


def test_parse_the_report_uri_format():
    [violation] = parse_reports(_csp_report())
    assert violation == Violation(
        directive="img-src",
        blocked="https://xtm.example.com",
        document="/presentations/:id/static",
        disposition="report",
        source="https://xtm.example.com/assets/index-abc.js",
        extension=False,
    )


def test_parse_the_reporting_api_format_ignores_other_report_types():
    [violation] = parse_reports(_reporting_api())
    assert violation.directive == "connect-src"
    assert violation.blocked == "wss://evil.example.org:8443"
    assert violation.document == "/traces"
    assert violation.disposition == "enforce"
    # A browser extension's script is not the platform's.
    assert violation.extension is True


def test_the_directive_falls_back_to_the_violated_one():
    [violation] = parse_reports(_csp_report(**{"effective-directive": None}))
    assert violation.directive == "img-src"


@pytest.mark.parametrize("value", ["", None, "not-a-directive!", "x" * 300])
def test_an_unknown_directive_is_bounded(value):
    [violation] = parse_reports(_csp_report(**{"effective-directive": value, "violated-directive": value}))
    assert violation.directive in ("unknown", "other")


def test_reports_per_request_are_capped():
    batch = _reporting_api()[:1] * (MAX_REPORTS_PER_REQUEST + 10)
    assert len(parse_reports(batch)) == MAX_REPORTS_PER_REQUEST


@pytest.mark.parametrize("payload", [{}, [], "text", 12, None, {"csp-report": "x"}, [{"type": "csp-violation"}]])
def test_payloads_that_carry_no_violation(payload):
    assert parse_reports(payload) == []


# -- aggregation -------------------------------------------------------------


def _violation(**overrides) -> Violation:
    fields = {
        "directive": "img-src",
        "blocked": "https://cdn.example.com",
        "document": "/chat",
        "disposition": "report",
        "source": "",
        "extension": False,
    }
    fields.update(overrides)
    return Violation(**fields)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_violation_is_logged_once_then_counted(caplog):
    clock = _Clock()
    log = ViolationLog(window_seconds=60, clock=clock)
    with caplog.at_level(logging.INFO, logger="app.services.csp_reports"):
        for _ in range(5):
            log.record([_violation()])
        clock.now += 61
        log.record([_violation()])
    first_seen = [r for r in caplog.records if "first seen" in r.getMessage()]
    summaries = [r for r in caplog.records if "summary" in r.getMessage()]
    assert len(first_seen) == 1
    assert first_seen[0].levelno == logging.WARNING
    assert "img-src" in first_seen[0].getMessage()
    assert "https://cdn.example.com" in first_seen[0].getMessage()
    assert len(summaries) == 1
    assert "5 violation" in summaries[0].getMessage()


def test_a_violation_seen_again_in_a_later_window_is_not_logged_as_new(caplog):
    clock = _Clock()
    log = ViolationLog(window_seconds=60, clock=clock)
    with caplog.at_level(logging.INFO, logger="app.services.csp_reports"):
        log.record([_violation()])
        clock.now += 61
        log.record([_violation()])
    assert len([r for r in caplog.records if "first seen" in r.getMessage()]) == 1


def test_extension_violations_are_counted_not_logged(caplog):
    clock = _Clock()
    log = ViolationLog(window_seconds=60, clock=clock)
    with caplog.at_level(logging.INFO, logger="app.services.csp_reports"):
        log.record([_violation(extension=True)] * 3)
        clock.now += 61
        log.flush()
    assert not [r for r in caplog.records if "first seen" in r.getMessage()]
    [summary] = [r for r in caplog.records if "summary" in r.getMessage()]
    assert "3 from browser extensions" in summary.getMessage()


def test_distinct_violations_tracked_per_window_are_bounded(caplog):
    log = ViolationLog(window_seconds=60, max_tracked=3, clock=_Clock())
    with caplog.at_level(logging.INFO, logger="app.services.csp_reports"):
        log.record([_violation(blocked=f"https://cdn{i}.example.com") for i in range(10)])
        log.flush()
    assert len([r for r in caplog.records if "first seen" in r.getMessage()]) == 3
    [summary] = [r for r in caplog.records if "summary" in r.getMessage()]
    assert "7 not tracked" in summary.getMessage()


def test_an_empty_window_writes_no_summary(caplog):
    log = ViolationLog(window_seconds=60, clock=_Clock())
    with caplog.at_level(logging.INFO, logger="app.services.csp_reports"):
        log.flush()
    assert not caplog.records


# -- the endpoint ------------------------------------------------------------


def test_accepts_the_report_uri_format(fake_redis, violation_log, caplog):
    with caplog.at_level(logging.INFO, logger="app.services.csp_reports"):
        response = _client().post(
            CSP_REPORT_PATH,
            content=json.dumps(_csp_report()),
            headers={"content-type": "application/csp-report"},
        )
    assert response.status_code == 204
    text = caplog.text
    assert "img-src" in text
    assert "/presentations/:id/static" in text
    assert "SECRET" not in text
    assert "frag" not in text


def test_accepts_the_reporting_api_format(fake_redis, violation_log):
    response = _client().post(
        CSP_REPORT_PATH,
        content=json.dumps(_reporting_api()),
        headers={"content-type": "application/reports+json"},
    )
    assert response.status_code == 204


def test_accepts_plain_json(fake_redis, violation_log):
    response = _client().post(CSP_REPORT_PATH, json=_csp_report())
    assert response.status_code == 204


def test_counts_violations_per_directive(fake_redis, violation_log):
    counter = csp_reports.csp_violation_reports.labels(directive="img-src", disposition="report", origin="page")
    before = counter._value.get()
    _client().post(CSP_REPORT_PATH, json=_csp_report())
    assert counter._value.get() == before + 1


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_refuses_other_content_types(fake_redis, violation_log, content_type):
    headers = {"content-type": content_type} if content_type else {}
    response = _client().post(CSP_REPORT_PATH, content=b"{}", headers=headers)
    assert response.status_code == 415


def test_refuses_malformed_json(fake_redis, violation_log):
    response = _client().post(CSP_REPORT_PATH, content=b"{not json", headers={"content-type": "application/csp-report"})
    assert response.status_code == 400


def test_deeply_nested_json_is_never_a_server_error(fake_redis, violation_log):
    """Nesting well under the size cap may exhaust the parser's recursion
    (platform-dependent): either way the answer is not a 500."""
    body = b"[" * 30000 + b"]" * 30000
    assert len(body) < MAX_REPORT_BODY_BYTES
    response = _client().post(CSP_REPORT_PATH, content=body, headers={"content-type": "application/csp-report"})
    assert response.status_code in (204, 400)


def test_a_parser_recursion_error_is_a_bad_request(fake_redis, violation_log, monkeypatch):
    from app.api.v1 import csp_reports as route

    def exhausted(_body):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(route.json, "loads", exhausted)
    response = _client().post(CSP_REPORT_PATH, content=b"[[[]]]", headers={"content-type": "application/csp-report"})
    assert response.status_code == 400


def test_refuses_a_body_over_the_cap_by_its_declared_length(fake_redis, violation_log):
    response = _client().post(
        CSP_REPORT_PATH,
        content=b"x" * (MAX_REPORT_BODY_BYTES + 1),
        headers={"content-type": "application/csp-report"},
    )
    assert response.status_code == 413


def test_refuses_a_streamed_body_over_the_cap(fake_redis, violation_log):
    def chunks():
        for _ in range(MAX_REPORT_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024

    response = _client().post(CSP_REPORT_PATH, content=chunks(), headers={"content-type": "application/csp-report"})
    assert response.status_code == 413


async def _post_raw(content_length: bytes | None, chunks: list[bytes]) -> tuple[int, bytes, int]:
    """POST *chunks* as the body events of one request, straight to the route's ASGI app.

    No HTTP client in between, so the ``Content-Length`` arrives exactly as
    written. Returns the status, the response body and how many body events the
    route read.
    """
    app = FastAPI()
    app.include_router(csp_report_router, prefix=CSP_REPORT_PATH)
    events = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    read = 0
    sent: list[dict] = []

    async def _receive():
        nonlocal read
        read += 1
        return events[read - 1]

    async def _send(message):
        sent.append(message)

    headers = [(b"content-type", b"application/csp-report")]
    if content_length is not None:
        headers.append((b"content-length", content_length))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": CSP_REPORT_PATH,
        "raw_path": CSP_REPORT_PATH.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("xtm.test", 443),
    }
    await app(scope, _receive, _send)
    [start] = [m for m in sent if m["type"] == "http.response.start"]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body, read


_REPORT = json.dumps(_csp_report()).encode()
_TOO_LARGE = b'{"detail":"Report too large"}'
# Chunks of 1 KiB: the one that crosses the cap is the last one read.
_OVER_THE_CAP = [b" " * 1024] * (MAX_REPORT_BODY_BYTES // 1024 + 4)


@pytest.mark.parametrize(
    ("content_length", "chunks", "status_code", "body", "events_read"),
    [
        # ``int()`` refuses more than 4300 digits: the previous reader answered 500.
        pytest.param(b"9" * 5000, [_REPORT], 413, _TOO_LARGE, 0, id="declared-5000-digits"),
        # ``isdigit()`` accepts it and ``int()`` refuses it: the previous reader answered 500.
        # It declares no length, so the bytes read decide.
        pytest.param("\u00b2".encode("latin-1"), [_REPORT], 204, b"", 1, id="declared-non-ascii-digit"),
        pytest.param(
            "\u00b2".encode("latin-1"),
            _OVER_THE_CAP,
            413,
            _TOO_LARGE,
            MAX_REPORT_BODY_BYTES // 1024 + 1,
            id="declared-non-ascii-digit-body-over-the-cap",
        ),
        pytest.param(
            str(MAX_REPORT_BODY_BYTES + 1).encode(), [_REPORT], 413, _TOO_LARGE, 0, id="declared-over-the-cap"
        ),
        # RFC 9110 optional whitespace around the value.
        pytest.param(
            f" {MAX_REPORT_BODY_BYTES + 1}\t".encode(), [_REPORT], 413, _TOO_LARGE, 0, id="declared-over-the-cap-padded"
        ),
        pytest.param(
            None, _OVER_THE_CAP, 413, _TOO_LARGE, MAX_REPORT_BODY_BYTES // 1024 + 1, id="chunked-over-the-cap"
        ),
        pytest.param(str(len(_REPORT)).encode(), [_REPORT], 204, b"", 1, id="normal-report"),
    ],
)
async def test_the_body_is_read_under_the_cap_whatever_its_declared_length(
    fake_redis, violation_log, today, content_length, chunks, status_code, body, events_read
):
    """Refused by its declared length before a byte is read, else cut off at the
    event that crosses the cap; a declared length is never a server error."""
    assert await _post_raw(content_length, chunks) == (status_code, body, events_read)
    key = violation_counter_key(today)
    counted = [[("hincrby", key, "img-src", 1), ("expire", key, EIGHT_DAYS)]]
    assert fake_redis.pipelines == (counted if status_code == 204 else [])


async def test_a_chunk_over_the_cap_is_refused_on_arrival(fake_redis, violation_log):
    """ASGI does not bound the size of one body event: one that alone exceeds
    the cap is refused as it arrives, nothing after it is read and nothing is
    parsed or counted."""
    chunks = [b"x" * (2 * MAX_REPORT_BODY_BYTES), _REPORT]
    assert await _post_raw(None, chunks) == (413, _TOO_LARGE, 1)
    assert fake_redis.pipelines == []


def test_a_body_of_exactly_the_cap_is_read(fake_redis, violation_log):
    body = json.dumps(_csp_report()).encode()
    body += b" " * (MAX_REPORT_BODY_BYTES - len(body))
    assert len(body) == MAX_REPORT_BODY_BYTES
    response = _client().post(CSP_REPORT_PATH, content=body, headers={"content-type": "application/csp-report"})
    assert response.status_code == 204


def test_is_rate_limited_per_client(fake_redis, violation_log):
    client = _client()
    statuses = [
        client.post(CSP_REPORT_PATH, json=_csp_report()).status_code
        for _ in range(csp_reports.CSP_REPORT_RATE_LIMIT + 1)
    ]
    assert statuses[:-1] == [204] * csp_reports.CSP_REPORT_RATE_LIMIT
    assert statuses[-1] == 429


@pytest.mark.parametrize(("fail_mode", "status_code"), [("open", 204), ("closed", 503)])
def test_a_redis_outage_writes_no_rate_limit_line_per_report(violation_log, monkeypatch, caplog, fail_mode, status_code):
    """Browsers send reports on their own, from every page: while Redis is down
    the rate limit cannot count them, and one warning with a traceback per
    report would flood the log. The outage is said once, by the counters."""

    async def _down():
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(rate_limit, "redis_client", _down)
    monkeypatch.setattr(csp_reports, "redis_client", _down)
    monkeypatch.setattr(csp_reports, "_counter_failure_logged", False)
    monkeypatch.setattr(settings, "rate_limit_fail_mode", fail_mode)
    with caplog.at_level(logging.DEBUG):
        statuses = [_client().post(CSP_REPORT_PATH, json=_csp_report()).status_code for _ in range(3)]
    assert statuses == [status_code] * 3
    loud = [r for r in caplog.records if r.name == "app.core.rate_limit" and r.levelno >= logging.WARNING]
    assert loud == []
    assert [r.name for r in caplog.records if r.name == "app.core.rate_limit"] == ["app.core.rate_limit"] * 3


@pytest.mark.parametrize("fail_mode", ["open", "closed"])
async def test_other_rate_limited_routes_keep_their_redis_failure_warning(monkeypatch, caplog, fail_mode):
    from fastapi import HTTPException

    async def _down():
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(rate_limit, "redis_client", _down)
    monkeypatch.setattr(settings, "rate_limit_fail_mode", fail_mode)
    with caplog.at_level(logging.WARNING, logger="app.core.rate_limit"):
        try:
            await rate_limit.enforce_rate_limit(None, "login", limit=5, window_seconds=60, bucket="1.2.3.4")
        except HTTPException as exc:
            assert (fail_mode, exc.status_code) == ("closed", 503)
    [record] = caplog.records
    assert record.levelno == (logging.WARNING if fail_mode == "open" else logging.ERROR)
    assert record.exc_info is not None


def test_is_reachable_under_the_url_path_prefix(fake_redis, violation_log, monkeypatch):
    monkeypatch.setattr(settings, "base_path", "/xtm-one")
    client = _client(base_path="/xtm-one")
    for path in ("/xtm-one/api/v1/csp-report", "/api/v1/csp-report"):
        assert client.post(path, json=_csp_report()).status_code == 204


# -- counters shared by every replica ----------------------------------------


@pytest.fixture
def today(monkeypatch) -> date:
    day = date(2026, 9, 28)
    monkeypatch.setattr(csp_reports, "_utc_today", lambda: day)
    return day


def _batch(directive: str, count: int) -> list[dict]:
    return [
        {
            "type": "csp-violation",
            "url": "https://xtm.example.com/chat",
            "body": {
                "documentURL": "https://xtm.example.com/chat",
                "blockedURL": f"https://cdn{index}.example.com/a.js",
                "effectiveDirective": directive,
                "disposition": "report",
            },
        }
        for index in range(count)
    ]


def _post_batch(batch: list[dict]):
    return _client().post(
        CSP_REPORT_PATH, content=json.dumps(batch), headers={"content-type": "application/reports+json"}
    )


def test_the_counters_are_one_hash_per_utc_day():
    assert violation_counter_key(date(2026, 9, 28)) == "csp:violations:20260928"


def test_a_report_is_counted_in_the_hash_of_its_day_for_eight_days(fake_redis, violation_log, today):
    assert _client().post(CSP_REPORT_PATH, json=_csp_report()).status_code == 204
    key = violation_counter_key(today)
    assert fake_redis.hashes == {key: {"img-src": "1"}}
    assert fake_redis.ttls == {key: EIGHT_DAYS}


def test_one_request_is_one_round_trip(fake_redis, violation_log, today):
    batch = _batch("connect-src", 20) + _batch("img-src", 12)
    assert len(batch) == MAX_REPORTS_PER_REQUEST
    assert _post_batch(batch).status_code == 204
    key = violation_counter_key(today)
    assert fake_redis.pipelines == [
        [("hincrby", key, "connect-src", 20), ("hincrby", key, "img-src", 12), ("expire", key, EIGHT_DAYS)]
    ]
    assert fake_redis.hashes[key] == {"connect-src": "20", "img-src": "12"}


def test_the_fields_are_the_known_directives_other_and_extensions(fake_redis, violation_log, today):
    batch = (
        _batch("IMG-SRC", 1)
        + _batch("made-up-src", 1)
        # No directive at all.
        + _batch("", 1)
        + _batch("x" * 300, 1)
        + _batch("img-src; script-src *", 1)
        # A browser extension's script, whatever directive it broke.
        + _reporting_api()[:1]
    )
    assert _post_batch(batch).status_code == 204
    assert fake_redis.hashes[violation_counter_key(today)] == {"img-src": "1", "other": "4", "extensions": "1"}


def test_the_hash_never_grows_past_the_known_directives(fake_redis, violation_log, today):
    names = sorted(KNOWN_DIRECTIVES) + [f"invented-{index}-src" for index in range(20)]
    for start in range(0, len(names), MAX_REPORTS_PER_REQUEST):
        batch = [item for name in names[start : start + MAX_REPORTS_PER_REQUEST] for item in _batch(name, 1)]
        assert _post_batch(batch).status_code == 204
    fields = fake_redis.hashes[violation_counter_key(today)]
    assert set(fields) == KNOWN_DIRECTIVES | {"other"}
    assert fields["other"] == "20"


def test_browser_extension_noise_is_counted_apart(fake_redis, violation_log, today):
    assert _post_batch(_reporting_api()[:1] * 3).status_code == 204
    assert fake_redis.hashes[violation_counter_key(today)] == {"extensions": "3"}


class _LateEveningWestOfUtc(datetime):
    """23:30 on 27 September at UTC-2: already 28 September in UTC."""

    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 9, 28, 1, 30, tzinfo=UTC)
        if tz is None:
            return instant.astimezone(timezone(timedelta(hours=-2))).replace(tzinfo=None)
        return instant.astimezone(tz)


def test_the_day_is_the_utc_day(fake_redis, violation_log, monkeypatch):
    monkeypatch.setattr(csp_reports, "datetime", _LateEveningWestOfUtc)
    assert _client().post(CSP_REPORT_PATH, json=_csp_report()).status_code == 204
    assert list(fake_redis.hashes) == ["csp:violations:20260928"]


def test_a_redis_failure_never_fails_the_report_or_its_logging(fake_redis, violation_log, caplog, today):
    fake_redis.fail = TimeoutError()
    with caplog.at_level(logging.DEBUG, logger="app.services.csp_reports"):
        first = _client().post(CSP_REPORT_PATH, json=_csp_report())
        second = _client().post(CSP_REPORT_PATH, json=_csp_report(**{"effective-directive": "font-src"}))
    assert (first.status_code, second.status_code) == (204, 204)
    assert len([r for r in caplog.records if "first seen" in r.getMessage()]) == 2
    failures = [r for r in caplog.records if "could not be counted" in r.getMessage()]
    # Once per process at warning, then at debug.
    assert [r.levelno for r in failures] == [logging.WARNING, logging.DEBUG]
    # ``str(TimeoutError())`` is empty: the line still says what failed.
    assert "Redis: TimeoutError." in failures[0].getMessage()
    assert fake_redis.hashes == {}


def test_a_redis_that_does_not_answer_holds_no_report(fake_redis, violation_log, monkeypatch, caplog, today):
    monkeypatch.setattr(csp_reports, "_COUNTER_TIMEOUT_SECONDS", 0.05)
    fake_redis.hang = True
    with caplog.at_level(logging.WARNING, logger="app.services.csp_reports"):
        assert _client().post(CSP_REPORT_PATH, json=_csp_report()).status_code == 204
    assert [r for r in caplog.records if "first seen" in r.getMessage()]
    [failure] = [r for r in caplog.records if "could not be counted" in r.getMessage()]
    assert "Redis: TimeoutError" in failure.getMessage()


async def test_nothing_to_count_costs_no_round_trip(fake_redis):
    await count_violations([])
    assert fake_redis.pipelines == []


def _seed(redis: _FakeRedis, day: date, **fields: int) -> None:
    redis.hashes[violation_counter_key(day)] = {name.replace("_", "-"): str(count) for name, count in fields.items()}


async def test_recent_violations_sum_the_last_seven_utc_days(fake_redis, today):
    _seed(fake_redis, today, img_src=3, extensions=2)
    _seed(fake_redis, today - timedelta(days=3), connect_src=4)
    _seed(fake_redis, today - timedelta(days=6), img_src=1, connect_src=1, other=1)
    # Seven days before today: outside the period, even while Redis still holds it.
    _seed(fake_redis, today - timedelta(days=7), img_src=100, extensions=100)
    recent = await recent_violations()
    assert recent == RecentViolations(
        days=7, known=True, by_directive={"connect-src": 5, "img-src": 4, "other": 1}, extensions=2
    )
    # Largest first.
    assert list(recent.by_directive) == ["connect-src", "img-src", "other"]
    assert recent.total == 10
    [pipeline] = fake_redis.pipelines
    assert pipeline == [("hgetall", violation_counter_key(today - timedelta(days=offset))) for offset in range(7)]


async def test_recent_violations_of_a_quiet_week_are_zero_and_known(fake_redis, today):
    recent = await recent_violations()
    assert (recent.known, recent.total, recent.extensions) == (True, 0, 0)


async def test_recent_violations_are_unknown_when_redis_cannot_be_read(fake_redis, caplog, today):
    _seed(fake_redis, today, img_src=3)
    fake_redis.fail = ConnectionError("Connection refused")
    with caplog.at_level(logging.WARNING, logger="app.services.csp_reports"):
        recent = await recent_violations()
    assert recent.known is False
    assert recent.days == 7
    [record] = [r for r in caplog.records if "could not be read" in r.getMessage()]
    assert "Connection refused" in record.getMessage()


async def test_recent_violations_are_unknown_when_redis_does_not_answer(fake_redis, monkeypatch, today):
    monkeypatch.setattr(csp_reports, "_COUNTER_TIMEOUT_SECONDS", 0.05)
    fake_redis.hang = True
    recent = await recent_violations()
    assert recent.known is False


@pytest.mark.parametrize("days", [0, -1, 9])
async def test_recent_violations_cover_no_more_than_the_counters_keep(fake_redis, days):
    with pytest.raises(ValueError, match="keep 8 days"):
        await recent_violations(days)


async def test_a_field_this_version_does_not_know_is_read_as_other(fake_redis, today):
    fake_redis.hashes[violation_counter_key(today)] = {"fenced-frame-src": "2", "img-src": "garbled", "font-src": "1"}
    recent = await recent_violations()
    assert recent.by_directive == {"other": 2, "font-src": 1}
