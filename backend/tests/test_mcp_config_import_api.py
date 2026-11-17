"""The HTTP surface for importing an MCP configuration.

The parsing itself is covered in ``test_mcp_config_import.py``; what matters
here is the endpoint behaviour around it — that a URL the platform is asked to
fetch is held to the same destination rules as any other outbound request, and
that one bad entry in a batch does not cost the user the rest.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import socket
import ssl
import time
import tracemalloc
import uuid
import zlib
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError

from app.a2a.client import destinations
from app.api import deps
from app.api.v1 import mcp_servers as mcp_api
from app.api.v1.schemas.mcp_server import MCPServerResponse
from app.core.database import get_db


class _FakeDb:
    """Records what the endpoint did without touching a database."""

    def __init__(self):
        self.added: list[object] = []
        self.commits = 0

    def in_transaction(self):
        # As after authentication: a transaction is open and holds a connection.
        return True

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        return None

    async def rollback(self):
        return None


def _user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="importer@example.com",
        full_name="Importer",
        role="user",
        is_admin=False,
    )


def _client(monkeypatch, *, user=None):
    user = user or _user()
    fake_db = _FakeDb()
    audit: list[dict] = []

    async def _log_audit(_db, **kwargs):
        audit.append(kwargs)

    def _to_response(row, owner_name=None, **_kwargs):
        # The real serializer reads the whole ORM row off a live session; these
        # tests are about what was created, so only the fields the response
        # model requires are filled in.
        now = datetime.now(UTC).isoformat()
        return MCPServerResponse(
            id=str(getattr(row, "id", None) or uuid.uuid4()),
            name=row.name,
            transport=row.transport,
            enabled=True,
            created_at=now,
            updated_at=now,
        )

    monkeypatch.setattr(mcp_api, "log_audit", _log_audit)
    monkeypatch.setattr(mcp_api, "_to_response", _to_response)

    app = FastAPI()
    app.include_router(mcp_api.router, prefix="/api/v1/mcp-servers")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None

    async def _get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app), fake_db, audit


@pytest.fixture(autouse=True)
def rate_limit_calls(monkeypatch):
    """Record the limiter's calls instead of reaching Redis.

    Tests about the limiter itself replace this with a counting backend.
    """
    calls: list[dict] = []

    async def _record(request, name, **kwargs):
        calls.append({"name": name, **kwargs})

    monkeypatch.setattr(mcp_api, "enforce_rate_limit", _record)
    return calls


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """No test in this module may reach DNS or open a socket.

    A test that needs either stubs it. Anything that lands here instead is
    recorded and fails the test at teardown: raised inside the fetch, it would
    only have been folded into the generic "no document" answer.
    """
    reached: list[str] = []

    async def _resolve(host, _port):
        reached.append(f"dns {host}")
        raise socket.gaierror(-2, "blocked in tests")

    def _stream(_self, _method, url, **_kwargs):
        reached.append(f"http {url}")
        raise httpx.ConnectError("blocked in tests")

    monkeypatch.setattr(destinations, "_resolve", _resolve)
    monkeypatch.setattr(httpx.AsyncClient, "stream", _stream)
    yield
    assert reached == [], f"a test reached the network: {reached}"


_PUBLIC_ADDRESS = "93.184.216.34"


def _dns(monkeypatch, answers=None):
    """Stand in for DNS under the real destination guard.

    Every name resolves to a public address unless ``answers`` maps it to
    another address or to an exception to raise.
    """
    answers = answers or {}
    looked_up: list[str] = []

    async def _resolve(host, port):
        looked_up.append(host)
        answer = answers.get(host, _PUBLIC_ADDRESS)
        if isinstance(answer, BaseException):
            raise answer
        family = socket.AF_INET6 if ":" in answer else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (answer, port))]

    monkeypatch.setattr(destinations, "_resolve", _resolve)
    return looked_up


CLIENT_CONFIG = """
{"mcpServers": {"github": {"command": "npx", "args": ["-y", "@mcp/github"],
 "env": {"GITHUB_TOKEN": "ghp_x"}}}}
