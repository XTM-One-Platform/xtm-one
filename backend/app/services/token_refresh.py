# SPDX-License-Identifier: Apache-2.0

"""OAuth token refresh service.

Periodically checks integrations and MCP servers with expiring access
tokens and refreshes them using the stored refresh_token + client
credentials.  Called from the autonomous scheduler.

Performance: uses a shared httpx client and bounded concurrency
(asyncio.Semaphore) to refresh tokens in parallel without overwhelming
OAuth providers.  With 200+ users each having multiple integrations,
sequential refresh could block the scheduler for minutes.

Rotating refresh tokens (Atlassian Jira):
  Atlassian issues a **new** refresh token on every exchange and disables
  the old one.  To prevent concurrent refreshes from invalidating each
  other, we acquire a short-lived Redis lock per integration before
  calling the token endpoint.  The inline-refresh path in
  ``integration_tools.py`` uses the same lock key.  Under the lock both
  paths refresh from the row as stored now, not from the copy they read
  earlier, and the result is committed before the lock is released:
  presenting a token another refresh already spent is answered like a
  dead grant.  The outcome is applied under a row lock, and only while the
  stored tokens are still the ones the refresh started from, so neither a
  re-authorization (which takes no refresh lock) nor a refresh that outlived
  its Redis lock is ever overwritten.

MCP servers get the same treatment under a lock of their own
(``token_refresh:mcp:<id>``): a server registered through dynamic client
registration is a public client, whose authorization server rotates the
refresh token and may revoke the whole grant when a spent one comes back.
A connection to an MCP server also asks for a refresh itself, under the same
lock and guards, when the token it holds is about to expire or was answered
with a ``401`` (:func:`current_mcp_access_token`): with a 10-minute
token, a connection opened before the sweep's refresh would otherwise keep
sending the old one.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.egress import egress_client, egress_route
from app.core.redis import redis_client
from app.core.security import decrypt_value, encrypt_value
from app.integrations.granted_scopes import record_granted_scopes
from app.integrations.service_token_exchange import (
    oauth_exchange_spec,
    read_service_token_answer,
    refresh_request,
    service_base_url,
)
from app.integrations.token_answer import MalformedTokenAnswerError
from app.integrations.token_endpoint import (
    TokenEndpointRefusedError,
    assert_token_endpoint_allowed,
)
from app.models.integration import Integration
from app.models.integration_user_credential import IntegrationUserCredential
from app.models.mcp_server import MCPServer
from app.models.mcp_server_user_credential import MCPServerUserCredential
from app.services.integration_status import record_status_reason
from app.services.integration_user_credentials import (
    holder_ref,
    integration_of,
    load_token_holder,
)
from app.services.mcp_user_credentials import (
    PER_USER,
    MCPTokenHolder,
    is_per_user,
    load_mcp_holder,
    mcp_holder_key,
    server_of,
)

logger = logging.getLogger(__name__)

# Max concurrent HTTP requests to OAuth providers during token refresh.
# Prevents hammering a single provider while still refreshing in parallel.
_REFRESH_CONCURRENCY = 10

# Lock TTL for preventing concurrent refresh of the same integration.  It
# should outlast a whole locked refresh (stored-row read, token request with a
# 30 s timeout per phase, commit) so two refreshes rarely present the same
# rotating token.  One that outlives it cannot overwrite what another stored:
# outcomes are only applied while the stored tokens are unchanged.
REFRESH_LOCK_TTL = 90  # seconds

# Atomic compare-and-delete: a refresh whose lock lapsed must not delete the
# lock another refresh acquired since.
_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# Integration statuses the refresh sweep works on (``reauthorize`` rows wait for
# the user to complete the OAuth flow again) and how far ahead of expiry it
# refreshes an access token.
_SWEEP_STATUSES = ("connected", "error")
_REFRESH_AHEAD = timedelta(minutes=15)

# MCP server OAuth statuses the sweep works on: a ``disconnected`` server waits
# for a new authorization.
_MCP_SWEEP_STATUSES = ("connected", "error")

# An MCP server's token is refreshed once this share of its lifetime has passed:
# ``_REFRESH_AHEAD`` alone refreshed a 10-minute token on every tick,
# since it is within 15 minutes of its expiry as soon as it is issued. Never
# more often than every ``_MCP_MIN_REFRESH_INTERVAL``, and never later than
# ``_REFRESH_AHEAD`` before the expiry.
_MCP_REFRESH_AT_LIFETIME_SHARE = 0.5
_MCP_MIN_REFRESH_INTERVAL = timedelta(minutes=2)

# The shortest back-off after a transient MCP refresh failure: the back-off is
# bounded by the token's remaining lifetime, so a short-lived token is
# retried before it expires instead of after a 15-minute outage.
_MCP_MIN_TRANSIENT_FAIL_COOLDOWN = 30  # seconds

# Providers that use rotating refresh tokens and require JSON body
# (no extra ``scope`` param) for the token refresh endpoint.
_JSON_REFRESH_PROVIDERS = frozenset({"jira"})

# Cooldown period after an auth error (401/403 with invalid refresh token).
# Prevents hammering the OAuth provider every scheduler tick.
_AUTH_FAIL_COOLDOWN = 3600  # 1 hour

# Cooldown period after a transient/unclassified refresh failure (network
# timeout, DNS error, 5xx, malformed response, ...).  Without it, a failing
# integration was retried on EVERY scheduler tick (~60s), producing an
# endless stream of warnings and hammering the provider.
_TRANSIENT_FAIL_COOLDOWN = 900  # 15 minutes

# Microsoft Entra ID error codes that mean the refresh token can NEVER work
# again without the user re-authorizing.  Matched as substrings of the token
# endpoint's error body, as a fallback for responses where the OAuth
# ``error`` field is not ``invalid_grant``.
#   - AADSTS50173: grant expired because it was revoked (password change, ...)
#   - AADSTS50057: user account is disabled
#   - AADSTS700003: device object was not found in the tenant
#   - AADSTS9002313: request malformed/invalid — a stored refresh token that
#     produces a malformed request is corrupted and will never be accepted
_TERMINAL_AADSTS_CODES = (
    "AADSTS50173",
    "AADSTS50057",
    "AADSTS700003",
    "AADSTS9002313",
)

# Some providers (Atlassian Jira observed in production) report a permanently
# dead refresh token under ``unauthorized_client`` / ``invalid_client`` rather
# than RFC 6749's ``invalid_grant``, or send ``invalid_grant`` with a 401/403
# instead of a 400 (Auth0-based servers: "Unknown or invalid refresh token.").
# Treat these only when the body carries an explicit refresh-token terminality
# marker, so a bad client secret or other client-authentication issue is not
# misclassified as re-authorization.
_REFRESH_TOKEN_ERROR_CODES = ("invalid_grant", "invalid_client", "unauthorized_client")
_TERMINAL_REFRESH_TOKEN_MARKERS = (
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

# GitHub and Slack refuse a refresh inside a success: GitHub's token endpoint
# (``POST https://github.com/login/oauth/access_token``) answers with ``error``
# / ``error_description`` fields, form-encoded unless the request asks for JSON
# (``build_token_request`` does), and every Slack Web API answer is a JSON
# object whose ``error`` names the failure (``"ok": false``). On a 2xx that
# carries no access token, only these exact codes are a verdict; any other
# ``error`` there (Slack's ``ratelimited``, ``internal_error``,
# ``service_unavailable``, ... or an unknown code) is retried. Neither provider
# documents them on another status, so they are matched on a 2xx only.
#   - GitHub ``bad_refresh_token``: "the refresh token that you specified is
#     invalid or expired", resolved by a new web application flow (docs.github.com,
#     "Generating a user access token for a GitHub App" > Troubleshooting).
#   - Slack ``invalid_refresh_token``: "The given refresh token is invalid"
#     (docs.slack.dev, ``oauth.v2.access`` > Errors).
_DEAD_REFRESH_TOKEN_CODES_INSIDE_A_SUCCESS = ("bad_refresh_token", "invalid_refresh_token")
# The client credentials, on the same two pages: GitHub
# ``incorrect_client_credentials`` ("the client_id or client_secret that you
# specify are incorrect"), Slack ``invalid_client_id`` / ``bad_client_secret``
# ("Value passed for client_id / client_secret was invalid").
_REJECTED_CLIENT_CODES_INSIDE_A_SUCCESS = ("incorrect_client_credentials", "invalid_client_id", "bad_client_secret")


def format_refresh_error(e: BaseException) -> str:
    """Format an exception for a refresh-failure log line, never empty.

    Many exception classes stringify to an empty message (bare
    ``asyncio.TimeoutError``, several httpx transport errors, ...), which
    produced useless "Failed to refresh token for X (provider): " log lines
    with no reason at all.  Fall back to ``repr`` so the exception class
    name is always present.
    """
    msg = str(e)
    return msg if msg.strip() else repr(e)


def is_terminal_refresh_failure(status_code: int, body: str) -> bool:
    """Whether a token-endpoint error response is permanent.

    RFC 6749 §5.2: a ``400`` with ``error=invalid_grant`` means the refresh
    token is expired, revoked or otherwise invalid — the grant is dead and
    retrying can never succeed; only a new user authorization fixes it.
    Some providers also spell the same condition differently (for example
    Atlassian's ``401/403 unauthorized_client`` when the refresh token was
    revoked), so on a 400/401/403 an explicit refresh-token terminality marker
    is recognized too.
    Some Microsoft Entra ID failures surface terminal conditions under other
    error codes, so the known-dead AADSTS codes are matched as a fallback, on a
    400 only: a 5xx or a 429 that quotes an AADSTS code is an outage.
    """
    body_text = body or ""
    body_lower = body_text.lower()
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        err = str(payload.get("error", "")).lower()
        if err == "invalid_grant" and status_code == 400:
            return True
        detail = f"{str(payload.get('error_description', '')).lower()}\n{body_lower}"
        if status_code in (400, 401, 403) and err in _REFRESH_TOKEN_ERROR_CODES and _names_a_dead_refresh_token(detail):
            return True
    else:
        # Plain text, a JSON value that is not an object, or a form-encoded
        # answer (``error_description=Token+was+globally+revoked``), decoded so
        # the marker matches the way it reads. A code is matched as a whole
        # word: Slack's ``invalid_client_id`` is not ``invalid_client``.
        text = urllib.parse.unquote_plus(body_text).lower()
        if status_code == 400 and _PLAIN_INVALID_GRANT_RE.search(text):
            return True
        if (
            status_code in (400, 401, 403)
            and _PLAIN_REFRESH_TOKEN_ERROR_CODE_RE.search(text)
            and _names_a_dead_refresh_token(text)
        ):
            return True
    return status_code == 400 and any(code.lower() in body_lower for code in _TERMINAL_AADSTS_CODES)


_PLAIN_INVALID_GRANT_RE = re.compile(r"\binvalid_grant\b")
_PLAIN_REFRESH_TOKEN_ERROR_CODE_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, _REFRESH_TOKEN_ERROR_CODES)) + r")\b")


def _names_a_dead_refresh_token(text: str) -> bool:
    return any(marker in text for marker in _TERMINAL_REFRESH_TOKEN_MARKERS)


def is_client_credentials_rejected(status_code: int, body: str) -> bool:
    """Whether the token endpoint refused the OAuth *client* credentials.

    RFC 6749 §5.2: ``invalid_client`` means client authentication failed —
    Notion answers ``401 invalid_client`` when the stored client secret no
    longer matches its app. The same credentials authenticate the
    authorization-code exchange, so neither a retry nor a new consent can work
    until the integration's client ID / secret are updated. A dead refresh
    token reported under ``invalid_client`` is caught first by
    :func:`is_terminal_refresh_failure`.
    """
    if status_code not in (400, 401):
        return False
    body_text = body or ""
    try:
        err_json = json.loads(body_text)
    except json.JSONDecodeError:
        return bool(_PLAIN_INVALID_CLIENT_RE.search(body_text))
    if not isinstance(err_json, dict):
        return False
    return str(err_json.get("error", "")).lower() == "invalid_client"


_PLAIN_INVALID_CLIENT_RE = re.compile(r"\berror\s*=\s*invalid_client\b", re.IGNORECASE)

_DEAD_GRANT_REASON = (
    "the OAuth grant is permanently invalid "
    "(invalid_grant / revoked or expired refresh token)"
)
_CLIENT_REJECTED_REASON = (
    "the token endpoint rejected the OAuth client credentials (invalid_client): "
    "update the integration's client ID / client secret first, then re-authorize"
)


async def rotated_client_secret(integration: object, used: str, response: httpx.Response) -> str | None:
    """The client secret read again from its vault after the token endpoint refused the client.

    A secret rotated in the vault while its old value was still cached is
    refused as the client (``invalid_client``, or GitHub's / Slack's client
    codes inside a 2xx - the refusals :func:`permanent_refresh_failure_reason`
    calls ``_CLIENT_REJECTED_REASON``): dropped from the cache and read again,
    a changed value earns the refresh one more try (scheduled and inline
    alike) before that verdict is final. ``None`` for any other answer, a
    dead refresh grant included, or a secret that reads the same (a stored
    value). A vault that cannot be read again raises
    :class:`~app.services.vault.SecretUnavailableError`: the refusal may be
    the rotation it would show, so the caller backs off as for any transient
    failure instead of applying the verdict.
    """
    from app.services.vault import invalidate_entity_secrets, reveal_field

    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        return None
    try:
        body = str(response.text)
    except Exception:
        return None
    if permanent_refresh_failure_reason(status, body) != _CLIENT_REJECTED_REASON:
        return None
    await invalidate_entity_secrets(integration, ("client_secret_enc",))
    fresh = await reveal_field(integration, "client_secret_enc") or ""
    return fresh if fresh and fresh != used else None


def permanent_refresh_failure_reason(status_code: int, body: str) -> str | None:
    """Why a token-endpoint answer can never succeed on retry, or ``None``.

    Shared by the scheduled sweep and the chat-time refresh
    (``integration_tools._do_inline_refresh``) so both stop on the same answers.
    A 2xx is read by :func:`_refused_inside_a_success_reason`.
    """
    if 200 <= status_code < 300:
        return _refused_inside_a_success_reason(body)
    if is_terminal_refresh_failure(status_code, body):
        return _DEAD_GRANT_REASON
    if is_client_credentials_rejected(status_code, body):
        return _CLIENT_REJECTED_REASON
    return None


def service_refresh_failure_reason(status_code: int, body: str) -> str | None:
    """:func:`permanent_refresh_failure_reason` for a service's own refresh (``oauth_exchange``).

    That request carries the refresh token alone, no client credentials: a
    401 / 403 can only refuse the token, so it is a dead grant (SimpleJWT's
    ``token_not_valid`` matches no OAuth marker), and a client verdict
    (``invalid_client``) cannot apply.
    """
    if status_code in (401, 403):
        return _DEAD_GRANT_REASON
    reason = permanent_refresh_failure_reason(status_code, body)
    return None if reason == _CLIENT_REJECTED_REASON else reason


def _refused_inside_a_success_reason(body: str) -> str | None:
    """The verdict on a refusal inside a 2xx: one documented code, exactly, and no access token.

    The body is a JSON object or, otherwise, read as form-encoded (GitHub's
    default). An ``error`` sent more than once, in either form, is no verdict.
    """
    try:
        fields = json.loads(body or "", object_pairs_hook=_every_value_of_each_key)
    except json.JSONDecodeError:
        fields = None
    if not isinstance(fields, dict):
        fields = urllib.parse.parse_qs(body or "", keep_blank_values=True)
    access_tokens, errors = fields.get("access_token", []), fields.get("error", [])
    if any(isinstance(token, str) and token.strip() for token in access_tokens):
        return None
    if len(errors) != 1 or not isinstance(errors[0], str):
        return None
    code = errors[0].lower()
    if code in _DEAD_REFRESH_TOKEN_CODES_INSIDE_A_SUCCESS:
        return _DEAD_GRANT_REASON
    if code in _REJECTED_CLIENT_CODES_INSIDE_A_SUCCESS:
        return _CLIENT_REJECTED_REASON
    return None


def _every_value_of_each_key(pairs: list[tuple[str, object]]) -> dict[str, list[object]]:
    """A JSON object as ``parse_qs`` reads a form: ``json.loads`` alone keeps only a repeated key's last value."""
    fields: dict[str, list[object]] = {}
    for key, value in pairs:
        fields.setdefault(key, []).append(value)
    return fields


