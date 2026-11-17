"""A refresh outcome and a re-authorization serialize on the integration row.

Every refresh outcome is applied after ``lock_stored_tokens`` row-locks the
integration in the transaction that commits it, and only while it still stores
the tokens the refresh started from. A session double cannot show that a
re-authorization committed meanwhile is seen, or that one arriving later waits
for the commit and then wins, so both refresh paths run here against
PostgreSQL. Same scratch-database recipe as
``test_store_message_sequence_postgres.py``; Redis and the token endpoint are
in-memory.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api import oauth_callbacks
from app.models.integration import Integration
from app.services import integration_tools, token_refresh
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

DEAD_REFRESH_TOKEN = {"error": "unauthorized_client", "error_description": "refresh_token is invalid"}


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the token URL's host; answer for DNS, never a real resolver."""
    import socket

    from app.integrations import token_endpoint

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "tokrefresh",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


class _FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        return 1

    async def eval(self, _script, _numkeys, key, token):
        if self.values.get(key) != token:
            return 0
        del self.values[key]
        return 1


class _TokenEndpoint:
    """Answers every refresh the same way, once ``before_answer`` has run."""

    def __init__(self, status_code: int, payload: dict, before_answer=None):
        self.status_code = status_code
        self.payload = payload
        self.before_answer = before_answer

    async def post(self, url, **_kwargs):
        if self.before_answer is not None:
            await self.before_answer()
        return httpx.Response(self.status_code, json=self.payload, request=httpx.Request("POST", url))


def _wire(monkeypatch, sessionmaker, endpoint: _TokenEndpoint) -> None:
    redis = _FakeRedis()

    async def _redis():
        return redis

    class _ChatClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            return await endpoint.post(url, **kwargs)

    monkeypatch.setattr(token_refresh, "redis_client", _redis)
    monkeypatch.setattr("app.core.database.async_session", sessionmaker)
    monkeypatch.setattr(integration_tools.httpx, "AsyncClient", _ChatClient)
    for module in (token_refresh, integration_tools, oauth_callbacks):
        monkeypatch.setattr(module, "decrypt_value", lambda value: value)
        monkeypatch.setattr(module, "encrypt_value", lambda value: value)
    # The client secret is read through the vault layer, which reads the stored text here.
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda value: value)


async def _seed(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as db:
        integration = Integration(
            provider="notion",
            auth_type="oauth",
            name="Company Notion",
            status="connected",
            is_company_managed=True,
            client_id_enc="client-id",
            client_secret_enc="client-secret",
            access_token_enc="at-1",
            refresh_token_enc="rt-1",
            token_url="https://api.notion.com/v1/oauth/token",
            token_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )
        db.add(integration)
        await db.commit()
        return integration.id


async def _read(sessionmaker, integration_id) -> Integration:
    async with sessionmaker() as db:
        return await db.get(Integration, integration_id)


async def _refresh(path: str, sessionmaker, integration_id, endpoint: _TokenEndpoint):
    """One refresh of the stored integration, by the sweep or by a chat turn."""
    async with sessionmaker() as db:
        row = await db.get(Integration, integration_id)
        await db.commit()
        if path == "sweep":
            return await token_refresh._refresh_one_integration(row, endpoint, asyncio.Semaphore(1))
        return await integration_tools._try_inline_refresh(db, row)


async def _reauthorize(sessionmaker, integration_id) -> None:
    async with sessionmaker() as db:
        await oauth_callbacks._handle_integration_callback(
            {"integration_id": str(integration_id), "user_id": str(uuid.uuid4()), "provider": "notion"},
            "the-code",
            db,
        )


async def _wait_for_a_writer_blocked_on_the_row(sessionmaker) -> None:
    for _ in range(200):
        async with sessionmaker() as probe:
            waiting = await probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the re-authorization never waited for the row lock")


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        pytest.param(403, DEAD_REFRESH_TOKEN, id="403"),
        # GitHub and Slack refuse a refresh inside a 200.
        pytest.param(200, {"error": "bad_refresh_token"}, id="200-github"),
        pytest.param(200, {"ok": False, "error": "invalid_refresh_token"}, id="200-slack"),
    ],
)
@pytest.mark.parametrize("path", ["sweep", "chat"])
async def test_a_reauthorization_landing_during_a_refused_refresh_waits_and_wins(monkeypatch, path, status_code, payload):
    """The callback had read the row (``connected``) and was exchanging its code
    when the refresh, answered ``refresh_token is invalid`` (or a dead refresh
    token inside a 200), locked the row to record a dead grant. The callback's
    write waits for that commit, then stores the new grant and ``connected``."""
    async with _temp_database() as sessionmaker:
        integration_id = await _seed(sessionmaker)
        endpoint = _TokenEndpoint(status_code, payload)
        _wire(monkeypatch, sessionmaker, endpoint)

        exchanging = asyncio.Event()
        row_locked = asyncio.Event()
        record_verdict = asyncio.Event()
        lock_stored_tokens = token_refresh.lock_stored_tokens

        async def _lock_then_wait(db, integration):
            stored = await lock_stored_tokens(db, integration)
            row_locked.set()
            await record_verdict.wait()
            return stored

        async def _exchange(_integration, _code, *, code_verifier=None):
            exchanging.set()
            await row_locked.wait()
            return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}

        monkeypatch.setattr(token_refresh, "lock_stored_tokens", _lock_then_wait)
        monkeypatch.setattr(oauth_callbacks, "exchange_code_for_tokens_with_integration", _exchange)

        callback = asyncio.create_task(_reauthorize(sessionmaker, integration_id))
        refresh = None
        try:
            await asyncio.wait_for(exchanging.wait(), 10)
            refresh = asyncio.create_task(_refresh(path, sessionmaker, integration_id, endpoint))
            await asyncio.wait_for(row_locked.wait(), 10)
            await _wait_for_a_writer_blocked_on_the_row(sessionmaker)
        finally:
            record_verdict.set()
            row_locked.set()
            await asyncio.wait_for(asyncio.gather(callback, *filter(None, [refresh])), 10)

        stored = await _read(sessionmaker, integration_id)
        assert (stored.access_token_enc, stored.refresh_token_enc) == ("at-new", "rt-new")
        assert stored.status == "connected"


@pytest.mark.parametrize("path", ["sweep", "chat"])
async def test_a_success_for_replaced_tokens_is_not_written_over_the_reauthorization(monkeypatch, path):
    async with _temp_database() as sessionmaker:
        integration_id = await _seed(sessionmaker)

        async def _reauthorized_meanwhile():
            async with sessionmaker() as db:
                await db.execute(
                    update(Integration)
                    .where(Integration.id == integration_id)
                    .values(access_token_enc="at-new", refresh_token_enc="rt-new")
                )
                await db.commit()

        endpoint = _TokenEndpoint(
            200,
            {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600},
            before_answer=_reauthorized_meanwhile,
        )
        _wire(monkeypatch, sessionmaker, endpoint)

        result = await asyncio.wait_for(_refresh(path, sessionmaker, integration_id, endpoint), 10)

        assert result == ("at-new" if path == "chat" else False)
        stored = await _read(sessionmaker, integration_id)
        assert (stored.access_token_enc, stored.refresh_token_enc) == ("at-new", "rt-new")
        assert stored.status == "connected"