"""


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_preview_reads_inline_content(monkeypatch):
    client, db, _ = _client(monkeypatch)

    resp = client.post("/api/v1/mcp-servers/import/config/preview", json={"content": CLIENT_CONFIG})

    assert resp.status_code == 200
    body = resp.json()
    assert body["format"] == "mcp_servers"
    assert [c["name"] for c in body["candidates"]] == ["github"]
    # A preview writes nothing: that is the whole reason it is a separate call.
    assert db.added == [] and db.commits == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"content": "{}", "url": "https://example.com/server.json"},
    ],
)
def test_preview_needs_exactly_one_source(monkeypatch, payload):
    client, _db, _ = _client(monkeypatch)
    resp = client.post("/api/v1/mcp-servers/import/config/preview", json=payload)
    assert resp.status_code == 400
    assert "either" in resp.json()["detail"].lower()


def test_preview_reports_an_unreadable_document_as_a_bad_request(monkeypatch):
    client, _db, _ = _client(monkeypatch)
    resp = client.post("/api/v1/mcp-servers/import/config/preview", json={"content": "not json"})
    assert resp.status_code == 400
    assert "json" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Preview from a URL — the request-forgery surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud credentials
        "http://127.0.0.1:8000/config.json",  # loopback
        "http://10.0.0.5/server.json",  # private range
        "file:///etc/passwd",  # not even http
    ],
)
def test_preview_refuses_a_url_pointing_inward(monkeypatch, url):
    """The user types the URL; the *platform* makes the request.

    Without this the box is a request-forgery primitive pointed at whatever
    the deployment can reach, the metadata endpoint most of all.
    """
    client, _db, _ = _client(monkeypatch)

    def _no_network(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("A refused URL must not be fetched")

    monkeypatch.setattr(httpx.AsyncClient, "get", _no_network)

    resp = client.post("/api/v1/mcp-servers/import/config/preview", json={"url": url})
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    # Worded for someone importing an MCP config, not for an A2A operator:
    # the shared guard's own message names A2A partners, which is a puzzle on
    # a screen that mentions neither.
    assert "a2a" not in detail
    assert "will not fetch from" in detail or "must use https" in detail


class _FakeStream:
    """Stands in for httpx's network_stream, which reports the peer address."""

    def __init__(self, peer: str | None):
        self._peer = peer

    def get_extra_info(self, key):
        return (self._peer, 443) if key == "server_addr" and self._peer else None


async def _streamed(body: bytes):
    yield body


def _network_response(status, headers=None, body=b"", *, peer="93.184.216.34"):
    """A response as the network hands it over: streamed, not read yet.

    ``httpx.Response(content=<bytes>)`` reads its body on construction, which
    no response from a socket does; a body that is already an async iterator
    is used as is.
    """
    content = _streamed(body) if isinstance(body, bytes) else body
    resp = httpx.Response(status, headers=headers or {}, content=content)
    resp.extensions["network_stream"] = _FakeStream(peer)
    return resp


def _stub_stream(monkeypatch, responses, *, peer="93.184.216.34"):
    """Patch ``AsyncClient.stream`` with a scripted sequence of responses.

    ``responses`` is a list of (status, headers, body) consumed one per hop, so
    a redirect chain can be described directly.
    """
    calls: list[str] = []
    remaining = list(responses)

    class _CM:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            status, headers, body = remaining.pop(0)
            return _network_response(status, headers, body, peer=peer)

        async def __aexit__(self, *_exc):
            return False

    def _stream(_self, _method, url, **_kwargs):
        calls.append(url)
        return _CM(url)

    monkeypatch.setattr(httpx.AsyncClient, "stream", _stream)
    return calls


def _assert_no_document(resp):
    """The one answer for every URL that produced no document.

    Status, body and headers, so that no failure can be told from another by
    anything but how long it took.
    """
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": mcp_api._CONFIG_FETCH_FAILED_DETAIL}
    assert sorted(resp.headers) == ["content-length", "content-type"]


def test_preview_fetches_an_allowed_url(monkeypatch):
    client, _db, _ = _client(monkeypatch)

    async def _allowed(_url, **_kwargs):
        return None

    monkeypatch.setattr(mcp_api, "assert_allowed_async", _allowed)
    _stub_stream(monkeypatch, [(200, {}, CLIENT_CONFIG.encode())])

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://example.com/config.json"},
    )
    assert resp.status_code == 200
    assert [c["name"] for c in resp.json()["candidates"]] == ["github"]


def test_preview_re_checks_the_target_of_a_redirect(monkeypatch):
    """A public URL that redirects inward must not walk past the check."""
    client, _db, _ = _client(monkeypatch)
    checked: list[str] = []

    async def _allowed(url, **_kwargs):
        checked.append(url)
        if "169.254.169.254" in url:
            raise mcp_api.DestinationRefusedError("cloud metadata endpoint", reason="forbidden_address")

    monkeypatch.setattr(mcp_api, "assert_allowed_async", _allowed)
    requested = _stub_stream(
        monkeypatch,
        [(302, {"location": "http://169.254.169.254/"}, b"")],
    )

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://example.com/redirect"},
    )
    # The target is refused before it is requested. Where a remote server
    # tried to send the platform is its answer, not the user's input, so the
    # refusal reads like every other fetch that produced no document.
    _assert_no_document(resp)
    assert any("169.254.169.254" in u for u in checked)
    assert requested == ["https://example.com/redirect"]


