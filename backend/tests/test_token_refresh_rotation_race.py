"""Concurrent refreshes of a rotating refresh token never read as a dead grant.

Atlassian issues a new refresh token on every refresh and refuses the one it
replaced with ``403 unauthorized_client "refresh_token is invalid"``, the same
answer it gives for a genuinely dead grant. The scheduled sweep and the
chat-time refresh share one Redis lock per integration, but each works from a
row it read earlier (sessions use ``expire_on_commit=False``), and the sweep
used to commit its results only at the end of the tick, after releasing each
lock. So a refresh could present a token another refresh had just spent, and
that answer marks the integration ``reauthorize``: a healthy
integration would be taken out of every ``connected``-only resolution until
someone re-authorized it.

These tests replay the interleavings against an in-memory row (written the way
SQLAlchemy flushes: changed attributes only), an in-memory Redis and a token
endpoint that rotates like Atlassian's. The row lock the outcomes are applied
under is PostgreSQL's: ``test_token_refresh_row_lock_postgres.py`` covers it.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.services import integration_tools, token_refresh

INTEGRATION_ID = "5f0c1d2e-3a4b-4c5d-8e6f-708192a3b4c5"
DEAD_REFRESH_TOKEN = {"error": "unauthorized_client", "error_description": "refresh_token is invalid"}


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the token URL's host; answer for DNS, never a real resolver."""
    import socket

    from app.integrations import token_endpoint

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


class _StoredRow:
    """The committed ``integrations`` row, as every process reads it."""

    def __init__(self, **values):
        self.values = values

    def read(self) -> SimpleNamespace:
        return SimpleNamespace(**self.values)


class _TrackedSession:
    """Flushes only the attributes changed since load, like a SQLAlchemy session."""

    def __init__(self, row: _StoredRow):
        self._row = row
        self._loaded: list[tuple[SimpleNamespace, dict]] = []
        self.commits = 0
        self.rollbacks = 0
        self.events: list[str] = []

    def track(self, obj: SimpleNamespace) -> SimpleNamespace:
        self._loaded.append((obj, dict(vars(obj))))
        return obj

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, ident, **_kwargs):
        if str(ident) != str(self._row.values["id"]):
            return None
        return self.track(self._row.read())

    async def refresh(self, obj, attribute_names=None, with_for_update=None):
        fresh = {name: self._row.values[name] for name in attribute_names or self._row.values}
        vars(obj).update(fresh)
        for loaded, base in self._loaded:
            if loaded is obj:
                base.update(fresh)
        if with_for_update:
            self.events.append("locked read")

    def in_transaction(self) -> bool:
        return False

    async def commit(self):
        self.commits += 1
        for obj, base in self._loaded:
            changed = {k: v for k, v in vars(obj).items() if base.get(k) != v}
            self._row.values.update(changed)
            base.update(changed)
            if changed:
                self.events.append("wrote " + ", ".join(sorted(changed)))

    async def rollback(self):
        self.rollbacks += 1


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
        """The refresh lock's compare-and-delete release."""
        if self.values.get(key) != token:
            return 0
        del self.values[key]
        return 1


def canned_answer(request: httpx.Request, status_code: int, payload: dict | str) -> httpx.Response:
    """A JSON answer, or a form-encoded one given as a string (GitHub's default)."""
    if isinstance(payload, str):
        headers = {"content-type": "application/x-www-form-urlencoded"}
        return httpx.Response(status_code, content=payload.encode(), headers=headers, request=request)
    return httpx.Response(status_code, json=payload, request=request)


# GitHub and Slack refuse a refresh inside a 200 (``permanent_refresh_failure_reason``).
REFUSALS_INSIDE_A_200 = [
    pytest.param(
        {"error": "bad_refresh_token", "error_description": "The refresh token passed is incorrect or expired."},
        id="github",
    ),
    pytest.param("error=bad_refresh_token&error_description=The+refresh+token+passed+is+incorrect+or+expired.", id="github-form"),
    pytest.param({"ok": False, "error": "invalid_refresh_token"}, id="slack"),
    pytest.param({"ok": False, "error": "bad_client_secret"}, id="slack-client"),
]


