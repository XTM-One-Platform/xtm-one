"""OAuth bundles whose service redeems the authorization code (``oauth_exchange``).

SaaS Manager keeps Entra ID for itself: the platform runs the redirect, the
service redeems the code and issues the JWT its API alone accepts, and
refreshes it at its own endpoint. Covered: the block's validation, the bundle
import rules, the answer mapping (body, cookie, expiry, every unusable token),
the callback, both refresh paths and the lookups that feed them - plus the
``fullmatch`` header checks of the declarative executor and the Test probe.
"""

from __future__ import annotations

import base64
import json
import socket
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import app.api.oauth_callbacks as callbacks
from app.api.v1 import integrations as integrations_api
from app.core.config import settings
from app.core.security import decrypt_value, encrypt_value
from app.integrations import service_token_exchange
from app.integrations.service_token_exchange import (
    read_service_token_answer,
    redeem_code,
    validate_oauth_exchange,
)
from app.integrations.token_endpoint import TokenEndpointRefusedError
from app.models.custom_integration_definition import CustomIntegrationDefinition
from app.models.integration import Integration
from app.models.user import User
from app.services import integration_tools, token_refresh
from app.services.declarative_http_executor import validate_execution_spec
from app.services.integration_bundle_manager import (
    build_integration_bundle_zip,
    parse_integration_bundle_zip,
)
from app.services.token_refresh import (
    _TRANSIENT_FAIL_COOLDOWN,
    MalformedTokenAnswerError,
    refresh_cooldown_key,
)
from tests.integration_credential_fakes import OWNER_ID, FakeSession, make_integration

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_BASE = "https://sm.example"
_CODE_PATH = "/api/auth/social/filigranssooidc/"
_REFRESH_PATH = "/api/auth/token/refresh/"
_SPEC = {
    "code_path": _CODE_PATH,
    "code_field": "code",
    "redirect_uri_field": "callback_url",
    "access_field": "access",
    "refresh_field": "refresh",
    "refresh_cookie_contains": "refresh",
    "refresh_path": _REFRESH_PATH,
    "refresh_request_field": "refresh",
}
_PROVIDER = "custom:saas-manager"
_SCOPES = "openid profile email offline_access User.Read"
_NOW = 1_000_000.0


