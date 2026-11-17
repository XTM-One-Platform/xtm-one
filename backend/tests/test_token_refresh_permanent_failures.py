"""Permanent OAuth refresh failures stop the retry loop on both refresh paths.

A Notion integration answered ``401 invalid_client`` on every hourly
refresh, forever: the stored client secret no longer matched the Notion app,
and the scheduler treated the 401 as a cooldown, not as a verdict.

Atlassian answered ``401 unauthorized_client "Token was globally
revoked"`` and ``403 unauthorized_client "refresh_token is invalid"``, with the
same hourly loop. Both are dead grants.

In both cases the chat-time refresh (``integration_tools._try_inline_refresh``)
ignored the scheduler's back-off and hit the token endpoint again on every
turn that used the integration.
"""

import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import inspect
from sqlalchemy.orm.attributes import set_committed_value

from app.api import oauth_callbacks
from app.models.integration import Integration
from app.models.mcp_server import MCPServer
from app.services import integration_tools, token_refresh
from app.services.token_refresh import (
    _AUTH_FAIL_COOLDOWN,
    _TRANSIENT_FAIL_COOLDOWN,
    _do_refresh_integration,
    is_client_credentials_rejected,
    permanent_refresh_failure_reason,
    refresh_cooldown_key,
)


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the token URL's host; answer for DNS, never a real resolver."""
    import socket

    from app.integrations import token_endpoint

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


NOTION_INVALID_CLIENT = {"error": "invalid_client", "request_id": "b2c3"}
ATLASSIAN_REVOKED = {"error": "unauthorized_client", "error_description": "Token was globally revoked"}
ATLASSIAN_INVALID_RT = {"error": "unauthorized_client", "error_description": "refresh_token is invalid"}


def _integration(**overrides) -> SimpleNamespace:
    values = dict(
        id="0c5a91f6-9be3-4a3f-9c86-2f3c1f1f1f1f",
        name="My Notion",
        provider="notion",
        status="connected",
        client_id_enc="client-id",
        client_secret_enc="client-secret",
        refresh_token_enc="refresh-token",
        access_token_enc="access-token",
        token_url="https://api.notion.com/v1/oauth/token",
        scopes=None,
        extra_config_enc=None,
        token_expires_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "https://example.com/token"),
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_invalid_client_is_a_client_credentials_rejection():
    assert is_client_credentials_rejected(401, json.dumps(NOTION_INVALID_CLIENT)) is True
    assert is_client_credentials_rejected(400, json.dumps(NOTION_INVALID_CLIENT)) is True
    assert is_client_credentials_rejected(401, "error=invalid_client&error_description=bad") is True


def test_other_answers_are_not_a_client_credentials_rejection():
    assert is_client_credentials_rejected(403, json.dumps(NOTION_INVALID_CLIENT)) is False
    assert is_client_credentials_rejected(500, json.dumps(NOTION_INVALID_CLIENT)) is False
    assert is_client_credentials_rejected(400, json.dumps({"error": "invalid_grant"})) is False
    assert is_client_credentials_rejected(401, "a log line mentioning invalid_client") is False
    assert is_client_credentials_rejected(401, "[]") is False
    assert is_client_credentials_rejected(401, "") is False


def test_the_reason_names_the_client_credentials_for_invalid_client():
    reason = permanent_refresh_failure_reason(401, json.dumps(NOTION_INVALID_CLIENT))
    assert reason is not None
    assert "client ID / client secret" in reason
    assert "first" in reason


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [(401, ATLASSIAN_REVOKED), (403, ATLASSIAN_INVALID_RT)],
)
def test_atlassian_dead_refresh_tokens_are_a_dead_grant(status_code, payload):
    reason = permanent_refresh_failure_reason(status_code, json.dumps(payload))
    assert reason is not None
    assert "grant is permanently invalid" in reason


