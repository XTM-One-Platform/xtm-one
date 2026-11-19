"""``reauthorize`` is terminal until the user completes a new authorization.

A dead grant or rejected client credentials mark an integration
``reauthorize`` (the amber "Re-authorization required" badge). From then on:

- neither the scheduled sweep nor the chat-time refresh calls its token
  endpoint again, however long it waits;
- editing its client secret, token URL or scopes leaves it marked and
  retries nothing: the edit is a precondition of the new authorization, not a
  substitute for it. A new client ID is a new OAuth app
  (``replace_oauth_client_id``): the previous app's tokens are dropped and the
  row waits in ``configured`` for an authorization against the new one, which
  no refresh selects either;
- the connection test refuses it instead of reporting it healthy;
- only a new successful authorization (the integration OAuth callback) writes
  ``connected`` back and clears the refresh back-off; a GitHub App installed
  from GitHub's own page is not one and reconnects nothing.

There is no automatic retry, no long back-off retry of ``reauthorize`` rows and
no clear-on-credential-update, by design. Transient answers (network errors,
malformed answers, 5xx, 429, other 4xx, unexplained 401 / 403, an AADSTS code
outside a 400, any other refusal inside a 2xx) never produce ``reauthorize``,
and the classifier's terminal set is exactly the documented table. GitHub and
Slack refuse a refresh inside a 2xx: their documented dead-refresh-token and
rejected-client codes there, exactly and only there, are the same verdicts.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import socket
import urllib.parse
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm.attributes import set_committed_value

import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.api import oauth_callbacks
from app.core.security import encrypt_value
from app.integrations import token_endpoint
from app.services import token_refresh
from app.services.integration_tools import _INLINE_TRANSIENT_FAIL_COOLDOWN
from app.services.token_refresh import (
    _AUTH_FAIL_COOLDOWN,
    _CLIENT_REJECTED_REASON,
    _DEAD_GRANT_REASON,
    _TRANSIENT_FAIL_COOLDOWN,
    _do_refresh_integration,
    permanent_refresh_failure_reason,
)
from app.tools.base import ToolCall
from tests.test_integration_oauth_sharing_guard import _build_client, _FakeDb, _make_integration
from tests.test_token_refresh_permanent_failures import (
    _back_off,
    _integration,
    _loaded_integration,
    _RefreshSession,
    _response,
)
from tests.test_token_refresh_permanent_failures import _inline_refresh as _chat_refresh


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the token URL's host; answer for DNS, never a real resolver."""

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


# ---------------------------------------------------------------------------
# The classifier's terminal set is exactly the documented table
# ---------------------------------------------------------------------------

# The documented table, spelled out. Growing or shrinking any of these changes
# which provider answers stop the refresh for good, so it is a deliberate edit
# of this test, the PR description's table and the integrations docs together.
DOCUMENTED_ERROR_CODES = ("invalid_grant", "invalid_client", "unauthorized_client")
DOCUMENTED_MARKERS = (
    "refresh_token is invalid",
    "refresh token is invalid",
    "invalid refresh token",
    "token was globally revoked",
    "refresh token was globally revoked",
    "refresh token was revoked",
    "refresh token has been revoked",
    "refresh token is revoked",
    "refresh token expired",
    "refresh token is expired",
)
DOCUMENTED_DEAD_AADSTS = ("AADSTS50173", "AADSTS50057", "AADSTS700003", "AADSTS9002313")
# Read inside a 2xx only: GitHub (docs.github.com, "Generating a user access
# token for a GitHub App" > Troubleshooting) and Slack (docs.slack.dev,
# ``oauth.v2.access`` > Errors) refuse a refresh inside a success.
DOCUMENTED_DEAD_CODES_INSIDE_A_SUCCESS = ("bad_refresh_token", "invalid_refresh_token")
DOCUMENTED_CLIENT_CODES_INSIDE_A_SUCCESS = ("incorrect_client_credentials", "invalid_client_id", "bad_client_secret")


def test_the_terminal_vocabulary_is_exactly_the_documented_one():
    assert token_refresh._REFRESH_TOKEN_ERROR_CODES == DOCUMENTED_ERROR_CODES
    assert token_refresh._TERMINAL_REFRESH_TOKEN_MARKERS == DOCUMENTED_MARKERS
    assert token_refresh._TERMINAL_AADSTS_CODES == DOCUMENTED_DEAD_AADSTS
    assert token_refresh._DEAD_REFRESH_TOKEN_CODES_INSIDE_A_SUCCESS == DOCUMENTED_DEAD_CODES_INSIDE_A_SUCCESS
    assert token_refresh._REJECTED_CLIENT_CODES_INSIDE_A_SUCCESS == DOCUMENTED_CLIENT_CODES_INSIDE_A_SUCCESS


def _documented_verdict(
    status_code: int, error: str | None, description: str, access_token: str | None = None
) -> str | None:
    """The table in the PR description and the integrations docs, as code."""
    if 200 <= status_code < 300:
        # A refusal inside a success: one documented code, exactly, on an
        # answer without an access token. The description is never read.
        if access_token is not None and access_token.strip():
            return None
        code = (error or "").lower()
        if code in DOCUMENTED_DEAD_CODES_INSIDE_A_SUCCESS:
            return _DEAD_GRANT_REASON
        if code in DOCUMENTED_CLIENT_CODES_INSIDE_A_SUCCESS:
            return _CLIENT_REJECTED_REASON
        return None
    text = description.lower()
    names_dead_token = any(marker in text for marker in DOCUMENTED_MARKERS)
    quotes_dead_aadsts = any(code.lower() in text for code in DOCUMENTED_DEAD_AADSTS)
    if status_code == 400 and error == "invalid_grant":
        return _DEAD_GRANT_REASON
    if status_code in (400, 401, 403) and error in DOCUMENTED_ERROR_CODES and names_dead_token:
        return _DEAD_GRANT_REASON
    if status_code == 400 and quotes_dead_aadsts:
        return _DEAD_GRANT_REASON
    if status_code in (400, 401) and error == "invalid_client":
        return _CLIENT_REJECTED_REASON
    return None