def test_preview_refuses_a_host_that_resolves_inward_at_connect_time(monkeypatch):
    """The DNS-to-connect gap: the name passed, the socket went elsewhere.

    ``assert_allowed_async`` resolves the hostname, then httpx resolves it
    again to connect. A name the attacker controls can answer publicly for the
    first and privately for the second, so the address actually reached is
    what has to be checked.
    """
    client, _db, _ = _client(monkeypatch)
    seen: list[str] = []

    async def _allowed(url, **_kwargs):
        seen.append(url)
        # The hostname passes; only the literal peer address is refused.
        if url.startswith("https://169.254.169.254"):
            raise mcp_api.DestinationRefusedError("cloud metadata endpoint", reason="forbidden_address")

    monkeypatch.setattr(mcp_api, "assert_allowed_async", _allowed)
    _stub_stream(
        monkeypatch,
        [(200, {}, CLIENT_CONFIG.encode())],
        peer="169.254.169.254",
    )

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://rebound.example.com/config.json"},
    )
    _assert_no_document(resp)
    # The peer address was the thing checked, not just the name.
    assert any(u.startswith("https://169.254.169.254") for u in seen)


def test_preview_refuses_a_connection_whose_address_it_cannot_confirm(monkeypatch):
    """No address to check is not the same as an address that passed."""
    client, _db, _ = _client(monkeypatch)

    async def _allowed(_url, **_kwargs):
        return None

    monkeypatch.setattr(mcp_api, "assert_allowed_async", _allowed)
    _stub_stream(monkeypatch, [(200, {}, CLIENT_CONFIG.encode())], peer=None)

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://example.com/config.json"},
    )
    # Refused - the document was there and is still not read.
    _assert_no_document(resp)


def test_preview_stops_reading_an_oversized_body(monkeypatch):
    """The cap must bound memory, not be consulted once the body is in it."""
    client, _db, _ = _client(monkeypatch)
    _dns(monkeypatch)
    chunk = b"x" * 65_536
    pulled: list[int] = []

    async def _endless():
        while True:
            pulled.append(len(chunk))
            yield chunk

    _stub_stream(monkeypatch, [(200, {}, _endless())])

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://example.com/huge.json"},
    )
    _assert_no_document(resp)
    # Reading stopped at the first chunk past the cap.
    assert sum(pulled) <= mcp_api.MAX_DOCUMENT_BYTES + len(chunk)


# ---------------------------------------------------------------------------
# Throttle and failure detail
# ---------------------------------------------------------------------------


async def _allow_everything(_url, **_kwargs):
    return None


def test_preview_by_url_is_throttled_per_user(monkeypatch, rate_limit_calls):
    user = _user()
    client, _db, _ = _client(monkeypatch, user=user)
    monkeypatch.setattr(mcp_api, "assert_allowed_async", _allow_everything)
    _stub_stream(monkeypatch, [(200, {}, CLIENT_CONFIG.encode())])

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://example.com/config.json"},
    )

    assert resp.status_code == 200
    assert rate_limit_calls == [
        {
            "name": "mcp-config-fetch",
            "limit": 20,
            "window_seconds": 60,
            "bucket": str(user.id),
        }
    ]


def test_preview_of_inline_content_is_not_throttled(monkeypatch, rate_limit_calls):
    """Nothing is fetched, so nothing the limit exists for is spent."""
    client, _db, _ = _client(monkeypatch)

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview", json={"content": CLIENT_CONFIG}
    )

    assert resp.status_code == 200
    assert rate_limit_calls == []


def test_preview_by_url_answers_429_past_the_limit(monkeypatch):
    """Through the real limiter, against a counting stand-in for Redis."""
    from app.core import rate_limit

    counters: dict[str, int] = {}

    class _CountingRedis:
        async def eval(self, _script, _numkeys, key, _window):
            counters[key] = counters.get(key, 0) + 1
            return counters[key]

        async def ttl(self, _key):
            return 42

    async def _redis():
        return _CountingRedis()

    monkeypatch.setattr(rate_limit, "redis_client", _redis)
    monkeypatch.setattr(mcp_api, "enforce_rate_limit", rate_limit.enforce_rate_limit)
    client, _db, _ = _client(monkeypatch)
    monkeypatch.setattr(mcp_api, "assert_allowed_async", _allow_everything)
    _stub_stream(monkeypatch, [(200, {}, CLIENT_CONFIG.encode())] * 20)

    for _ in range(20):
        resp = client.post(
            "/api/v1/mcp-servers/import/config/preview",
            json={"url": "https://example.com/config.json"},
        )
        assert resp.status_code == 200

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://example.com/config.json"},
    )
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "42"