def _jwt(exp: float) -> str:
    def segment(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{segment({'alg': 'HS256'})}.{segment({'exp': exp})}.signature"


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the URL's host: answer for DNS, never a real resolver."""
    from app.integrations import token_endpoint

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


@pytest.fixture
def _frozen_clock(monkeypatch):
    """Freeze the clock ``oauth.jwt_expiry_seconds`` reads the ``exp`` against."""
    from datetime import datetime

    from app.integrations import oauth

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(_NOW, tz=tz)

    monkeypatch.setattr(oauth, "datetime", _FrozenDatetime)


class _Service:
    """A stand-in SaaS Manager: records each request, answers with *answer*."""

    def __init__(self, answer):
        self.answer = answer
        self.requests: list[tuple[str, str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _served_by(service: _Service):
    """Every client the platform opens answers through *service*."""
    transport = httpx.MockTransport(service)

    def _client(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, transport=transport, **kwargs)

    return patch("app.integrations.service_token_exchange.httpx.AsyncClient", _client)


def _answer(status: int = 200, *, path: str = _CODE_PATH, **kwargs) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", _BASE + path), **kwargs)


# ── Validation ───────────────────────────────────────────────────────────────


def test_a_complete_block_is_valid():
    assert validate_oauth_exchange(_SPEC) == []


def test_the_optional_fields_may_be_left_out():
    spec = {k: v for k, v in _SPEC.items() if k not in ("refresh_field", "refresh_cookie_contains")}

    assert validate_oauth_exchange(spec) == []


@pytest.mark.parametrize(
    "field",
    ["code_path", "code_field", "redirect_uri_field", "access_field", "refresh_path", "refresh_request_field"],
)
@pytest.mark.parametrize("value", [None, "", "  ", 3], ids=["missing", "empty", "blank", "int"])
def test_every_required_field_is_a_non_empty_string(field, value):
    spec = {**_SPEC, field: value}
    if value is None:
        del spec[field]

    assert validate_oauth_exchange(spec) == [f"oauth_exchange.{field} must be a non-empty string."]


@pytest.mark.parametrize("field", ["refresh_field", "refresh_cookie_contains"])
def test_an_optional_field_set_is_a_non_empty_string(field):
    assert validate_oauth_exchange({**_SPEC, field: ""}) == [
        f"oauth_exchange.{field} must be a non-empty string when set."
    ]


@pytest.mark.parametrize("field", ["code_path", "refresh_path"])
@pytest.mark.parametrize(
    "path",
    [
        pytest.param("api/auth/", id="relative"),
        pytest.param("https://evil.example/x", id="absolute_url"),
        pytest.param("/api/auth/?next=x", id="query"),
        pytest.param("/api/auth/#x", id="fragment"),
        pytest.param("/api/{id}/", id="placeholder"),
        pytest.param("/api/../admin/", id="dot_dot"),
        pytest.param("/api/a b/", id="space"),
        pytest.param("/api/auth/\n", id="trailing_newline"),
    ],
)
def test_each_path_names_one_endpoint_under_the_base_url(field, path):
    assert validate_oauth_exchange({**_SPEC, field: path}) == [
        f"oauth_exchange.{field} must be an absolute path ('/...') without a "
        "query, fragment, placeholder or '..' segment."
    ]


def test_an_unknown_key_is_refused():
    assert validate_oauth_exchange({**_SPEC, "token_url": "https://x"}) == [
        "oauth_exchange has unknown keys: ['token_url']."
    ]


def test_the_block_is_an_object():
    assert validate_oauth_exchange(["code_path"]) == ["oauth_exchange must be an object."]


# ── Bundle import rules ──────────────────────────────────────────────────────


def _metadata(**overrides) -> dict:
    metadata = {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "SaaS Manager",
        "slug": "saas-manager",
        "description": "Instances.",
        "author": "QA",
        "provider": "saas-manager",
        "auth_type": "oauth",
        "settings": [],
        "oauth_exchange": _SPEC,
        "exported_at": "2026-10-02T00:00:00Z",
    }
    metadata.update(overrides)
    return metadata


def test_a_bundle_keeps_its_oauth_exchange():
    parsed = parse_integration_bundle_zip(build_integration_bundle_zip(_metadata()))

    assert parsed.metadata_raw["oauth_exchange"] == _SPEC


def test_oauth_exchange_needs_an_oauth_bundle():
    with pytest.raises(ValueError) as excinfo:
        build_integration_bundle_zip(_metadata(auth_type="api_key"))

    assert str(excinfo.value) == "metadata.json field 'oauth_exchange' requires auth_type 'oauth'."


def test_an_invalid_oauth_exchange_refuses_the_bundle():
    with pytest.raises(ValueError) as excinfo:
        build_integration_bundle_zip(_metadata(oauth_exchange={**_SPEC, "code_path": "/x?y"}))

    assert str(excinfo.value) == (
        "oauth_exchange.code_path must be an absolute path ('/...') without a "
        "query, fragment, placeholder or '..' segment."
    )


def test_a_bundle_test_connection_auth_follows_the_tool_auth_rules():
    test_connection = {
        "mode": "http",
        "path": "/api/region/",
        "method": "GET",
        "auth": {"type": "header", "header": "X Key"},
    }

    with pytest.raises(ValueError) as excinfo:
        build_integration_bundle_zip(_metadata(test_connection=test_connection))

    assert str(excinfo.value) == (
        "test_connection.auth.header must be a valid HTTP header name "
        "(RFC 9110 token characters only)."
    )


def test_a_valid_test_connection_auth_is_kept():
    test_connection = {"mode": "http", "path": "/api/region/", "method": "GET", "auth": {"type": "bearer"}}

    parsed = parse_integration_bundle_zip(
        build_integration_bundle_zip(_metadata(test_connection=test_connection))
    )

    assert parsed.metadata_raw["test_connection"]["auth"] == {"type": "bearer"}


# ── Answer mapping ───────────────────────────────────────────────────────────


def test_body_tokens_and_the_jwt_expiry_map_to_the_oauth_shape(_frozen_clock):
    access = _jwt(_NOW + 600)

    tokens = read_service_token_answer(_SPEC, _answer(json={"access": access, "refresh": "r2"}))

    assert tokens == {"access_token": access, "refresh_token": "r2", "expires_in": 600}


def test_a_refresh_token_sent_as_a_cookie_is_read(_frozen_clock):
    resp = _answer(
        json={"access": "opaque-access"},
        headers={"set-cookie": "auth-refresh-token=r3; Path=/; HttpOnly"},
    )

    assert read_service_token_answer(_SPEC, resp) == {
        "access_token": "opaque-access",
        "refresh_token": "r3",
    }


def test_the_body_refresh_token_wins_over_a_cookie():
    resp = _answer(
        json={"access": "a", "refresh": "from-body"},
        headers={"set-cookie": "auth-refresh-token=from-cookie; Path=/"},
    )

    assert read_service_token_answer(_SPEC, resp)["refresh_token"] == "from-body"


def test_without_refresh_fields_only_the_access_token_is_read(_frozen_clock):
    spec = {k: v for k, v in _SPEC.items() if k not in ("refresh_field", "refresh_cookie_contains")}
    access = _jwt(_NOW + 60)

    assert read_service_token_answer(spec, _answer(json={"access": access, "refresh": "r"})) == {
        "access_token": access,
        "expires_in": 60,
    }


@pytest.mark.parametrize("exp", [_NOW, _NOW - 5], ids=["expiring_now", "expired"])
def test_an_expired_access_token_is_a_malformed_answer(exp, _frozen_clock):
    with pytest.raises(MalformedTokenAnswerError) as excinfo:
        read_service_token_answer(_SPEC, _answer(json={"access": _jwt(exp), "refresh": "r"}))

    assert str(excinfo.value) == "the service answered with an expired access token"


def test_the_builder_refuses_an_invalid_oauth_exchange_before_writing_the_zip(monkeypatch):
    from app.services import integration_bundle_manager

    zipped = MagicMock(side_effect=AssertionError("the ZIP must not be written"))
    monkeypatch.setattr(integration_bundle_manager.zipfile, "ZipFile", zipped)

    with pytest.raises(ValueError, match="requires auth_type 'oauth'"):
        build_integration_bundle_zip(_metadata(auth_type="api_key"))

    zipped.assert_not_called()


def test_a_nested_access_field_is_read():
    spec = {**_SPEC, "access_field": "tokens.access"}

    assert read_service_token_answer(spec, _answer(json={"tokens": {"access": "a"}})) == {
        "access_token": "a"
    }


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(_answer(content=b"<html>", headers={"content-type": "text/html"}), id="not_json"),
        pytest.param(_answer(json=["access"]), id="json_list"),
        pytest.param(_answer(json={"key": "abc"}), id="access_missing"),
        pytest.param(_answer(json={"access": 123}), id="access_not_a_string"),
        pytest.param(_answer(json={"access": ""}), id="access_empty"),
        pytest.param(_answer(json={"access": "a b"}), id="access_space"),
        pytest.param(_answer(json={"access": "abc\n"}), id="access_trailing_newline"),
        pytest.param(_answer(json={"access": "a\r\nX-Injected: 1"}), id="access_crlf"),
        pytest.param(_answer(json={"access": "jéton"}), id="access_non_ascii"),
        pytest.param(_answer(json={"access": "a" * 8193}), id="access_too_long"),
        pytest.param(_answer(json={"access": "a", "refresh": 7}), id="refresh_not_a_string"),
        pytest.param(_answer(json={"access": "a", "refresh": "r\r\n"}), id="refresh_crlf"),
    ],
)
def test_an_unusable_answer_is_a_malformed_token_answer(answer):
    with pytest.raises(MalformedTokenAnswerError):
        read_service_token_answer(_SPEC, answer)


# ── Redeeming the code ───────────────────────────────────────────────────────


def _sm_integration(**overrides) -> Integration:
    values = dict(
        provider=_PROVIDER,
        name="SaaS Manager",
        status="configured",
        access_token_enc=None,
        client_id_enc=encrypt_value("entra-client-id"),
        client_secret_enc=encrypt_value("entra-secret"),
        token_url="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        scopes=_SCOPES,
        extra_config_enc=encrypt_value(json.dumps({"base_url": _BASE + "/"})),
    )
    values.update(overrides)
    return make_integration(**values)


@pytest.mark.asyncio
async def test_the_code_is_handed_to_the_service(_frozen_clock):
    access = _jwt(_NOW + 300)
    service = _Service(_answer(json={"access": access, "refresh": "r1"}))

    with _served_by(service):
        tokens = await redeem_code(
            _SPEC, _sm_integration(), "the-code", redirect_uri="https://xtm.example/api/oauth/callback"
        )

    assert service.requests == [
        ("POST", _CODE_PATH, {"code": "the-code", "callback_url": "https://xtm.example/api/oauth/callback"})
    ]
    assert tokens == {"access_token": access, "refresh_token": "r1", "expires_in": 300}


@pytest.mark.asyncio
async def test_a_refused_code_raises():
    service = _Service(_answer(400, json={"detail": "invalid code"}))

    with _served_by(service), pytest.raises(httpx.HTTPStatusError):
        await redeem_code(_SPEC, _sm_integration(), "the-code", redirect_uri="https://x/cb")

    assert len(service.requests) == 1


@pytest.mark.asyncio
async def test_no_base_url_sends_nothing():
    service = _Service(_answer(json={"access": "a"}))

    with _served_by(service), pytest.raises(ValueError):
        await redeem_code(_SPEC, _sm_integration(extra_config_enc=None), "c", redirect_uri="https://x/cb")

    assert service.requests == []


@pytest.mark.asyncio
async def test_a_metadata_endpoint_base_url_is_refused_before_the_code_leaves():
    service = _Service(_answer(json={"access": "a"}))
    integration = _sm_integration(
        extra_config_enc=encrypt_value(json.dumps({"base_url": "http://169.254.169.254"}))
    )

    with _served_by(service), pytest.raises(TokenEndpointRefusedError):
        await redeem_code(_SPEC, integration, "c", redirect_uri="https://x/cb")

    assert service.requests == []


# ── The callback ─────────────────────────────────────────────────────────────


def _owner() -> User:
    return User(
        id=OWNER_ID,
        email="owner@example.com",
        full_name="Owner",
        role="admin",
        oauth_share_dismissed_integration_ids=[],
    )


class _CallbackSession(FakeSession):
    def __init__(self, integration: Integration, metadata: dict | None):
        rows: dict = {Integration: [integration]}
        if metadata is not None:
            rows[CustomIntegrationDefinition] = [
                CustomIntegrationDefinition(provider_key=_PROVIDER, metadata_json=metadata)
            ]
        super().__init__(rows)
        self.user = _owner()

    async def get(self, _model, key):
        return self.user if key == self.user.id else None


@pytest.fixture
def _callback(monkeypatch):
    async def _audit(*_args, **_kwargs):
        return None

    standard_exchange = AsyncMock(return_value={"access_token": "entra-token", "refresh_token": "entra-refresh"})
    monkeypatch.setattr(callbacks, "log_audit", _audit)
    monkeypatch.setattr(callbacks, "exchange_code_for_tokens_with_integration", standard_exchange)

    async def _run(integration, service, metadata):
        db = _CallbackSession(integration, metadata)
        state = {"integration_id": str(integration.id), "user_id": str(OWNER_ID), "provider": _PROVIDER}
        with _served_by(service):
            response = await callbacks._handle_integration_callback(state, "the-code", db)
        return SimpleNamespace(response=response, standard_exchange=standard_exchange)

    return _run


@pytest.mark.asyncio
async def test_the_callback_stores_the_service_tokens(_callback, _frozen_clock):
    access = _jwt(_NOW + 300)
    service = _Service(_answer(json={"access": access, "refresh": "r1"}))
    integration = _sm_integration()

    result = await _callback(integration, service, _metadata())

    assert service.requests == [
        ("POST", _CODE_PATH, {"code": "the-code", "callback_url": f"{settings.base_url}/api/oauth/callback"})
    ]
    result.standard_exchange.assert_not_awaited()
    assert decrypt_value(integration.access_token_enc) == access
    assert decrypt_value(integration.refresh_token_enc) == "r1"
    assert integration.status == "connected"
    assert integration.scopes == _SCOPES
    assert integration.token_expires_at is not None
    assert "connected=" in result.response.headers["location"]


@pytest.mark.asyncio
async def test_a_refused_code_redirects_with_the_exchange_error(_callback):
    service = _Service(_answer(400, json={"detail": "invalid code"}))
    integration = _sm_integration()

    result = await _callback(integration, service, _metadata())

    assert "error=token_exchange_failed" in result.response.headers["location"]
    assert integration.access_token_enc is None
    result.standard_exchange.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_custom_bundle_without_oauth_exchange_keeps_the_standard_exchange_and_its_scopes(_callback):
    service = _Service(_answer(json={"access": "never"}))
    integration = _sm_integration()

    result = await _callback(integration, service, _metadata(oauth_exchange=None))

    result.standard_exchange.assert_awaited_once()
    assert service.requests == []
    assert decrypt_value(integration.access_token_enc) == "entra-token"
    assert integration.scopes == _SCOPES


@pytest.mark.asyncio
async def test_an_invalid_persisted_oauth_exchange_falls_back_to_the_standard_exchange(_callback, caplog):
    service = _Service(_answer(json={"access": "never"}))

    with caplog.at_level("WARNING", logger=service_token_exchange.__name__):
        result = await _callback(_sm_integration(), service, _metadata(oauth_exchange={"code_path": "/x"}))

    result.standard_exchange.assert_awaited_once()
    assert service.requests == []
    assert "Ignoring the invalid oauth_exchange of custom provider 'custom:saas-manager'" in caplog.text


# ── Scheduled refresh ────────────────────────────────────────────────────────


def _refresh_row(**overrides) -> SimpleNamespace:
    values = dict(
        id="5d1f0f3c-6a3e-4c55-9a43-1f2b3c4d5e6f",
        name="SaaS Manager",
        provider=_PROVIDER,
        status="connected",
        client_id_enc="entra-client-id",
        client_secret_enc="entra-secret",
        refresh_token_enc="r1",
        access_token_enc="old-access",
        token_url="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        scopes=_SCOPES,
        extra_config_enc=json.dumps({"base_url": _BASE}),
        token_expires_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


async def _scheduled_refresh(row, service: _Service):
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)
    async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(service)) as http:
        with (
            patch("app.services.token_refresh.decrypt_value", side_effect=lambda v: v),
            patch("app.services.token_refresh.encrypt_value", side_effect=lambda v: v),
            patch("app.core.security.decrypt_json", side_effect=lambda v: json.loads(v) if v else None),
            patch("app.services.token_refresh.redis_client", new=AsyncMock(return_value=fake_redis)),
        ):
            ok = await token_refresh._do_refresh_integration(row, http, service_exchange=_SPEC)
    return ok, fake_redis


