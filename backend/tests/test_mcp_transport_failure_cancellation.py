"""A failed ``streamable_http`` connection is a failure, not a cancellation.

The first request of a ``streamable_http`` connection runs in a task of the MCP
SDK's task group, so its failure reaches ``initialize()`` as the cancellation
the group delivers to its host task. ``MCPConnection.connect()`` reports the
failure the group raises on close instead, and withdraws that cancellation, so
the caller's next await is not cancelled. A cancellation of the caller's own
still ends the connection attempt as one.

Driven through the real SDK transport against loopback sockets; the Redis
failure bookkeeping is stubbed.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from app.tools import mcp_client


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
    monkeypatch.setattr(mcp_client, "_is_mcp_circuit_open", _closed)
    monkeypatch.setattr(mcp_client, "_open_mcp_circuit", _noop)
    return keys


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _runtime(url: str) -> mcp_client.MCPServerRuntime:
    return mcp_client.MCPServerRuntime(name="Remote", transport="streamable_http", url=url, server_id="srv-5240")


@asynccontextmanager
async def _server(head: str | None) -> AsyncIterator[tuple[int, asyncio.Event]]:
    """A loopback HTTP server answering *head* to every request, or never when ``None``."""
    accepted = asyncio.Event()

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.set()
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            await reader.readuntil(b"\r\n\r\n")
            if head is None:
                await reader.read()  # never answers: the client hangs up
                return
            writer.write(f"{head}Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], accepted
    finally:
        server.close()
        await server.wait_closed()


async def _next_await_is_not_cancelled() -> bool:
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        return False
    return True


async def test_a_closed_port_answers_an_error_and_leaves_no_cancellation(recorded_failures):
    runtime = _runtime(f"http://127.0.0.1:{_closed_port()}/mcp")

    result = await mcp_client.MCPManager().test_connection(runtime)

    assert result["status"] == "error"
    assert set(result) == {"status", "message"}
    assert asyncio.current_task().cancelling() == 0
    assert await _next_await_is_not_cancelled() is True
    # Counted like any unreachable server (it used to escape as a cancellation).
    assert recorded_failures == ["srv-5240"]


async def test_connect_raises_the_transport_failure(recorded_failures):
    connection = mcp_client.MCPConnection(_runtime(f"http://127.0.0.1:{_closed_port()}/mcp"))

    # Not the CancelledError the SDK's task group delivered to initialize().
    with pytest.raises(ExceptionGroup) as caught:
        await connection.connect(force=True)

    assert mcp_client._connect_error_detail(caught.value) == "ConnectError: All connection attempts failed"
    assert asyncio.current_task().cancelling() == 0


async def test_a_401_answers_auth_required(recorded_failures):
    challenge = 'WWW-Authenticate: Bearer resource_metadata="https://auth.example.test/.well-known/oauth-protected-resource"\r\n'
    async with _server(f"HTTP/1.1 401 Unauthorized\r\n{challenge}") as (port, _accepted):
        result = await mcp_client.MCPManager().test_connection(_runtime(f"http://127.0.0.1:{port}/mcp"))

    assert result == {
        "status": "auth_required",
        "message": "MCP server 'Remote' requires authorization",
        "resource_metadata": "https://auth.example.test/.well-known/oauth-protected-resource",
    }
    assert asyncio.current_task().cancelling() == 0
    assert await _next_await_is_not_cancelled() is True
    assert recorded_failures == []


async def test_a_server_error_answers_an_error(recorded_failures):
    async with _server("HTTP/1.1 500 Internal Server Error\r\n") as (port, _accepted):
        result = await mcp_client.MCPManager().test_connection(_runtime(f"http://127.0.0.1:{port}/mcp"))

    assert result["status"] == "error"
    assert set(result) == {"status", "message"}
    assert asyncio.current_task().cancelling() == 0
    assert await _next_await_is_not_cancelled() is True


async def test_the_callers_cancellation_still_ends_the_attempt(recorded_failures):
    async with _server(None) as (port, accepted):
        connection = mcp_client.MCPConnection(_runtime(f"http://127.0.0.1:{port}/mcp"))
        attempt = asyncio.ensure_future(connection.connect(force=True))
        await asyncio.wait_for(accepted.wait(), timeout=10)
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt

    assert attempt.cancelled() is True
    assert recorded_failures == []


async def test_a_cancellation_the_caller_already_carried_is_kept(recorded_failures):
    task = asyncio.current_task()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.sleep(0)
    assert task.cancelling() == 1
    try:
        connection = mcp_client.MCPConnection(_runtime(f"http://127.0.0.1:{_closed_port()}/mcp"))
        with pytest.raises(BaseException) as caught:
            await connection.connect(force=True)
        # Only the transport's own cancellation is withdrawn, never the one
        # the task carried before the attempt: it still ends the next await.
        assert task.cancelling() == 1
        assert type(caught.value) is ExceptionGroup
        assert await _next_await_is_not_cancelled() is False
    finally:
        while task.cancelling():
            task.uncancel()