class _RotatingTokenEndpoint:
    """A refresh token works once; presenting it again reads as a dead grant."""

    def __init__(self, live_refresh_token: str):
        self.live = live_refresh_token
        self.presented: list[str] = []
        self.requests: list[tuple[str, dict]] = []
        self.issued = 1
        self.while_in_flight = None
        self.error: Exception | None = None
        self.answer: tuple[int, dict | str] | None = None

    async def post(self, url, **kwargs):
        body = kwargs.get("json") or kwargs.get("data") or {}
        self.presented.append(body["refresh_token"])
        self.requests.append((url, body))
        if self.while_in_flight is not None:
            await self.while_in_flight()
        if self.error is not None:
            raise self.error
        request = httpx.Request("POST", url)
        if self.answer is not None:
            return canned_answer(request, *self.answer)
        if body["refresh_token"] != self.live:
            return httpx.Response(403, json=DEAD_REFRESH_TOKEN, request=request)
        self.issued += 1
        self.live = f"rt-{self.issued}"
        return httpx.Response(
            200,
            json={"access_token": f"at-{self.issued}", "refresh_token": self.live, "expires_in": 3600},
            request=request,
        )


class _ChatHttpClient:
    """Stands in for ``httpx.AsyncClient`` inside the chat-time refresh."""

    endpoint: _RotatingTokenEndpoint

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        return await self.endpoint.post(url, **kwargs)


@pytest.fixture
def world(monkeypatch):
    row = _StoredRow(
        id=INTEGRATION_ID,
        name="Jira",
        provider="jira",
        auth_type="oauth",
        status="connected",
        client_id_enc="client-id",
        client_secret_enc="client-secret",
        access_token_enc="at-1",
        refresh_token_enc="rt-1",
        token_url="https://auth.atlassian.com/oauth/token",
        scopes=None,
        extra_config_enc=None,
        token_expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    redis = _FakeRedis()
    endpoint = _RotatingTokenEndpoint("rt-1")
    sessions: list[_TrackedSession] = []

    async def _redis():
        return redis

    def _session() -> _TrackedSession:
        sessions.append(_TrackedSession(row))
        return sessions[-1]

    monkeypatch.setattr(token_refresh, "redis_client", _redis)
    monkeypatch.setattr("app.core.redis.redis_client", _redis)
    monkeypatch.setattr("app.core.database.async_session", _session)
    for module in (token_refresh, integration_tools):
        monkeypatch.setattr(module, "decrypt_value", lambda value: value)
        monkeypatch.setattr(module, "encrypt_value", lambda value: value)
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda value: value)
    client = type("_Client", (_ChatHttpClient,), {"endpoint": endpoint})
    monkeypatch.setattr(integration_tools.httpx, "AsyncClient", client)
    return SimpleNamespace(row=row, redis=redis, endpoint=endpoint, sessions=sessions)


async def _chat_refresh(world, integ: SimpleNamespace) -> tuple[str | None, _TrackedSession]:
    """A chat turn refreshing ``integ``, a row its own session loaded earlier."""
    turn_session = _TrackedSession(world.row)
    turn_session.track(integ)
    token = await integration_tools._try_inline_refresh(turn_session, integ)
    return token, turn_session


async def _sweep_refresh(integ: SimpleNamespace, endpoint) -> bool:
    return await token_refresh._refresh_one_integration(integ, endpoint, asyncio.Semaphore(1))


def _cooldowns(world) -> list[str]:
    return [key for key in world.redis.values if key.startswith("token_refresh_cooldown:")]


async def test_the_sweep_does_not_spend_a_token_a_chat_turn_already_rotated(world):
    """The sweep read the row, then a chat turn refreshed it before the sweep got there."""
    sweep_row = world.row.read()
    chat_row = world.row.read()
    chat_done = asyncio.Event()

    async def _chat():
        token, _ = await _chat_refresh(world, chat_row)
        chat_done.set()
        return token

    async def _sweep():
        await chat_done.wait()
        return await _sweep_refresh(sweep_row, world.endpoint)

    chat_token, _ = await asyncio.gather(_chat(), _sweep())

    assert chat_token == "at-2"
    assert world.endpoint.presented == ["rt-1"]
    assert sweep_row.status == "connected"
    assert world.row.values["status"] == "connected"
    assert world.row.values["refresh_token_enc"] == "rt-2"
    assert _cooldowns(world) == []


