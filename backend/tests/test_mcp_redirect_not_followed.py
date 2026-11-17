"""A redirect the MCP transport does not follow is named, not a TaskGroup failure.

Since mcp 1.30 the remote transports follow a redirect only when it stays on
the endpoint's origin and keeps the request method. Any other one fails the
connection with an ``httpx.HTTPStatusError`` for the redirect response, deep
inside the SDK's task group. ``MCPConnection.connect()`` raises
``MCPRedirectNotFollowedError`` naming the (redacted) target instead, logged as
a WARNING without a traceback; a same-origin 307/308 (a trailing slash) still
connects. The authorization probe follows redirects by the same rule.

Driven through ``mcp_http_client_factory`` (``MCPManager.test_connection``)
and the real SDK transports against a FastMCP server on loopback; the Redis
failure bookkeeping is stubbed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from app.tools import mcp_client

_SECRET = "s3cr3t-value"


@pytest.fixture
def recorded_failures(monkeypatch) -> list[str]:
    """The failure keys ``connect()`` counted; the circuit breaker stays closed."""
    keys: list[str] = []

    async def _record(key: str) -> int:
        keys.append(key)
        return 1

    async def _closed(_key: str) -> bool:
        return False

    async def _noop(_key: str) -> None:
        return None

    monkeypatch.setattr(mcp_client, "_record_mcp_connection_failure", _record)
    monkeypatch.setattr(mcp_client, "_reset_mcp_connection_failures", _noop)
    monkeypatch.setattr(mcp_client, "_is_mcp_circuit_open", _closed)
    monkeypatch.setattr(mcp_client, "_open_mcp_circuit", _noop)
    monkeypatch.setattr(mcp_client, "_close_mcp_circuit", _noop)
    return keys


# path -> (status, Location); ``{port}`` is the server's own port.
_REDIRECTS = {
    # Another origin: same port, another host name for the same server.
    "/moved": (307, f"http://admin:hunter2@localhost:{{port}}/mcp?token={_SECRET}#frag"),
    "/sse-moved": (307, f"http://localhost:{{port}}/sse?token={_SECRET}"),
    # Same origin, but a 302 turns the POST into a GET.
    "/post-302": (302, "/mcp"),
    # Same origin, method kept: followed.
    "/mcp/": (307, "/mcp"),
    "/old": (308, "http://127.0.0.1:{port}/mcp"),
    "/sse/": (307, "/sse"),
    # The authorization probe.
    "/to-gate": (307, "/gate"),
    "/to-gate-elsewhere": (307, "http://localhost:{port}/gate"),
}


class _App:
    """FastMCP over both transports, behind the redirects above and a 401 at ``/gate``."""

    def __init__(self) -> None:
        mcp = FastMCP("redirect-5882")

        @mcp.tool()
        def echo(text: str) -> str:
            """Echo the text back."""
            return text

        self.streamable = mcp.streamable_http_app()
        self.sse = mcp.sse_app()
        self.seen: list[tuple[str, str, str]] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.streamable(scope, receive, send)  # lifespan: the session manager
            return
        host = dict(scope["headers"]).get(b"host", b"").decode()
        self.seen.append((scope["method"], host.split(":")[0], scope["path"]))
        path = scope["path"]
        if path in _REDIRECTS:
            status, location = _REDIRECTS[path]
            await _answer(send, status, [(b"location", location.format(port=scope["server"][1]).encode())])
        elif path == "/gate":
            challenge = b'Bearer resource_metadata="https://auth.example.test/prm"'
            await _answer(send, 401, [(b"www-authenticate", challenge)])
        elif path.startswith(("/sse", "/messages")):
            await self.sse(scope, receive, send)
        else:
            await self.streamable(scope, receive, send)


async def _answer(send, status: int, headers: list[tuple[bytes, bytes]]) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [*headers, (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


@asynccontextmanager
async def _serve() -> AsyncIterator[tuple[str, int, _App]]:
    app = _App()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}", port, app
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


def _runtime(url: str, transport: str = "streamable_http") -> mcp_client.MCPServerRuntime:
    return mcp_client.MCPServerRuntime(name="Remote", transport=transport, url=url, server_id="srv-5882")


def _named(location: str) -> dict[str, str]:
    return {
        "status": "error",
        "message": (
            f"MCP server 'Remote' redirects to {location}, which its connection does not follow. "
            "If that is the intended server, save the server with that URL."
        ),
        "redirect_location": location,
    }


def _own_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == mcp_client.__name__]


# ---------------------------------------------------------------------------
# Not followed: named, a WARNING without a traceback, counted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transport", "path", "target"),
    [
        ("streamable_http", "/moved", "http://localhost:{port}/mcp"),
        ("sse", "/sse-moved", "http://localhost:{port}/sse"),
        ("streamable_http", "/post-302", "http://127.0.0.1:{port}/mcp"),
    ],
    ids=["cross-origin-307", "cross-origin-307-sse", "same-origin-302-post"],
)
async def test_a_redirect_the_transport_does_not_follow_is_named(transport, path, target, recorded_failures, caplog):
    async with asyncio.timeout(30), _serve() as (base, port, app):
        with caplog.at_level(logging.DEBUG, logger=mcp_client.__name__):
            result = await mcp_client.MCPManager().test_connection(_runtime(f"{base}{path}", transport))

    location = target.format(port=port)
    assert result == _named(location)
    # Never followed: the other host name was never asked for.
    assert [host for _method, host, _path in app.seen] == ["127.0.0.1"]
    assert _SECRET not in str(result)
    warnings = [r for r in _own_records(caplog) if r.levelno >= logging.WARNING]
    assert [(r.levelno, r.exc_info) for r in warnings] == [(logging.WARNING, None)]
    assert warnings[0].getMessage().startswith(f"MCP server 'Remote' redirects to {location!r} (HTTP ")
    assert _SECRET not in caplog.text
    assert recorded_failures == ["srv-5882"]
    assert asyncio.current_task().cancelling() == 0


async def test_connect_raises_the_named_redirect_and_a_lazy_tool_call_reports_it(recorded_failures):
    async with asyncio.timeout(30), _serve() as (base, port, _app):
        connection = mcp_client.MCPConnection(_runtime(f"{base}/moved"))
        with pytest.raises(mcp_client.MCPRedirectNotFollowedError) as caught:
            await connection.connect(force=True)

        lazy = mcp_client.MCPConnection(_runtime(f"{base}/moved"), cached_tools=[])
        result = await lazy.execute_tool(mcp_client.ToolCall(id="c1", name="echo", arguments={"text": "x"}))

    assert caught.value.location == f"http://localhost:{port}/mcp"
    assert isinstance(caught.value.__cause__, BaseExceptionGroup)
    assert result.is_error is True
    assert result.content == (
        f"Failed to connect to MCP server 'Remote': {_named(f'http://localhost:{port}/mcp')['message']}"
    )


# ---------------------------------------------------------------------------
# Followed: a same-origin 307/308 that keeps the method still connects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transport", "path"),
    [("streamable_http", "/mcp/"), ("streamable_http", "/old"), ("sse", "/sse/")],
    ids=["trailing-slash-307", "same-origin-308", "trailing-slash-307-sse"],
)
async def test_a_same_origin_redirect_still_connects(transport, path, recorded_failures):
    async with asyncio.timeout(30), _serve() as (base, _port, app):
        result = await mcp_client.MCPManager().test_connection(_runtime(f"{base}{path}", transport))

    assert result["status"] == "ok"
    assert [tool["name"] for tool in result["tools"]] == ["echo"]
    assert app.seen[0][2] == path
    assert recorded_failures == []


# ---------------------------------------------------------------------------
# The authorization probe follows the same rule
# ---------------------------------------------------------------------------


async def test_the_probe_follows_a_same_origin_redirect():
    async with asyncio.timeout(30), _serve() as (base, _port, _app):
        challenge = await mcp_client.probe_auth_challenge(f"{base}/to-gate")
    assert challenge == (True, "https://auth.example.test/prm")


async def test_the_probe_does_not_follow_a_redirect_to_another_origin():
    async with asyncio.timeout(30), _serve() as (base, _port, app):
        challenge = await mcp_client.probe_auth_challenge(f"{base}/to-gate-elsewhere")
    # The 401 behind the redirect is on a host the transport never reaches.
    assert challenge is None
    assert app.seen == [("POST", "127.0.0.1", "/to-gate-elsewhere")]


# ---------------------------------------------------------------------------
# What the location shows
# ---------------------------------------------------------------------------


def _redirect_error(status: int, location: str | None, url: str = "https://mcp.example.com/mcp") -> BaseException:
    request = httpx.Request("POST", url)
    headers = {"location": location} if location is not None else {}
    response = httpx.Response(status, headers=headers, request=request)
    if response.has_redirect_location:
        response.next_request = httpx.Request("POST", request.url.join(location))
    error = httpx.HTTPStatusError("not followed", request=request, response=response)
    return ExceptionGroup("unhandled errors in a TaskGroup", [error])


_LONG_PATH = "a" * 400


@pytest.mark.parametrize(
    ("status", "location", "shown"),
    [
        (307, "https://www.example.com/mcp", "https://www.example.com/mcp"),
        (308, "https://user:pass@www.example.com/mcp?token=abc&x=1#part", "https://www.example.com/mcp"),
        (301, "/mcp/", "https://mcp.example.com/mcp/"),
        (302, "http://mcp.example.com/mcp", "http://mcp.example.com/mcp"),
        (307, "https://mcp.example.com/s/ghp_" + "a" * 36 + "/mcp", "https://mcp.example.com/s/***/mcp"),
        (307, f"https://www.example.com/{_LONG_PATH}", f"https://www.example.com/{_LONG_PATH}"[:300] + "…"),
    ],
    ids=["plain", "userinfo-query-fragment", "relative", "https-to-http", "token-segment", "overlong"],
)
def test_the_location_is_shown_redacted(status, location, shown):
    assert mcp_client._unfollowed_redirect(_redirect_error(status, location)) == (status, shown)


@pytest.mark.parametrize(
    ("status", "location"),
    [(307, None), (401, None), (500, None), (200, "https://www.example.com/mcp")],
    ids=["redirect-without-location", "unauthorized", "server-error", "not-a-redirect-status"],
)
def test_anything_but_a_redirect_is_not_one(status, location):
    assert mcp_client._unfollowed_redirect(_redirect_error(status, location)) is None