def usable_token_answer(payload: object) -> dict:
    """The token endpoint's 2xx answer, when it is a refresh.

    RFC 6749 section 5.1: a successful answer is a JSON object carrying the new
    ``access_token``. ``{}``, an ``error`` inside a 200 or a non-object body is
    a malformed answer, which no refresh path records as a refresh: each backs
    off, or applies the verdict :func:`read_token_answer` finds in it.
    """
    if not isinstance(payload, dict):
        raise MalformedTokenAnswerError("the token endpoint's answer is not a JSON object")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        error = payload.get("error")
        raise MalformedTokenAnswerError(
            "the token endpoint answered without an access token"
            + (f" (error: {str(error)[:100]})" if error else ""),
            refused=bool(error),
        )
    if not isinstance(payload.get("refresh_token") or "", str):
        raise MalformedTokenAnswerError("the token endpoint's refresh token is not a string")
    return payload


def read_token_answer(resp: httpx.Response) -> dict:
    """:func:`usable_token_answer` of the token endpoint's 2xx answer ``resp``.

    When it is not a refresh, the error carries the classifier's ``reason``,
    read off the body as sent: a form-encoded answer is not JSON.
    """
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    try:
        return usable_token_answer(payload)
    except MalformedTokenAnswerError as exc:
        reason = permanent_refresh_failure_reason(resp.status_code, resp.text)
        if reason is None:
            raise
        raise MalformedTokenAnswerError(str(exc), reason=reason) from None