_STATUS_CODES = (400, 401, 403, 404, 408, 409, 422, 429, 500, 502, 503, 504)
_ERRORS = (
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "invalid_request",
    "invalid_scope",
    "access_denied",
    "temporarily_unavailable",
    # Read inside a 2xx only: no verdict on any other status.
    *DOCUMENTED_DEAD_CODES_INSIDE_A_SUCCESS,
    *DOCUMENTED_CLIENT_CODES_INSIDE_A_SUCCESS,
    # Slack spells an RFC code and more: a code is matched as a whole word.
    "invalid_grant_type",
    None,
)
_DESCRIPTIONS = (
    "",
    "Unauthorized",
    "refresh_token is invalid",
    "Token was globally revoked",
    "Unknown or invalid refresh token.",
    "The refresh token has been revoked",
    "AADSTS50173: The provided grant has expired due to it being revoked.",
    "AADSTS700003: Device object was not found in the tenant directory.",
    "AADSTS70000: The request was denied because one or more scopes are unauthorized.",
    "AADSTS7000215: Invalid client secret provided.",
)


def _json_body(error: str | None, description: str) -> str:
    payload: dict[str, str] = {}
    if error is not None:
        payload["error"] = error
    if description:
        payload["error_description"] = description
    return json.dumps(payload)


def _form_fields(error: str | None, description: str) -> list[tuple[str, str]]:
    fields = []
    if error is not None:
        fields.append(("error", error))
    if description:
        fields.append(("error_description", description))
    return fields


def _form_body(error: str | None, description: str) -> str:
    """``key=value`` pairs as a lenient server writes them, spaces left as is."""
    return "&".join(f"{key}={value}" for key, value in _form_fields(error, description))


def _urlencoded_body(error: str | None, description: str) -> str:
    """A real ``application/x-www-form-urlencoded`` answer: ``+`` for a space, ``%3A`` for a colon."""
    return urllib.parse.urlencode(_form_fields(error, description))


@pytest.mark.parametrize(
    ("status_code", "error", "description"),
    list(itertools.product(_STATUS_CODES, _ERRORS, _DESCRIPTIONS)),
)
@pytest.mark.parametrize(
    "encode", [_json_body, _form_body, _urlencoded_body], ids=["json", "form", "urlencoded"]
)
def test_the_classifier_matches_the_documented_table(status_code, error, description, encode):
    """Every other combination of status, error code and description stays retryable."""
    body = encode(error, description)

    assert permanent_refresh_failure_reason(status_code, body) == _documented_verdict(
        status_code, error, description
    )


@pytest.mark.parametrize(
    "body",
    [
        "error=unauthorized_client&error_description=Token+was+globally+revoked",
        "error=unauthorized_client&error_description=refresh_token%20is%20invalid",
        "error=invalid_grant&error_description=Unknown+or+invalid+refresh+token.",
    ],
)
def test_a_url_encoded_dead_grant_is_a_dead_grant(body):
    """A form-encoded answer is decoded before the refresh-token marker is looked for."""
    assert permanent_refresh_failure_reason(401, body) == _DEAD_GRANT_REASON


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (400, "error=invalid_grant_type"),
        (400, "error=invalid_grant_type&error_description=refresh_token+is+invalid"),
        (401, "error=invalid_client_id&error_description=refresh_token+is+invalid"),
        (403, "error=unauthorized_client_credentials&error_description=Token+was+globally+revoked"),
    ],
)
def test_a_plain_answer_names_an_error_code_only_as_a_whole_word(status_code, body):
    """Slack's ``invalid_grant_type`` / ``invalid_client_id`` are not ``invalid_grant`` / ``invalid_client``."""
    assert permanent_refresh_failure_reason(status_code, body) is None


# Around the 2xx range: its bounds are part of the rule.
_SUCCESS_STATUS_CODES = (199, 200, 201, 204, 299, 300)
_SUCCESS_ERRORS = (
    *DOCUMENTED_DEAD_CODES_INSIDE_A_SUCCESS,
    *DOCUMENTED_CLIENT_CODES_INSIDE_A_SUCCESS,
    "BAD_REFRESH_TOKEN",
    "Invalid_Refresh_Token",
    "Incorrect_Client_Credentials",
    # A code is the whole value, never a part of it.
    "bad_refresh_token_v2",
    " bad_refresh_token",
    "refresh_token",
    # RFC 6749 codes, which neither provider sends inside a 2xx.
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    # Slack's other answers: transient, or not about the refresh token.
    "ratelimited",
    "internal_error",
    "service_unavailable",
    "fatal_error",
    "request_timeout",
    "team_access_not_granted",
    "invalid_grant_type",
    "invalid_auth",
    "token_revoked",
    # GitHub's other token endpoint answers.
    "unsupported_grant_type",
    "bad_verification_code",
    "an_unknown_code",
    None,
)
_SUCCESS_DESCRIPTIONS = ("", "The refresh token passed is incorrect or expired.")
_ACCESS_TOKENS = (None, "  ", "ghu_new")


def _success_fields(error: str | None, description: str, access_token: str | None) -> list[tuple[str, str]]:
    fields = [] if access_token is None else [("access_token", access_token)]
    return fields + _form_fields(error, description)


def _success_json_body(error: str | None, description: str, access_token: str | None) -> str:
    return json.dumps(dict(_success_fields(error, description, access_token)))


def _slack_body(error: str | None, description: str, access_token: str | None) -> str:
    """Slack's shape: ``ok`` is not read, only ``error`` and ``access_token``."""
    return json.dumps({"ok": False, **dict(_success_fields(error, description, access_token))})