def test_an_invalid_client_carrying_a_dead_token_marker_is_a_dead_grant():
    payload = {"error": "invalid_client", "error_description": "refresh_token is invalid"}
    reason = permanent_refresh_failure_reason(401, json.dumps(payload))
    assert reason is not None
    assert "grant is permanently invalid" in reason


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (401, {"error": "unauthorized_client"}),
        (403, {"error": "access_denied"}),
        (503, {"error": "temporarily_unavailable"}),
        (429, {"error": "rate_limited"}),
    ],
)
def test_other_failures_stay_retryable(status_code, payload):
    assert permanent_refresh_failure_reason(status_code, json.dumps(payload)) is None


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (
            503,
            json.dumps(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "AADSTS50173: The provided grant has expired due to it being revoked.",
                }
            ),
        ),
        (500, "AADSTS9002313: Invalid request. Request is malformed or invalid."),
        (
            429,
            json.dumps({"error": "invalid_request", "error_description": "AADSTS50057: The user account is disabled."}),
        ),
        (
            401,
            json.dumps(
                {
                    "error": "invalid_grant",
                    "error_description": "AADSTS700003: Device object was not found in the tenant directory.",
                }
            ),
        ),
        (502, "<html><body>Bad gateway. Upstream said AADSTS50173.</body></html>"),
    ],
)
def test_an_aadsts_code_outside_a_400_is_not_a_verdict(status_code, body):
    """RFC 6749 errors are 400s: an outage or a throttle quoting an AADSTS code is not a dead grant."""
    assert permanent_refresh_failure_reason(status_code, body) is None


@pytest.mark.parametrize("status_code", [401, 403])
def test_an_invalid_grant_naming_the_refresh_token_is_a_dead_grant_on_any_client_error(status_code):
    """Auth0-based servers (Atlassian) answer a dead refresh token with 403 invalid_grant."""
    body = json.dumps({"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."})
    reason = permanent_refresh_failure_reason(status_code, body)
    assert reason is not None
    assert "grant is permanently invalid" in reason


def test_an_invalid_grant_outside_a_400_needs_a_refresh_token_marker():
    assert permanent_refresh_failure_reason(403, json.dumps({"error": "invalid_grant", "error_description": "Unauthorized"})) is None
    body = json.dumps({"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."})
    assert permanent_refresh_failure_reason(503, body) is None
    assert permanent_refresh_failure_reason(429, body) is None


def test_a_dead_aadsts_code_on_a_400_client_error_is_still_a_dead_grant():
    body = json.dumps({"error": "unauthorized_client", "error_description": "AADSTS50057: The user account is disabled."})
    reason = permanent_refresh_failure_reason(400, body)
    assert reason is not None
    assert "grant is permanently invalid" in reason


# ---------------------------------------------------------------------------
# Scheduled refresh
# ---------------------------------------------------------------------------


async def _scheduled_refresh(integration, response):
    http = AsyncMock()
    http.post = AsyncMock(return_value=response)
    fake_redis = AsyncMock()
    with (
        patch("app.services.token_refresh.decrypt_value", side_effect=lambda v: v),
        patch("app.core.security.decrypt_stored_value", side_effect=lambda v: v),
        patch("app.services.token_refresh.redis_client", new=AsyncMock(return_value=fake_redis)),
    ):
        ok = await _do_refresh_integration(integration, http)
    return ok, fake_redis


async def test_notion_invalid_client_marks_reauthorize_and_says_what_to_update(caplog):
    integration = _integration()

    with caplog.at_level(logging.WARNING, logger="app.services.token_refresh"):
        ok, fake_redis = await _scheduled_refresh(integration, _response(401, NOTION_INVALID_CLIENT))

    assert ok is False
    assert integration.status == "reauthorize"
    fake_redis.set.assert_not_called()
    assert "update the integration's client ID / client secret first" in caplog.text


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [(401, ATLASSIAN_REVOKED), (403, ATLASSIAN_INVALID_RT)],
)
async def test_atlassian_dead_refresh_tokens_mark_reauthorize(status_code, payload):
    integration = _integration(name="Jira", provider="jira", token_url="https://auth.atlassian.com/oauth/token")

    ok, fake_redis = await _scheduled_refresh(integration, _response(status_code, payload))

    assert ok is False
    assert integration.status == "reauthorize"
    fake_redis.set.assert_not_called()


async def test_a_scheduled_refresh_outage_quoting_an_aadsts_code_keeps_the_transient_back_off():
    integration = _integration(name="saasmanager", provider="microsoft")
    body = {"error": "temporarily_unavailable", "error_description": "AADSTS50173: grant revoked"}

    ok, fake_redis = await _scheduled_refresh(integration, _response(503, body))

    assert ok is False
    assert integration.status == "error"
    _, kwargs = fake_redis.set.await_args
    assert kwargs.get("ex") == _TRANSIENT_FAIL_COOLDOWN


