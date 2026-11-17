"""The MCP SDK's own client keeps working past a refused token.

End to end over a real socket: the SDK's ``streamablehttp_client`` /
``sse_client`` and ``ClientSession``, given the connection's auth hook
(``StoredMCPOAuthToken``), against a FastMCP server that only accepts the
refreshed token. The hook is handed to the transports exactly as
``MCPConnection`` hands it (``auth=``); what these tests pin is that the SDK
applies it to every request it sends - the stream and the posts of SSE, every
post of Streamable HTTP - and replays a refused request with the new token.

``current_mcp_access_token`` (the refresh itself) is replaced by a stub here:
its own tests are in ``test_mcp_oauth_token_refresh.py``.
"""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP

from app.services import token_refresh
from app.services.mcp_oauth_tokens import StoredMCPOAuthToken
from app.services.token_refresh import MCPAccessToken

SERVER_ID = "6b1f1c1e-4d7a-4c55-9e0f-5168e2e00001"


class _BearerGate:
    """ASGI middleware answering ``401`` to the requests ``refuse`` names."""

    def __init__(self, app, refuse: Callable[[str, str], bool]):
        self.app = app
        self.refuse = refuse
        self.seen: list[tuple[str, str]] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ")
        self.seen.append((scope["method"], token))
        if not self.refuse(scope["method"], token):
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"www-authenticate", b'Bearer error="invalid_token"'), (b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})


def _mcp_server() -> FastMCP:
    mcp = FastMCP("refresh-e2e")

    @mcp.tool()
    def echo(text: str) -> str:
        """Echo the text back."""
        return f"echo: {text}"

    return mcp


@asynccontextmanager
async def _serve(app):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


@pytest.fixture
def refreshed(monkeypatch):
    """The token store: a refused ``at-1`` is replaced by ``at-2``."""
    state = SimpleNamespace(asked=[])

    async def _current(server_id, *, refused=None):
        state.asked.append(refused)
        return MCPAccessToken("at-2", datetime.now(UTC) + timedelta(minutes=10))

    monkeypatch.setattr(token_refresh, "current_mcp_access_token", _current)
    return state


def _hook() -> StoredMCPOAuthToken:
    return StoredMCPOAuthToken(SERVER_ID, "at-1", datetime.now(UTC) + timedelta(minutes=10))


async def _list_and_call(session: ClientSession) -> str:
    await session.initialize()
    tools = await session.list_tools()
    assert [tool.name for tool in tools.tools] == ["echo"]
    result = await session.call_tool("echo", {"text": "still connected"})
    return result.content[0].text


async def test_streamable_http_replays_the_refused_request_with_the_refreshed_token(refreshed):
    gate = _BearerGate(_mcp_server().streamable_http_app(), lambda _method, token: token != "at-2")
    auth = _hook()

    async with (
        asyncio.timeout(30),
        _serve(gate) as base,
        streamablehttp_client(f"{base}/mcp", auth=auth) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        text = await _list_and_call(session)

    assert text == "echo: still connected"
    # One refusal, one replacement; every later request carries the new token.
    assert gate.seen == [
        ("POST", "at-1"),  # initialize, refused
        ("POST", "at-2"),  # initialize, replayed
        ("POST", "at-2"),  # notifications/initialized
        ("GET", "at-2"),  # the server's stream
        ("POST", "at-2"),  # tools/list
        ("POST", "at-2"),  # tools/call
        ("DELETE", "at-2"),  # session end
    ]
    assert refreshed.asked == ["at-1"]


_POSTS_WITH_THE_NEW_TOKEN = [("POST", "at-2")] * 4  # initialize, initialized, tools/list, tools/call


@pytest.mark.parametrize(
    ("refused_on", "expected"),
    [
        ("GET", [("GET", "at-1"), ("GET", "at-2"), *_POSTS_WITH_THE_NEW_TOKEN]),
        ("POST", [("GET", "at-1"), ("POST", "at-1"), *_POSTS_WITH_THE_NEW_TOKEN]),
    ],
    ids=["the-stream", "a-post"],
)
async def test_sse_replays_the_refused_request_with_the_refreshed_token(refreshed, refused_on, expected):
    """SSE opens a stream (GET) and posts each message: the hook covers both."""
    gate = _BearerGate(
        _mcp_server().sse_app(),
        lambda method, token: token != "at-2" and (method == refused_on or token != "at-1"),
    )
    auth = _hook()

    async with (
        asyncio.timeout(30),
        _serve(gate) as base,
        sse_client(f"{base}/sse", auth=auth) as (read, write),
        ClientSession(read, write) as session,
    ):
        text = await _list_and_call(session)

    assert text == "echo: still connected"
    assert gate.seen == expected
    assert refreshed.asked == ["at-1"]