def _success_form_body(error: str | None, description: str, access_token: str | None) -> str:
    return "&".join(f"{key}={value}" for key, value in _success_fields(error, description, access_token))


def _success_urlencoded_body(error: str | None, description: str, access_token: str | None) -> str:
    return urllib.parse.urlencode(_success_fields(error, description, access_token))


@pytest.mark.parametrize(
    ("status_code", "error", "description", "access_token"),
    list(itertools.product(_SUCCESS_STATUS_CODES, _SUCCESS_ERRORS, _SUCCESS_DESCRIPTIONS, _ACCESS_TOKENS)),
)
@pytest.mark.parametrize(
    "encode",
    [_success_json_body, _slack_body, _success_form_body, _success_urlencoded_body],
    ids=["json", "slack", "form", "urlencoded"],
)
def test_a_refusal_inside_a_2xx_matches_the_documented_table(status_code, error, description, access_token, encode):
    """Only a documented code, exactly, on a 2xx without an access token is a verdict."""
    body = encode(error, description, access_token)

    assert permanent_refresh_failure_reason(status_code, body) == _documented_verdict(
        status_code, error, description, access_token
    )


@pytest.mark.parametrize(
    ("body", "verdict"),
    [
        pytest.param(
            json.dumps(
                {
                    "error": "bad_refresh_token",
                    "error_description": "The refresh token passed is incorrect or expired.",
                    "error_uri": "https://docs.github.com/apps/creating-github-apps",
                }
            ),
            _DEAD_GRANT_REASON,
            id="github",
        ),
        pytest.param(
            "error=bad_refresh_token&error_description=The+refresh+token+passed+is+incorrect+or+expired."
            "&error_uri=https%3A%2F%2Fdocs.github.com%2Fapps%2Fcreating-github-apps",
            _DEAD_GRANT_REASON,
            id="github-form",
        ),
        pytest.param(
            json.dumps(
                {
                    "error": "incorrect_client_credentials",
                    "error_description": "The client_id and/or client_secret passed are incorrect.",
                }
            ),
            _CLIENT_REJECTED_REASON,
            id="github-client",
        ),
        pytest.param(
            "error=incorrect_client_credentials"
            "&error_description=The+client_id+and%2For+client_secret+passed+are+incorrect.",
            _CLIENT_REJECTED_REASON,
            id="github-client-form",
        ),
        pytest.param(json.dumps({"ok": False, "error": "invalid_refresh_token"}), _DEAD_GRANT_REASON, id="slack"),
        pytest.param(json.dumps({"ok": False, "error": "invalid_client_id"}), _CLIENT_REJECTED_REASON, id="slack-id"),
        pytest.param(
            json.dumps({"ok": False, "error": "bad_client_secret"}), _CLIENT_REJECTED_REASON, id="slack-secret"
        ),
    ],
)
def test_github_and_slack_refusals_inside_a_200_are_a_verdict(body, verdict):
    assert permanent_refresh_failure_reason(200, body) == verdict


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("error=bad_refresh_token&error=bad_refresh_token", id="error-sent-twice"),
        pytest.param("error=bad_refresh_token&error=ratelimited", id="two-errors"),
        pytest.param('{"error": "ratelimited", "error": "bad_refresh_token"}', id="json-two-errors"),
        pytest.param('{"error": "invalid_refresh_token", "error": "invalid_refresh_token"}', id="json-error-sent-twice"),
        pytest.param(json.dumps({"error": ["bad_refresh_token"]}), id="error-not-a-string"),
        pytest.param(json.dumps([{"error": "bad_refresh_token"}]), id="not-an-object"),
        pytest.param(json.dumps({"access_token": "ghu_new", "error": "bad_refresh_token"}), id="with-an-access-token"),
        pytest.param("access_token=ghu_new&error=invalid_refresh_token", id="form-with-an-access-token"),
    ],
)
def test_an_ambiguous_refusal_inside_a_200_is_no_verdict(body):
    assert permanent_refresh_failure_reason(200, body) is None


# ---------------------------------------------------------------------------
# A reauthorize row is never refreshed
# ---------------------------------------------------------------------------