async def test_the_sweep_stores_the_rotated_token_before_releasing_the_lock(world, monkeypatch):
    """A chat turn that takes the lock right after the sweep must read the sweep's token."""
    chat_row = world.row.read()
    sweep_row = world.row.read()
    stored_when_released: list[str] = []
    release = token_refresh.release_refresh_lock

    async def _release(integration_id, token):
        stored_when_released.append(world.row.values["refresh_token_enc"])
        await release(integration_id, token)

    monkeypatch.setattr(token_refresh, "release_refresh_lock", _release)

    assert await _sweep_refresh(sweep_row, world.endpoint) is True
    token, _ = await _chat_refresh(world, chat_row)

    assert stored_when_released[0] == "rt-2"
    assert token == "at-2"
    assert world.endpoint.presented == ["rt-1"]
    assert world.row.values["status"] == "connected"
    assert world.row.values["refresh_token_enc"] == "rt-2"


async def test_a_chat_turn_with_a_stale_row_uses_the_token_another_turn_stored(world):
    first_turn = world.row.read()
    second_turn = world.row.read()

    first_token, _ = await _chat_refresh(world, first_turn)
    second_token, _ = await _chat_refresh(world, second_turn)

    assert first_token == second_token == "at-2"
    assert world.endpoint.presented == ["rt-1"]
    assert world.row.values["status"] == "connected"
    assert _cooldowns(world) == []


async def _reauthorized_mid_flight(world):
    async def _callback_commits():
        world.row.values.update(access_token_enc="at-new", refresh_token_enc="rt-new", status="connected")

    world.endpoint.live = "rt-spent-elsewhere"
    world.endpoint.while_in_flight = _callback_commits


async def test_a_chat_time_dead_grant_about_a_replaced_token_is_not_a_verdict(world):
    """A re-authorization (which takes no refresh lock) landed while the refresh was in flight."""
    await _reauthorized_mid_flight(world)
    integ = world.row.read()

    token, turn_session = await _chat_refresh(world, integ)

    assert token == "at-new"
    assert world.row.values["status"] == "connected"
    assert world.row.values["refresh_token_enc"] == "rt-new"
    assert turn_session.rollbacks == 0
    assert _cooldowns(world) == []


async def test_a_scheduled_dead_grant_about_a_replaced_token_is_not_a_verdict(world):
    await _reauthorized_mid_flight(world)
    sweep_row = world.row.read()

    assert await _sweep_refresh(sweep_row, world.endpoint) is False

    assert sweep_row.status == "connected"
    assert world.row.values["status"] == "connected"
    assert world.row.values["refresh_token_enc"] == "rt-new"
    assert _cooldowns(world) == []


@pytest.mark.parametrize("path", ["chat", "sweep"])
@pytest.mark.parametrize("payload", REFUSALS_INSIDE_A_200)
async def test_a_refusal_inside_a_200_about_a_replaced_token_is_not_a_verdict(world, path, payload):
    """GitHub or Slack refused the refresh token a re-authorization replaced while the refresh was in flight."""

    async def _callback_commits():
        world.row.values.update(access_token_enc="at-new", refresh_token_enc="rt-new", status="connected")

    world.endpoint.answer = (200, payload)
    world.endpoint.while_in_flight = _callback_commits

    if path == "chat":
        token, turn_session = await _chat_refresh(world, world.row.read())
        assert token == "at-new"
        assert (turn_session.commits, turn_session.rollbacks) == (0, 0)
    else:
        assert await _sweep_refresh(world.row.read(), world.endpoint) is False

    assert _stored_tokens(world) == ("at-new", "rt-new")
    assert world.row.values["status"] == "connected"
    assert _cooldowns(world) == []


@pytest.mark.parametrize("status", ["connected", "error"])
@pytest.mark.parametrize("payload", REFUSALS_INSIDE_A_200)
async def test_a_refusal_inside_a_200_marks_the_integration_and_both_paths_leave_it(world, status, payload):
    world.row.values["status"] = status
    world.endpoint.answer = (200, payload)

    assert await _sweep_refresh(world.row.read(), world.endpoint) is False
    assert world.row.values["status"] == "reauthorize"
    assert _cooldowns(world) == []

    token, _ = await _chat_refresh(world, world.row.read())
    assert token is None
    assert world.endpoint.presented == ["rt-1"]