def test_preview_by_url_hands_back_the_auth_connection_before_fetching(monkeypatch):
    client, db, _ = _client(monkeypatch)
    order: list[str] = []

    async def _commit():
        order.append("released")

    async def _allowed(_url, **_kwargs):
        order.append("fetched")

    monkeypatch.setattr(db, "commit", _commit)
    monkeypatch.setattr(mcp_api, "assert_allowed_async", _allowed)
    _stub_stream(monkeypatch, [(200, {}, CLIENT_CONFIG.encode())])

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://example.com/config.json"},
    )

    assert resp.status_code == 200
    assert order[:2] == ["released", "fetched"]


def test_preview_by_url_throttles_then_releases_then_fetches(monkeypatch):
    """The budget is spent before anything else, and nothing outbound precedes it."""
    client, db, _ = _client(monkeypatch)
    order: list[str] = []

    async def _throttle(_request, _name, **_kwargs):
        order.append("throttled")

    async def _commit():
        order.append("released")

    async def _resolve(host, port):
        order.append("resolved")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_ADDRESS, port))]

    monkeypatch.setattr(mcp_api, "enforce_rate_limit", _throttle)
    monkeypatch.setattr(db, "commit", _commit)
    monkeypatch.setattr(destinations, "_resolve", _resolve)
    requested = _stub_stream(monkeypatch, [(200, {}, CLIENT_CONFIG.encode())])

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://example.com/config.json"},
    )

    assert resp.status_code == 200
    assert order == ["throttled", "released", "resolved"]
    assert requested == ["https://example.com/config.json"]


def test_preview_past_the_limit_does_no_outbound_work(monkeypatch):
    client, db, _ = _client(monkeypatch)

    async def _throttled(_request, _name, **_kwargs):
        raise HTTPException(status_code=429, detail="Too many requests.", headers={"Retry-After": "7"})

    monkeypatch.setattr(mcp_api, "enforce_rate_limit", _throttled)

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://example.com/config.json"},
    )

    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "7"
    # Neither DNS nor a socket (the autouse guard fails the test otherwise),
    # and the session is left to the request's own teardown.
    assert db.commits == 0


def test_preview_by_url_uses_the_session_only_to_release_it(monkeypatch):
    """After the release nothing reads or writes through the session again."""
    client, db, audit = _client(monkeypatch)
    _dns(monkeypatch)
    _stub_stream(monkeypatch, [(200, {}, CLIENT_CONFIG.encode())])

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://example.com/config.json"},
    )

    assert resp.status_code == 200
    assert db.commits == 1
    assert db.added == [] and audit == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:99999/config.json",  # port out of range
        "https://[::1/config.json",  # unbalanced IPv6 bracket
        "https://exa\tmple.com/config.json",  # a character the fetch refuses
        "https://\u2603.example/config.json",  # not a valid IDNA 2008 name
    ],
)
def test_preview_refuses_an_unusable_url_before_spending_anything(
    monkeypatch, rate_limit_calls, url
):
    """Decided from the string alone: no DNS, no socket, no budget, no 500."""
    client, db, _ = _client(monkeypatch)

    resp = client.post("/api/v1/mcp-servers/import/config/preview", json={"url": url})

    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("That is not a usable URL")
    assert rate_limit_calls == [] and db.commits == 0


def test_preview_refuses_a_url_pointing_inward_before_spending_the_budget(
    monkeypatch, rate_limit_calls
):
    client, _db, _ = _client(monkeypatch)

    resp = client.post(
        "/api/v1/mcp-servers/import/config/preview",
        json={"url": "https://10.0.0.5/server.json"},
    )

    assert resp.status_code == 400
    assert "will not fetch from" in resp.json()["detail"]
    assert rate_limit_calls == []


# ---------------------------------------------------------------------------
# Every fetch that produced no document answers the same thing
# ---------------------------------------------------------------------------

_PROBE = {"url": "https://config.example.com:8443/servers.json?token=secret"}
_PROBE_HOST = "config.example.com"


def _raise_on_request(monkeypatch, exc):
    def _stream(_self, _method, _url, **_kwargs):
        raise exc

    monkeypatch.setattr(httpx.AsyncClient, "stream", _stream)