class _SweepDB:
    """Records the sweep's SELECTs and answers them with no rows."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    async def commit(self):
        return None


async def test_the_sweep_selects_only_connected_and_error_rows():
    """Reauthorize is terminal until the user re-authorizes: the hourly sweep never selects it."""
    db = _SweepDB()

    assert await token_refresh.refresh_expiring_tokens(db) == 0

    # The integrations, the people's own credentials on per_user ones,
    # the MCP servers, the people's own credentials on per_user MCP servers.
    integrations_select, credentials_select, mcp_select, mcp_credentials_select = db.statements
    for statement, column in (
        (integrations_select, "status"),
        (credentials_select, "status"),
        (mcp_select, "oauth_status"),
        (mcp_credentials_select, "oauth_status"),
    ):
        compiled = statement.compile(dialect=postgresql.dialect())
        status_filters = [value for key, value in compiled.params.items() if key.startswith(f"{column}_")]
        assert status_filters == [["connected", "error"]]


async def test_the_sweep_leaves_a_row_that_was_marked_after_it_was_selected():
    """The row is re-read under the refresh lock: a verdict recorded meanwhile is final."""
    selected = _integration(auth_type="oauth", status="connected")
    stored = SimpleNamespace(**{**vars(selected), "status": "reauthorize"})
    session = _RefreshSession(stored)
    http = AsyncMock()
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)
    fake_redis.set = AsyncMock(return_value=True)
    with (
        patch("app.services.token_refresh.redis_client", new=AsyncMock(return_value=fake_redis)),
        patch("app.core.database.async_session", new=lambda: session),
    ):
        ok = await token_refresh._refresh_one_integration(selected, http, asyncio.Semaphore(1))

    assert ok is False
    http.post.assert_not_awaited()
    assert stored.status == "reauthorize"
    assert session.commits == 0


@pytest.mark.parametrize("marked", ["turn row", "stored row"])
async def test_a_chat_turn_never_refreshes_an_integration_awaiting_reauthorization(marked):
    """Reauthorize is terminal until the user re-authorizes, whichever copy the turn read."""
    integration = _integration(status="reauthorize" if marked == "turn row" else "connected")
    stored = SimpleNamespace(**{**vars(integration), "status": "reauthorize"})

    result = await _chat_refresh(integration, _response(200, {"access_token": "new"}), stored=stored)

    assert result.token is None
    assert result.client.posts == 0
    assert result.stored.status == "reauthorize"
    assert result.session.commits == 0


# ---------------------------------------------------------------------------
# Transient answers never produce reauthorize
# ---------------------------------------------------------------------------

_TRANSIENT_ANSWERS = [
    pytest.param(500, {"error": "server_error"}, _TRANSIENT_FAIL_COOLDOWN, id="500"),
    pytest.param(502, {"error": "bad_gateway"}, _TRANSIENT_FAIL_COOLDOWN, id="502"),
    pytest.param(503, {"error": "temporarily_unavailable"}, _TRANSIENT_FAIL_COOLDOWN, id="503"),
    pytest.param(429, {"error": "rate_limited"}, _TRANSIENT_FAIL_COOLDOWN, id="429"),
    pytest.param(400, {"error": "invalid_request"}, _TRANSIENT_FAIL_COOLDOWN, id="400-invalid_request"),
    pytest.param(400, {"error": "invalid_scope"}, _TRANSIENT_FAIL_COOLDOWN, id="400-invalid_scope"),
    pytest.param(404, {"error": "not_found"}, _TRANSIENT_FAIL_COOLDOWN, id="404"),
    pytest.param(409, {"error": "conflict"}, _TRANSIENT_FAIL_COOLDOWN, id="409"),
    pytest.param(422, {"error": "unprocessable"}, _TRANSIENT_FAIL_COOLDOWN, id="422"),
    pytest.param(401, {"error": "unauthorized_client"}, _AUTH_FAIL_COOLDOWN, id="401-unexplained"),
    pytest.param(403, {"error": "access_denied"}, _AUTH_FAIL_COOLDOWN, id="403-unexplained"),
    pytest.param(
        503,
        {"error": "temporarily_unavailable", "error_description": "AADSTS50173: grant revoked"},
        _TRANSIENT_FAIL_COOLDOWN,
        id="503-aadsts",
    ),
    pytest.param(
        429,
        {"error": "invalid_request", "error_description": "AADSTS50057: The user account is disabled."},
        _TRANSIENT_FAIL_COOLDOWN,
        id="429-aadsts",
    ),
    pytest.param(
        401,
        {"error": "invalid_grant", "error_description": "AADSTS700003: Device object was not found."},
        _AUTH_FAIL_COOLDOWN,
        id="401-aadsts",
    ),
]


def _answer_200(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("POST", "https://x/token"))


def _raw_answer_200(body: str, content_type: str) -> httpx.Response:
    return httpx.Response(
        200, content=body.encode(), headers={"content-type": content_type}, request=httpx.Request("POST", "https://x/token")
    )


def _form_answer_200(body: str) -> httpx.Response:
    """GitHub's token endpoint answers form-encoded unless the request asks for JSON."""
    return _raw_answer_200(body, "application/x-www-form-urlencoded")


# A 2xx is a refresh only when it carries an access token (RFC 6749 section 5.1).
_MALFORMED_2XX = [
    pytest.param(_answer_200({}), id="2xx-empty"),
    pytest.param(_answer_200([{"access_token": "new"}]), id="2xx-not-an-object"),
    pytest.param(_answer_200({"access_token": "  "}), id="2xx-blank-token"),
    pytest.param(_answer_200({"access_token": 42}), id="2xx-token-not-a-string"),
    pytest.param(_answer_200({"access_token": "new", "refresh_token": ["new"]}), id="2xx-refresh-token-not-a-string"),
    # Form-encoded without a verdict: not JSON, so not a refresh either.
    pytest.param(_form_answer_200("error=ratelimited"), id="2xx-form-unknown-code"),
    pytest.param(_form_answer_200("error=bad_refresh_token&error=bad_refresh_token"), id="2xx-form-error-sent-twice"),
    pytest.param(_form_answer_200("access_token=ghu_new&error=bad_refresh_token"), id="2xx-form-with-an-access-token"),
]

# GitHub and Slack refuse a refresh inside a 200: their documented codes, exactly.
_DEAD_GRANTS_INSIDE_A_2XX = [
    pytest.param(
        _answer_200({"error": "bad_refresh_token", "error_description": "The refresh token passed is incorrect or expired."}),
        id="2xx-github",
    ),
    pytest.param(
        _form_answer_200("error=bad_refresh_token&error_description=The+refresh+token+passed+is+incorrect+or+expired."),
        id="2xx-github-form",
    ),
    pytest.param(_answer_200({"ok": False, "error": "invalid_refresh_token"}), id="2xx-slack"),
    pytest.param(_answer_200({"ok": False, "error": "INVALID_REFRESH_TOKEN"}), id="2xx-slack-any-case"),
]
_CLIENT_REJECTIONS_INSIDE_A_2XX = [
    pytest.param(
        _answer_200(
            {
                "error": "incorrect_client_credentials",
                "error_description": "The client_id and/or client_secret passed are incorrect.",
            }
        ),
        id="2xx-github-client",
    ),
    pytest.param(_form_answer_200("error=incorrect_client_credentials"), id="2xx-github-client-form"),
    pytest.param(_answer_200({"ok": False, "error": "invalid_client_id"}), id="2xx-slack-client-id"),
    pytest.param(_answer_200({"ok": False, "error": "bad_client_secret"}), id="2xx-slack-client-secret"),
]
_PERMANENT_REFUSALS_INSIDE_A_2XX = [*_DEAD_GRANTS_INSIDE_A_2XX, *_CLIENT_REJECTIONS_INSIDE_A_2XX]

