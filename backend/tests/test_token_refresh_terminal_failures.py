"""Tests for terminal token-refresh failure handling.

Production symptoms:

1. ``Failed to refresh token for [Sam] Notion (notion): `` — the reason was
   completely empty because the generic exception handler formatted the
   exception with ``%s`` and many exception classes (bare timeouts, several
   httpx transport errors) stringify to an empty message.  The failing
   integration was then retried on every scheduler tick with no backoff.

2. Permanently-dead OAuth grants (HTTP 400 ``invalid_grant`` — Microsoft
   AADSTS50173/AADSTS50057/AADSTS700003, Notion "Invalid refresh token")
   were retried forever on an hourly cooldown.  These failures are terminal:
   the refresh token can never work again, so the integration must be marked
   as needing re-authorization (``status = "reauthorize"``, already rendered
   by the frontend) and excluded from the refresh sweep until the user
   completes the OAuth flow again.
"""

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.token_refresh import (
    _TRANSIENT_FAIL_COOLDOWN,
    _do_refresh_integration,
    format_refresh_error,
    is_terminal_refresh_failure,
    refresh_expiring_tokens,
)


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the token URL's host; answer for DNS, never a real resolver."""
    import socket

    from app.integrations import token_endpoint

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


@pytest.fixture(autouse=True)
def _stored_secrets_are_plain(monkeypatch):
    """The rows below store plain stand-ins; the client secret is read through the vault-aware reader."""
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda value: value)


# ---------------------------------------------------------------------------
# format_refresh_error — never returns an empty reason
# ---------------------------------------------------------------------------


def test_format_refresh_error_falls_back_to_repr_for_empty_str():
    exc = ValueError()  # str(exc) == ""
    assert str(exc) == "", "pre-condition: bare ValueError stringifies to empty"
    formatted = format_refresh_error(exc)
    assert formatted, "formatted reason must never be empty"
    assert "ValueError" in formatted


def test_format_refresh_error_falls_back_for_whitespace_str():
    exc = RuntimeError("   ")
    formatted = format_refresh_error(exc)
    assert formatted.strip(), "whitespace-only str must fall back to repr"
    assert "RuntimeError" in formatted


def test_format_refresh_error_keeps_meaningful_message():
    exc = RuntimeError("token endpoint unreachable")
    assert format_refresh_error(exc) == "token endpoint unreachable"


def test_format_refresh_error_handles_bare_timeout():

    exc = TimeoutError()
    formatted = format_refresh_error(exc)
    assert formatted, "bare TimeoutError must not produce an empty reason"
    assert "TimeoutError" in formatted


# ---------------------------------------------------------------------------
# is_terminal_refresh_failure — invalid_grant / dead AADSTS codes on 400
# ---------------------------------------------------------------------------


def test_invalid_grant_400_json_is_terminal():
    body = json.dumps(
        {"error": "invalid_grant", "error_description": "Invalid refresh token"}
    )
    assert is_terminal_refresh_failure(400, body) is True


def test_invalid_grant_400_plaintext_is_terminal():
    assert is_terminal_refresh_failure(400, "error=invalid_grant") is True


def test_aadsts_dead_grant_codes_are_terminal():
    for code in ("AADSTS50173", "AADSTS50057", "AADSTS700003", "AADSTS9002313"):
        body = json.dumps(
            {
                "error": "invalid_request",
                "error_description": f"{code}: something terminal happened.",
            }
        )
        assert is_terminal_refresh_failure(400, body) is True, code


def test_invalid_grant_on_non_400_is_not_terminal():
    body = json.dumps({"error": "invalid_grant"})
    assert is_terminal_refresh_failure(503, body) is False


def test_atlassian_unauthorized_client_revoked_token_is_terminal():
    body = json.dumps(
        {
            "error": "unauthorized_client",
            "error_description": "Token was globally revoked",
        }
    )
    assert is_terminal_refresh_failure(401, body) is True


def test_atlassian_unauthorized_client_invalid_refresh_token_is_terminal():
    body = json.dumps(
        {
            "error": "unauthorized_client",
            "error_description": "refresh_token is invalid",
        }
    )
    assert is_terminal_refresh_failure(403, body) is True


def test_other_400_errors_are_not_terminal():
    body = json.dumps(
        {"error": "invalid_client", "error_description": "bad client secret"}
    )
    assert is_terminal_refresh_failure(400, body) is False
    assert is_terminal_refresh_failure(401, body) is False
    assert is_terminal_refresh_failure(400, "") is False


def test_plaintext_unauthorized_client_requires_terminal_marker():
    assert is_terminal_refresh_failure(403, "error=unauthorized_client") is False
    assert (
        is_terminal_refresh_failure(
            403,
            "error=unauthorized_client&error_description=refresh_token is invalid",
        )
        is True
    )