def _gzip_bomb(decoded_bytes: int) -> bytes:
    """A small gzip body that inflates to ``decoded_bytes`` of zeros."""
    packer = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    block = b"\0" * 1_000_000
    parts = [packer.compress(block) for _ in range(decoded_bytes // len(block))]
    parts.append(packer.flush())
    return b"".join(parts)


def _redirect(location):
    return (302, {"location": location}, b"")


def _answer(monkeypatch, responses, **kwargs):
    _dns(monkeypatch, kwargs.pop("dns", None))
    _stub_stream(monkeypatch, responses, **kwargs)


_NO_DOCUMENT_CASES = {
    # Name resolution, from the backend's own resolver.
    "name does not resolve": lambda mp: _dns(mp, {_PROBE_HOST: socket.gaierror(-2, "Name or service not known")}),
    "name resolves to a private address": lambda mp: _dns(mp, {_PROBE_HOST: "10.1.2.3"}),
    "name resolves to the metadata endpoint": lambda mp: _dns(mp, {_PROBE_HOST: "169.254.169.254"}),
    # Connecting, inside and outside httpx's own exception family.
    "connection refused": lambda mp: (_dns(mp), _raise_on_request(mp, httpx.ConnectError("[Errno 111] Connection refused"))),
    "connect timeout": lambda mp: (_dns(mp), _raise_on_request(mp, httpx.ConnectTimeout("timed out"))),
    "read timeout": lambda mp: (_dns(mp), _raise_on_request(mp, httpx.ReadTimeout(""))),
    "TLS failure": lambda mp: (_dns(mp), _raise_on_request(mp, httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))),
    "server hung up": lambda mp: (_dns(mp), _raise_on_request(mp, httpx.RemoteProtocolError("Server disconnected without sending a response."))),
    "raw ssl.SSLError": lambda mp: (_dns(mp), _raise_on_request(mp, ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))),
    "raw OSError": lambda mp: (_dns(mp), _raise_on_request(mp, OSError(113, "No route to host"))),
    "raw socket.gaierror": lambda mp: (_dns(mp), _raise_on_request(mp, socket.gaierror(-3, "Temporary failure in name resolution"))),
    "raw TimeoutError": lambda mp: (_dns(mp), _raise_on_request(mp, TimeoutError())),
    "unexpected library error": lambda mp: (_dns(mp), _raise_on_request(mp, RuntimeError("h11 state machine confused"))),
    # An HTTP answer that is not a document.
    "HTTP 401": lambda mp: _answer(mp, [(401, {"www-authenticate": "Basic"}, b"login")]),
    "HTTP 404": lambda mp: _answer(mp, [(404, {}, b"not found")]),
    "HTTP 502": lambda mp: _answer(mp, [(502, {"server": "nginx/1.25"}, b"bad gateway")]),
    # Redirects the remote server chose.
    "redirect loop": lambda mp: _answer(mp, [_redirect("https://config.example.com:8443/next")] * mcp_api._CONFIG_FETCH_MAX_REDIRECTS),
    "redirect without a target": lambda mp: _answer(mp, [(302, {}, b"")]),
    "redirect to a malformed URL": lambda mp: _answer(mp, [_redirect("https://[::1")]),
    "redirect to an out-of-range port": lambda mp: _answer(mp, [_redirect("https://config.example.com:99999/x")]),
    "redirect to the metadata endpoint": lambda mp: _answer(mp, [_redirect("https://169.254.169.254/latest/")]),
    "redirect to plain http": lambda mp: _answer(mp, [_redirect("http://config.example.com/servers.json")]),
    "redirect to a name that resolves inward": lambda mp: _answer(
        mp, [_redirect("https://intranet.example.com/x")], dns={"intranet.example.com": "10.9.8.7"}
    ),
    # The address actually connected to.
    "peer is a refused address": lambda mp: _answer(mp, [(200, {}, CLIENT_CONFIG.encode())], peer="169.254.169.254"),
    "peer address unknown": lambda mp: _answer(mp, [(200, {}, CLIENT_CONFIG.encode())], peer=None),
    "peer is not an address": lambda mp: _answer(mp, [(200, {}, CLIENT_CONFIG.encode())], peer="config.example.com"),
    # A body that is not a document this can read.
    "body over the size cap": lambda mp: _answer(mp, [(200, {}, b"x" * (mcp_api.MAX_DOCUMENT_BYTES + 1))]),
    "body not UTF-8": lambda mp: _answer(mp, [(200, {}, b"\xff\xfe{\x00}")]),
    "compressed body over the size cap": lambda mp: _answer(
        mp, [(200, {"content-encoding": "gzip"}, _gzip_bomb(2 * mcp_api.MAX_DOCUMENT_BYTES))]
    ),
    "unsupported content coding": lambda mp: _answer(mp, [(200, {"content-encoding": "br"}, b"\x1b\x00\x00")]),
    "stacked content codings": lambda mp: _answer(
        mp, [(200, {"content-encoding": "gzip, gzip"}, gzip.compress(gzip.compress(CLIENT_CONFIG.encode())))]
    ),
    "body that is not the gzip it claims": lambda mp: _answer(mp, [(200, {"content-encoding": "gzip"}, b"not gzip")]),
}