# Any other refusal reported inside a success backs off like an unexplained 401 / 403.
_REFUSALS_INSIDE_A_2XX = [
    pytest.param(_answer_200({"error": "temporarily_unavailable"}), id="2xx-error"),
    *(
        pytest.param(_answer_200({"ok": False, "error": code}), id=f"2xx-slack-{code}")
        for code in (
            "ratelimited",
            "internal_error",
            "service_unavailable",
            "fatal_error",
            "request_timeout",
            "team_access_not_granted",
            "invalid_grant_type",
            "invalid_auth",
            "token_revoked",
        )
    ),
    pytest.param(_answer_200({"error": "unsupported_grant_type"}), id="2xx-github-unsupported_grant_type"),
    pytest.param(_answer_200({"error": "bad_refresh_token_v2"}), id="2xx-unknown-code"),
    pytest.param(
        _raw_answer_200('{"error": "ratelimited", "error": "bad_refresh_token"}', "application/json"),
        id="2xx-json-error-sent-twice",
    ),
]

# An access token makes a 2xx a refresh, whatever else it says.
_REFRESHES_NAMING_AN_ERROR = [
    pytest.param(_answer_200({"access_token": "new", "error": "bad_refresh_token"}), id="2xx-github"),
    pytest.param(_answer_200({"ok": False, "access_token": "new", "error": "invalid_refresh_token"}), id="2xx-slack"),
]

_TRANSIENT_ERRORS = [
    pytest.param(httpx.ConnectTimeout("token endpoint unreachable"), id="network"),
    pytest.param(
        httpx.Response(200, content=b"<html>maintenance</html>", request=httpx.Request("POST", "https://x/token")),
        id="malformed",
    ),
    *_MALFORMED_2XX,
]


async def _sweep_refresh(integration, *, response=None, error=None):
    http = AsyncMock()
    http.post = AsyncMock(return_value=response, side_effect=error)
    fake_redis = AsyncMock()
    with (
        patch("app.services.token_refresh.decrypt_value", side_effect=lambda v: v),
        patch("app.core.security.decrypt_stored_value", side_effect=lambda v: v),
        patch("app.services.token_refresh.redis_client", new=AsyncMock(return_value=fake_redis)),
    ):
        ok = await _do_refresh_integration(integration, http)
    return ok, fake_redis


@pytest.mark.parametrize("status", ["connected", "error"])
@pytest.mark.parametrize(("status_code", "payload", "cooldown"), _TRANSIENT_ANSWERS)
async def test_a_transient_answer_never_marks_the_sweep_reauthorize(status, status_code, payload, cooldown):
    integration = _integration(status=status)

    ok, fake_redis = await _sweep_refresh(integration, response=_response(status_code, payload))

    assert ok is False
    assert integration.status == "error"
    _, kwargs = fake_redis.set.await_args
    assert kwargs.get("ex") == cooldown


@pytest.mark.parametrize("failure", _TRANSIENT_ERRORS)
async def test_a_network_error_or_malformed_answer_never_marks_the_sweep_reauthorize(failure):
    integration = _integration()
    if isinstance(failure, httpx.Response):
        ok, fake_redis = await _sweep_refresh(integration, response=failure)
    else:
        ok, fake_redis = await _sweep_refresh(integration, error=failure)

    assert ok is False
    assert integration.status == "error"
    _, kwargs = fake_redis.set.await_args
    assert kwargs.get("ex") == _TRANSIENT_FAIL_COOLDOWN
    # Nothing recorded as a refresh: same tokens, no expiry stamped.
    assert (integration.access_token_enc, integration.refresh_token_enc) == ("access-token", "refresh-token")
    assert integration.token_expires_at is None


@pytest.mark.parametrize(("status_code", "payload", "_cooldown"), _TRANSIENT_ANSWERS)
async def test_a_transient_answer_never_marks_a_chat_refresh_reauthorize(status_code, payload, _cooldown):
    result = await _chat_refresh(_integration(), _response(status_code, payload))

    assert result.token is None
    assert result.client.posts == 1
    assert result.stored.status == "connected"
    assert result.session.commits == 0


@pytest.mark.parametrize("failure", _TRANSIENT_ERRORS)
async def test_a_network_error_or_malformed_answer_never_marks_a_chat_refresh_reauthorize(failure):
    if isinstance(failure, httpx.Response):
        result = await _chat_refresh(_integration(), failure)
    else:
        result = await _chat_refresh(_integration(), error=failure)

    assert result.token is None
    assert result.stored.status == "connected"
    assert result.session.commits == 0
    # The next tool calls of the turn do not ask the endpoint again.
    assert _back_off(result) == (_INLINE_TRANSIENT_FAIL_COOLDOWN, True)


@pytest.mark.parametrize("answer", _REFUSALS_INSIDE_A_2XX)
async def test_a_refusal_inside_a_2xx_backs_off_the_sweep_like_an_unexplained_401(answer):
    integration = _integration()

    ok, fake_redis = await _sweep_refresh(integration, response=answer)

    assert ok is False
    assert integration.status == "error"
    _, kwargs = fake_redis.set.await_args
    assert kwargs.get("ex") == _AUTH_FAIL_COOLDOWN
    assert (integration.access_token_enc, integration.token_expires_at) == ("access-token", None)


@pytest.mark.parametrize("answer", _REFUSALS_INSIDE_A_2XX)
async def test_a_refusal_inside_a_2xx_backs_off_a_chat_refresh_like_an_unexplained_401(answer):
    result = await _chat_refresh(_integration(), answer)

    assert result.token is None
    assert result.stored.status == "connected"
    assert result.session.commits == 0
    assert _back_off(result) == (_AUTH_FAIL_COOLDOWN, False)