# ---------------------------------------------------------------------------
# _do_refresh_integration — terminal failures mark reauthorize + stop retries
# ---------------------------------------------------------------------------


def _make_integration(**overrides) -> SimpleNamespace:
    defaults = dict(
        id="0c5a91f6-9be3-4a3f-9c86-2f3c1f1f1f1f",
        name="[Sam] Notion",
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
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _http_returning(status_code: int, payload: dict) -> AsyncMock:
    resp = httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "https://example.com/token"),
    )
    http = AsyncMock()
    http.post = AsyncMock(return_value=resp)
    return http


async def test_terminal_invalid_grant_marks_reauthorize():
    integration = _make_integration()
    http = _http_returning(
        400, {"error": "invalid_grant", "error_description": "Invalid refresh token"}
    )
    fake_redis = AsyncMock()

    with (
        patch(
            "app.services.token_refresh.decrypt_value", side_effect=lambda v: v
        ),
        patch(
            "app.services.token_refresh.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
    ):
        ok = await _do_refresh_integration(integration, http)

    assert ok is False
    assert integration.status == "reauthorize", (
        "terminal invalid_grant must mark the integration as needing "
        "re-authorization so the refresh sweep stops picking it up"
    )


async def test_terminal_aadsts_revoked_grant_marks_reauthorize():
    integration = _make_integration(name="saasmanager", provider="microsoft")
    http = _http_returning(
        400,
        {
            "error": "invalid_grant",
            "error_description": (
                "AADSTS50173: The provided grant has expired due to it being "
                "revoked, a fresh auth token is needed."
            ),
        },
    )
    fake_redis = AsyncMock()

    with (
        patch(
            "app.services.token_refresh.decrypt_value", side_effect=lambda v: v
        ),
        patch(
            "app.services.token_refresh.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
    ):
        ok = await _do_refresh_integration(integration, http)

    assert ok is False
    assert integration.status == "reauthorize"


async def test_terminal_unauthorized_client_marks_reauthorize():
    integration = _make_integration(name="Jira", provider="jira")
    http = _http_returning(
        403,
        {
            "error": "unauthorized_client",
            "error_description": "refresh_token is invalid",
        },
    )
    fake_redis = AsyncMock()

    with (
        patch(
            "app.services.token_refresh.decrypt_value", side_effect=lambda v: v
        ),
        patch(
            "app.services.token_refresh.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
    ):
        ok = await _do_refresh_integration(integration, http)

    assert ok is False
    assert integration.status == "reauthorize"
    fake_redis.set.assert_not_called()


async def test_terminal_marker_past_log_truncation_is_still_detected():
    """Terminal detection must use the FULL response body: an invalid_grant
    marker sitting past the 500-char log truncation point must still mark the
    integration as needing re-authorization."""
    integration = _make_integration(name="saasmanager", provider="microsoft")
    http = _http_returning(
        400,
        {
            "error_description": "x" * 600,  # pushes "error" past 500 chars
            "error": "invalid_grant",
        },
    )
    fake_redis = AsyncMock()

    with (
        patch(
            "app.services.token_refresh.decrypt_value", side_effect=lambda v: v
        ),
        patch(
            "app.services.token_refresh.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
    ):
        ok = await _do_refresh_integration(integration, http)

    assert ok is False
    assert integration.status == "reauthorize"


async def test_empty_refresh_token_marks_reauthorize_without_http_call():
    """A present-but-empty (or whitespace-only) refresh token can only produce
    a malformed request (Microsoft AADSTS9002313) — it must be marked terminal
    without hitting the token endpoint at all."""
    for stored_value in ("", "   "):
        integration = _make_integration(refresh_token_enc="sentinel")
        http = AsyncMock()

        def _decrypt(value: str, *, _v: str = stored_value) -> str:
            return _v if value == "sentinel" else value

        with patch("app.services.token_refresh.decrypt_value", side_effect=_decrypt):
            ok = await _do_refresh_integration(integration, http)

        assert ok is False
        assert integration.status == "reauthorize", repr(stored_value)
        http.post.assert_not_called()


async def test_non_terminal_http_error_keeps_error_status_with_cooldown():
    """A 503 from the token endpoint is transient: status stays "error" (still
    eligible for the next sweep) and a cooldown is set so it is not retried
    on every scheduler tick."""
    integration = _make_integration()
    http = _http_returning(503, {"error": "temporarily_unavailable"})
    fake_redis = AsyncMock()

    with (
        patch(
            "app.services.token_refresh.decrypt_value", side_effect=lambda v: v
        ),
        patch(
            "app.services.token_refresh.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
    ):
        ok = await _do_refresh_integration(integration, http)

    assert ok is False
    assert integration.status == "error"
    fake_redis.set.assert_awaited_once()
    args, kwargs = fake_redis.set.await_args
    assert args[0] == f"token_refresh_cooldown:{integration.id}"
    assert kwargs.get("ex") == _TRANSIENT_FAIL_COOLDOWN


async def test_generic_exception_logs_class_name_and_sets_cooldown(caplog):
    """A transport error with an empty __str__ must log the exception class
    name (never an empty reason) and set a transient cooldown instead of
    retrying on every scheduler tick."""
    integration = _make_integration()
    http = AsyncMock()
    empty_exc = httpx.ConnectTimeout("")
    http.post = AsyncMock(side_effect=empty_exc)
    fake_redis = AsyncMock()

    with (
        caplog.at_level(logging.WARNING, logger="app.services.token_refresh"),
        patch(
            "app.services.token_refresh.decrypt_value", side_effect=lambda v: v
        ),
        patch(
            "app.services.token_refresh.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
    ):
        ok = await _do_refresh_integration(integration, http)

    assert ok is False
    assert integration.status == "error"

    matching = [
        r
        for r in caplog.records
        if "Failed to refresh token" in r.getMessage() and r.levelname == "WARNING"
    ]
    assert matching, f"expected a refresh-failure warning, got: {caplog.records!r}"
    for record in matching:
        assert "ConnectTimeout" in record.getMessage(), (
            "the warning must include the exception class name when str(e) "
            f"is empty, got: {record.getMessage()!r}"
        )
        assert record.exc_info, "warning must carry exc_info for the traceback"

    fake_redis.set.assert_awaited_once()
    _, kwargs = fake_redis.set.await_args
    assert kwargs.get("ex") == _TRANSIENT_FAIL_COOLDOWN


# ---------------------------------------------------------------------------
# refresh_expiring_tokens — reauthorize rows are excluded from the sweep
# ---------------------------------------------------------------------------


def test_refresh_sweep_excludes_reauthorize_status():
    """The integration sweep must only select "connected"/"error" rows so a
    terminal invalid_grant (status "reauthorize") is skipped on every
    subsequent cycle until the user re-authorizes (the OAuth callback resets
    the status to "connected")."""
    import re

    source = inspect.getsource(refresh_expiring_tokens)
    # Tolerant of formatting (list vs tuple, quote style, line breaks):
    # extract the argument of Integration.status.in_(...) and compare the
    # quoted status literals it contains.
    match = re.search(
        r"Integration\.status\.in_\(\s*[\[\(](.*?)[\]\)]\s*\)",
        source,
        re.DOTALL,
    )
    assert match, "the refresh sweep must filter on Integration.status.in_(...)"
    statuses = set(re.findall(r"[\"'](\w+)[\"']", match.group(1)))
    assert statuses == {"connected", "error"}, (
        f"the refresh sweep must select only connected/error rows — got "
        f"{statuses!r}; including 'reauthorize' would resurrect the "
        "terminal-failure hot loop"
    )


def test_mark_needs_reauthorization_sets_frontend_known_status():
    """The "reauthorize" status is what the frontend already renders as the
    amber "Re-authorization required" badge (INTEGRATION_STATUS.reauthorize)."""
    from app.services.token_refresh import _mark_needs_reauthorization

    integration = _make_integration()
    _mark_needs_reauthorization(integration, "test reason")
    assert integration.status == "reauthorize"


# ---------------------------------------------------------------------------
# _refresh_one_mcp — same hot-loop protection for MCP server tokens
# ---------------------------------------------------------------------------


def _make_mcp_server(**overrides) -> SimpleNamespace:
    defaults = dict(
        id="7f0e4d3a-1111-4222-8333-944455556666",
        name="Test MCP",
        oauth_status="connected",
        client_id_enc="client-id",
        client_secret_enc="client-secret",
        refresh_token_enc="refresh-token",
        access_token_enc="access-token",
        token_url="https://example.com/oauth/token",
        scopes=None,
        # RFC 8707 resource indicator — unset for a hand-configured server.
        oauth_resource=None,
        token_expires_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


async def test_mcp_refresh_skips_when_cooldown_active():
    from app.services.token_refresh import _refresh_one_mcp

    srv = _make_mcp_server()
    http = AsyncMock()
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value="1")  # cooldown active

    with patch(
        "app.services.token_refresh.redis_client",
        new=AsyncMock(return_value=fake_redis),
    ):
        ok = await _refresh_one_mcp(srv, http, asyncio.Semaphore(1))

    assert ok is False
    http.post.assert_not_called()
    assert srv.oauth_status == "connected", "cooldown skip must not mutate status"


async def test_mcp_refresh_failure_sets_transient_cooldown(caplog):
    from app.services.token_refresh import _do_refresh_mcp

    srv = _make_mcp_server()
    http = AsyncMock()
    http.post = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)

    with (
        caplog.at_level(logging.WARNING, logger="app.services.token_refresh"),
        patch(
            "app.services.token_refresh.decrypt_value", side_effect=lambda v: v
        ),
        patch(
            "app.services.token_refresh.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
    ):
        ok = await _do_refresh_mcp(srv, http)

    assert ok is False
    assert srv.oauth_status == "error"
    fake_redis.set.assert_awaited_once()
    args, kwargs = fake_redis.set.await_args
    assert args[0] == f"token_refresh_cooldown:mcp:{srv.id}", (
        "MCP cooldown keys must use the distinct 'mcp:' prefix to avoid "
        "colliding with integration cooldown keys"
    )
    assert kwargs.get("ex") == _TRANSIENT_FAIL_COOLDOWN
    matching = [
        r for r in caplog.records if "Failed to refresh token" in r.getMessage()
    ]
    assert matching
    for record in matching:
        assert "ConnectTimeout" in record.getMessage()


async def test_mcp_refresh_terminal_invalid_grant_sets_long_cooldown():
    """MCP servers have no "reauthorize" state, so a terminal invalid_grant
    gets the long auth cooldown instead of being dropped from the sweep."""
    from app.services.token_refresh import _AUTH_FAIL_COOLDOWN, _do_refresh_mcp

    srv = _make_mcp_server()
    resp = httpx.Response(
        400,
        json={"error": "invalid_grant", "error_description": "revoked"},
        request=httpx.Request("POST", "https://example.com/oauth/token"),
    )
    http = AsyncMock()
    http.post = AsyncMock(return_value=resp)
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)

    with (
        patch(
            "app.services.token_refresh.decrypt_value", side_effect=lambda v: v
        ),
        patch(
            "app.services.token_refresh.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
    ):
        ok = await _do_refresh_mcp(srv, http)

    assert ok is False
    assert srv.oauth_status == "error"
    fake_redis.set.assert_awaited_once()
    _, kwargs = fake_redis.set.await_args
    assert kwargs.get("ex") == _AUTH_FAIL_COOLDOWN


# ---------------------------------------------------------------------------
# Security audit — "reauthorize" rows must not be reported as healthy
# ---------------------------------------------------------------------------


async def test_security_audit_reports_reauthorize_integrations():
    from app.api.v1.security_audit import _check_integrations

    # _check_integrations issues four COUNT queries in order:
    # total, errored, expired, reauthorize.
    counts = iter([5, 0, 0, 2])

    def _scalar_result():
        result = SimpleNamespace()
        result.scalar = lambda: next(counts)
        return result

    fake_db = SimpleNamespace()
    fake_db.execute = AsyncMock(side_effect=lambda *_: _scalar_result())

    findings: list = []
    await _check_integrations(fake_db, findings)

    reauth = [f for f in findings if "re-authorization" in f.title]
    assert reauth, f"expected a re-authorization finding, got: {findings!r}"
    assert reauth[0].severity == "warning"
    assert "2" in reauth[0].title
    healthy = [f for f in findings if f.severity == "pass"]
    assert not healthy, "must not report 'all healthy' while reauthorize rows exist"


# ---------------------------------------------------------------------------
# Successful refresh without expires_in must stamp a synthetic TTL
# ---------------------------------------------------------------------------


async def test_successful_refresh_without_expires_in_stamps_synthetic_ttl():
    """Notion omits expires_in.  A successful refresh that left
    token_expires_at NULL was re-selected on every 60s scheduler tick."""
    from datetime import UTC, datetime, timedelta

    from app.integrations.oauth import UNKNOWN_EXPIRY_TTL_SECONDS

    integration = _make_integration(token_expires_at=None)
    http = _http_returning(
        200, {"access_token": "new-at", "refresh_token": "new-rt"}
    )

    with (
        patch(
            "app.services.token_refresh.decrypt_value", side_effect=lambda v: v
        ),
        patch(
            "app.services.token_refresh.encrypt_value", side_effect=lambda v: v
        ),
    ):
        ok = await _do_refresh_integration(integration, http)

    assert ok is True
    assert integration.access_token_enc == "new-at"
    assert integration.refresh_token_enc == "new-rt"
    assert integration.token_expires_at is not None
    delta = integration.token_expires_at - datetime.now(UTC)
    assert timedelta(seconds=UNKNOWN_EXPIRY_TTL_SECONDS - 15) <= delta <= timedelta(
        seconds=UNKNOWN_EXPIRY_TTL_SECONDS + 5
    )