@pytest.mark.parametrize("case", list(_NO_DOCUMENT_CASES))
def test_preview_fetch_failures_do_not_say_whether_anything_listens(monkeypatch, case):
    """Whatever stopped the fetch, the caller learns one thing: no document.

    Not whether the name resolves, to what, whether a port is open, what the
    server answered, where it redirected, or what it sent. The guard's own
    refusals are in the list: a name's DNS answer or a redirect target is
    something the backend learned, not something the user typed.
    """
    client, _db, _ = _client(monkeypatch)
    _NO_DOCUMENT_CASES[case](monkeypatch)

    resp = client.post("/api/v1/mcp-servers/import/config/preview", json=_PROBE)

    _assert_no_document(resp)


def test_preview_gives_up_on_a_server_that_trickles_its_body(monkeypatch):
    """Per-read timeouts never trip on a byte every few seconds; the total does."""
    client, _db, _ = _client(monkeypatch)
    _dns(monkeypatch)
    monkeypatch.setattr(mcp_api, "_CONFIG_FETCH_TOTAL_SECONDS", 0.2, raising=False)

    async def _trickle():
        yield b'{"mcpServers": '
        await asyncio.sleep(3)
        yield b"{}}"

    _stub_stream(monkeypatch, [(200, {}, _trickle())])

    started = time.monotonic()
    resp = client.post("/api/v1/mcp-servers/import/config/preview", json=_PROBE)

    _assert_no_document(resp)
    assert time.monotonic() - started < 2.5


def test_preview_never_inflates_a_compressed_body_past_the_cap(monkeypatch):
    """A few KB of gzip must not become tens of MB in memory before the cap is read."""
    client, _db, _ = _client(monkeypatch)
    _dns(monkeypatch)
    bomb = _gzip_bomb(32 * mcp_api.MAX_DOCUMENT_BYTES)
    _stub_stream(monkeypatch, [(200, {"content-encoding": "gzip"}, bomb)])

    tracemalloc.start()
    try:
        resp = client.post("/api/v1/mcp-servers/import/config/preview", json=_PROBE)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    _assert_no_document(resp)
    assert peak < 12 * mcp_api.MAX_DOCUMENT_BYTES


def test_preview_asks_for_an_uncompressed_document(monkeypatch):
    client, _db, _ = _client(monkeypatch)
    _dns(monkeypatch)
    sent: list[dict] = []

    class _CM:
        async def __aenter__(self):
            return _network_response(200, body=CLIENT_CONFIG.encode())

        async def __aexit__(self, *_exc):
            return False

    def _stream(_self, _method, _url, **kwargs):
        sent.append(dict(kwargs.get("headers") or {}))
        return _CM()

    monkeypatch.setattr(httpx.AsyncClient, "stream", _stream)

    resp = client.post("/api/v1/mcp-servers/import/config/preview", json=_PROBE)

    assert resp.status_code == 200
    assert sent[0]["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    "encoding, encode",
    [
        ("gzip", gzip.compress),
        ("x-gzip", gzip.compress),
        ("GZip", gzip.compress),
        ("deflate", zlib.compress),
        ("deflate", lambda data: zlib.compress(data)[2:-4]),  # a bare deflate stream
        ("identity", lambda data: data),
    ],
)
def test_preview_still_reads_a_compressed_document(monkeypatch, encoding, encode):
    """A server that compresses anyway (a pre-gzipped object, say) still works."""
    client, _db, _ = _client(monkeypatch)
    _dns(monkeypatch)
    _stub_stream(monkeypatch, [(200, {"content-encoding": encoding}, encode(CLIENT_CONFIG.encode()))])

    resp = client.post("/api/v1/mcp-servers/import/config/preview", json=_PROBE)

    assert resp.status_code == 200, resp.text
    assert [c["name"] for c in resp.json()["candidates"]] == ["github"]


def test_preview_follows_an_allowed_redirect_to_the_document(monkeypatch):
    client, _db, _ = _client(monkeypatch)
    looked_up = _dns(monkeypatch)
    requested = _stub_stream(
        monkeypatch,
        [
            _redirect("https://raw.example.net/servers.json"),
            (200, {}, CLIENT_CONFIG.encode()),
        ],
    )

    resp = client.post("/api/v1/mcp-servers/import/config/preview", json=_PROBE)

    assert resp.status_code == 200
    assert requested[-1] == "https://raw.example.net/servers.json"
    # Both hops went past the name check.
    assert looked_up == [_PROBE_HOST, "raw.example.net"]


