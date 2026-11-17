"""An MCP server's refresh token is presented once, and a refresh never overwrites a newer grant.

The scheduled sweep refreshes an MCP server's OAuth token, and so does a
connection whose token is about to expire or was refused (under the same
lock: ``test_mcp_oauth_token_refresh.py``). A tick that outlasts the scheduler's fleet-wide tick marker lets another
worker's tick select the same servers, and a server registered through dynamic
client registration is a public client: its authorization server rotates the
refresh token on every use and, with reuse detection, revokes the whole grant
when a spent one comes back. A re-authorization or a disconnect can also commit
while a refresh is in flight; the refresh's answer is about the tokens it
started from and must not be written over what was committed meanwhile.

These tests run the real tick (``refresh_expiring_tokens``) against an
in-memory row (written the way SQLAlchemy flushes: changed attributes only), an
in-memory Redis and a token endpoint that rotates with reuse detection. The row
lock the outcomes are applied under is PostgreSQL's:
``test_mcp_token_refresh_row_lock_postgres.py`` covers it.
"""

import asyncio
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.models.mcp_server import MCPServer
from app.services import token_refresh
from tests.test_token_refresh_rotation_race import (
    REFUSALS_INSIDE_A_200,
    _FakeRedis,
    _StoredRow,
    _TrackedSession,
    canned_answer,
)

SERVER_ID = "0c9b8a7d-6e5f-4a3b-9c2d-1e0f9a8b7c6d"


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the token URL's host; answer for DNS, never a real resolver."""
    from app.integrations import token_endpoint

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


class _TickSession(_TrackedSession):
    """The scheduler's session: the sweep's MCP server SELECT loads the stored row."""

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        rows = [self.track(self._row.read())] if entity is MCPServer else []
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


class _RotatingTokenEndpoint:
    """An OAuth 2.1 authorization server rotating refresh tokens, with reuse detection.

    A refresh token works once. Presenting a spent one is answered
    ``400 invalid_grant`` and revokes the whole grant, the live token included.
    """

    def __init__(self):
        self.live: str | None = "rt-1"
        self.presented: list[str] = []
        self.issued = 1
        self.while_in_flight = None
        self.answer: tuple[int, dict | str] | None = None
        self.error: Exception | None = None

    async def post(self, url, **kwargs):
        refresh_token = kwargs["data"]["refresh_token"]
        self.presented.append(refresh_token)
        if self.while_in_flight is not None:
            await self.while_in_flight()
        if self.error is not None:
            raise self.error
        request = httpx.Request("POST", url)
        if self.answer is not None:
            return canned_answer(request, *self.answer)
        if refresh_token != self.live:
            self.live = None
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "refresh token reused"}, request=request
            )
        self.issued += 1
        self.live = f"rt-{self.issued}"
        return httpx.Response(
            200,
            json={"access_token": f"at-{self.issued}", "refresh_token": self.live, "expires_in": 3600},
            request=request,
        )


class _HttpClient:
    """Stands in for ``httpx.AsyncClient`` in the sweep."""

    endpoint: _RotatingTokenEndpoint

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        return await self.endpoint.post(url, **kwargs)


def _expiring_soon() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=2)


def _issued_for_an_hour() -> datetime:
    """When a one-hour token :func:`_expiring_soon` was received: due for its refresh."""
    return datetime.now(UTC) - timedelta(minutes=58)


@pytest.fixture
def world(monkeypatch):
    row = _StoredRow(
        id=SERVER_ID,
        name="Remote MCP",
        auth_mode="oauth",
        oauth_status="connected",
        client_id_enc="dyn-client",
        client_secret_enc=None,
        access_token_enc="at-1",
        refresh_token_enc="rt-1",
        token_url="https://auth.example.com/oauth/token",
        scopes=None,
        oauth_resource="https://mcp.example.com/mcp",
        token_expires_at=_expiring_soon(),
        token_issued_at=_issued_for_an_hour(),
    )
    redis = _FakeRedis()
    endpoint = _RotatingTokenEndpoint()
    sessions: list[_TrackedSession] = []

    async def _redis():
        return redis

    def _session() -> _TrackedSession:
        sessions.append(_TrackedSession(row))
        return sessions[-1]

    monkeypatch.setattr(token_refresh, "redis_client", _redis)
    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr(token_refresh, "decrypt_value", lambda value: value)
    monkeypatch.setattr(token_refresh, "encrypt_value", lambda value: value)
    monkeypatch.setattr(token_refresh.httpx, "AsyncClient", type("_Client", (_HttpClient,), {"endpoint": endpoint}))
    return SimpleNamespace(row=row, redis=redis, endpoint=endpoint, sessions=sessions)