@pytest.mark.asyncio
async def test_the_scheduled_refresh_asks_the_service(_frozen_clock):
    access = _jwt(_NOW + 300)
    service = _Service(_answer(json={"access": access, "refresh": "r2"}, path=_REFRESH_PATH))
    row = _refresh_row(status="error")

    ok, _redis = await _scheduled_refresh(row, service)

    assert ok is True
    assert service.requests == [("POST", _REFRESH_PATH, {"refresh": "r1"})]
    assert row.access_token_enc == access
    assert row.refresh_token_enc == "r2"
    assert row.status == "connected"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_scheduled_refresh_needs_reauthorization(status):
    # Only the refresh token was presented: SimpleJWT's token_not_valid, which
    # matches no OAuth marker, can only be that token refused.
    service = _Service(
        _answer(status, json={"detail": "Token is invalid or expired", "code": "token_not_valid"}, path=_REFRESH_PATH)
    )
    row = _refresh_row()

    ok, fake_redis = await _scheduled_refresh(row, service)

    assert ok is False
    assert row.status == "reauthorize"
    assert row.access_token_enc == "old-access"
    fake_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_a_failing_scheduled_service_refresh_is_not_a_dead_grant():
    service = _Service(_answer(503, json={"detail": "maintenance"}, path=_REFRESH_PATH))
    row = _refresh_row()

    ok, fake_redis = await _scheduled_refresh(row, service)

    assert ok is False
    assert row.status == "error"
    args, kwargs = fake_redis.set.await_args
    assert args[0] == refresh_cooldown_key(row.id)
    assert kwargs["ex"] == _TRANSIENT_FAIL_COOLDOWN