# ---------------------------------------------------------------------------
# A document that came back: what the caller may learn of it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, fragment",
    [
        (b"[" * 200_000, "nested too deeply"),
        (b"1" * 5000, "number too long"),
    ],
    ids=["deep nesting", "5000-digit integer"],
)
@pytest.mark.parametrize("source", ["url", "content"])
def test_preview_reports_a_hostile_document_as_unreadable_not_a_crash(
    monkeypatch, source, body, fragment
):
    client, _db, _ = _client(monkeypatch)
    if source == "url":
        _answer(monkeypatch, [(200, {}, body)])
        payload = _PROBE
    else:
        payload = {"content": body.decode()}

    resp = client.post("/api/v1/mcp-servers/import/config/preview", json=payload)

    assert resp.status_code == 400
    assert fragment in resp.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        b"<html><head><title>Jenkins 2.401.3</title></head><body>Welcome</body></html>",
        b'{"name": "node-1", "cluster_name": "prod-es", "tagline": "You Know, for Search"}',
    ],
    ids=["an HTML page", "a JSON banner"],
)
def test_preview_parse_error_never_echoes_the_fetched_body(monkeypatch, body):
    """A document came back and is not a configuration: say that, not what it held."""
    client, _db, _ = _client(monkeypatch)
    _answer(monkeypatch, [(200, {"server": "Jetty(10.0.15)"}, body)])

    resp = client.post("/api/v1/mcp-servers/import/config/preview", json=_PROBE)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    for leaked in ("Jenkins", "Welcome", "prod-es", "node-1", "You Know", "Jetty"):
        assert leaked not in detail


# ---------------------------------------------------------------------------
# What reaches the log
# ---------------------------------------------------------------------------


def test_preview_fetch_failure_log_never_carries_the_query_string(monkeypatch, caplog):
    """The cause goes to the log, reduced to hosts: an exception may quote the URL."""
    client, _db, _ = _client(monkeypatch)
    _dns(monkeypatch)

    def _quoting(_self, _method, url, **_kwargs):
        raise httpx.ConnectError(f"failed to reach {url}")

    monkeypatch.setattr(httpx.AsyncClient, "stream", _quoting)
    with caplog.at_level("INFO", logger=mcp_api.logger.name):
        resp = client.post(
            "/api/v1/mcp-servers/import/config/preview",
            json={"url": "https://user:pw@example.com/gist/5f3c9a/config.json?token=s3cret#frag"},
        )

    _assert_no_document(resp)
    assert "MCP configuration fetch from example.com failed" in caplog.text
    for secret in ("s3cret", "pw@", "user:", "5f3c9a", "config.json", "#frag"):
        # A path can be the secret too (a secret gist), not only the query.
        assert secret not in caplog.text


def test_preview_fetch_failure_log_names_the_hop_that_failed(monkeypatch, caplog):
    client, _db, _ = _client(monkeypatch)
    _dns(monkeypatch)
    hops = iter([_redirect("https://mirror.example.net/servers.json")])

    class _CM:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            if "mirror.example.net" in self.url:
                raise httpx.ConnectError("[Errno 111] Connection refused")
            status, headers, body = next(hops)
            return _network_response(status, headers, body)

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(httpx.AsyncClient, "stream", lambda _self, _m, url, **_k: _CM(url))
    with caplog.at_level("INFO", logger=mcp_api.logger.name):
        resp = client.post("/api/v1/mcp-servers/import/config/preview", json=_PROBE)

    _assert_no_document(resp)
    assert "fetch from mirror.example.net failed" in caplog.text
    assert "Connection refused" in caplog.text


def test_preview_logs_an_unexpected_failure_louder_but_just_as_redacted(monkeypatch, caplog):
    client, _db, _ = _client(monkeypatch)
    _dns(monkeypatch)
    _raise_on_request(monkeypatch, RuntimeError("boom at https://u:p@example.com/private/path?t=1"))

    with caplog.at_level("INFO", logger=mcp_api.logger.name):
        resp = client.post("/api/v1/mcp-servers/import/config/preview", json=_PROBE)

    _assert_no_document(resp)
    records = [r for r in caplog.records if r.name == mcp_api.logger.name]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "RuntimeError" in caplog.text
    for secret in ("u:p@", "/private/path", "t=1"):
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _candidate(**overrides):
    base = {
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@mcp/github"],
        "env": {"GITHUB_TOKEN": "ghp_x"},
        "headers": {},
        "missing_secrets": [],
        "source": "mcpServers.github",
    }
    base.update(overrides)
    return base


def test_import_creates_the_selected_servers(monkeypatch):
    client, db, audit = _client(monkeypatch)

    resp = client.post("/api/v1/mcp-servers/import/config", json={"servers": [_candidate()]})

    assert resp.status_code == 200
    assert resp.json()["failed"] == []
    assert len(db.added) == 1
    created = db.added[0]
    assert (created.name, created.transport, created.command) == ("github", "stdio", "npx")
    # Secrets never land in plaintext.
    assert created.env_enc is not None
    assert audit[0]["action"] == "import"