_VERDICT_LOG_LINES = [
    *(pytest.param(*p.values, "grant is permanently invalid", id=p.id) for p in _DEAD_GRANTS_INSIDE_A_2XX),
    *(
        pytest.param(*p.values, "update the integration's client ID / client secret first", id=p.id)
        for p in _CLIENT_REJECTIONS_INSIDE_A_2XX
    ),
]


@pytest.mark.parametrize("status", ["connected", "error"])
@pytest.mark.parametrize(("answer", "logged"), _VERDICT_LOG_LINES)
async def test_a_permanent_refusal_inside_a_2xx_marks_the_sweep_reauthorize(status, answer, logged, caplog):
    """GitHub and Slack report a dead grant or rejected client credentials inside a 200."""
    integration = _integration(status=status)

    with caplog.at_level(logging.WARNING, logger="app.services.token_refresh"):
        ok, fake_redis = await _sweep_refresh(integration, response=answer)

    assert ok is False
    assert integration.status == "reauthorize"
    assert logged in caplog.text
    # A verdict, not a back-off, and never a refresh.
    fake_redis.set.assert_not_called()
    assert (integration.access_token_enc, integration.refresh_token_enc) == ("access-token", "refresh-token")
    assert integration.token_expires_at is None


@pytest.mark.parametrize(("answer", "logged"), _VERDICT_LOG_LINES)
async def test_a_permanent_refusal_inside_a_2xx_marks_a_chat_refresh_reauthorize(answer, logged, caplog):
    integration = _integration()

    with caplog.at_level(logging.WARNING, logger="app.services.token_refresh"):
        result = await _chat_refresh(integration, answer)

    assert result.token is None
    assert result.client.posts == 1
    assert result.stored.status == "reauthorize"
    assert (result.session.locked_reads, result.session.commits) == (1, 1)
    assert logged in caplog.text
    # Written on the refresh's own session: the turn's row and session are left alone.
    assert integration.status == "connected"
    result.db.commit.assert_not_awaited()
    result.redis.set.assert_not_called()


@pytest.mark.parametrize("answer", _PERMANENT_REFUSALS_INSIDE_A_2XX)
async def test_a_chat_refresh_backs_off_when_a_verdict_inside_a_2xx_cannot_be_saved(answer):
    result = await _chat_refresh(_integration(), answer, commit_error=ConnectionError("database down"))

    assert result.token is None
    assert result.session.locked_reads == 1
    assert result.session.commits == 0
    result.db.commit.assert_not_awaited()
    assert _back_off(result) == (_AUTH_FAIL_COOLDOWN, False)


@pytest.mark.parametrize("answer", _PERMANENT_REFUSALS_INSIDE_A_2XX)
async def test_a_chat_verdict_inside_a_2xx_is_not_written_over_a_row_marked_meanwhile(answer):
    """Another refresh recorded a verdict on the same tokens while this one was in flight."""

    async def _marked_meanwhile(session, _obj, attribute_names=None, with_for_update=None):
        session.row.status = "reauthorize"
        session.locked_reads += bool(with_for_update)

    with patch.object(_RefreshSession, "refresh", _marked_meanwhile):
        result = await _chat_refresh(_integration(), answer)

    assert result.token is None
    assert result.session.locked_reads == 1
    assert result.session.commits == 0
    assert result.stored.status == "reauthorize"
    assert _back_off(result) == (_AUTH_FAIL_COOLDOWN, False)


@pytest.mark.parametrize("answer", _REFRESHES_NAMING_AN_ERROR)
async def test_an_access_token_inside_a_2xx_is_a_refresh_whatever_error_it_names(answer):
    integration = _integration(status="error")

    ok, fake_redis = await _sweep_refresh(integration, response=answer)
    assert ok is True
    assert integration.status == "connected"
    assert integration.access_token_enc != "access-token"
    fake_redis.set.assert_not_called()

    result = await _chat_refresh(_integration(), answer)
    assert result.token == "new"
    assert result.stored.status == "connected"


@pytest.mark.parametrize(
    ("answer", "cooldown"),
    [
        *(pytest.param(*p.values, _TRANSIENT_FAIL_COOLDOWN, id=p.id) for p in _MALFORMED_2XX),
        *(pytest.param(*p.values, _AUTH_FAIL_COOLDOWN, id=p.id) for p in _REFUSALS_INSIDE_A_2XX),
        # No reauthorize state: a permanent refusal inside a 2xx gets the hourly back-off.
        *(pytest.param(*p.values, _AUTH_FAIL_COOLDOWN, id=p.id) for p in _PERMANENT_REFUSALS_INSIDE_A_2XX),
    ],
)
async def test_an_mcp_server_answer_without_an_access_token_is_never_a_refresh(answer, cooldown):
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Company MCP",
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
    http.post = AsyncMock(return_value=answer)
    fake_redis = AsyncMock()
    with (
        patch("app.services.token_refresh.decrypt_value", side_effect=lambda v: v),
        patch("app.core.security.decrypt_stored_value", side_effect=lambda v: v),
        patch("app.services.token_refresh.redis_client", new=AsyncMock(return_value=fake_redis)),
    ):
        ok = await token_refresh._do_refresh_mcp(srv, http)

    assert ok is False
    assert srv.oauth_status == "error"
    _, kwargs = fake_redis.set.await_args
    assert kwargs.get("ex") == cooldown
    assert (srv.access_token_enc, srv.refresh_token_enc, srv.token_expires_at) == ("access-token", "refresh-token", None)