@pytest.mark.parametrize("payload", REFUSALS_INSIDE_A_200)
async def test_a_chat_time_refusal_inside_a_200_is_saved_without_touching_the_turn_session(world, payload):
    world.endpoint.answer = (200, payload)
    integ = world.row.read()

    token, turn_session = await _chat_refresh(world, integ)

    assert token is None
    assert world.row.values["status"] == "reauthorize"
    assert integ.status == "connected"
    assert (turn_session.commits, turn_session.rollbacks) == (0, 0)
    # The sweep, which re-reads the row under the lock, leaves it alone too.
    assert await _sweep_refresh(world.row.read(), world.endpoint) is False
    assert world.endpoint.presented == ["rt-1"]


async def test_a_chat_time_verdict_is_saved_without_touching_the_turn_session(world):
    """Rolling the turn's session back would expire every row the turn loaded."""
    world.endpoint.live = "rt-revoked-upstream"
    integ = world.row.read()

    token, turn_session = await _chat_refresh(world, integ)

    assert token is None
    assert world.row.values["status"] == "reauthorize"
    assert integ.status == "connected"
    assert turn_session.commits == 0
    assert turn_session.rollbacks == 0


@pytest.mark.parametrize("status", ["connected", "error"])
async def test_a_genuinely_dead_grant_still_marks_the_integration_on_both_paths(world, status):
    """An integration left in ``error`` by the old hourly loop moves to ``reauthorize`` too."""
    world.row.values["status"] = status
    world.endpoint.live = "rt-revoked-upstream"

    assert await _sweep_refresh(world.row.read(), world.endpoint) is False
    assert world.row.values["status"] == "reauthorize"
    assert _cooldowns(world) == []

    # Once marked, the chat path leaves the token endpoint alone.
    token, _ = await _chat_refresh(world, world.row.read())
    assert token is None
    assert world.endpoint.presented == ["rt-1"]


