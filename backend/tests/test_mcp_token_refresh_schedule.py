"""A short-lived MCP OAuth token is refreshed on a schedule set by its lifetime.

The sweep refreshed an MCP server's token once it expired within 15 minutes.
A 10-minute token is inside that window as soon as it is issued, so the server
was refreshed (and its rotating refresh token rotated) on every 60 s tick, under
a lock the connection path then waited for. A failed refresh also backed off
for 15 minutes, longer than the token lived. These tests run the real tick
(``refresh_expiring_tokens``) against an in-memory row and Redis, as
``test_mcp_token_refresh_race.py`` does, with a token endpoint issuing
10-minute tokens; time passing is the stored timestamps moving back.
"""

import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.oauth import apply_mcp_token_expiry
from app.services import token_refresh
from app.services.token_refresh import (
    _AUTH_FAIL_COOLDOWN,
    _TRANSIENT_FAIL_COOLDOWN,
    mcp_refresh_due_at,
    mcp_transient_refresh_cooldown,
)
from tests.test_mcp_token_refresh_race import SERVER_ID, _HttpClient, _TickSession
from tests.test_token_refresh_rotation_race import _FakeRedis, _StoredRow, _TrackedSession

LIFETIME = 600  # seconds: the authorization server's 10-minute tokens


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the token URL's host; answer for DNS, never a real resolver."""
    from app.integrations import token_endpoint

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


class _TenMinuteTokenEndpoint:
    """An authorization server issuing 10-minute tokens and rotating the refresh token."""

    def __init__(self):
        self.presented: list[str] = []
        self.issued = 1
        self.answer: tuple[int, dict] | None = None

    async def post(self, url, **kwargs):
        self.presented.append(kwargs["data"]["refresh_token"])
        request = httpx.Request("POST", url)
        if self.answer is not None:
            return httpx.Response(self.answer[0], json=self.answer[1], request=request)
        self.issued += 1
        return httpx.Response(
            200,
            json={"access_token": f"at-{self.issued}", "refresh_token": f"rt-{self.issued}", "expires_in": LIFETIME},
            request=request,
        )


class _ExpiryRecordingRedis(_FakeRedis):
    def __init__(self):
        super().__init__()
        self.expiries: dict[str, int | None] = {}

    async def set(self, key, value, *, ex=None, nx=False):
        stored = await super().set(key, value, ex=ex, nx=nx)
        if stored:
            self.expiries[key] = ex
        return stored


def _issued(seconds_ago: float) -> dict:
    """A 10-minute token received ``seconds_ago``."""
    issued_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return {"token_issued_at": issued_at, "token_expires_at": issued_at + timedelta(seconds=LIFETIME)}


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
        **_issued(0),
    )
    redis = _ExpiryRecordingRedis()
    endpoint = _TenMinuteTokenEndpoint()
    locks: list[str] = []
    acquire = token_refresh.acquire_refresh_lock

    async def _redis():
        return redis

    async def _acquire(lock_id):
        locks.append(lock_id)
        return await acquire(lock_id)

    monkeypatch.setattr(token_refresh, "redis_client", _redis)
    monkeypatch.setattr(token_refresh, "acquire_refresh_lock", _acquire)
    monkeypatch.setattr("app.core.database.async_session", lambda: _TrackedSession(row))
    monkeypatch.setattr(token_refresh, "decrypt_value", lambda value: value)
    monkeypatch.setattr(token_refresh, "encrypt_value", lambda value: value)
    monkeypatch.setattr(token_refresh.httpx, "AsyncClient", type("_Client", (_HttpClient,), {"endpoint": endpoint}))
    return SimpleNamespace(row=row, redis=redis, endpoint=endpoint, locks=locks)


async def _tick(world) -> int:
    return await token_refresh.refresh_expiring_tokens(_TickSession(world.row))


def _pass(world, seconds: float) -> None:
    """``seconds`` go by: the stored token is that much older."""
    for name in ("token_issued_at", "token_expires_at"):
        world.row.values[name] -= timedelta(seconds=seconds)


def _cooldown_key() -> str:
    return token_refresh.mcp_refresh_cooldown_key(SERVER_ID)


# ── When a token is due ──────────────────────────────────────────────────────

_NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "due_at"),
    [
        pytest.param(_NOW, _NOW + timedelta(minutes=10), _NOW + timedelta(minutes=5), id="10-minute token: halfway"),
        pytest.param(_NOW, _NOW + timedelta(hours=1), _NOW + timedelta(minutes=45), id="1-hour token: 15 min ahead"),
        pytest.param(_NOW, _NOW + timedelta(days=1), _NOW + timedelta(days=1, minutes=-15), id="1-day token: 15 min ahead"),
        pytest.param(_NOW, _NOW + timedelta(minutes=1), _NOW + timedelta(minutes=2), id="1-minute token: 2 min floor"),
        pytest.param(_NOW, _NOW + timedelta(seconds=1), _NOW + timedelta(minutes=2), id="stamped expired: 2 min floor"),
        pytest.param(None, _NOW + timedelta(minutes=10), _NOW - timedelta(minutes=5), id="unknown issue: 15 min ahead"),
        pytest.param(_NOW, None, None, id="unknown expiry: due now"),
        pytest.param(
            _NOW.replace(tzinfo=None),
            (_NOW + timedelta(minutes=10)).replace(tzinfo=None),
            _NOW + timedelta(minutes=5),
            id="naive timestamps read as UTC",
        ),
    ],
)
def test_a_token_is_due_by_its_lifetime(issued_at, expires_at, due_at):
    assert mcp_refresh_due_at(issued_at, expires_at) == due_at