async def test_an_unexplained_401_keeps_the_hourly_back_off():
    integration = _integration()

    ok, fake_redis = await _scheduled_refresh(integration, _response(401, {"error": "unauthorized_client"}))

    assert ok is False
    assert integration.status == "error"
    args, kwargs = fake_redis.set.await_args
    assert args[0] == refresh_cooldown_key(integration.id)
    assert kwargs.get("ex") == _AUTH_FAIL_COOLDOWN


# ---------------------------------------------------------------------------
# Chat-time refresh
# ---------------------------------------------------------------------------


class _FakeAsyncClient:
    """Stands in for ``httpx.AsyncClient`` inside ``_do_inline_refresh``."""

    response: httpx.Response | None = None
    error: Exception | None = None
    posts: int = 0
    last_request: tuple[str, dict] | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        type(self).posts += 1
        type(self).last_request = (url, kwargs)
        if type(self).error is not None:
            raise type(self).error
        return type(self).response


def _fake_client(response: httpx.Response | None = None, error: Exception | None = None) -> type[_FakeAsyncClient]:
    return type("_Client", (_FakeAsyncClient,), {"response": response, "error": error, "posts": 0})


class _RefreshSession:
    """The chat-time refresh's own session over the stored ``row``."""

    def __init__(self, row, *, commit_error: Exception | None = None):
        self.row = row
        self.commit_error = commit_error
        self.commits = 0
        self.locked_reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def in_transaction(self) -> bool:
        return False

    async def get(self, _model, _ident, **_kwargs):
        return self.row

    async def refresh(self, _obj, attribute_names=None, with_for_update=None):
        self.locked_reads += bool(with_for_update)

    async def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1


async def _inline_refresh(integration, response=None, *, error=None, cooldown=None, stored=None, commit_error=None):
    """Run a chat-time refresh; by default the stored row matches what the turn read."""
    client = _fake_client(response, error)
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=cooldown)
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    session = _RefreshSession(stored or SimpleNamespace(**vars(integration)), commit_error=commit_error)
    with (
        patch("app.services.token_refresh.redis_client", new=AsyncMock(return_value=fake_redis)),
        patch("app.services.integration_tools.decrypt_value", side_effect=lambda v: v),
        patch("app.core.security.decrypt_stored_value", side_effect=lambda v: v),
        patch("app.services.integration_tools.encrypt_value", side_effect=lambda v: v),
        patch("app.services.integration_tools.httpx.AsyncClient", new=client),
        patch("app.services.token_refresh.acquire_refresh_lock", new=AsyncMock(return_value="lock-token")),
        patch("app.services.token_refresh.release_refresh_lock", new=AsyncMock()),
        patch("app.core.database.async_session", new=lambda: session),
        patch("app.core.database.release_db_connection_before_loop", new=AsyncMock()),
    ):
        token = await integration_tools._try_inline_refresh(db, integration)
    return SimpleNamespace(token=token, client=client, redis=fake_redis, db=db, session=session, stored=session.row)


def _back_off(result) -> tuple[int | None, bool]:
    args, kwargs = result.redis.set.await_args
    assert args[0] == refresh_cooldown_key(_integration().id)
    assert args[1] == token_refresh._cooldown_value("refresh-token")
    return kwargs.get("ex"), kwargs.get("nx", False)


async def test_chat_refresh_honours_the_scheduler_cooldown():
    result = await _inline_refresh(_integration(), _response(200, {"access_token": "new"}), cooldown="1")

    assert result.token is None
    assert result.client.posts == 0


async def test_chat_refresh_honours_a_back_off_learned_from_its_refresh_token():
    cooldown = token_refresh._cooldown_value("refresh-token")
    result = await _inline_refresh(_integration(), _response(200, {"access_token": "new"}), cooldown=cooldown)

    assert result.token is None
    assert result.client.posts == 0


async def test_chat_refresh_ignores_a_back_off_learned_from_a_replaced_refresh_token():
    """A refresh of the previous grant failed after the re-authorization cleared its back-off."""
    cooldown = token_refresh._cooldown_value("refresh-token-before-reauthorization")
    result = await _inline_refresh(_integration(), _response(200, {"access_token": "new"}), cooldown=cooldown)

    assert result.token == "new"
    assert result.client.posts == 1


async def test_chat_refresh_releases_the_db_connection_before_reading_the_cooldown():
    integration = _integration()
    order: list[str] = []
    fake_redis = AsyncMock()

    async def _get(_key):
        order.append("cooldown")
        return "1"

    async def _release(_db):
        order.append("release")

    fake_redis.get = _get
    with (
        patch("app.services.token_refresh.redis_client", new=AsyncMock(return_value=fake_redis)),
        patch("app.core.database.release_db_connection_before_loop", new=_release),
    ):
        token = await integration_tools._try_inline_refresh(MagicMock(), integration)

    assert token is None
    assert order == ["release", "cooldown"]