@pytest.mark.asyncio
async def test_a_scheduled_refresh_without_base_url_sends_nothing():
    service = _Service(_answer(json={"access": "a"}, path=_REFRESH_PATH))
    row = _refresh_row(extra_config_enc=None)

    ok, fake_redis = await _scheduled_refresh(row, service)

    assert ok is False
    assert service.requests == []
    assert row.access_token_enc == "old-access"
    args, kwargs = fake_redis.set.await_args
    assert args[0] == refresh_cooldown_key(row.id)
    assert kwargs["ex"] == _TRANSIENT_FAIL_COOLDOWN


@pytest.mark.asyncio
async def test_a_scheduled_refresh_answer_without_token_is_not_recorded():
    service = _Service(_answer(json={"detail": "ok"}, path=_REFRESH_PATH))
    row = _refresh_row()

    ok, _redis = await _scheduled_refresh(row, service)

    assert ok is False
    assert row.access_token_enc == "old-access"
    assert row.status == "error"


class _StoredSession(FakeSession):
    """The refresh's own session over one stored row (and its bundle)."""

    def __init__(self, row: Integration, metadata: dict | None):
        rows: dict = {Integration: [row]}
        if metadata is not None:
            rows[CustomIntegrationDefinition] = [
                CustomIntegrationDefinition(provider_key=row.provider, metadata_json=metadata)
            ]
        super().__init__(rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _due_row(provider: str = _PROVIDER) -> Integration:
    return make_integration(
        provider=provider,
        refresh_token_enc="r1",
        client_id_enc="cid",
        token_url="https://login.microsoftonline.com/t/oauth2/v2.0/token",
    )


@pytest.mark.parametrize(
    ("provider", "metadata", "expected"),
    [
        pytest.param(_PROVIDER, {"oauth_exchange": _SPEC}, _SPEC, id="bundle_with_exchange"),
        pytest.param(_PROVIDER, {"tools": []}, None, id="bundle_without_exchange"),
        pytest.param("microsoft", None, None, id="native_provider"),
    ],
)
@pytest.mark.asyncio
async def test_the_sweep_hands_the_bundle_exchange_to_the_refresh(provider, metadata, expected):
    row = _due_row(provider)
    spy = AsyncMock(return_value=True)

    with (
        patch("app.core.database.async_session", new=lambda: _StoredSession(row, metadata)),
        patch("app.core.database.release_db_connection_before_loop", new=AsyncMock()),
        patch("app.services.relays.integration_egress_route", new=AsyncMock(return_value=None)),
        patch.object(token_refresh, "_do_refresh_integration", spy),
    ):
        assert await token_refresh._refresh_stored_integration(row.id, MagicMock()) is True

    assert spy.await_args.kwargs["service_exchange"] == expected


# ── Inline (chat-time) refresh ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_inline_refresh_asks_the_service(_frozen_clock):
    access = _jwt(_NOW + 300)
    service = _Service(_answer(json={"access": access}, path=_REFRESH_PATH))
    row = _refresh_row()
    db = MagicMock()
    db.commit = AsyncMock()
    transport = httpx.MockTransport(service)

    def _client(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(*args, transport=transport, **kwargs)

    with (
        patch("app.services.integration_tools.decrypt_value", side_effect=lambda v: v),
        patch("app.services.integration_tools.encrypt_value", side_effect=lambda v: v),
        patch("app.core.security.decrypt_json", side_effect=lambda v: json.loads(v) if v else None),
        patch("app.services.integration_tools.httpx.AsyncClient", _client),
    ):
        token = await integration_tools._do_inline_refresh(
            db, row, AsyncMock(return_value=True), service_exchange=_SPEC
        )

    assert token == access
    assert service.requests == [("POST", _REFRESH_PATH, {"refresh": "r1"})]
    assert row.access_token_enc == access
    # No refresh token in the answer: the stored one stays.
    assert row.refresh_token_enc == "r1"
    db.commit.assert_awaited_once()


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        pytest.param({"oauth_exchange": _SPEC}, _SPEC, id="bundle_with_exchange"),
        pytest.param(None, None, id="no_bundle"),
    ],
)
@pytest.mark.asyncio
async def test_the_inline_refresh_reads_the_bundle_exchange(metadata, expected):
    row = _due_row()
    spy = AsyncMock(return_value="new")

    with (
        patch("app.core.database.async_session", new=lambda: _StoredSession(row, metadata)),
        patch("app.core.database.release_db_connection_before_loop", new=AsyncMock()),
        patch.object(integration_tools, "_do_inline_refresh", spy),
    ):
        token = await integration_tools._refresh_inline_locked(row, token_refresh.stored_tokens(row))

    assert token == "new"
    assert spy.await_args.kwargs["service_exchange"] == expected