def _refused_inside_a_success(exc: BaseException) -> bool:
    return isinstance(exc, MalformedTokenAnswerError) and exc.refused


def _permanent_refusal_inside_a_success(exc: BaseException) -> str | None:
    return exc.reason if isinstance(exc, MalformedTokenAnswerError) else None


def refresh_cooldown_key(integration_id: object) -> str:
    """Redis key holding a failing integration's refresh back-off."""
    return f"token_refresh_cooldown:{integration_id}"


def mcp_refresh_cooldown_key(server_id: object) -> str:
    """Redis key holding a failing MCP server's refresh back-off.

    Distinct ``mcp:`` prefix: MCP servers and integrations live in different
    tables, so a shared prefix could in principle collide.
    """
    return f"token_refresh_cooldown:mcp:{server_id}"


def mcp_refresh_lock_id(server_id: object) -> str:
    """The id :func:`acquire_refresh_lock` takes for an MCP server (``token_refresh:mcp:<id>``).

    Distinct ``mcp:`` prefix, as for :func:`mcp_refresh_cooldown_key`.
    """
    return f"mcp:{server_id}"


# A back-off is learned from one refresh token: its value names that token (a
# digest of the stored ciphertext, never the token) and it only holds a refresh
# back while the row still stores it, so a back-off a stale refresh starts after
# a re-authorization never applies to the new grant. ``"1"``, written by older
# workers, applies to any token.
_UNBOUND_COOLDOWN = "1"


def _cooldown_value(refresh_token_enc: str | None) -> str:
    return hashlib.sha256((refresh_token_enc or "").encode()).hexdigest()[:16]


async def start_refresh_cooldown(
    key: str, refresh_token_enc: str | None, seconds: int, *, only_if_absent: bool = False
) -> None:
    """Back off refreshing ``refresh_token_enc`` for ``seconds``."""
    redis = await redis_client()
    await redis.set(key, _cooldown_value(refresh_token_enc), ex=seconds, nx=only_if_absent)


async def refresh_cooling_down(key: str, refresh_token_enc: str | None) -> bool:
    """Whether a back-off holds back a refresh of ``refresh_token_enc``."""
    redis = await redis_client()
    value = await redis.get(key)
    return value is not None and value in (_UNBOUND_COOLDOWN, _cooldown_value(refresh_token_enc))


async def clear_refresh_cooldown(key: str) -> None:
    """Drop a refresh back-off once the user re-authorized (best effort).

    A back-off started against the previous grant would otherwise keep both
    refresh paths off the fresh one for up to an hour.
    """
    try:
        redis = await redis_client()
        await redis.delete(key)
    except Exception as exc:
        logger.warning("Could not clear the refresh cooldown %s: %s", key, format_refresh_error(exc))


def stored_tokens(
    integration: Integration | IntegrationUserCredential | MCPServer,
) -> tuple[str | None, str | None]:
    """The ``(access, refresh)`` token ciphertexts a refresh starts from."""
    return (integration.access_token_enc, integration.refresh_token_enc)


async def lock_stored_tokens(
    db: AsyncSession, integration: Integration | IntegrationUserCredential
) -> tuple[str | None, str | None]:
    """Row-lock ``integration`` and read its tokens and status again, as committed now.

    Awaited right before a refresh outcome is applied, in the transaction that
    commits it: a re-authorization or another refresh that stored tokens since
    the refresh read the row shows here, and one that lands later waits for
    this commit.
    """
    await db.refresh(integration, ["access_token_enc", "refresh_token_enc", "status"], with_for_update=True)
    return stored_tokens(integration)


async def lock_stored_mcp_tokens(db: AsyncSession, srv: MCPTokenHolder) -> tuple[str | None, str | None]:
    """:func:`lock_stored_tokens` for an MCP token holder (a server or a person's credential), whose status is ``oauth_status``."""
    await db.refresh(srv, ["access_token_enc", "refresh_token_enc", "oauth_status"], with_for_update=True)
    return stored_tokens(srv)


def _mark_needs_reauthorization(integration: Integration | IntegrationUserCredential, reason: str) -> None:
    """Mark an integration's OAuth grant as permanently dead.

    ``status = "reauthorize"`` is already understood by the frontend
    (``INTEGRATION_STATUS.reauthorize`` renders the amber "Re-authorization
    required" badge and the header keeps its Re-authorize button), and the
    refresh sweep only selects ``connected``/``error`` rows — so this both
    surfaces the problem to the user and removes the integration from the
    refresh loop until the OAuth callback resets it to ``connected``.
    """
    record_status_reason(integration, "reauthorize", f"re-authorization required: {reason}")
    logger.warning(
        "Marking integration %s (%s) as needing re-authorization: %s. "
        "Automatic token refresh is suspended until the user re-authorizes.",
        integration.name,
        integration.provider,
        reason,
    )


async def _token_endpoint_allowed(token_url: str, *, what: str) -> bool:
    """Check the destination before a refresh POST; on refusal, say why."""
    try:
        await assert_token_endpoint_allowed(token_url)
    except TokenEndpointRefusedError as exc:
        logger.warning("Refusing to refresh the OAuth token of %s: %s", what, exc)
        return False
    return True