async def test_chat_refresh_saves_new_tokens_on_its_own_session_after_a_locked_read():
    integration = _integration()

    result = await _inline_refresh(integration, _response(200, {"access_token": "new", "refresh_token": "rt-2"}))

    assert result.token == "new"
    assert (result.stored.access_token_enc, result.stored.refresh_token_enc) == ("new", "rt-2")
    assert (result.session.locked_reads, result.session.commits) == (1, 1)
    # The turn's row and session are left alone.
    assert integration.access_token_enc == "access-token"
    result.db.commit.assert_not_awaited()


async def test_chat_refresh_sends_the_client_credentials_as_stored():
    """The client secret, token URL and scopes were edited after the turn read the row."""
    integration = _integration(provider="hubspot", token_url="https://api.hubapi.com/oauth/v1/token", scopes="crm")
    stored = SimpleNamespace(
        **{
            **vars(integration),
            "client_secret_enc": "rotated-secret",
            "token_url": "https://api.hubapi.com/oauth/v3/token",
            "scopes": "crm oauth",
        }
    )

    result = await _inline_refresh(integration, _response(200, {"access_token": "new"}), stored=stored)

    assert result.token == "new"
    url, kwargs = result.client.last_request
    assert url == "https://api.hubapi.com/oauth/v3/token"
    assert kwargs["data"]["client_secret"] == "rotated-secret"
    assert kwargs["data"]["scope"] == "crm oauth"


async def test_chat_refresh_backs_off_when_the_reauthorize_status_cannot_be_saved():
    """The verdict is written on the refresh's own session; the turn's session is never touched."""
    result = await _inline_refresh(
        _integration(), _response(401, NOTION_INVALID_CLIENT), commit_error=ConnectionError("database down")
    )

    assert result.token is None
    assert result.session.locked_reads == 1
    result.db.rollback.assert_not_awaited()
    result.db.commit.assert_not_awaited()
    assert _back_off(result) == (_AUTH_FAIL_COOLDOWN, False)


async def test_chat_refresh_skips_an_integration_awaiting_reauthorization():
    result = await _inline_refresh(_integration(status="reauthorize"), _response(200, {"access_token": "new"}))

    assert result.token is None
    assert result.client.posts == 0


async def test_chat_refresh_marks_notion_invalid_client_reauthorize(caplog):
    integration = _integration()

    with caplog.at_level(logging.WARNING, logger="app.services.token_refresh"):
        result = await _inline_refresh(integration, _response(401, NOTION_INVALID_CLIENT))

    assert result.token is None
    assert result.client.posts == 1
    assert result.stored.status == "reauthorize"
    assert (result.session.locked_reads, result.session.commits) == (1, 1)
    assert "client ID / client secret" in caplog.text
    # Written on the refresh's own session: the turn's row and session are left alone.
    assert integration.status == "connected"
    result.db.commit.assert_not_awaited()
    result.redis.set.assert_not_called()


async def test_chat_refresh_marks_an_atlassian_dead_token_reauthorize(caplog):
    integration = _integration(name="Jira", provider="jira", token_url="https://auth.atlassian.com/oauth/token")

    with caplog.at_level(logging.WARNING, logger="app.services.token_refresh"):
        result = await _inline_refresh(integration, _response(403, ATLASSIAN_INVALID_RT))

    assert result.token is None
    assert result.stored.status == "reauthorize"
    assert "grant is permanently invalid" in caplog.text
    result.db.commit.assert_not_awaited()


async def test_chat_refresh_starts_the_back_off_on_an_unexplained_auth_failure():
    integration = _integration()

    result = await _inline_refresh(integration, _response(401, {"error": "unauthorized_client"}))

    assert result.token is None
    assert result.stored.status == "connected"
    assert result.session.commits == 0
    result.db.commit.assert_not_called()
    assert _back_off(result) == (_AUTH_FAIL_COOLDOWN, False)


async def test_chat_refresh_backs_off_briefly_after_a_transient_failure():
    """No verdict, but the rest of the turn stays off a failing token endpoint."""
    integration = _integration()

    result = await _inline_refresh(integration, _response(503, {"error": "temporarily_unavailable"}))

    assert result.token is None
    assert result.stored.status == "connected"
    assert result.session.commits == 0
    result.db.commit.assert_not_called()
    assert _back_off(result) == (integration_tools._INLINE_TRANSIENT_FAIL_COOLDOWN, True)