# ── The sweep ────────────────────────────────────────────────────────────────


async def test_a_10_minute_token_is_not_refreshed_on_every_tick(world):
    """It is within 15 minutes of its expiry as soon as it is issued."""
    for _ in range(5):
        assert await _tick(world) == 0
        _pass(world, 60)

    assert world.endpoint.presented == []
    # Not even locked: the connection path never waits for a refresh that does nothing.
    assert world.locks == []


async def test_a_10_minute_token_is_refreshed_once_per_half_lifetime(world):
    _pass(world, LIFETIME / 2 - 30)
    assert await _tick(world) == 0
    _pass(world, 31)

    assert await _tick(world) == 1
    assert world.endpoint.presented == ["rt-1"]
    values = world.row.values
    assert (values["access_token_enc"], values["refresh_token_enc"], values["oauth_status"]) == ("at-2", "rt-2", "connected")
    lifetime = values["token_expires_at"] - values["token_issued_at"]
    assert lifetime.total_seconds() == pytest.approx(LIFETIME, abs=2)

    # The new token waits for its own halfway point.
    for _ in range(4):
        _pass(world, 60)
        assert await _tick(world) == 0
    _pass(world, 61)
    assert await _tick(world) == 1
    assert world.endpoint.presented == ["rt-1", "rt-2"]


async def test_a_token_stored_before_its_issue_time_was_recorded_is_refreshed_15_minutes_ahead(world):
    world.row.values.update(token_issued_at=None, token_expires_at=datetime.now(UTC) + timedelta(minutes=16))
    assert await _tick(world) == 0

    world.row.values["token_expires_at"] = datetime.now(UTC) + timedelta(minutes=14)
    assert await _tick(world) == 1
    # The refresh records it: the next token is scheduled by its lifetime.
    assert world.row.values["token_issued_at"] is not None
    assert await _tick(world) == 0
    assert world.endpoint.presented == ["rt-1"]


# ── A failed refresh ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        pytest.param(503, {"error": "temporarily_unavailable"}, id="503"),
        pytest.param(500, {"error": "server_error"}, id="500"),
        pytest.param(429, {"error": "rate_limited"}, id="429"),
    ],
)
async def test_a_transient_failure_is_retried_before_the_token_expires(world, status_code, payload):
    _pass(world, LIFETIME / 2 + 1)
    world.endpoint.answer = (status_code, payload)

    assert await _tick(world) == 0
    remaining = (world.row.values["token_expires_at"] - datetime.now(UTC)).total_seconds()
    cooldown = world.redis.expiries[_cooldown_key()]
    assert cooldown == pytest.approx(remaining / 2, abs=2)
    assert world.row.values["oauth_status"] == "error"

    # The back-off holds until it lapses, then the token, still valid, is refreshed.
    assert await _tick(world) == 0
    assert world.endpoint.presented == ["rt-1"]
    del world.redis.values[_cooldown_key()]
    _pass(world, cooldown)
    assert world.row.values["token_expires_at"] > datetime.now(UTC)
    world.endpoint.answer = None

    assert await _tick(world) == 1
    assert world.endpoint.presented == ["rt-1", "rt-1"]
    assert world.row.values["oauth_status"] == "connected"


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        pytest.param(400, {"error": "invalid_grant"}, id="dead grant"),
        pytest.param(401, {"error": "invalid_client"}, id="rejected client"),
        pytest.param(403, {"error": "access_denied"}, id="unexplained 403"),
        pytest.param(200, {"error": "bad_refresh_token"}, id="refusal inside a 200"),
    ],
)
async def test_a_refused_refresh_keeps_the_long_back_off(world, status_code, payload):
    _pass(world, LIFETIME / 2 + 1)
    world.endpoint.answer = (status_code, payload)

    assert await _tick(world) == 0
    assert world.redis.expiries[_cooldown_key()] == _AUTH_FAIL_COOLDOWN


@pytest.mark.parametrize(
    ("remaining", "cooldown"),
    [
        pytest.param(None, _TRANSIENT_FAIL_COOLDOWN, id="unknown expiry"),
        pytest.param(3600, _TRANSIENT_FAIL_COOLDOWN, id="an hour left"),
        pytest.param(1800, _TRANSIENT_FAIL_COOLDOWN, id="half an hour left"),
        pytest.param(300, 150, id="5 minutes left"),
        pytest.param(40, 30, id="40 s left: floor"),
        pytest.param(-120, 30, id="expired: floor"),
    ],
)
def test_the_transient_back_off_is_bounded_by_the_remaining_lifetime(remaining, cooldown):
    expires_at = None if remaining is None else datetime.now(UTC) + timedelta(seconds=remaining)
    assert mcp_transient_refresh_cooldown(expires_at) == pytest.approx(cooldown, abs=1)


# ── Where the issue time is recorded ─────────────────────────────────────────


def test_a_received_token_records_when_it_was_issued():
    server = SimpleNamespace(token_expires_at=None, token_issued_at=None, refresh_token_enc="rt")
    before = datetime.now(UTC)

    apply_mcp_token_expiry(server, {"access_token": "at", "refresh_token": "rt", "expires_in": LIFETIME})

    assert before <= server.token_issued_at <= datetime.now(UTC)
    lifetime = server.token_expires_at - server.token_issued_at
    assert lifetime.total_seconds() == pytest.approx(LIFETIME, abs=1)