async def test_the_sweep_skips_a_row_another_process_already_refreshed(world):
    """The token is no longer due once the stored row carries a fresh expiry."""
    sweep_row = world.row.read()
    world.row.values.update(
        access_token_enc="at-9",
        refresh_token_enc="rt-9",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert await _sweep_refresh(sweep_row, world.endpoint) is False
    assert world.endpoint.presented == []
    assert world.row.values["refresh_token_enc"] == "rt-9"


def _stored_tokens(world) -> tuple[str, str]:
    return world.row.values["access_token_enc"], world.row.values["refresh_token_enc"]


@pytest.mark.parametrize("path", ["chat", "sweep"])
async def test_a_success_for_a_replaced_token_does_not_overwrite_the_reauthorization(world, path):
    """The provider accepted the old refresh token while a re-authorization committed."""

    async def _callback_commits():
        world.row.values.update(access_token_enc="at-new", refresh_token_enc="rt-new", status="connected")

    world.endpoint.while_in_flight = _callback_commits

    if path == "chat":
        token, _ = await _chat_refresh(world, world.row.read())
        assert token == "at-new"
    else:
        assert await _sweep_refresh(world.row.read(), world.endpoint) is False

    assert world.endpoint.presented == ["rt-1"]
    assert _stored_tokens(world) == ("at-new", "rt-new")
    assert world.row.values["status"] == "connected"


async def test_a_chat_refresh_sends_the_client_credentials_as_stored(world):
    """The client secret and token URL were edited after the turn read the row."""
    turn_row = world.row.read()
    world.row.values.update(client_secret_enc="rotated-secret", token_url="https://auth.atlassian.com/oauth/token?v=2")

    token, _ = await _chat_refresh(world, turn_row)

    assert token == "at-2"
    url, body = world.endpoint.requests[-1]
    assert url == "https://auth.atlassian.com/oauth/token?v=2"
    assert body["client_secret"] == "rotated-secret"


@pytest.mark.parametrize(
    ("path", "answer"),
    [
        ("sweep", "rotated"),
        ("sweep", "dead grant"),
        ("sweep", "unexplained 401"),
        ("sweep", "outage"),
        ("sweep", "network error"),
        ("sweep", "dead grant inside a 200"),
        ("sweep", "refusal inside a 200"),
        ("chat", "rotated"),
        ("chat", "dead grant"),
        ("chat", "dead grant inside a 200"),
    ],
)
async def test_every_outcome_is_written_right_after_a_locked_read(world, path, answer):
    """The read that confirms the tokens and the write share one transaction."""
    if answer == "dead grant":
        world.endpoint.live = "rt-revoked-upstream"
    elif answer == "dead grant inside a 200":
        world.endpoint.answer = (200, {"error": "bad_refresh_token"})
    elif answer == "refusal inside a 200":
        world.endpoint.answer = (200, {"ok": False, "error": "ratelimited"})
    elif answer == "unexplained 401":
        world.endpoint.answer = (401, {"error": "unauthorized_client"})
    elif answer == "outage":
        world.endpoint.answer = (503, {"error": "temporarily_unavailable"})
    elif answer == "network error":
        world.endpoint.error = httpx.ConnectError("token endpoint unreachable")

    if path == "sweep":
        await _sweep_refresh(world.row.read(), world.endpoint)
    else:
        await _chat_refresh(world, world.row.read())

    writers = [session for session in world.sessions if any(e.startswith("wrote") for e in session.events)]
    assert len(writers) == 1
    locked_read, write = writers[0].events
    assert locked_read == "locked read"
    assert write.startswith("wrote")


async def test_a_refresh_that_outlived_its_lock_cannot_leave_the_integration_marked(world, monkeypatch):
    """A chat refresh's lock lapsed while its answer was on the way, and the sweep took it.

    The chat request had rotated the token, so the sweep presented a spent one
    and recorded a dead grant. The chat answer then arrives for the tokens still
    stored: it is saved, and the verdict about those tokens is lifted.
    """

    class _SlowFirstAnswer(_RotatingTokenEndpoint):
        def __init__(self):
            super().__init__("rt-1")
            self.first_rotated = asyncio.Event()
            self.answer_first = asyncio.Event()

        async def post(self, url, **kwargs):
            if self.presented:
                return await super().post(url, **kwargs)
            response = await super().post(url, **kwargs)
            self.first_rotated.set()
            await self.answer_first.wait()
            return response

    endpoint = _SlowFirstAnswer()
    monkeypatch.setattr(integration_tools.httpx, "AsyncClient", type("_Client", (_ChatHttpClient,), {"endpoint": endpoint}))

    chat = asyncio.create_task(_chat_refresh(world, world.row.read()))
    await endpoint.first_rotated.wait()
    world.redis.values.pop(f"token_refresh:{INTEGRATION_ID}")
    assert await _sweep_refresh(world.row.read(), endpoint) is False
    assert world.row.values["status"] == "reauthorize"

    endpoint.answer_first.set()
    token, _ = await chat

    assert token == "at-2"
    assert endpoint.presented == ["rt-1", "rt-1"]
    assert _stored_tokens(world) == ("at-2", "rt-2")
    assert world.row.values["status"] == "connected"
    assert _cooldowns(world) == []


async def test_releasing_a_lapsed_refresh_lock_leaves_the_next_holder_s_lock_alone(world):
    first = await token_refresh.acquire_refresh_lock(INTEGRATION_ID)
    world.redis.values.pop(f"token_refresh:{INTEGRATION_ID}")
    second = await token_refresh.acquire_refresh_lock(INTEGRATION_ID)
    assert first and second and first != second

    await token_refresh.release_refresh_lock(INTEGRATION_ID, first)
    assert await token_refresh.acquire_refresh_lock(INTEGRATION_ID) is None

    await token_refresh.release_refresh_lock(INTEGRATION_ID, second)
    assert await token_refresh.acquire_refresh_lock(INTEGRATION_ID) is not None


async def test_a_back_off_learned_from_a_replaced_token_does_not_hold_the_new_grant_back(world):
    """A re-authorization committed and cleared the back-off while a refresh of the
    old token was in flight; that refresh then failed and started one."""

    async def _reauthorize():
        world.row.values.update(access_token_enc="at-new", refresh_token_enc="rt-new", status="connected")
        await token_refresh.clear_refresh_cooldown(token_refresh.refresh_cooldown_key(INTEGRATION_ID))
        world.endpoint.live = "rt-new"

    world.endpoint.while_in_flight = _reauthorize
    world.endpoint.error = httpx.ConnectError("token endpoint unreachable")

    token, _ = await _chat_refresh(world, world.row.read())

    assert token is None
    assert _cooldowns(world) == [token_refresh.refresh_cooldown_key(INTEGRATION_ID)]

    world.endpoint.while_in_flight = None
    world.endpoint.error = None
    new_grant = world.row.read()
    assert await integration_tools._inline_refresh_on_cooldown(new_grant) is False
    assert await _sweep_refresh(new_grant, world.endpoint) is True
    assert world.endpoint.presented == ["rt-1", "rt-new"]