@pytest.mark.parametrize(
    ("status_code", "payload", "cooldown"),
    [
        (401, {"error": "invalid_client"}, _AUTH_FAIL_COOLDOWN),
        (400, {"error": "invalid_grant"}, _AUTH_FAIL_COOLDOWN),
        (403, {"error": "unauthorized_client", "error_description": "refresh_token is invalid"}, _AUTH_FAIL_COOLDOWN),
        (401, {"error": "unauthorized_client"}, _AUTH_FAIL_COOLDOWN),
        (503, {"error": "temporarily_unavailable"}, _TRANSIENT_FAIL_COOLDOWN),
        (200, {"error": "bad_refresh_token"}, _AUTH_FAIL_COOLDOWN),
        (200, {"ok": False, "error": "invalid_refresh_token"}, _AUTH_FAIL_COOLDOWN),
        (200, {"error": "incorrect_client_credentials"}, _AUTH_FAIL_COOLDOWN),
        (200, {"ok": False, "error": "bad_client_secret"}, _AUTH_FAIL_COOLDOWN),
    ],
)
async def test_an_mcp_server_is_never_marked_reauthorize(status_code, payload, cooldown):
    """MCP servers have no reauthorize state: a permanent answer gets the hourly back-off."""
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Company MCP",
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
    http.post = AsyncMock(return_value=_response(status_code, payload))
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
    assert kwargs.get("ex") == cooldown


# ---------------------------------------------------------------------------
# Editing the credentials keeps the mark and retries nothing
# ---------------------------------------------------------------------------

_CREDENTIAL_EDITS = [
    pytest.param({"client_id": "old-client-id"}, id="same_client_id"),
    pytest.param({"client_secret": "rotated-secret"}, id="client_secret"),
    pytest.param({"client_id": "old-client-id", "client_secret": "rotated-secret"}, id="same_client_id+secret"),
    pytest.param({"token_url": "https://api.notion.com/v1/oauth/token-v2"}, id="token_url"),
    pytest.param({"authorize_url": "https://api.notion.com/v1/oauth/authorize-v2"}, id="authorize_url"),
    pytest.param({"scopes": "read write"}, id="scopes"),
]

# A different client ID is a different OAuth app: its predecessor's
# tokens are dropped, and the row waits for an authorization against it.
_APP_REPLACING_EDITS = [
    pytest.param({"client_id": "new-client-id"}, id="client_id"),
    pytest.param({"client_id": "new-client-id", "client_secret": "rotated-secret"}, id="client_id+secret"),
]


def _reauthorize_row(user_id) -> object:
    return _make_integration(
        user_id=user_id,
        provider="notion",
        auth_type="oauth",
        name="Notion",
        status="reauthorize",
        client_id_enc=encrypt_value("old-client-id"),
        client_secret_enc="old-client-secret",
        access_token_enc="old-access",
        refresh_token_enc="old-refresh",
        token_url="https://api.notion.com/v1/oauth/token",
        authorize_url="https://api.notion.com/v1/oauth/authorize",
        scopes=None,
    )


@pytest.fixture
def integrations_client(monkeypatch):
    fake_db = _FakeDb()
    fake_user = SimpleNamespace(id=uuid.uuid4(), full_name="Admin", role="admin", is_admin=True)
    fake_redis = AsyncMock()
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=fake_redis))
    with _build_client(fake_db, fake_user, monkeypatch) as client:
        yield client, fake_db, fake_user, fake_redis


@pytest.mark.parametrize("edit", _CREDENTIAL_EDITS)
def test_editing_the_credentials_in_the_rest_api_keeps_reauthorize(integrations_client, edit):
    """Updating a rejected client secret is a precondition of the new authorization, not a substitute."""
    client, db, user, fake_redis = integrations_client
    integration = _reauthorize_row(user.id)
    db.integrations[str(integration.id)] = integration

    resp = client.put(f"/api/v1/integrations/{integration.id}", json={"id": str(integration.id), **edit})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "reauthorize"
    assert db.integrations[str(integration.id)].status == "reauthorize"
    # Neither a retry nor a cleared back-off: the refresh machinery is not touched.
    assert fake_redis.method_calls == []


@pytest.mark.parametrize("edit", _APP_REPLACING_EDITS)
def test_a_new_client_id_in_the_rest_api_waits_for_an_authorization_against_it(integrations_client, edit):
    """The previous app's grant is gone, not revived: still no refresh, still a new authorization."""
    client, db, user, fake_redis = integrations_client
    integration = _reauthorize_row(user.id)
    db.integrations[str(integration.id)] = integration

    resp = client.put(f"/api/v1/integrations/{integration.id}", json={"id": str(integration.id), **edit})

    assert resp.status_code == 200, resp.text
    stored = db.integrations[str(integration.id)]
    assert resp.json()["status"] == stored.status == "configured"
    assert stored.status not in token_refresh._SWEEP_STATUSES
    assert (stored.access_token_enc, stored.refresh_token_enc) == (None, None)
    assert fake_redis.method_calls == []


def test_the_connection_test_refuses_an_integration_awaiting_reauthorization(integrations_client):
    """A still-valid access token must not report a dead grant as healthy."""
    client, db, user, _fake_redis = integrations_client
    integration = _reauthorize_row(user.id)
    db.integrations[str(integration.id)] = integration

    resp = client.post(f"/api/v1/integrations/{integration.id}/test")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "error", "message": "Integration is not connected"}
    assert db.integrations[str(integration.id)].status == "reauthorize"


class _BuilderSession:
    def __init__(self):
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