async def _back_off_refused_token_url(cooldown_key: str) -> None:
    """Back off a refused token URL, once the stored tokens were confirmed unchanged.

    A refused token URL is a configuration error, not a transient failure: it
    takes the long cooldown so the sweep does not re-check it every tick, and
    the back-off is unbound. It is written only after the row-locked check, so
    a re-authorization, which clears it after its own commit, is never undone
    by a check about the tokens it replaced.
    """
    redis = await redis_client()
    await redis.set(cooldown_key, _UNBOUND_COOLDOWN, ex=_AUTH_FAIL_COOLDOWN)


async def acquire_refresh_lock(integration_id: str) -> str | None:
    """Try to acquire a Redis lock for refreshing a specific integration.

    Returns the holder token to release it with, or None if another refresh
    is already in progress.  The lock auto-expires after REFRESH_LOCK_TTL
    seconds as a safety net.
    """
    token = uuid.uuid4().hex
    redis = await redis_client()
    acquired = await redis.set(
        f"token_refresh:{integration_id}",
        token,
        nx=True,
        ex=REFRESH_LOCK_TTL,
    )
    return token if acquired else None


async def release_refresh_lock(integration_id: str, token: str) -> None:
    """Release the Redis refresh lock for an integration, if ``token`` still holds it."""
    redis = await redis_client()
    await redis.eval(_RELEASE_LOCK_SCRIPT, 1, f"token_refresh:{integration_id}", token)