async def test_chat_refresh_backs_off_briefly_after_a_network_error():
    result = await _inline_refresh(_integration(), error=httpx.ConnectTimeout("token endpoint unreachable"))

    assert result.token is None
    assert result.session.commits == 0
    assert _back_off(result) == (integration_tools._INLINE_TRANSIENT_FAIL_COOLDOWN, True)


async def test_chat_refresh_outage_quoting_an_aadsts_code_is_not_a_verdict():
    integration = _integration(name="saasmanager", provider="microsoft")
    body = {"error": "temporarily_unavailable", "error_description": "AADSTS50173: grant revoked"}

    result = await _inline_refresh(integration, _response(503, body))

    assert result.token is None
    assert result.stored.status == "connected"
    assert _back_off(result) == (integration_tools._INLINE_TRANSIENT_FAIL_COOLDOWN, True)


async def test_chat_refresh_still_refreshes_when_the_cooldown_cannot_be_read():
    integration = _integration()
    client = _fake_client(_response(200, {"access_token": "new", "expires_in": 3600}))
    session = _RefreshSession(SimpleNamespace(**vars(integration)))
    with (
        patch("app.services.token_refresh.redis_client", new=AsyncMock(side_effect=ConnectionError("redis down"))),
        patch("app.services.integration_tools.decrypt_value", side_effect=lambda v: v),
        patch("app.core.security.decrypt_stored_value", side_effect=lambda v: v),
        patch("app.services.integration_tools.encrypt_value", side_effect=lambda v: v),
        patch("app.services.integration_tools.httpx.AsyncClient", new=client),
        patch("app.services.token_refresh.acquire_refresh_lock", new=AsyncMock(return_value="lock-token")),
        patch("app.services.token_refresh.release_refresh_lock", new=AsyncMock()),
        patch("app.core.database.async_session", new=lambda: session),
        patch("app.core.database.release_db_connection_before_loop", new=AsyncMock()),
    ):
        token = await integration_tools._try_inline_refresh(MagicMock(), integration)

    assert token == "new"
    assert client.posts == 1
    assert session.commits == 1


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


async def test_mcp_refresh_with_rejected_client_credentials_gets_the_long_back_off():
    """MCP servers have no ``reauthorize`` state: a permanent answer gets the hourly back-off."""
    srv = SimpleNamespace(
        id="7f0e4d3a-1111-4222-8333-944455556666",
        name="Test MCP",
        oauth_status="connected",
        client_id_enc="client-id",
        client_secret_enc="client-secret",
        refresh_token_enc="refresh-token",
        access_token_enc="access-token",
        token_url="https://mcp.example.com/oauth/token",
        scopes=None,
        oauth_resource=None,
        token_expires_at=None,
    )
    http = AsyncMock()
    http.post = AsyncMock(return_value=_response(400, {"error": "invalid_client"}))
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)
    with (
        patch("app.services.token_refresh.decrypt_value", side_effect=lambda v: v),
        patch("app.core.security.decrypt_stored_value", side_effect=lambda v: v),
        patch("app.services.token_refresh.redis_client", new=AsyncMock(return_value=fake_redis)),
    ):
        ok = await token_refresh._do_refresh_mcp(srv, http)

    assert ok is False
    assert srv.oauth_status == "error"
    _, kwargs = fake_redis.set.await_args
    assert kwargs.get("ex") == _AUTH_FAIL_COOLDOWN


# ---------------------------------------------------------------------------
# Re-authorization
# ---------------------------------------------------------------------------


class _CallbackDB:
    """Answers the callback's lookup with ``row`` and counts commits."""

    def __init__(self, row):
        self._row = row
        self.commits = 0

    async def execute(self, _statement):
        row = self._row
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    async def get(self, _model, _ident, **_kwargs):
        """No actor row: the connect audit is skipped."""
        return None

    async def commit(self):
        self.commits += 1


def _loaded_integration(status: str) -> Integration:
    """An integration as a session loads it: no pending change on any attribute."""
    integration = Integration()
    values = dict(
        id=uuid.uuid4(),
        provider="notion",
        status=status,
        extra_config_enc=None,
        access_token_enc="old-access",
        refresh_token_enc="old-refresh",
        scopes=None,
        token_expires_at=None,
    )
    for key, value in values.items():
        set_committed_value(integration, key, value)
    return integration