async def _tick(world) -> _TickSession:
    """One scheduler tick's token refresh, on a session of its own."""
    tick = _TickSession(world.row)
    await token_refresh.refresh_expiring_tokens(tick)
    return tick


def _committed_meanwhile(world, **values):
    """A ``while_in_flight`` hook: another session commits ``values`` during the token request."""

    async def _commit():
        world.row.values.update(values)

    return _commit


def _stored(world) -> tuple:
    values = world.row.values
    return values["access_token_enc"], values["refresh_token_enc"], values["oauth_status"]


def _cooldowns(world) -> list[str]:
    return [key for key in world.redis.values if key.startswith("token_refresh_cooldown:")]


def _writes(session: _TrackedSession) -> list[str]:
    return [event for event in session.events if event.startswith("wrote")]


async def test_two_overlapping_sweeps_present_the_refresh_token_once(world):
    """A tick outlasted the tick marker and another worker's tick selected the same server."""
    first_in_flight = asyncio.Event()
    answer_first = asyncio.Event()

    async def _hold_the_first_request():
        if len(world.endpoint.presented) == 1:
            first_in_flight.set()
            await answer_first.wait()

    world.endpoint.while_in_flight = _hold_the_first_request

    first = asyncio.create_task(_tick(world))
    await asyncio.wait_for(first_in_flight.wait(), 5)
    second = await asyncio.wait_for(_tick(world), 5)
    answer_first.set()
    await asyncio.wait_for(first, 5)

    assert world.endpoint.presented == ["rt-1"]
    assert _stored(world) == ("at-2", "rt-2", "connected")
    assert _cooldowns(world) == []
    assert _writes(second) == []


async def test_a_sweep_that_selected_the_server_earlier_reads_it_again_under_the_lock(world):
    """Another worker's sweep refreshed and stored the server after this one selected it."""
    selected_earlier = world.row.read()
    await _tick(world)

    assert await token_refresh._refresh_one_mcp(selected_earlier, world.endpoint, asyncio.Semaphore(1)) is False

    assert world.endpoint.presented == ["rt-1"]
    assert _stored(world) == ("at-2", "rt-2", "connected")


async def test_a_success_for_the_replaced_grant_is_not_written_over_a_reauthorization(world):
    """The authorization server still accepted the old refresh token once the new grant was stored."""
    world.endpoint.while_in_flight = _committed_meanwhile(
        world, access_token_enc="at-new", refresh_token_enc="rt-new", oauth_status="connected"
    )

    tick = await _tick(world)

    assert world.endpoint.presented == ["rt-1"]
    assert _stored(world) == ("at-new", "rt-new", "connected")
    assert _writes(tick) == []


async def test_a_refusal_of_the_replaced_grant_does_not_mark_the_new_one_error(world):
    world.endpoint.answer = (400, {"error": "invalid_grant", "error_description": "refresh token revoked"})
    world.endpoint.while_in_flight = _committed_meanwhile(
        world, access_token_enc="at-new", refresh_token_enc="rt-new", oauth_status="connected"
    )

    tick = await _tick(world)

    assert _stored(world) == ("at-new", "rt-new", "connected")
    assert _cooldowns(world) == []
    assert _writes(tick) == []


@pytest.mark.parametrize("payload", REFUSALS_INSIDE_A_200)
async def test_a_refusal_inside_a_200_of_the_replaced_grant_does_not_mark_the_new_one_error(world, payload):
    world.endpoint.answer = (200, payload)
    world.endpoint.while_in_flight = _committed_meanwhile(
        world, access_token_enc="at-new", refresh_token_enc="rt-new", oauth_status="connected"
    )

    tick = await _tick(world)

    assert _stored(world) == ("at-new", "rt-new", "connected")
    assert _cooldowns(world) == []
    assert _writes(tick) == []