# ── fullmatch header checks ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "error"),
    [
        pytest.param(
            {"auth": {"type": "header", "header": "X-Key\n"}},
            "tools[0].execution.auth.header must be a valid HTTP header name "
            "(RFC 9110 token characters only).",
            id="header_name",
        ),
        pytest.param(
            {"auth": {"type": "header", "prefix": "Token\n"}},
            "tools[0].execution.auth.prefix must be a printable-ASCII string "
            "(no control characters).",
            id="header_prefix",
        ),
        pytest.param(
            {"method": "POST", "content_type": "application/json\n"},
            "tools[0].execution.content_type must be a printable-ASCII JSON "
            "media type (no control characters).",
            id="content_type",
        ),
    ],
)
def test_a_trailing_newline_never_passes_a_header_check(spec, error):
    assert validate_execution_spec({"method": "GET", "path": "/x", **spec}, tool_index=0) == [error]


class _ProbeDb:
    def __init__(self, test_connection: dict):
        self._definition = SimpleNamespace(metadata_json={"test_connection": test_connection})

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._definition)

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_the_probe_refuses_a_persisted_header_with_a_trailing_newline():
    integration = Integration(
        id=uuid.uuid4(),
        name="Custom",
        provider="custom:demo",
        auth_type="api_key",
        status="connected",
        api_key_enc=encrypt_value("key"),
        extra_config_enc=encrypt_value(json.dumps({"base_url": _BASE})),
    )
    db = _ProbeDb(
        {"mode": "http", "path": "/health", "method": "GET", "auth": {"type": "header", "header": "X-Key\n"}}
    )
    no_request = MagicMock(side_effect=AssertionError("the probe must not send a request"))

    with patch("httpx.AsyncClient", no_request):
        result = await integrations_api._test_integration(
            integration, SimpleNamespace(id=uuid.uuid4(), is_admin=True), [], db
        )

    assert result == {
        "status": "error",
        "message": (
            "Invalid test_connection.auth in the bundle: the header name must use "
            "RFC 9110 token characters and the prefix must be printable ASCII."
        ),
    }
    no_request.assert_not_called()