async def _reauthorize(monkeypatch, integration: Integration) -> AsyncMock:
    async def _exchange(_integration, _code, *, code_verifier=None):
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}

    fake_redis = AsyncMock()
    monkeypatch.setattr(oauth_callbacks, "exchange_code_for_tokens_with_integration", _exchange)
    monkeypatch.setattr(oauth_callbacks, "encrypt_value", lambda value: f"enc:{value}")
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=fake_redis))

    await oauth_callbacks._handle_integration_callback(
        {"integration_id": str(integration.id), "user_id": str(uuid.uuid4()), "provider": "notion"},
        "the-code",
        _CallbackDB(integration),
    )
    return fake_redis


async def test_reauthorizing_an_integration_clears_its_refresh_back_off(monkeypatch):
    """A back-off started against the old grant must not keep both refresh paths off the new one."""
    integration = _loaded_integration("reauthorize")

    fake_redis = await _reauthorize(monkeypatch, integration)

    assert integration.status == "connected"
    fake_redis.delete.assert_awaited_once_with(refresh_cooldown_key(integration.id))


async def test_reauthorizing_writes_connected_even_over_a_row_read_as_connected(monkeypatch):
    """A refresh may record a verdict about the replaced tokens while the code exchange runs.

    The session flushes changed attributes only, so a status that still reads
    ``connected`` would otherwise be left out of the write and keep the verdict.
    """
    integration = _loaded_integration("connected")

    await _reauthorize(monkeypatch, integration)

    assert integration.status == "connected"
    assert inspect(integration).attrs.status.history.has_changes()


def _loaded_mcp_server(oauth_status: str) -> MCPServer:
    """An MCP server as a session loads it: no pending change on any attribute."""
    srv = MCPServer()
    values = dict(
        id=uuid.uuid4(),
        name="Company MCP",
        oauth_status=oauth_status,
        access_token_enc="old-access",
        refresh_token_enc="old-refresh",
        token_expires_at=None,
        scopes=None,
    )
    for key, value in values.items():
        set_committed_value(srv, key, value)
    return srv


async def _reauthorize_mcp(monkeypatch, srv: MCPServer) -> AsyncMock:
    async def _exchange(_srv, _code, *, code_verifier=None):
        return {"access_token": "new-access", "refresh_token": "new-refresh"}

    async def _discover(_db, _srv):
        return None

    fake_redis = AsyncMock()
    monkeypatch.setattr(oauth_callbacks, "exchange_code_for_tokens_with_mcp_server", _exchange)
    monkeypatch.setattr(oauth_callbacks, "_discover_tools_after_authorization", _discover)
    monkeypatch.setattr(oauth_callbacks, "encrypt_value", lambda value: f"enc:{value}")
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=fake_redis))

    await oauth_callbacks._handle_mcp_server_callback(
        {"entity_id": str(srv.id), "user_id": str(uuid.uuid4()), "entity_type": "mcp_server"},
        "the-code",
        _CallbackDB(srv),
    )
    return fake_redis


async def test_reauthorizing_an_mcp_server_clears_its_refresh_back_off(monkeypatch):
    srv = _loaded_mcp_server("error")

    fake_redis = await _reauthorize_mcp(monkeypatch, srv)

    assert srv.oauth_status == "connected"
    fake_redis.delete.assert_awaited_once_with(f"token_refresh_cooldown:mcp:{srv.id}")


async def test_reauthorizing_an_mcp_server_writes_connected_even_over_a_row_read_as_connected(monkeypatch):
    """A refresh may record ``error`` about the replaced tokens while the code exchange runs.

    The session flushes changed attributes only, so a status that still reads
    ``connected`` would otherwise be left out of the write and keep the error.
    """
    srv = _loaded_mcp_server("connected")

    await _reauthorize_mcp(monkeypatch, srv)

    assert srv.oauth_status == "connected"
    assert inspect(srv).attrs.oauth_status.history.has_changes()


async def test_a_redis_outage_does_not_fail_the_reauthorization(monkeypatch, caplog):
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(side_effect=ConnectionError("redis down")))

    with caplog.at_level(logging.WARNING, logger="app.services.token_refresh"):
        await token_refresh.clear_refresh_cooldown(refresh_cooldown_key("abc"))

    assert "Could not clear the refresh cooldown" in caplog.text