@pytest.mark.parametrize("payload", REFUSALS_INSIDE_A_200)
async def test_a_permanent_refusal_inside_a_200_backs_the_server_off_without_a_reauthorize_state(world, payload):
    """MCP servers have no reauthorize state: the verdict is the hourly back-off (pinned in the contract test)."""
    world.endpoint.answer = (200, payload)

    await _tick(world)

    assert _stored(world) == ("at-1", "rt-1", "error")
    assert _cooldowns(world) == [token_refresh.mcp_refresh_cooldown_key(SERVER_ID)]


async def test_an_answer_arriving_after_a_disconnect_is_not_written_back(world):
    """Switching the server to header authentication cleared its tokens during the request."""
    world.endpoint.while_in_flight = _committed_meanwhile(
        world,
        auth_mode="headers",
        oauth_status="disconnected",
        access_token_enc=None,
        refresh_token_enc=None,
        token_expires_at=None,
    )

    tick = await _tick(world)

    assert world.endpoint.presented == ["rt-1"]
    assert _stored(world) == (None, None, "disconnected")
    assert world.row.values["token_expires_at"] is None
    assert _writes(tick) == []


async def test_a_refusal_after_the_dynamic_client_was_discarded_leaves_the_server_disconnected(world):
    """Discarding a dynamically registered client disconnects the server but keeps its tokens."""
    world.endpoint.answer = (401, {"error": "invalid_client"})
    world.endpoint.while_in_flight = _committed_meanwhile(world, client_id_enc=None, oauth_status="disconnected")

    tick = await _tick(world)

    assert _stored(world) == ("at-1", "rt-1", "disconnected")
    assert _cooldowns(world) == []
    assert _writes(tick) == []


async def test_the_rotated_tokens_are_stored_before_the_lock_is_released(world, monkeypatch):
    """The next sweep to take the lock must read the rotated refresh token, never the spent one."""
    stored_at_release: list[str] = []
    release = token_refresh.release_refresh_lock

    async def _release(lock_id, token):
        stored_at_release.append(world.row.values["refresh_token_enc"])
        await release(lock_id, token)

    monkeypatch.setattr(token_refresh, "release_refresh_lock", _release)

    await _tick(world)

    assert stored_at_release == ["rt-2"]
    assert world.redis.values == {}


async def test_the_refresh_lock_is_the_server_s_own_not_an_integration_s(world):
    world.redis.values[f"token_refresh:{SERVER_ID}"] = "an integration refresh"

    await _tick(world)

    assert world.endpoint.presented == ["rt-1"]
    assert f"token_refresh:mcp:{SERVER_ID}" not in world.redis.values

    world.row.values.update(token_expires_at=_expiring_soon(), token_issued_at=_issued_for_an_hour())
    world.redis.values[f"token_refresh:mcp:{SERVER_ID}"] = "another worker's sweep"

    await _tick(world)

    assert world.endpoint.presented == ["rt-1"]
    assert _stored(world) == ("at-2", "rt-2", "connected")


@pytest.mark.parametrize(
    "answer", ["rotated", "dead grant", "dead grant inside a 200", "rejected client", "outage", "network error"]
)
async def test_every_outcome_is_written_right_after_a_locked_read(world, answer):
    """The read that confirms the stored tokens and the write share one transaction; the tick writes nothing."""
    if answer == "dead grant":
        world.endpoint.answer = (400, {"error": "invalid_grant"})
    elif answer == "dead grant inside a 200":
        world.endpoint.answer = (200, {"error": "bad_refresh_token"})
    elif answer == "rejected client":
        world.endpoint.answer = (401, {"error": "invalid_client"})
    elif answer == "outage":
        world.endpoint.answer = (503, {"error": "temporarily_unavailable"})
    elif answer == "network error":
        world.endpoint.error = httpx.ConnectError("token endpoint unreachable")

    tick = await _tick(world)

    writers = [session for session in world.sessions if _writes(session)]
    assert len(writers) == 1
    locked_read, write = writers[0].events
    assert locked_read == "locked read"
    assert write.startswith("wrote")
    assert _writes(tick) == []