# ── The bundle's OAuth endpoints (``oauth`` block) ───────────────────────────

_OAUTH = {
    "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    "scopes": _SCOPES,
}


def test_a_valid_oauth_block_is_kept():
    parsed = parse_integration_bundle_zip(build_integration_bundle_zip(_metadata(oauth=_OAUTH)))

    assert parsed.metadata_raw["oauth"] == _OAUTH


def test_the_oauth_block_needs_an_oauth_bundle():
    with pytest.raises(ValueError) as excinfo:
        build_integration_bundle_zip(_metadata(auth_type="api_key", oauth_exchange=None, oauth=_OAUTH))

    assert str(excinfo.value) == "metadata.json field 'oauth' requires auth_type 'oauth'."


@pytest.mark.parametrize("key", ["authorize_url", "token_url"])
@pytest.mark.parametrize(
    "url",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param(3, id="not_a_string"),
        pytest.param("/oauth2/authorize", id="relative"),
        pytest.param("ftp://login.example/authorize", id="ftp"),
        pytest.param("javascript:alert(1)", id="javascript"),
        pytest.param("https:///authorize", id="no_host"),
        pytest.param("https://login.example/a b", id="space"),
        pytest.param("https://login.example/a\n", id="trailing_newline"),
        pytest.param("https://[::1/authorize", id="unbalanced_ipv6"),
    ],
)
def test_each_oauth_url_is_an_absolute_http_url(key, url):
    from app.services.integration_bundle_manager import oauth_endpoint_errors

    oauth = {**_OAUTH, key: url}
    if url is None:
        del oauth[key]

    assert oauth_endpoint_errors(oauth) == [f"oauth.{key} must be an absolute http(s) URL."]


