"""An MCP refresh outcome and a re-authorization serialize on the server row.

Every outcome of the scheduled MCP refresh is applied after
``lock_stored_mcp_tokens`` row-locks the server in the transaction that commits
it, and only while it still stores the tokens the refresh started from. A
session double cannot show that a re-authorization committed meanwhile is seen,
or that one arriving later waits for the commit and then wins, so the refresh
runs here against PostgreSQL, with the scratch-database recipe of
``test_token_refresh_row_lock_postgres.py``; Redis and the token endpoint are
in-memory.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api import oauth_callbacks
from app.models.mcp_server import MCPServer
from app.services import token_refresh
from tests.test_token_refresh_row_lock_postgres import (
    _FakeRedis,
    _temp_database,
    _TokenEndpoint,
    _wait_for_a_writer_blocked_on_the_row,
)

pytestmark = pytest.mark.integration

REVOKED_GRANT = {"error": "invalid_grant", "error_description": "refresh token revoked"}


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the token URL's host; answer for DNS, never a real resolver."""
    from app.integrations import token_endpoint

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


def _wire(monkeypatch, sessionmaker) -> None:
    redis = _FakeRedis()

    async def _redis():
        return redis

    async def _no_discovery(_db, _srv):
        return None

    monkeypatch.setattr(token_refresh, "redis_client", _redis)
    monkeypatch.setattr("app.core.database.async_session", sessionmaker)
    monkeypatch.setattr(oauth_callbacks, "_discover_tools_after_authorization", _no_discovery)
    for module in (token_refresh, oauth_callbacks):
        monkeypatch.setattr(module, "decrypt_value", lambda value: value)
        monkeypatch.setattr(module, "encrypt_value", lambda value: value)


async def _seed(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as db:
        srv = MCPServer(
            name="Company MCP",
            transport="streamable_http",
            url="https://mcp.example.com/mcp",
            auth_mode="oauth",
            oauth_status="connected",
            is_company_managed=True,
            client_id_enc="dyn-client",
            access_token_enc="at-1",
            refresh_token_enc="rt-1",
            token_url="https://auth.example.com/oauth/token",
            oauth_resource="https://mcp.example.com/mcp",
            token_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )
        db.add(srv)
        await db.commit()
        return srv.id


async def _read(sessionmaker, server_id) -> MCPServer:
    async with sessionmaker() as db:
        return await db.get(MCPServer, server_id)


async def _sweep(sessionmaker, server_id, endpoint: _TokenEndpoint) -> bool:
    """One scheduled refresh of the stored server, from a row the sweep read first."""
    async with sessionmaker() as db:
        srv = await db.get(MCPServer, server_id)
        await db.commit()
        return await token_refresh._refresh_one_mcp(srv, endpoint, asyncio.Semaphore(1))


async def _reauthorize(sessionmaker, server_id) -> None:
    async with sessionmaker() as db:
        await oauth_callbacks._handle_mcp_server_callback(
            {"entity_id": str(server_id), "user_id": str(uuid.uuid4()), "entity_type": "mcp_server"},
            "the-code",
            db,
        )


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        pytest.param(400, REVOKED_GRANT, id="400"),
        # A refusal inside a 200 (GitHub's code).
        pytest.param(200, {"error": "bad_refresh_token"}, id="200"),
    ],
)
async def test_a_reauthorization_landing_during_a_refused_refresh_waits_and_wins(monkeypatch, status_code, payload):
    """The callback had read the server (``connected``) and was exchanging its code
    when the refresh, answered ``invalid_grant`` (or a dead refresh token inside
    a 200), locked the row to record ``error``. The callback's write waits for
    that commit, then stores the new grant and ``connected``."""
    async with _temp_database() as sessionmaker:
        server_id = await _seed(sessionmaker)
        endpoint = _TokenEndpoint(status_code, payload)
        _wire(monkeypatch, sessionmaker)

        exchanging = asyncio.Event()
        row_locked = asyncio.Event()
        record_verdict = asyncio.Event()
        lock_stored_mcp_tokens = token_refresh.lock_stored_mcp_tokens

        async def _lock_then_wait(db, srv):
            stored = await lock_stored_mcp_tokens(db, srv)
            row_locked.set()
            await record_verdict.wait()
            return stored

        async def _exchange(_srv, _code, *, code_verifier=None):
            exchanging.set()
            await row_locked.wait()
            return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}

        monkeypatch.setattr(token_refresh, "lock_stored_mcp_tokens", _lock_then_wait)
        monkeypatch.setattr(oauth_callbacks, "exchange_code_for_tokens_with_mcp_server", _exchange)

        callback = asyncio.create_task(_reauthorize(sessionmaker, server_id))
        refresh = None
        try:
            await asyncio.wait_for(exchanging.wait(), 10)
            refresh = asyncio.create_task(_sweep(sessionmaker, server_id, endpoint))
            await asyncio.wait_for(row_locked.wait(), 10)
            await _wait_for_a_writer_blocked_on_the_row(sessionmaker)
        finally:
            record_verdict.set()
            row_locked.set()
            await asyncio.wait_for(asyncio.gather(callback, *filter(None, [refresh])), 10)

        stored = await _read(sessionmaker, server_id)
        assert (stored.access_token_enc, stored.refresh_token_enc) == ("at-new", "rt-new")
        assert stored.oauth_status == "connected"


async def test_a_success_for_replaced_tokens_is_not_written_over_the_reauthorization(monkeypatch):
    async with _temp_database() as sessionmaker:
        server_id = await _seed(sessionmaker)

        async def _reauthorized_meanwhile():
            async with sessionmaker() as db:
                await db.execute(
                    update(MCPServer)
                    .where(MCPServer.id == server_id)
                    .values(access_token_enc="at-new", refresh_token_enc="rt-new")
                )
                await db.commit()

        endpoint = _TokenEndpoint(
            200,
            {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600},
            before_answer=_reauthorized_meanwhile,
        )
        _wire(monkeypatch, sessionmaker)

        assert await asyncio.wait_for(_sweep(sessionmaker, server_id, endpoint), 10) is False

        stored = await _read(sessionmaker, server_id)
        assert (stored.access_token_enc, stored.refresh_token_enc) == ("at-new", "rt-new")
        assert stored.oauth_status == "connected"