async def _refresh_one_integration(
    integration: Integration | IntegrationUserCredential,
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> bool:
    """Refresh a single token holder (an integration, or a person's credential on
    one).  Returns True on success."""

    if not integration.client_id_enc or not integration.token_url:
        return False

    # The holder's own lock and back-off: a person's refresh never holds back
    # the integration's, or another person's.
    ref = holder_ref(integration)

    # Check the failure cooldown BEFORE taking a concurrency slot so
    # integrations that are backing off don't occupy semaphore slots and
    # serialize the real refresh attempts.
    if await refresh_cooling_down(refresh_cooldown_key(ref), integration.refresh_token_enc):
        return False

    async with sem:
        # Acquire a per-integration lock to prevent concurrent refreshes
        # (critical for providers with rotating refresh tokens like Jira).
        lock = await acquire_refresh_lock(ref)
        if lock is None:
            logger.debug(
                "Skipping refresh for %s (%s): another refresh in progress",
                integration.name,
                integration.provider,
            )
            return False

        try:
            return await _refresh_stored_integration(ref, http)
        finally:
            await release_refresh_lock(ref, lock)


def _refresh_still_due(integration: Integration | IntegrationUserCredential) -> bool:
    """Whether the stored row still needs the refresh the sweep selected it for."""
    if integration.auth_type != "oauth" or integration.status not in _SWEEP_STATUSES:
        return False
    if not integration.refresh_token_enc or not integration.client_id_enc or not integration.token_url:
        return False
    expires_at = integration.token_expires_at
    return expires_at is None or expires_at < datetime.now(UTC) + _REFRESH_AHEAD


async def _refresh_stored_integration(integration_id: object, http: httpx.AsyncClient) -> bool:
    """Refresh a token holder from its stored row, holding its refresh lock.

    *integration_id* is the holder's reference (``integration_user_credentials.
    holder_ref``): an integration's id, or a person's credential on one.

    The sweep selected its rows before the refresh gather, and a chat turn,
    another worker's sweep or a re-authorization can store newer tokens in the
    meantime. The row is read again here, so a rotating refresh token another
    refresh already spent is never presented, and the result is committed
    before the lock is released, so the next holder reads the new token.
    """
    from app.core.database import async_session, release_db_connection_before_loop
    from app.services.relays import integration_egress_route

    async with async_session() as db:
        integration = await load_token_holder(db, str(integration_id))
        if integration is None or not _refresh_still_due(integration):
            return False
        seen = stored_tokens(integration)
        route = await integration_egress_route(db, integration_of(integration))
        service_exchange = await oauth_exchange_spec(db, integration.provider)
        # No pooled connection is held across the token request.
        await release_db_connection_before_loop(db)

        async def _tokens_unchanged() -> bool:
            return await lock_stored_tokens(db, integration) == seen

        with egress_route(route):
            ok = await _do_refresh_integration(
                integration,
                http,
                tokens_unchanged=_tokens_unchanged,
                service_exchange=service_exchange,
            )
        await db.commit()
        return ok


async def _tokens_still_stored(
    integration: Integration | IntegrationUserCredential, tokens_unchanged: Callable[[], Awaitable[bool]] | None
) -> bool:
    if tokens_unchanged is None or await tokens_unchanged():
        return True
    logger.info(
        "Ignoring the refresh answer for %s (%s): its tokens were replaced while the refresh was in flight",
        integration.name,
        integration.provider,
    )
    return False


async def _do_refresh_integration(
    integration: Integration | IntegrationUserCredential,
    http: httpx.AsyncClient,
    *,
    tokens_unchanged: Callable[[], Awaitable[bool]] | None = None,
    service_exchange: dict | None = None,
) -> bool:
    """Execute the actual token refresh HTTP call for an integration.

    ``tokens_unchanged`` is awaited right before any outcome is applied: it
    row-locks the stored integration in the transaction that commits the
    outcome and says whether it still stores the tokens this call started
    from. When a re-authorization or another refresh replaced them meanwhile,
    the answer was about spent tokens, so nothing is applied.

    ``service_exchange`` is the bundle's ``oauth_exchange`` block when the
    service issued the tokens (``integrations/service_token_exchange.py``):
    the refresh token goes to the service's refresh endpoint instead of the
    token URL, and its answer is read the service's way.

    *integration* is a token holder: an integration, or a person's credential
    on one, which reads the OAuth app from its integration and holds
    the tokens, expiry, status and back-off this refresh writes.
    """
    from app.integrations.oauth import (
        apply_oauth_token_expiry,
        build_token_request,
        provider_token_url,
        substitute_tenant,
    )
    from app.services.vault import SecretUnavailableError, reveal_field

    # The OAuth app is the integration's (its vault record too); the back-off
    # is the holder's own.
    app_row = integration_of(integration)
    cooldown_key = refresh_cooldown_key(holder_ref(integration))
    client_id = decrypt_value(integration.client_id_enc)
    try:
        # A service that refreshes its own tokens is never sent the client
        # secret: its vault is not read, so a vault outage does not stop it.
        client_secret = (
            ""
            if service_exchange is not None
            else await reveal_field(app_row, "client_secret_enc") or ""
        )
    except SecretUnavailableError:
        # The client secret's vault did not answer: not the grant's fault,
        # so the integration keeps its status and the next sweep tries again.
        logger.warning("Skipping refresh for %s (%s): its client secret could not be read from its vault", integration.name, integration.provider)
        return False
    refresh_token = decrypt_value(integration.refresh_token_enc)

    if not refresh_token or not refresh_token.strip():
        # A present-but-empty (or whitespace-only) refresh token can only
        # produce a malformed request (Microsoft rejects it with
        # AADSTS9002313) — don't even send it, the row needs a fresh
        # OAuth grant.
        if await _tokens_still_stored(integration, tokens_unchanged):
            _mark_needs_reauthorization(
                integration, "stored refresh token is empty"
            )
        return False

    params: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    is_json_provider = integration.provider in _JSON_REFRESH_PROVIDERS

    # Only include scope for providers that expect it in refresh requests.
    # Atlassian (Jira) does NOT accept a scope parameter during refresh
    # and requires a JSON body.
    if integration.scopes and not is_json_provider:
        params["scope"] = integration.scopes

    service_request: dict | None = None
    # The provider's host for this configuration (DocuSign's environment), as
    # the chat-time refresh resolves it; the Entra tenant substitution follows.
    token_url = await provider_token_url(app_row, integration.token_url)
    if service_exchange is not None:
        try:
            token_url, service_request = refresh_request(
                service_exchange, await service_base_url(integration), refresh_token
            )
        except ValueError as exc:
            logger.warning(
                "Cannot refresh %s (%s): %s",
                integration.name,
                integration.provider,
                exc,
            )
            # A configuration gap, not a refusal: back off like a transient
            # failure instead of warning on every sweep.
            await start_refresh_cooldown(
                cooldown_key,
                integration.refresh_token_enc,
                _TRANSIENT_FAIL_COOLDOWN,
            )
            return False
    elif token_url and "/common/" in token_url and integration.extra_config_enc:
        try:
            from app.services.vault import reveal_json_field

            # A tenant id held as a vault reference is resolved (#6080).
            extra = await reveal_json_field(integration, "extra_config_enc")
            tenant_id = extra.get("tenant_id") if isinstance(extra, dict) else None
            if tenant_id:
                token_url = substitute_tenant(token_url, tenant_id)
        except Exception as exc:
            logger.warning("Failed to extract tenant_id for token URL: %s", exc)

    if not await _token_endpoint_allowed(
        token_url or "",
        what=f"integration {integration.name} ({integration.provider})",
    ):
        if await _tokens_still_stored(integration, tokens_unchanged):
            record_status_reason(integration, "error", "token refresh refused: the token endpoint is not allowed")
            await _back_off_refused_token_url(cooldown_key)
        return False

    # Applies the provider-specific auth style: Notion needs HTTP Basic auth +
    # JSON body (rejects credentials-in-body with 401 invalid_client), Atlassian
    # needs a JSON body, everyone else uses a form-encoded body.
    request_kwargs = service_request or build_token_request(
        integration.provider,
        client_id,
        client_secret,
        params,
        json_provider=is_json_provider,
    )

    try:
        async with contextlib.AsyncExitStack() as stack:
            client = http
            if integration.provider == "openapi":
                # A user-supplied token URL: pinned connection, client certificate.
                from app.integrations.providers.openapi import token_client_options

                client = await stack.enter_async_context(
                    egress_client(**token_client_options(app_row))
                )
            resp = await client.post(token_url, timeout=30, **request_kwargs)
            # A rotated client secret only matters to the standard token request.
            if service_request is None and (
                retry_secret := await rotated_client_secret(app_row, client_secret, resp)
            ):
                retry_kwargs = build_token_request(
                    integration.provider, client_id, retry_secret, params, json_provider=is_json_provider
                )
                resp = await client.post(token_url, timeout=30, **retry_kwargs)
        resp.raise_for_status()
        tokens = (
            read_service_token_answer(service_exchange, resp)
            if service_exchange is not None
            else read_token_answer(resp)
        )
    except SecretUnavailableError:
        # The client was refused and its vault did not answer the read that
        # would show a rotation: not a verdict on the grant, so the
        # integration keeps its status and the next sweep tries again, as when
        # the first read fails.
        logger.warning(
            "Skipping refresh for %s (%s): its client secret was refused and could not be read again from its vault",
            integration.name,
            integration.provider,
        )
        return False
    except httpx.HTTPStatusError as e:
        # Full body for terminal detection (an ``invalid_grant`` / AADSTS
        # marker could sit past a truncation point); truncated for logging.
        body = ""
        with contextlib.suppress(Exception):
            body = e.response.text
        logger.warning(
            "Failed to refresh token for %s (%s): %s | body: %s",
            integration.name,
            integration.provider,
            format_refresh_error(e),
            body[:500],
            exc_info=True,
        )
        if not await _tokens_still_stored(integration, tokens_unchanged):
            return False
        # Permanent failures (a dead grant, or client credentials the token
        # endpoint refuses): no retry can ever succeed — mark the integration
        # as needing re-authorization and stop retrying it on every cycle
        # instead of hammering the token endpoint until the end of time.
        reason = (
            service_refresh_failure_reason(e.response.status_code, body)
            if service_exchange is not None
            else permanent_refresh_failure_reason(e.response.status_code, body)
        )
        if reason:
            _mark_needs_reauthorization(integration, reason)
            return False
        record_status_reason(
            integration, "error", f"token refresh failed: HTTP {e.response.status_code} from the token endpoint"
        )
        # Apply a cooldown so the scheduler doesn't retry a failing
        # integration on every tick: 1 hour for auth errors (401/403 —
        # possibly rotated client credentials), 15 minutes for anything
        # else (5xx, rate limiting, ...).
        cooldown = (
            _AUTH_FAIL_COOLDOWN
            if e.response.status_code in (401, 403)
            else _TRANSIENT_FAIL_COOLDOWN
        )
        await start_refresh_cooldown(cooldown_key, integration.refresh_token_enc, cooldown)
        logger.info(
            "Cooldown set for %s (%s): will retry in %ds after HTTP %d",
            integration.name,
            integration.provider,
            cooldown,
            e.response.status_code,
        )
        return False
    except Exception as e:
        # ``format_refresh_error`` (str-or-repr) rather than plain ``%s``:
        # bare timeouts and several httpx transport errors stringify to an
        # empty message, which produced "Failed to refresh token for X
        # (provider): " lines with no reason at all.
        logger.warning(
            "Failed to refresh token for %s (%s): %s",
            integration.name,
            integration.provider,
            format_refresh_error(e),
            exc_info=True,
        )
        if not await _tokens_still_stored(integration, tokens_unchanged):
            return False
        # A refusal inside a 2xx that no retry can overturn (GitHub, Slack):
        # the same verdict as a permanent error answer.
        reason = _permanent_refusal_inside_a_success(e)
        if reason:
            _mark_needs_reauthorization(integration, reason)
            return False
        record_status_reason(integration, "error", f"token refresh failed: {format_refresh_error(e)}")
        # Transient failure (network timeout, DNS, a malformed answer, ...):
        # back off instead of retrying on every scheduler tick.
        cooldown = _AUTH_FAIL_COOLDOWN if _refused_inside_a_success(e) else _TRANSIENT_FAIL_COOLDOWN
        await start_refresh_cooldown(cooldown_key, integration.refresh_token_enc, cooldown)
        return False

    if not await _tokens_still_stored(integration, tokens_unchanged):
        return False
    integration.access_token_enc = encrypt_value(tokens["access_token"])
    if tokens.get("refresh_token"):
        integration.refresh_token_enc = encrypt_value(tokens["refresh_token"])
    apply_oauth_token_expiry(integration, tokens)
    record_granted_scopes(integration, tokens, keep_when_absent=True)
    # ``reauthorize`` too: a refresh that outlived its lock may have recorded a
    # refusal of these same tokens meanwhile, and they just worked.
    if integration.status in ("error", "reauthorize"):
        integration.status = "connected"

    logger.info(
        "Refreshed OAuth token for integration %s (%s)",
        integration.name,
        integration.provider,
    )
    return True


async def _refresh_one_mcp(
    srv: MCPTokenHolder,
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> bool:
    """Refresh a single MCP token holder (a server, or a person's credential on one).  Returns True on success."""
    if not srv.client_id_enc or not srv.token_url:
        return False

    key = mcp_holder_key(srv)
    # Check the failure cooldown BEFORE taking a concurrency slot (same
    # rationale as ``_refresh_one_integration``).
    if await refresh_cooling_down(mcp_refresh_cooldown_key(key), srv.refresh_token_enc):
        return False

    async with sem:
        # A tick that outlasts the scheduler's tick marker lets another
        # worker's sweep select the same server: only one of them may present
        # its refresh token.
        lock_id = mcp_refresh_lock_id(key)
        lock = await acquire_refresh_lock(lock_id)
        if lock is None:
            logger.debug("Skipping refresh for MCP server %s: another refresh in progress", srv.name)
            return False

        try:
            return await _refresh_stored_mcp(key, http)
        finally:
            await release_refresh_lock(lock_id, lock)


def mcp_refresh_due_at(issued_at: datetime | None, expires_at: datetime | None) -> datetime | None:
    """When the sweep refreshes an MCP token received at ``issued_at`` and expiring at ``expires_at``.

    Once :data:`_MCP_REFRESH_AT_LIFETIME_SHARE` of its lifetime has passed, but
    no later than :data:`_REFRESH_AHEAD` before its expiry and no sooner than
    :data:`_MCP_MIN_REFRESH_INTERVAL` after it was received: a 10-minute
    token is refreshed 5 minutes after it was issued, a one-hour one 15 minutes
    before it expires, a 1-minute one (or one a skewed clock stamped as already
    expired) after 2 minutes, while a connection that uses it asks for its own
    refresh 60 s before its expiry (:func:`current_mcp_access_token`).
    ``None`` (an unknown expiry) is due now; an unknown ``issued_at`` (a token
    stored before it was recorded) is refreshed :data:`_REFRESH_AHEAD` ahead.
    """
    if expires_at is None:
        return None
    expires_at = _as_utc(expires_at)
    if issued_at is None:
        return expires_at - _REFRESH_AHEAD
    issued_at = _as_utc(issued_at)
    lifetime = expires_at - issued_at
    ahead = min(_REFRESH_AHEAD, lifetime * (1 - _MCP_REFRESH_AT_LIFETIME_SHARE))
    return max(expires_at - ahead, issued_at + _MCP_MIN_REFRESH_INTERVAL)


def _mcp_token_due(srv: MCPTokenHolder) -> bool:
    due_at = mcp_refresh_due_at(srv.token_issued_at, srv.token_expires_at)
    return due_at is None or due_at <= datetime.now(UTC)


def _mcp_refresh_still_due(srv: MCPTokenHolder) -> bool:
    """Whether the stored holder still needs the refresh the sweep selected it for."""
    if srv.auth_mode != "oauth" or srv.oauth_status not in _MCP_SWEEP_STATUSES:
        return False
    if isinstance(srv, MCPServer) and is_per_user(srv):
        # The row's own token is never used in per_user mode.
        return False
    if not srv.refresh_token_enc or not srv.client_id_enc or not srv.token_url:
        return False
    return _mcp_token_due(srv)


def mcp_transient_refresh_cooldown(expires_at: datetime | None) -> int:
    """Seconds an MCP server backs off after a transient refresh failure.

    Half the token's remaining lifetime, between
    :data:`_MCP_MIN_TRANSIENT_FAIL_COOLDOWN` and :data:`_TRANSIENT_FAIL_COOLDOWN`:
    one failure from the authorization server (a 5xx, a 429, a timeout) is
    retried while the token still works, instead of after it expired. An
    unknown expiry keeps the full back-off.
    """
    if expires_at is None:
        return _TRANSIENT_FAIL_COOLDOWN
    remaining = (_as_utc(expires_at) - datetime.now(UTC)).total_seconds()
    return int(min(_TRANSIENT_FAIL_COOLDOWN, max(_MCP_MIN_TRANSIENT_FAIL_COOLDOWN, remaining / 2)))


async def _refresh_stored_mcp(holder_key: object, http: httpx.AsyncClient) -> bool:
    """Refresh an MCP token holder from its stored row, holding its refresh lock.

    *holder_key* is :func:`mcp_holder_key`: a server's id, or a person's
    credential on a ``per_user`` server.

    As :func:`_refresh_stored_integration`: the row is read again on a session
    of its own, so a refresh token another sweep already spent is never
    presented, and the outcome is committed before the lock is released, so
    nothing is left for the sweep's final commit to write over a
    re-authorization.
    """
    from app.core.database import async_session, release_db_connection_before_loop
    from app.services.relays import mcp_server_egress_route

    async with async_session() as db:
        srv = await load_mcp_holder(db, holder_key)
        if srv is None or not _mcp_refresh_still_due(srv):
            return False
        seen = stored_tokens(srv)
        route = await mcp_server_egress_route(db, server_of(srv))
        # No pooled connection is held across the token request.
        await release_db_connection_before_loop(db)

        async def _tokens_unchanged() -> bool:
            # Discarding a dynamically registered client disconnects the server
            # but keeps its tokens: the answer is no longer the refresh's to
            # record either.
            tokens = await lock_stored_mcp_tokens(db, srv)
            return tokens == seen and srv.oauth_status in _MCP_SWEEP_STATUSES

        with egress_route(route):
            ok = await _do_refresh_mcp(srv, http, tokens_unchanged=_tokens_unchanged)
        await db.commit()
        return ok


async def _mcp_tokens_still_stored(
    srv: MCPTokenHolder, tokens_unchanged: Callable[[], Awaitable[bool]] | None
) -> bool:
    if tokens_unchanged is None or await tokens_unchanged():
        return True
    logger.info(
        "Ignoring the refresh answer for MCP server %s: it was re-authorized or disconnected "
        "while the refresh was in flight",
        srv.name,
    )
    return False


async def _do_refresh_mcp(
    srv: MCPTokenHolder,
    http: httpx.AsyncClient,
    *,
    tokens_unchanged: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Execute the token refresh HTTP call for an MCP server.

    ``tokens_unchanged`` is awaited right before any outcome is applied, as in
    :func:`_do_refresh_integration`; when the server was re-authorized,
    disconnected or refreshed by someone else meanwhile, nothing is applied.
    """
    from app.services.vault import SecretUnavailableError, reveal_field

    cooldown_key = mcp_refresh_cooldown_key(mcp_holder_key(srv))
    client_id = decrypt_value(srv.client_id_enc)
    try:
        # The OAuth client is the server's, a person's credential included.
        client_secret = await reveal_field(server_of(srv), "client_secret_enc") or ""
    except SecretUnavailableError:
        # The client secret's vault did not answer: not the grant's fault,
        # so the server keeps its status and the next sweep tries again.
        logger.warning("Skipping refresh for MCP server %s: its client secret could not be read from its vault", srv.name)
        return False
    refresh_token = decrypt_value(srv.refresh_token_enc)

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    # Omit the secret entirely for a public client (every dynamically
    # registered one). Sending an empty ``client_secret`` is a
    # client-authentication attempt, which an OAuth 2.1 server that issued
    # no secret answers with ``invalid_client`` — the refresh would fail
    # every time and park the server in "error".
    if client_secret:
        payload["client_secret"] = client_secret
    if srv.scopes:
        payload["scope"] = srv.scopes
    # RFC 8707: a refreshed token must stay bound to the same MCP server.
    if srv.oauth_resource:
        payload["resource"] = srv.oauth_resource

    if not await _token_endpoint_allowed(srv.token_url, what=f"MCP server {srv.name}"):
        if await _mcp_tokens_still_stored(srv, tokens_unchanged):
            srv.oauth_status = "error"
            await _back_off_refused_token_url(cooldown_key)
        return False

    try:
        resp = await http.post(
            srv.token_url,
            data=payload,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        tokens = read_token_answer(resp)
    except Exception as e:
        logger.warning(
            "Failed to refresh token for MCP server %s: %s",
            srv.name,
            format_refresh_error(e),
            exc_info=True,
        )
        if not await _mcp_tokens_still_stored(srv, tokens_unchanged):
            return False
        srv.oauth_status = "error"
        # Back off instead of retrying on every scheduler tick — the
        # same hot loop as integrations.  MCP servers have no
        # "reauthorize" state (the frontend union is disconnected /
        # connected / error), so permanent failures (the integrations'
        # classifier) and auth failures get the long cooldown instead of
        # being excluded from the sweep entirely. A transient failure backs
        # off for at most half the token's remaining lifetime.
        cooldown = mcp_transient_refresh_cooldown(srv.token_expires_at)
        if isinstance(e, httpx.HTTPStatusError):
            # Full body for terminal detection — truncating first could
            # hide an ``invalid_grant`` / AADSTS marker past the cutoff.
            body = ""
            with contextlib.suppress(Exception):
                body = e.response.text
            if e.response.status_code in (401, 403) or permanent_refresh_failure_reason(
                e.response.status_code, body
            ):
                cooldown = _AUTH_FAIL_COOLDOWN
        elif _refused_inside_a_success(e):
            # A refusal inside a 2xx, the classifier's permanent ones included.
            cooldown = _AUTH_FAIL_COOLDOWN
        await start_refresh_cooldown(cooldown_key, srv.refresh_token_enc, cooldown)
        return False

    if not await _mcp_tokens_still_stored(srv, tokens_unchanged):
        return False
    srv.access_token_enc = encrypt_value(tokens["access_token"])
    if tokens.get("refresh_token"):
        srv.refresh_token_enc = encrypt_value(tokens["refresh_token"])
    from app.integrations.oauth import apply_mcp_token_expiry

    apply_mcp_token_expiry(srv, tokens)
    # A refresh that outlived its lock may have recorded a refusal of these
    # same tokens meanwhile, and they just worked.
    if srv.oauth_status == "error":
        srv.oauth_status = "connected"

    logger.info("Refreshed OAuth token for MCP server %s", srv.name)
    return True


# ---------------------------------------------------------------------------
# An MCP server's token while a connection uses it
# ---------------------------------------------------------------------------
#: How close to its expiry a token a connection holds is replaced: by the stored
#: one when the sweep already refreshed it, by a refresh otherwise.
IN_USE_REFRESH_AHEAD = timedelta(seconds=60)
#: A refresh forced by a ``401`` happens at most once per server in this window:
#: a server that refuses every token answers the refreshed one with a ``401``
#: too, and each connection would otherwise refresh again.
_FORCED_REFRESH_WINDOW = 60  # seconds
#: How long a connection waits for a refresh another process holds the lock for.
_LOCK_WAIT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class MCPAccessToken:
    """An MCP server's access token, as a connection sends it."""

    value: str
    expires_at: datetime | None


def mcp_forced_refresh_key(server_id: object) -> str:
    """Redis key marking an MCP server's last refresh forced by a ``401``."""
    return f"token_refresh_forced:mcp:{server_id}"


def _as_utc(moment: datetime) -> datetime:
    # A stored ``token_expires_at`` is written in UTC; a naive one read back is UTC.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def token_expires_soon(expires_at: datetime | None, ahead: timedelta = IN_USE_REFRESH_AHEAD) -> bool:
    """Whether a token expiring at ``expires_at`` is due for replacement.

    The one reading of "about to expire" for a token a connection holds, shared
    by the connection's auth hook and the refresh it asks for. ``None`` is an
    unknown expiry: never due.
    """
    return expires_at is not None and _as_utc(expires_at) < datetime.now(UTC) + ahead


def _still_valid(token: MCPAccessToken | None) -> bool:
    return token is not None and not token_expires_soon(token.expires_at, timedelta(0))


def _sendable_token(srv: MCPTokenHolder | None, refused: str | None) -> MCPAccessToken | None:
    """The stored access token of ``srv``, unless it is ``refused`` or there is none.

    Never the token on the row of a ``per_user`` server: only a person's own
    credential is sent there.
    """
    if srv is None or srv.auth_mode != "oauth" or not srv.access_token_enc:
        return None
    if isinstance(srv, MCPServer) and is_per_user(srv):
        return None
    try:
        value = decrypt_value(srv.access_token_enc)
    except Exception:
        return None
    if not value or value == refused:
        return None
    return MCPAccessToken(value, srv.token_expires_at)


def _fresh(token: MCPAccessToken | None) -> bool:
    return token is not None and not token_expires_soon(token.expires_at)


async def current_mcp_access_token(server_id: object, *, refused: str | None = None) -> MCPAccessToken | None:
    """The access token a connection to MCP server ``server_id`` should send now.

    ``server_id`` is the token holder's key (:func:`mcp_holder_key`): the
    server's id, or a person's credential on a ``per_user`` server.

    The sweep (:func:`refresh_expiring_tokens`) refreshes a stored token ahead
    of its expiry, but a connection holds the token it was opened with. Its
    ``httpx.Auth`` hook (``services/mcp_oauth_tokens.py``) asks here when that
    token is about to expire (``refused`` is ``None``) or was answered with a
    ``401`` (``refused`` is that token), and gets:

    - the stored token, when it is not ``refused`` and not about to expire: the
      sweep or another connection refreshed it already;
    - otherwise a refresh, under the server's refresh lock and with the sweep's
      guards (the row read again under the lock, the outcome applied only while
      the stored tokens are the ones it started from, the failure back-off),
      then the token it stored. While another process holds the lock, a token
      still valid is sent as it is, and a refused one waits a bounded time for
      that refresh.

    The refresh runs to completion even if the connection is cancelled
    meanwhile (an anyio scope of the transport, a connect timeout, a stopped
    run): once the authorization server answered, the rotated refresh token
    exists nowhere else, and the lock is released however it ends. A refresh
    after a ``401`` happens at most once per server per
    :data:`_FORCED_REFRESH_WINDOW`, counted only when the token endpoint is
    asked. Returns ``None`` when there is no token worth sending: none stored,
    or a refused one nothing replaced.
    """
    from app.core.cancellation import run_to_completion
    from app.core.database import async_session

    async with async_session() as db:
        srv = await load_mcp_holder(db, server_id)
        current = _sendable_token(srv, refused)
        if _fresh(current):
            return current
        if srv is None or srv.oauth_status not in _MCP_SWEEP_STATUSES or not (
            srv.refresh_token_enc and srv.client_id_enc and srv.token_url
        ):
            return current

    locked, refreshed = await run_to_completion(_refresh_mcp_holding_lock(server_id, refused))
    if locked:
        return refreshed or current
    if refused is None and _still_valid(current):
        return current  # the lock holder is refreshing it
    return await _await_refresh_in_flight(server_id, refused) or current


async def _refresh_mcp_holding_lock(
    server_id: object, refused: str | None
) -> tuple[bool, MCPAccessToken | None]:
    """Take the server's refresh lock, refresh, release; whether the lock was free."""
    lock_id = mcp_refresh_lock_id(server_id)
    lock = await acquire_refresh_lock(lock_id)
    if lock is None:
        return False, None
    try:
        return True, await _refresh_mcp_in_use(server_id, refused)
    finally:
        await release_refresh_lock(lock_id, lock)


async def _await_refresh_in_flight(server_id: object, refused: str | None) -> MCPAccessToken | None:
    """The token another process's refresh stores, once it does (bounded wait)."""
    from app.core.database import async_session

    waited = 0.0
    while waited < _LOCK_WAIT_SECONDS:
        await asyncio.sleep(_LOCK_POLL_SECONDS)
        waited += _LOCK_POLL_SECONDS
        async with async_session() as db:
            token = _sendable_token(await load_mcp_holder(db, server_id), refused)
        if _fresh(token):
            return token
    return None


async def _claim_forced_refresh(server_id: object) -> bool:
    """Count a refresh a ``401`` forces against the server's window; whether it may run."""
    redis = await redis_client()
    return bool(await redis.set(mcp_forced_refresh_key(server_id), "1", ex=_FORCED_REFRESH_WINDOW, nx=True))


async def _refresh_mcp_in_use(server_id: object, refused: str | None) -> MCPAccessToken | None:
    """Refresh MCP server ``server_id`` for a connection, holding its refresh lock.

    Through the server's relay group when it is routed through one, as
    the sweep refreshes it: the hook runs in the transport's tasks, outside any
    route the connection was opened in, so the route is read from the row.
    """
    from app.core.database import async_session, release_db_connection_before_loop
    from app.services.relays import mcp_server_egress_route

    async with async_session() as db:
        srv = await load_mcp_holder(db, server_id)
        if srv is None or srv.oauth_status not in _MCP_SWEEP_STATUSES:
            return None
        if not srv.refresh_token_enc or not srv.client_id_enc or not srv.token_url:
            return None
        # Refreshed by the sweep or another connection while this one waited.
        current = _sendable_token(srv, refused)
        if _fresh(current):
            return current
        key = mcp_holder_key(srv)
        if await refresh_cooling_down(mcp_refresh_cooldown_key(key), srv.refresh_token_enc):
            return None
        # Counted here, right before the token endpoint is asked: a refusal
        # nothing could act on (the lock busy, a back-off, a gone row) does not
        # spend the window.
        if refused is not None and not await _claim_forced_refresh(key):
            return None
        seen = stored_tokens(srv)
        route = await mcp_server_egress_route(db, server_of(srv))
        await release_db_connection_before_loop(db)

        async def _tokens_unchanged() -> bool:
            tokens = await lock_stored_mcp_tokens(db, srv)
            return tokens == seen and srv.oauth_status in _MCP_SWEEP_STATUSES

        with egress_route(route):
            async with egress_client() as http:
                ok = await _do_refresh_mcp(srv, http, tokens_unchanged=_tokens_unchanged)
        await db.commit()
        return _sendable_token(srv, refused) if ok else None


async def refresh_expiring_tokens(db: AsyncSession) -> int:
    """Refresh OAuth tokens that are expired, expiring soon, or have unknown expiry.

    Covers:
      - Tokens that expire within the next 15 minutes; an MCP server's only
        once it is due by its lifetime (:func:`mcp_refresh_due_at`:
        half of it passed, at least 2 minutes after it was received)
      - Tokens that have already expired (token_expires_at in the past)
      - Tokens with no expiry tracked (token_expires_at is NULL) — selected
        once, then stamped with a 1-hour synthetic TTL by
        ``apply_oauth_token_expiry`` so they are not re-selected on every
        60s scheduler tick (Notion omits ``expires_in`` without token
        rotation)

    Uses a shared httpx client and bounded concurrency to avoid blocking the
    scheduler loop for minutes when many integrations need refreshing.

    Returns the number of tokens refreshed.
    """
    from sqlalchemy import or_

    threshold = datetime.now(UTC) + _REFRESH_AHEAD

    # ------------------------------------------------------------------
    # 1. Integration tokens
    # ------------------------------------------------------------------
    # ``status = "reauthorize"`` rows (terminal invalid_grant) are
    # intentionally NOT selected: their refresh token is permanently dead
    # and only a new user authorization (which resets the status to
    # "connected") can revive them.
    result = await db.execute(
        select(Integration).where(
            Integration.auth_type == "oauth",
            Integration.status.in_(["connected", "error"]),
            Integration.refresh_token_enc.isnot(None),
            or_(
                Integration.token_expires_at < threshold,
                Integration.token_expires_at.is_(None),
            ),
        )
    )
    expiring: list[Integration | IntegrationUserCredential] = list(result.scalars().all())

    # People's own credentials on per_user integrations: the same
    # selection; the OAuth app is read from their integration.
    credential_result = await db.execute(
        select(IntegrationUserCredential)
        .join(Integration, Integration.id == IntegrationUserCredential.integration_id)
        .where(
            Integration.auth_type == "oauth",
            IntegrationUserCredential.status.in_(["connected", "error"]),
            IntegrationUserCredential.refresh_token_enc.isnot(None),
            or_(
                IntegrationUserCredential.token_expires_at < threshold,
                IntegrationUserCredential.token_expires_at.is_(None),
            ),
        )
    )
    expiring.extend(credential_result.scalars().all())

    # ------------------------------------------------------------------
    # 2. MCP server OAuth tokens
    # ------------------------------------------------------------------
    mcp_result = await db.execute(
        select(MCPServer).where(
            MCPServer.auth_mode == "oauth",
            # A per_user server's own row token is never used: not refreshed.
            MCPServer.credential_mode != PER_USER,
            MCPServer.oauth_status.in_(["connected", "error"]),
            MCPServer.refresh_token_enc.isnot(None),
            or_(
                MCPServer.token_expires_at < threshold,
                MCPServer.token_expires_at.is_(None),
            ),
        )
    )
    mcp_holders: list[MCPTokenHolder] = list(mcp_result.scalars().all())
    # People's own credentials on per_user servers: the same selection; the
    # OAuth client is read from their server.
    mcp_credential_result = await db.execute(
        select(MCPServerUserCredential)
        .join(MCPServer, MCPServer.id == MCPServerUserCredential.server_id)
        .where(
            MCPServer.auth_mode == "oauth",
            MCPServer.credential_mode == PER_USER,
            MCPServerUserCredential.oauth_status.in_(["connected", "error"]),
            MCPServerUserCredential.refresh_token_enc.isnot(None),
            or_(
                MCPServerUserCredential.token_expires_at < threshold,
                MCPServerUserCredential.token_expires_at.is_(None),
            ),
        )
    )
    mcp_holders.extend(mcp_credential_result.scalars().all())
    # The SELECT takes every token within ``_REFRESH_AHEAD`` of its expiry; a
    # short-lived one is only due once part of its lifetime has passed,
    # so a server is neither refreshed nor locked on every tick.
    mcp_expiring = [srv for srv in mcp_holders if _mcp_token_due(srv)]

    if not expiring and not mcp_expiring:
        return 0

    # Hand the pooled connection back before the HTTP refresh gather: the
    # SELECTs above opened a transaction that would otherwise pin one
    # connection for the whole refresh phase (minutes when many tokens
    # expire together) on every scheduler tick. The ORM
    # rows stay usable (expire_on_commit=False). Each integration and MCP
    # server is re-read and committed under its own refresh lock; nothing is
    # written through the rows selected here.
    from app.core.database import release_db_connection_before_loop

    await release_db_connection_before_loop(db)

    # ------------------------------------------------------------------
    # 3. Refresh in parallel with bounded concurrency + shared client
    # ------------------------------------------------------------------
    sem = asyncio.Semaphore(_REFRESH_CONCURRENCY)
    refreshed = 0

    async with egress_client() as http:
        tasks = [_refresh_one_integration(integ, http, sem) for integ in expiring] + [
            _refresh_one_mcp(srv, http, sem) for srv in mcp_expiring
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if r is True:
                refreshed += 1
            elif isinstance(r, BaseException):
                # ``%r`` + ``exc_info`` rather than ``%s``: many exception
                # classes (httpx transport errors, KeyError on a missing
                # field, CancelledError) stringify to an empty message,
                # which produced the useless "Unexpected token refresh
                # error: " log lines with no class name and no traceback.
                #
                # ``BaseException`` rather than ``Exception``: with
                # ``return_exceptions=True``, ``asyncio.gather`` returns a
                # *cancelled* task's ``CancelledError`` in the results list
                # — and ``CancelledError`` derives from ``BaseException``
                # (not ``Exception``) on Python 3.8+, so the narrower check
                # would silently skip it.  This loop only inspects returned
                # values, never re-raises, so handling ``BaseException``
                # here cannot swallow a cancellation of the surrounding
                # task (that case makes ``gather`` itself raise instead of
                # returning results).
                logger.warning(
                    "Unexpected token refresh error: %r", r, exc_info=r
                )

    # Every refresh committed its own outcome under its lock. Never write an
    # outcome through the rows selected above: this commit would land it over
    # a re-authorization committed in the meantime.
    await db.commit()

    return refreshed