@pytest.mark.parametrize(
    ("oauth", "errors"),
    [
        pytest.param({**_OAUTH, "scopes": 7}, ["oauth.scopes must be a printable string."], id="scopes_int"),
        pytest.param({**_OAUTH, "scopes": "a\nb"}, ["oauth.scopes must be a printable string."], id="scopes_newline"),
        pytest.param({**_OAUTH, "prompt": "consent"}, ["oauth has unknown keys: ['prompt']."], id="unknown_key"),
        pytest.param("https://x", ["oauth must be an object."], id="not_an_object"),
    ],
)
def test_the_rest_of_the_oauth_block_is_checked(oauth, errors):
    from app.services.integration_bundle_manager import oauth_endpoint_errors

    assert oauth_endpoint_errors(oauth) == errors


# ── Client secret vault on the service path ──────────────────────────

_INVALID_CLIENT = {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret"}


@pytest.mark.asyncio
async def test_the_scheduled_service_refresh_never_reads_or_retries_the_client_secret():
    service = _Service(_answer(401, json=_INVALID_CLIENT, path=_REFRESH_PATH))
    reveal = AsyncMock(return_value="vault-secret")
    rotated = AsyncMock(return_value="rotated-secret")

    with (
        patch("app.services.vault.reveal_field", reveal),
        patch.object(token_refresh, "rotated_client_secret", rotated),
    ):
        ok, _redis = await _scheduled_refresh(_refresh_row(), service)

    assert ok is False
    reveal.assert_not_awaited()
    rotated.assert_not_awaited()
    assert service.requests == [("POST", _REFRESH_PATH, {"refresh": "r1"})]


@pytest.mark.asyncio
async def test_the_inline_service_refresh_never_reads_or_retries_the_client_secret():
    service = _Service(_answer(401, json=_INVALID_CLIENT, path=_REFRESH_PATH))
    reveal = AsyncMock(return_value="vault-secret")
    rotated = AsyncMock(return_value="rotated-secret")
    db = MagicMock()
    db.commit = AsyncMock()
    transport = httpx.MockTransport(service)

    def _client(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(*args, transport=transport, **kwargs)

    with (
        patch("app.services.integration_tools.decrypt_value", side_effect=lambda v: v),
        patch("app.core.security.decrypt_json", side_effect=lambda v: json.loads(v) if v else None),
        patch("app.services.integration_tools.httpx.AsyncClient", _client),
        patch.object(integration_tools, "reveal_field", reveal),
        patch.object(token_refresh, "rotated_client_secret", rotated),
        patch.object(integration_tools, "_record_inline_refresh_failure", AsyncMock(return_value=None)),
    ):
        token = await integration_tools._do_inline_refresh(
            db, _refresh_row(), AsyncMock(return_value=True), service_exchange=_SPEC
        )

    assert token is None
    reveal.assert_not_awaited()
    rotated.assert_not_awaited()
    assert service.requests == [("POST", _REFRESH_PATH, {"refresh": "r1"})]


# ── Review follow-ups ────────────────────────────────────────────────


def _inline_patches(service: _Service, **extra):
    transport = httpx.MockTransport(service)

    def _client(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(*args, transport=transport, **kwargs)

    patches = [
        patch("app.services.integration_tools.decrypt_value", side_effect=lambda v: v),
        patch("app.services.integration_tools.encrypt_value", side_effect=lambda v: v),
        patch(
            "app.core.security.decrypt_json",
            side_effect=lambda v: json.loads(v) if v else None,
        ),
        patch("app.services.integration_tools.httpx.AsyncClient", _client),
    ]
    patches += [patch.object(integration_tools, name, value) for name, value in extra.items()]
    return patches


async def _inline(service: _Service, row, **extra):
    from contextlib import ExitStack

    db = MagicMock()
    db.commit = AsyncMock()
    with ExitStack() as stack:
        for p in _inline_patches(service, **extra):
            stack.enter_context(p)
        return await integration_tools._do_inline_refresh(
            db, row, AsyncMock(return_value=True), service_exchange=_SPEC
        )


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_a_refused_inline_service_refresh_is_recorded_as_a_dead_grant(status):
    service = _Service(_answer(status, json={"code": "token_not_valid"}, path=_REFRESH_PATH))
    record = AsyncMock(return_value=None)

    token = await _inline(service, _refresh_row(), _record_inline_refresh_failure=record)

    assert token is None
    assert record.await_args.args[4] == token_refresh._DEAD_GRANT_REASON
    assert record.await_args.kwargs == {"auth_failure": True}


@pytest.mark.asyncio
async def test_an_inline_service_refresh_without_base_url_backs_off_briefly():
    service = _Service(_answer(json={"access": "a"}, path=_REFRESH_PATH))
    back_off = AsyncMock()

    token = await _inline(service, _refresh_row(extra_config_enc=None), _start_inline_back_off=back_off)

    assert token is None
    assert service.requests == []
    back_off.assert_awaited_once_with(
        _refresh_row().id,
        "r1",
        integration_tools._INLINE_TRANSIENT_FAIL_COOLDOWN,
        only_if_absent=True,
    )


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        pytest.param(401, "{}", token_refresh._DEAD_GRANT_REASON, id="401"),
        pytest.param(403, "{}", token_refresh._DEAD_GRANT_REASON, id="403"),
        pytest.param(400, json.dumps({"error": "invalid_client"}), None, id="no_client_was_sent"),
        pytest.param(
            400,
            json.dumps({"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."}),
            token_refresh._DEAD_GRANT_REASON,
            id="invalid_grant",
        ),
        pytest.param(503, "{}", None, id="outage"),
    ],
)
def test_the_service_refresh_verdict(status, body, expected):
    assert token_refresh.service_refresh_failure_reason(status, body) == expected


def _jwt_payload(payload: str) -> str:
    segment = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJIUzI1NiJ9.{segment}.signature"


@pytest.mark.parametrize("exp", ["Infinity", "-Infinity", "NaN", '"soon"', "null"])
def test_a_non_finite_or_unreadable_exp_leaves_the_expiry_to_the_platform(exp):
    access = _jwt_payload(f'{{"exp": {exp}}}')

    assert read_service_token_answer(_SPEC, _answer(json={"access": access})) == {"access_token": access}


def test_a_jwt_lifetime_is_capped():
    from app.integrations.oauth import MAX_TOKEN_LIFETIME_SECONDS

    access = _jwt(4_102_444_800)  # 2100

    assert read_service_token_answer(_SPEC, _answer(json={"access": access}))["expires_in"] == (
        MAX_TOKEN_LIFETIME_SECONDS
    )


@pytest.mark.parametrize(
    "extra_config_enc",
    [pytest.param("not-a-fernet-token", id="corrupt"), pytest.param(None, id="missing")],
)
async def test_an_unreadable_extra_config_means_no_base_url(extra_config_enc):
    with pytest.raises(ValueError, match="no base_url"):
        await service_token_exchange.service_base_url(SimpleNamespace(extra_config_enc=extra_config_enc))


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/%2e%2e/admin/", id="encoded_dot_dot"),
        pytest.param("/api/%2E%2E/admin/", id="encoded_dot_dot_upper"),
        pytest.param("/api/.%2e/admin/", id="half_encoded"),
    ],
)
def test_an_encoded_dot_dot_segment_is_refused(path):
    assert validate_oauth_exchange({**_SPEC, "refresh_path": path}) == [
        "oauth_exchange.refresh_path must be an absolute path ('/...') without a "
        "query, fragment, placeholder or '..' segment."
    ]


def test_the_code_and_the_redirect_uri_take_two_fields():
    assert validate_oauth_exchange({**_SPEC, "redirect_uri_field": "code"}) == [
        "oauth_exchange.code_field and oauth_exchange.redirect_uri_field must differ."
    ]


def test_malformed_token_answer_error_is_one_class_on_both_import_paths():
    from app.integrations import token_answer

    assert token_refresh.MalformedTokenAnswerError is token_answer.MalformedTokenAnswerError