def test_import_refuses_a_candidate_whose_url_points_inward(monkeypatch):
    """The preview checked the *document's* URL, not the servers inside it."""
    client, db, _ = _client(monkeypatch)

    resp = client.post(
        "/api/v1/mcp-servers/import/config",
        json={
            "servers": [
                _candidate(
                    name="evil",
                    transport="streamable_http",
                    command=None,
                    url="http://169.254.169.254/mcp",
                )
            ]
        },
    )

    assert resp.status_code == 200
    assert db.added == []
    # Refused, and said in terms that make sense on this screen — the shared
    # guard's own message names A2A partners, which nothing here mentions.
    error = resp.json()["failed"][0]["error"].lower()
    assert "a2a" not in error
    assert "https" in error or "will not fetch from" in error


@pytest.mark.parametrize(
    "bad, fragment",
    [
        (_candidate(name="  "), "name is required"),
        (_candidate(transport="carrier-pigeon"), "unknown transport"),
        (_candidate(command=None), "needs a command"),
        (_candidate(transport="streamable_http", command=None, url=None), "needs a url"),
    ],
)
def test_import_reports_a_bad_entry_without_losing_the_batch(monkeypatch, bad, fragment):
    client, db, _ = _client(monkeypatch)

    resp = client.post(
        "/api/v1/mcp-servers/import/config",
        json={"servers": [bad, _candidate(name="good")]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["failed"]) == 1
    assert fragment in body["failed"][0]["error"].lower()
    # The healthy one still landed.
    assert [s.name for s in db.added] == ["good"]


@pytest.mark.parametrize(
    "field, overrides",
    [
        ("name", {"name": "n" * 201}),
        ("command", {"command": "c" * 501}),
        (
            "url",
            {"transport": "streamable_http", "command": None, "url": "https://e.com/" + "u" * 2000},
        ),
    ],
)
def test_import_reports_an_over_long_value_instead_of_failing_the_batch(monkeypatch, field, overrides):
    """The columns are bounded and the values come from a file nobody trimmed.

    Left to the database these raise DataError mid-batch and take the whole
    request down with a 500; here each is one reported entry.
    """
    client, db, _ = _client(monkeypatch)

    resp = client.post(
        "/api/v1/mcp-servers/import/config",
        json={"servers": [_candidate(**overrides), _candidate(name="good")]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["failed"]) == 1
    assert "too long" in body["failed"][0]["error"].lower()
    assert field in body["failed"][0]["error"].lower()
    # The healthy one still landed, and the failure name is itself bounded.
    assert [s.name for s in db.added] == ["good"]
    assert len(body["failed"][0]["name"]) <= 200


def test_import_reports_a_database_data_error_as_one_failed_entry(monkeypatch):
    """Backstop for any value the explicit length checks do not bound."""
    client, db, _ = _client(monkeypatch)

    async def _boom():
        raise DataError("stmt", {}, Exception("value too long"))

    db.flush = _boom

    resp = client.post("/api/v1/mcp-servers/import/config", json={"servers": [_candidate()]})

    assert resp.status_code == 200
    assert resp.json()["created"] == []
    assert len(resp.json()["failed"]) == 1


def test_import_needs_at_least_one_server(monkeypatch):
    client, _db, _ = _client(monkeypatch)
    resp = client.post("/api/v1/mcp-servers/import/config", json={"servers": []})
    assert resp.status_code == 400


def test_import_holds_a_vault_reference_to_the_rules_of_the_form(monkeypatch):
    """A reference in an imported document is a known provider, an allowed path, a role it admits."""
    from app.services.vault.base import VaultProvider
    from app.services.vault.config import VaultProviderConfig
    from app.services.vault.resolver import SecretResolver, set_resolver

    class _Unread(VaultProvider):
        async def read(self, path):  # pragma: no cover - an import never reads the secret
            raise AssertionError(path)

    def _provider(name, **settings):
        return _Unread(VaultProviderConfig(name=name, type="hashicorp", settings=settings))

    set_resolver(
        SecretResolver(
            {"corp": _provider("corp"), "team": _provider("team", WRITERS="all", PATHS="mcp")},
            {},
            300,
        )
    )
    client, db, _ = _client(monkeypatch)

    resp = client.post(
        "/api/v1/mcp-servers/import/config",
        json={
            "servers": [
                _candidate(name="admins-only", env={"GITHUB_TOKEN": "vault://corp/mcp#token"}),
                _candidate(name="outside", headers={"Authorization": "vault://team/prod#auth"}),
                _candidate(name="unknown", env={"GITHUB_TOKEN": "vault://nope/x"}),
                _candidate(name="good", env={"GITHUB_TOKEN": "vault://team/mcp/github#token"}),
            ]
        },
    )

    assert resp.status_code == 200
    failed = {entry["name"]: entry["error"] for entry in resp.json()["failed"]}
    assert "only administrators" in failed["admins-only"]
    assert "outside the paths" in failed["outside"]
    assert "No vault provider named 'nope'" in failed["unknown"]
    assert [s.name for s in db.added] == ["good"]