async def _update_from_the_agent_builder(monkeypatch, edit: dict) -> tuple[SimpleNamespace, _BuilderSession, AsyncMock]:
    integration = SimpleNamespace(
        id=uuid.uuid4(),
        name="Notion",
        provider="notion",
        auth_type="oauth",
        status="reauthorize",
        is_builtin=False,
        client_id_enc=encrypt_value("old-client-id"),
        client_secret_enc="old-client-secret",
        access_token_enc="old-access",
        refresh_token_enc="old-refresh",
        authorize_url="https://api.notion.com/v1/oauth/authorize",
        token_url="https://api.notion.com/v1/oauth/token",
        scopes=None,
    )

    async def _manageable(_db, _model, _uid, _entity_uuid, *, action="modify", entity_label="entity", lock=False):
        return integration, None

    async def _caller(_db, _uid):
        return SimpleNamespace(id=_uid, role="user", is_admin=False)

    session = _BuilderSession()
    fake_redis = AsyncMock()
    monkeypatch.setattr(builtin_tools_module, "_load_manageable_entity_for_builder", _manageable)
    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _caller)
    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=fake_redis))

    _, executor = builtin_tools_module.build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_integration",
            arguments={"integration_id": str(integration.id), **edit},
        )
    )

    assert not result.is_error, result.content
    return integration, session, fake_redis


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param({"client_id": "old-client-id", "client_secret": "rotated-secret"}, id="same_client_id+secret"),
        pytest.param({"token_url": "https://api.notion.com/v1/oauth/token-v2"}, id="token_url"),
        pytest.param({"scopes": "read write"}, id="scopes"),
    ],
)
async def test_editing_the_credentials_from_the_agent_builder_keeps_reauthorize(monkeypatch, edit):
    integration, session, fake_redis = await _update_from_the_agent_builder(monkeypatch, edit)

    assert session.commits == 1
    assert integration.status == "reauthorize"
    assert fake_redis.method_calls == []


async def test_a_new_client_id_from_the_agent_builder_waits_for_an_authorization_against_it(monkeypatch):
    integration, session, fake_redis = await _update_from_the_agent_builder(
        monkeypatch, {"client_id": "new-client-id", "client_secret": "rotated-secret"}
    )

    assert session.commits == 1
    assert integration.status == "configured"
    assert integration.status not in token_refresh._SWEEP_STATUSES
    assert (integration.access_token_enc, integration.refresh_token_enc) == (None, None)
    assert fake_redis.method_calls == []


# ---------------------------------------------------------------------------
# A new authorization is the one way back to connected
# ---------------------------------------------------------------------------


class _GitHubInstallDB:
    """Answers the OAuth-default lookup, then every integration lookup with ``target``."""

    def __init__(self, target):
        self.target = target
        self.lookups = 0
        self.commits = 0

    async def execute(self, _statement):
        self.lookups += 1
        if self.lookups == 1:
            default = SimpleNamespace(client_id_enc="gh-client-id", client_secret_enc="gh-client-secret")
            return SimpleNamespace(scalar_one_or_none=lambda: default)
        target = self.target
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: target, all=lambda: [target]))

    def in_transaction(self):
        return False

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


class _GitHubClient:
    """The code exchange, then a GitHub for which every token is one account that sees the installation."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, *_args, **_kwargs):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 28800},
        )

    async def get(self, url, *_args, **_kwargs):
        if str(url).endswith("/user"):
            return SimpleNamespace(is_success=True, json=lambda: {"id": 7})
        return SimpleNamespace(is_success=True, json=lambda: {"installations": [{"id": 4242, "account": {}}]})


async def test_a_github_app_install_started_from_github_reconnects_nothing(monkeypatch):
    """Not an authorization: the installer's token is stored nowhere, so
    a row waiting for a new consent stays marked and keeps its back-off."""
    integration = _loaded_integration("reauthorize")
    for key, value in {
        "provider": "github",
        "auth_type": "oauth",
        "client_id_enc": "gh-client-id",
        "extra_config_enc": json.dumps({"installations": [{"id": "1"}]}),
    }.items():
        set_committed_value(integration, key, value)
    fake_redis = AsyncMock()
    monkeypatch.setattr(oauth_callbacks.httpx, "AsyncClient", _GitHubClient)
    monkeypatch.setattr(oauth_callbacks, "decrypt_value", lambda value: value)
    monkeypatch.setattr(oauth_callbacks, "decrypt_json", json.loads)
    monkeypatch.setattr(oauth_callbacks, "encrypt_value", lambda value: f"enc:{value}")

    async def _stored_as_plain_text(entity, column):
        # The OAuth default's client secret is read through the vault resolver.
        return getattr(entity, column)

    monkeypatch.setattr(oauth_callbacks, "reveal_field", _stored_as_plain_text)
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=fake_redis))
    db = _GitHubInstallDB(integration)

    await oauth_callbacks._handle_github_app_install("the-code", "4242", db)

    assert integration.status == "reauthorize"
    assert (integration.access_token_enc, integration.refresh_token_enc) == ("old-access", "old-refresh")
    assert fake_redis.method_calls == []


@pytest.mark.parametrize(
    "verdict",
    [
        pytest.param(_response(401, {"error": "invalid_client"}), id="401-invalid_client"),
        *_PERMANENT_REFUSALS_INSIDE_A_2XX,
    ],
)
async def test_after_a_verdict_only_the_new_authorization_reconnects(monkeypatch, verdict):
    """The whole life of a dead grant: marked, left alone by every refresh, back on a new consent."""
    integration = _integration(status="connected")

    ok, _ = await _sweep_refresh(integration, response=verdict)
    assert ok is False
    assert integration.status == "reauthorize"

    # Hours later, chat turns keep using it: no refresh, still marked.
    for _ in range(3):
        result = await _chat_refresh(integration, _response(200, {"access_token": "new"}))
        assert result.client.posts == 0
    assert integration.status == "reauthorize"

    # The user completes the OAuth flow again.
    row = _loaded_integration("reauthorize")

    async def _exchange(_integration, _code, *, code_verifier=None):
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}

    monkeypatch.setattr(oauth_callbacks, "exchange_code_for_tokens_with_integration", _exchange)
    monkeypatch.setattr(oauth_callbacks, "encrypt_value", lambda value: f"enc:{value}")
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=MagicMock(delete=AsyncMock())))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)),
        get=AsyncMock(return_value=None),
        commit=AsyncMock(),
    )
    await oauth_callbacks._handle_integration_callback(
        {"integration_id": str(row.id), "user_id": str(uuid.uuid4()), "provider": "notion"},
        "the-code",
        db,
    )

    assert row.status == "connected"
    assert row.status in token_refresh._SWEEP_STATUSES
