# SPDX-License-Identifier: Apache-2.0

"""Shared HTTP utilities for integration providers.

Contains:
  - ``AutoRefreshClient``: httpx wrapper that auto-refreshes OAuth tokens on
    401/403 and retries the request once.
  - ``RefreshableHeaders`` / ``ensure_fresh_token()`` /
    ``bearer_seconds_remaining()``: keep a short-lived bearer JWT (the 10-minute
    cross-platform XTM token) valid across a long autonomous run by re-minting
    it just *before* it expires — the proactive complement to
    ``AutoRefreshClient``'s reactive 401/403 retry.
  - ``format_http_error()``: builds human-readable error messages from non-success
    HTTP responses so the LLM gets meaningful context instead of raw exceptions.
  - ``format_network_error()``: builds human-readable error messages from network
    exceptions (connection errors, timeouts).
  - ``parse_compound_token()``: splits ``||``-delimited compound tokens used by
    many providers (e.g. ``base_url||api_key``).
  - ``operator_base_url()``: checks and normalises the instance URL an operator
    configures for a self-hosted provider (http(s), no credentials, query or
    fragment).
  - ``normalize_bearer_token()`` / ``bearer_auth_headers()``: build a
    ``Bearer`` Authorization header, failing fast with
    ``EmptyBearerTokenError`` on a blank/whitespace token so an illegal
    ``"Bearer "`` header value can never reach httpx.
  - ``is_publish_allowed()``: shared channel/recipient whitelist check used by
    all channel bot providers.
  - ``safe_request()``: rate-limit-aware request wrapper that retries HTTP
    429 responses using *decorrelated jitter* anchored at the
    ``Retry-After`` floor, so concurrent callers never reconverge on the
    same wake-up instant. Callers may additionally opt idempotent calls
    into retrying transient upstream statuses (typically
    ``TRANSIENT_RETRY_STATUSES`` = 502/503/504) via ``retry_statuses``.
  - ``RateLimiter`` / ``get_shared_rate_limiter()``: a process-wide,
    per-key token-bucket rate limiter (with an optional concurrency cap)
    that *prevents* a burst of parallel outbound calls from firing at
    once — the complement to ``safe_request``'s retry, for providers with
    a strict per-account rate cap (e.g. the HubSpot CRM Search API).
  - ``PLATFORM_USER_AGENT``: the one User-Agent the platform announces on
    its outbound requests (integration providers and services alike).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import math
import random
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from types import TracebackType
from typing import Any
from urllib.parse import urlparse, urlsplit

import httpx

from app.core.egress import egress_client
from app.core.redaction import redact_secrets
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry constants for safe_request 429 handling
# ---------------------------------------------------------------------------

# Maximum number of retries when the provider returns HTTP 429.
_RATE_LIMIT_MAX_RETRIES = 3
# Base delay (seconds) for the exponential-backoff component of the
# decorrelated-jitter draw above the Retry-After floor.
_RATE_LIMIT_BASE_DELAY = 1.0
# Upper bound (seconds) applied independently to each component of a
# single retry sleep: the parsed ``Retry-After`` floor is clamped to this
# value (so a hostile ``Retry-After: 86400`` cannot park a worker for
# 24 h) **and** the exponential-backoff cap (``base * 2^attempt``) is
# clamped to this value.  The decorrelated-jitter draw is
# ``random.uniform(retry_after, retry_after + cap)``, so the structural
# worst-case single sleep is ``_RATE_LIMIT_MAX_DELAY + cap_max`` where
# ``cap_max = min(_RATE_LIMIT_MAX_DELAY, _RATE_LIMIT_BASE_DELAY *
# 2 ** (_RATE_LIMIT_MAX_RETRIES - 1))``.  With the current constants
# (``_RATE_LIMIT_MAX_RETRIES = 3``, ``_RATE_LIMIT_BASE_DELAY = 1.0``),
# ``cap_max = 4 s``, so the actual worst-case single sleep is ~34 s.
# If ``_RATE_LIMIT_MAX_RETRIES`` is raised high enough for the
# exponential term to saturate the cap, the worst case can grow up to
# ``~2 * _RATE_LIMIT_MAX_DELAY`` (~60 s) — that extra spread above the
# floor is the de-correlation knob and is the intended behaviour, not a
# bug.
_RATE_LIMIT_MAX_DELAY = 30.0

# Transient upstream statuses that idempotent callers may opt into retrying
# via ``safe_request(..., retry_statuses=TRANSIENT_RETRY_STATUSES)``: the
# gateway/proxy tier answering 502 (bad gateway), 503 (service unavailable)
# or 504 (gateway timeout) is usually healthy again within seconds, so a
# short retry turns an intermittent blip into a successful tool call
# instead of a hard user-facing failure. Retrying is NOT the
# default because non-idempotent calls (mutations) could be applied twice
# when the first attempt succeeded behind a failing proxy.
TRANSIENT_RETRY_STATUSES = frozenset({502, 503, 504})

# Default timeout (seconds) for integration HTTP requests.
DEFAULT_TIMEOUT = 30

#: The platform's one outbound User-Agent: import it, never write the string out
#: again (``tests/test_platform_user_agent.py`` fails on a copy). It carries a way
#: to reach the operator because public hosts (Wikimedia's User-Agent policy
#: among them) throttle or block automated clients that give none.
PLATFORM_USER_AGENT = "FiligranCopilot/1.0 (+https://filigran.io)"


# ---------------------------------------------------------------------------
# Shared error formatting – used by all provider tool functions
# ---------------------------------------------------------------------------


_NOT_FOUND_WORDING = "resource not found"


def is_not_found_error(err: str | None) -> bool:
    """Whether *err* reports a 404: ``format_http_error``'s wording, or a message naming the status.

    ``format_http_error`` words a 404 without the digits, so a ``"404" in err`` test alone never
    matches an error built from a real response.
    """
    if not err:
        return False
    return _NOT_FOUND_WORDING in err or "404" in err


def format_http_error(resp: httpx.Response, provider: str, action: str) -> str:
    """Build a human-readable error string from a non-success HTTP response.

    Call this instead of ``resp.raise_for_status()`` inside integration tool
    functions.  The returned string is surfaced directly to the LLM so it can
    understand *what* went wrong and advise the user accordingly.

    Parameters
    ----------
    resp:
        The failed ``httpx.Response``.
    provider:
        Human-readable provider name (e.g. ``"GitHub"``, ``"Metabase"``).
    action:
        Short description of the attempted action (e.g. ``"list repos"``,
        ``"create issue"``).
    """
    status = resp.status_code

    if status == 401:
        return (
            f"Error: {provider} authentication failed — the token may be expired "
            f"or revoked. The user should re-authorize this integration."
        )
    if status == 403:
        detail = _extract_error_detail(resp)
        hint = f" Detail: {detail}" if detail else ""
        return (
            f"Error: {provider} access denied while trying to {action} — "
            f"insufficient permissions or scopes. The user should check the "
            f"integration permissions.{hint}"
        )
    if status == 404:
        detail = _extract_error_detail(resp)
        hint = f" Detail: {detail}" if detail else ""
        return f"Error: {provider} {_NOT_FOUND_WORDING} while trying to {action}.{hint}"
    if status == 400:
        detail = _extract_error_detail(resp)
        # Surface the machine-readable ``category`` when the provider
        # includes it (e.g. HubSpot returns ``"category": "VALIDATION_ERROR"``
        # alongside a generic top-level message).  This makes 400 logs
        # actionable (e.g. "UNKNOWN_PROPERTY property=nfr_license_type
        # [VALIDATION_ERROR]") without requiring the caller to parse the body.
        with suppress(Exception):
            category = resp.json().get("category")
            if category and category not in (detail or ""):
                detail = f"{detail + ' ' if detail else ''}[{category}]"
        hint = f" {detail}" if detail else ""
        return f"Error: {provider} rejected the request to {action} — bad request.{hint}"
    if status == 422:
        detail = _extract_error_detail(resp)
        hint = f" {detail}" if detail else ""
        return f"Error: {provider} rejected the request to {action} — validation error.{hint}"
    if status == 429:
        return f"Error: {provider} rate limit exceeded while trying to {action}. Wait a moment and try again."
    if status >= 500:
        return (
            f"Error: {provider} server error ({status}) while trying to "
            f"{action}. The service may be temporarily unavailable."
        )

    detail = _extract_error_detail(resp)
    return (
        f"Error: {provider} returned HTTP {status} while trying to {action}. {detail}"
    )


def format_network_error(exc: Exception, provider: str, action: str) -> str:
    """Build a human-readable error string from a network-level exception.

    Call this inside ``except`` blocks wrapping HTTP calls so that connection
    and timeout errors produce useful feedback for the LLM.
    """
    if getattr(exc, "is_relay_error", False):
        # A relay failure already names its cause and its group; the generic
        # "the service may be unreachable" would send the reader the wrong way.
        return f"Error: {provider} request failed while trying to {action}: {exc}"
    if isinstance(exc, httpx.ConnectError):
        return f"Error: Could not connect to {provider} while trying to {action} — the service may be unreachable."
    if isinstance(exc, httpx.TimeoutException):
        return f"Error: {provider} request timed out while trying to {action}. The service may be slow or overloaded."
    # format_exception_message: several transport errors stringify to "" —
    # fall back to the exception class name so the reason is never empty.
    return (
        f"Error: {provider} request failed while trying to {action}: "
        f"{format_exception_message(exc)}"
    )


# Keys under which HubSpot reports missing scope names in 403 bodies.
_REQUIRED_SCOPE_KEYS = ("requiredScopes", "requiredGranularScopes", "missingScopes")


def _extract_required_scopes(body: Any) -> list[str]:
    """Collect missing-scope names from a HubSpot 403 scope-error body.

    The generic ``message`` alone ("One or more of the following scopes are
    required.") is not actionable; surfacing the scope names lets the
    user/operator know exactly which private-app scopes to grant without
    having to inspect the raw response.

    HubSpot uses several layouts in practice, all handled here:
      • Top-level:       ``{"requiredScopes": ["content"]}``
      • Context-nested:  ``{"context": {"requiredScopes": [...]}}``
      • Errors-nested (the documented MISSING_SCOPES shape):
        ``{"errors": [{"message": "...", "context":
        {"requiredGranularScopes": ["files"]}}]}``
    Scope entries may be flat strings or objects carrying ``scopeTypes`` /
    ``requiredAllScopes`` / ``requiredAnyScopes`` sub-lists.

    Returns a deduplicated list preserving first-seen order.
    """
    if not isinstance(body, dict):
        return []

    def _collect(raw_scopes: Any, out: list[str]) -> None:
        if not isinstance(raw_scopes, list):
            return
        for item in raw_scopes:
            if isinstance(item, str) and item:
                out.append(item)
            elif isinstance(item, dict):
                for sub_key in ("scopeTypes", "requiredAllScopes", "requiredAnyScopes"):
                    sub = item.get(sub_key)
                    if isinstance(sub, list):
                        out.extend(s for s in sub if isinstance(s, str) and s)

    containers: list[Any] = [body, body.get("context")]
    errors = body.get("errors")
    if isinstance(errors, list):
        for e in errors[:5]:
            if isinstance(e, dict):
                containers.extend((e, e.get("context")))

    scope_names: list[str] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in _REQUIRED_SCOPE_KEYS:
            _collect(container.get(key), scope_names)

    # Deduplicate, preserving insertion order.
    seen: set[str] = set()
    unique: list[str] = []
    for s in scope_names:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def required_scopes(resp: httpx.Response) -> list[str]:
    """The scope names a 403 body says are missing (empty when it names none)."""
    try:
        body = resp.json()
    except ValueError:
        return []
    return _extract_required_scopes(body)


def _append_scope_hint(detail: str, body: Any) -> str:
    """Append ``"Required scopes: …"`` to *detail* when *body* carries them.

    The combined result is capped at 300 characters, truncating the *base*
    detail first so the scope names — the actionable part — always survive.
    """
    scopes = _extract_required_scopes(body)
    if not scopes:
        return detail
    suffix = f"Required scopes: {', '.join(scopes[:10])}."  # cap for readability
    # Reserve room for the suffix plus the joining space; when the suffix
    # alone fills the cap, return it on its own (no leading space).
    budget = 300 - len(suffix) - 1
    if not detail or budget <= 0:
        return suffix[:300]
    # Drop a single sentence-ending period so "detail. Required scopes:"
    # doesn't double up — but leave an ellipsis ("...") untouched.
    base = detail
    if base.endswith(".") and not base.endswith(".."):
        base = base[:-1]
    return f"{base[:budget]} {suffix}"


def _collect_validation_children(node: object, path: str = "") -> list[str]:
    """Flatten an OpenAEV ``ValidationErrorBag.errors`` tree into readable reasons.

    OpenAEV (and other Symfony-style APIs) serialize bean-validation failures as
    an OBJECT, not an array::

        {"code": 400, "message": "Validation Failed",
         "errors": {"children": {"<field>": {"errors": ["must not be blank"]}}}}

    The top-level ``message`` stays the generic "Validation Failed", so the only
    actionable content lives under ``errors.children.<field>.errors[]`` (Spring
    keys nested-DTO failures by dotted/indexed path, e.g.
    ``action_output_parsers[0]...contract_output_element_rule``). Walk the tree
    and return ``field: reason`` strings so the caller can surface WHY a 400
    happened instead of an opaque "bad request" - the exact gap behind the silent
    arsenal fork/finalize 400s.
    """
    found: list[str] = []
    if not isinstance(node, dict):
        return found
    msg = node.get("message")
    if isinstance(msg, str) and msg.strip():
        found.append(f"{path}: {msg.strip()}" if path else msg.strip())
    errs = node.get("errors")
    if isinstance(errs, list):
        for e in errs:
            if isinstance(e, str) and e.strip():
                found.append(f"{path}: {e.strip()}" if path else e.strip())
    children = node.get("children")
    if isinstance(children, dict):
        for field, child in children.items():
            key = str(field)
            # Symfony/Spring keys list elements by numeric index. Render those as
            # ``field[0]`` (matching the caller's own JSON shape, e.g.
            # ``action_output_parsers[0].contract_output_element_rule``) rather
            # than a dot-joined ``field.0`` so a surfaced reason maps straight
            # back to the offending input.
            if key.isdigit():
                child_path = f"{path}[{key}]" if path else f"[{key}]"
            else:
                child_path = f"{path}.{key}" if path else key
            found.extend(_collect_validation_children(child, child_path))
    return found


#: The longest provider detail an error answer quotes.
_ERROR_DETAIL_MAX_CHARS = 300


def _extract_error_detail(resp: httpx.Response) -> str:
    """The provider's own words on a failed response, redacted and bounded.

    Only the body is read, never a header: the detail is passed through
    :func:`redact_secrets` (a server echoing the request's token or key) and
    capped at ``_ERROR_DETAIL_MAX_CHARS`` characters.
    """
    detail = _raw_error_detail(resp)
    if not detail:
        return ""
    return redact_secrets(detail)[:_ERROR_DETAIL_MAX_CHARS]


def _collect_field_errors(errors: dict[str, Any]) -> list[str]:
    """``field: reason`` of a field-keyed ``errors`` map.

    The RFC 7807 validation shape of ASP.NET and the Rails one carry the
    reasons as ``{"errors": {"Name": ["The Name field is required."]}}``
    under a generic ``title`` or ``message``.
    """
    found: list[str] = []
    for field, reasons in errors.items():
        if isinstance(reasons, str):
            reasons = [reasons]
        if not isinstance(reasons, list):
            continue
        texts = [str(r).strip() for r in reasons if isinstance(r, (str, int, float)) and str(r).strip()]
        if texts:
            found.append(f"{field}: {'; '.join(texts)}" if str(field) else "; ".join(texts))
    return found


def _raw_error_detail(resp: httpx.Response) -> str:
    """Try to extract a meaningful error detail from a response body."""
    try:
        body = resp.json()

        # Many providers (GitHub, HubSpot, …) return an ``errors`` array
        # alongside a generic top-level ``message`` (e.g. "Validation
        # Failed" / "There was a problem with the request.").  Surface up to
        # five entries so multi-field validation failures are fully visible.
        #
        # Supported item shapes (at least one field must be present):
        #   • Standard: ``{"message": "some explanation"}``
        #   • HubSpot:  ``{"error": "UNKNOWN_PROPERTY", "name": "prop_name"}``
        #     — the ``"error"`` key carries the machine-readable code and
        #     ``"name"`` identifies the offending property/field.
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            parts: list[str] = []
            for e in errors[:5]:  # cap to keep the message scannable
                if not isinstance(e, dict):
                    continue
                msg = e.get("message")
                if msg:
                    parts.append(str(msg))
                else:
                    # HubSpot-style: synthesise a readable token from the
                    # error code and the offending property/field name.
                    code = e.get("error")
                    name = e.get("name")
                    segment = " ".join(
                        p for p in (str(code) if code else None, f"property={name}" if name else None)
                        if p
                    )
                    if segment:
                        parts.append(segment)
            if parts:
                # HubSpot's documented MISSING_SCOPES shape nests the scope
                # names under ``errors[*].context`` — attach them here too so
                # the early return on a populated ``errors`` array never
                # swallows the actionable scope hint.
                return _append_scope_hint("; ".join(parts)[:300], body)

        # OpenAEV returns ``errors`` as an OBJECT (a Symfony-style validation
        # bag) rather than an array — the actionable per-field reasons are nested
        # under ``errors.children`` while ``message`` is a generic "Validation
        # Failed". Walk the tree so a bean-validation 400 (e.g. a missing/invalid
        # output-parser field on an arsenal fork) is self-correctable instead of
        # opaque. Prefer the nested reasons over the generic top-level message.
        if isinstance(errors, dict):
            field_msgs = _collect_validation_children(errors) or _collect_field_errors(errors)
            if field_msgs:
                return _append_scope_hint("; ".join(field_msgs[:8])[:300], body)

        detail = (
            body.get("message")
            or body.get("error")
            or body.get("detail")
            or body.get("error_description")
            or body.get("title")
            or ""
        )
        # Google-style ``{"error": {"code": 400, "message": "..."}}``.
        if isinstance(detail, dict) and isinstance(detail.get("message"), str) and detail["message"].strip():
            detail = detail["message"]
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, default=str)[:300]
        detail = str(detail)[:300] if detail else ""
        if not detail and body:
            # A JSON body in no shape known above still says what was wrong:
            # quote it bounded rather than answer a bare "bad request".
            detail = json.dumps(body, separators=(",", ":"), default=str)[:300]
        return _append_scope_hint(detail, body)
    except Exception:
        return (resp.text or "").strip()[:300]


# ---------------------------------------------------------------------------
# Compound token parsing – used by providers that store multiple values in
# a single ``||``-delimited string (e.g. ``base_url||api_key``).
# ---------------------------------------------------------------------------


def parse_compound_token(
    token: str,
    field_count: int,
    *,
    defaults: tuple[str, ...] | None = None,
    greedy_last: bool = False,
) -> tuple[str, ...]:
    """Split a ``||``-delimited compound token into *field_count* parts.

    Parameters
    ----------
    token:
        The raw compound token string (e.g. ``"base_url||api_key"``).
    field_count:
        Expected number of fields.
    defaults:
        Optional default values (length must equal *field_count*).  When
        *token* has fewer segments than *field_count*, missing values are
        filled from *defaults*.  When *defaults* is ``None`` and the token
        has fewer segments, missing positions are filled with ``""``.
    greedy_last:
        When ``True``, the final field absorbs any remaining ``||`` (via
        ``str.split("||", field_count - 1)``). Use this for compounds whose
        LAST field is a user-supplied secret that may legitimately contain
        ``||`` (e.g. a Splunk token, ServiceNow/Qualys password, Sumo Logic
        access key, Tenable secret key) — otherwise such a credential
        mis-parses into the wrong fields and fails auth confusingly.

    Returns
    -------
    tuple[str, ...]
        A tuple of exactly *field_count* strings.
    """
    parts = (
        token.split("||", field_count - 1) if greedy_last else token.split("||")
    )
    result: list[str] = []
    for i in range(field_count):
        if i < len(parts) and parts[i]:
            result.append(parts[i])
        elif defaults and i < len(defaults):
            result.append(defaults[i])
        else:
            result.append("")
    return tuple(result)


def operator_base_url(raw: str | None, *, strip_suffix: str = "") -> str | None:
    """*raw* as ``scheme://host[:port][/path]`` (no trailing slash), ``None`` when unusable.

    The instance URL of a self-hosted provider is the operator's choice, so any
    http(s) host is accepted (such an instance often lives on the private
    network), but never one carrying credentials, a query or a fragment: they
    would change where the token goes or what the API path means. A pasted API
    root (*strip_suffix*, e.g. ``/api/v4``) is reduced to the instance URL.
    """
    text = (raw or "").strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    path = parts.path.rstrip("/")
    if strip_suffix and path.endswith(strip_suffix):
        path = path[: -len(strip_suffix)]
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{host}:{port}" if port else host
    return f"{parts.scheme}://{netloc}{path}"


# ---------------------------------------------------------------------------
# Bearer Authorization header – guarded construction.
#
# Building an ``Authorization: Bearer <token>`` header from a blank or
# whitespace-only token yields the value ``"Bearer "`` (or ``"Bearer    "``),
# which httpx/h11 rejects with an opaque ``LocalProtocolError: Illegal header
# value`` that surfaces as a useless HTTP 500. A missing/expired credential is
# an *expected* condition (the integration just needs re-authorizing), so the
# only safe place to fail is *before* the header is ever constructed. Routing
# every Bearer header through the helpers below makes the empty-token guard
# inseparable from header construction, so it cannot be silently dropped by a
# future refactor (the failure mode this repeatedly re-introduced — see the
# Teams channel and Jira project listing regressions).
# ---------------------------------------------------------------------------

# User-facing message (not a secret) shown when a credential is blank/missing.
MISSING_TOKEN_MESSAGE = (
    "Integration token is missing or expired. "  # noqa: S105
    "Please re-authorize the integration."
)


class EmptyBearerTokenError(ValueError):
    """Raised when a Bearer header would be built from a blank token.

    Carries the user-facing :data:`MISSING_TOKEN_MESSAGE` so callers can map it
    to a clear ``401`` (or an error result) instead of leaking an httpx
    ``LocalProtocolError``. Subclasses ``ValueError`` to keep the
    ``app/integrations`` layer free of any FastAPI/HTTP coupling.
    """


def normalize_bearer_token(token: str | None) -> str:
    """Strip *token* and return it, raising if it is blank/whitespace-only.

    This is the single guard that protects every ``Bearer`` header: an empty
    result can never be returned, so the caller cannot build an illegal
    ``"Bearer "`` header value.
    """
    normalized = (token or "").strip()
    if not normalized:
        raise EmptyBearerTokenError(MISSING_TOKEN_MESSAGE)
    return normalized


def bearer_auth_headers(
    token: str | None,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build an ``{"Authorization": "Bearer <token>"}`` header dict.

    Guards against blank tokens via :func:`normalize_bearer_token` (raising
    :class:`EmptyBearerTokenError`), so the returned dict always carries a
    well-formed header value. Extra headers (e.g. ``Accept``) can be merged in,
    but the computed ``Authorization`` is applied LAST and always wins - an
    ``Authorization`` key in *extra* can never overwrite it with a blank/illegal
    value, keeping the no-illegal-header invariant intact.
    """
    auth_value = f"Bearer {normalize_bearer_token(token)}"
    headers = dict(extra) if extra else {}
    headers["Authorization"] = auth_value
    return headers


# ---------------------------------------------------------------------------
# Channel bot publish whitelist – shared by all channel bot providers.
# ---------------------------------------------------------------------------


def is_publish_allowed(channel_id: str, allowed_ids: set[str] | None) -> bool:
    """Return ``True`` if *channel_id* is in *allowed_ids* or no whitelist is set.

    All channel bot providers (Slack, Teams, Discord, Telegram, WhatsApp) use
    this same logic to enforce publish-channel restrictions.
    """
    return allowed_ids is None or str(channel_id) in allowed_ids


# ---------------------------------------------------------------------------
# App-only (client-credentials) access-token cache
# ---------------------------------------------------------------------------
#
# Providers that authenticate with app-only client credentials (e.g. the
# Microsoft Defender XDR / Sentinel app-only auth mode) mint a fresh Azure AD
# access token on *every* tool call. In an agentic SOC run an agent may invoke
# a dozen+ tools, hammering the Azure AD token endpoint and risking throttling.
# The delegated-OAuth path already caches per-(integration, scope) tokens in
# Redis; this gives the app-only path the same benefit with a small in-process
# TTL cache keyed by a hash of the *credentials + scope* (so a rotated secret
# transparently invalidates the entry and the key never contains secrets).

# Default access-token lifetime (seconds) assumed when the token endpoint does
# not return ``expires_in`` — Azure AD app-only tokens live ~1 h.
_APP_TOKEN_DEFAULT_TTL = 3600
# Safety margin (seconds) subtracted from the token lifetime so a cached token
# is never handed out within this window of its actual expiry.
_APP_TOKEN_EXPIRY_SKEW = 120

# Upper bound on the number of cached app-only tokens. A long-lived process
# serving many one-off integrations/tenants could otherwise accumulate stale
# entries indefinitely (a write-only key that is never read again after expiry
# is never evicted by ``get_cached_app_token``). When the cache reaches this
# size a write first sweeps expired entries, then — if still full — evicts the
# soonest-to-expire ones, keeping the dict bounded.
_APP_TOKEN_CACHE_MAX = 1024

# cache_key -> (access_token, monotonic_expiry)
_APP_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _evict_app_tokens_if_full(now: float) -> None:
    """Bound ``_APP_TOKEN_CACHE`` size before inserting a new entry.

    Called from :func:`set_cached_app_token`. Sweeps expired entries first
    (cheap and correct); if every remaining entry is still live, drops the
    soonest-to-expire 10 % so the dict can't grow without bound.
    """
    if len(_APP_TOKEN_CACHE) < _APP_TOKEN_CACHE_MAX:
        return
    for key in [k for k, (_, exp) in _APP_TOKEN_CACHE.items() if exp <= now]:
        _APP_TOKEN_CACHE.pop(key, None)
    if len(_APP_TOKEN_CACHE) < _APP_TOKEN_CACHE_MAX:
        return
    # All live — evict the soonest-to-expire batch to make room.
    victims = sorted(_APP_TOKEN_CACHE.items(), key=lambda kv: kv[1][1])[
        : max(1, _APP_TOKEN_CACHE_MAX // 10)
    ]
    for key, _ in victims:
        _APP_TOKEN_CACHE.pop(key, None)


def app_token_cache_key(*parts: str) -> str:
    """Build an opaque, secret-free cache key from credential material + scope.

    The parts (tenant id, client id, client secret, scope, …) are hashed with
    SHA-256 so the resulting key never embeds the secret and rotating any part
    naturally produces a fresh key (invalidating the stale token).
    """
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return digest


def get_cached_app_token(cache_key: str) -> str | None:
    """Return a still-valid cached app-only token for *cache_key*, or ``None``."""
    entry = _APP_TOKEN_CACHE.get(cache_key)
    if not entry:
        return None
    token, expiry = entry
    if time.monotonic() < expiry:
        return token
    # Expired — drop it so the dict doesn't grow unbounded with stale entries.
    _APP_TOKEN_CACHE.pop(cache_key, None)
    return None


def set_cached_app_token(
    cache_key: str, token: str, expires_in: int | None = None
) -> None:
    """Cache *token* under *cache_key* until shortly before it expires.

    ``expires_in`` is the token lifetime in seconds as reported by the token
    endpoint; when missing or invalid it falls back to
    :data:`_APP_TOKEN_DEFAULT_TTL`. The effective TTL always reserves
    :data:`_APP_TOKEN_EXPIRY_SKEW` so a cached token is never served within that
    window of its real expiry. When the lifetime is shorter than the skew (a very
    short-lived token), the entry is **not** cached at all — caching it would
    inevitably serve it inside the skew window, defeating the guarantee.
    """
    try:
        lifetime = int(expires_in) if expires_in is not None else _APP_TOKEN_DEFAULT_TTL
    except (TypeError, ValueError):
        lifetime = _APP_TOKEN_DEFAULT_TTL
    if lifetime <= 0:
        lifetime = _APP_TOKEN_DEFAULT_TTL
    # Always honour the skew: never cache for longer than ``lifetime - skew``.
    # ``max(60, …)`` would round a short lifetime *up*, serving the token inside
    # the skew window — so skip caching when the remaining budget is non-positive.
    ttl = lifetime - _APP_TOKEN_EXPIRY_SKEW
    if ttl <= 0:
        return
    now = time.monotonic()
    _evict_app_tokens_if_full(now)
    _APP_TOKEN_CACHE[cache_key] = (token, now + ttl)


# ---------------------------------------------------------------------------
# Shared async rate limiter – token bucket + optional concurrency cap
# ---------------------------------------------------------------------------
#
# Some providers enforce a strict *per-account* request rate far below their
# general burst limit — most notably the HubSpot CRM Search API, capped at
# 5 requests/second per HubSpot account and shared across every user and
# integration in the portal. When an agent issues several parallel
# tool calls that each hit such an endpoint, they fire in the same instant,
# all breach the cap at once, and each independently retries — a self-
# amplifying 429 storm that the per-call decorrelated-jitter retry in
# ``safe_request`` de-correlates but cannot prevent (it only spaces out the
# *retries*, after the burst already happened).
#
# ``RateLimiter`` prevents the burst itself: a token bucket paces the *rate*
# at which outbound requests start, and an optional semaphore caps how many
# run concurrently. ``get_shared_rate_limiter`` returns a single process-wide
# instance per key, so every concurrent caller for the same account — across
# conversations, agents and assignments in this process — shares one bucket.
#
# Scope: the limiter is in-process. With several backend replicas each paces
# independently (aggregate ``rate × replicas``); this still removes the
# single-process simultaneous burst that drives the storm, and a cross-replica
# (Redis) limiter could layer on top later if a portal-wide cap is ever needed.

# Tolerance for float rounding when checking whether a whole token has accrued,
# so ``(1 / rate) * rate`` landing at 0.999999999 never spins the acquire loop
# forever on a perfectly-advanced (e.g. test fake) clock.
_RATE_LIMITER_TOKEN_EPSILON = 1e-9

# Upper bound on distinct keys held in the shared registry. Realistic
# deployments hold one per provider account (far fewer); the cap is a defensive
# guard so a very long-lived process serving many accounts can never grow it
# without bound.
_RATE_LIMITER_REGISTRY_MAX = 2048


class RateLimiter:
    """Async token-bucket rate limiter with an optional concurrency cap.

    A token bucket refills at ``rate`` tokens/second up to ``capacity`` tokens;
    each acquired slot consumes one token, so the sustained request rate is
    bounded by ``rate`` and the largest instantaneous burst by ``capacity``
    (the worst-case number of requests in any one-second window is therefore
    ``capacity + rate``). When ``max_concurrency`` is set, an additional
    semaphore caps how many slots are held — i.e. how many requests are in
    flight — at once.

    Use :meth:`slot` as an async context manager around a single outbound
    request::

        async with limiter.slot():
            resp = await client.get(url)

    ``time_fn`` / ``sleep_fn`` are injection points for deterministic tests;
    they default to :func:`time.monotonic` and :func:`asyncio.sleep`.
    """

    def __init__(
        self,
        *,
        rate: float,
        capacity: float,
        max_concurrency: int | None = None,
        time_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        # Guard against nonsensical config: a non-positive rate would make the
        # bucket never refill (every caller would wait forever) and a sub-1
        # capacity could never hold a whole token. Both collapse to safe
        # minimums rather than dividing by zero or deadlocking.
        self._rate = rate if rate > 0 else 0.0001
        self._capacity = capacity if capacity >= 1.0 else 1.0
        self._time = time_fn or time.monotonic
        self._sleep = sleep_fn or asyncio.sleep
        self._tokens = self._capacity
        self._updated = self._time()
        self._lock = asyncio.Lock()
        self._sem = (
            asyncio.Semaphore(max_concurrency)
            if max_concurrency and max_concurrency > 0
            else None
        )
        self._in_use = 0

    @property
    def last_refill(self) -> float:
        """Monotonic timestamp of the last token refill (for LRU eviction)."""
        return self._updated

    @property
    def in_use(self) -> int:
        """Callers inside :meth:`slot` right now, waiting for it or holding it."""
        return self._in_use

    async def _acquire_token(self) -> None:
        """Block until a token is available, then consume one.

        Token accounting is serialised under ``self._lock`` and the lock is
        held across the wait *on purpose*: it spaces queued callers out one at
        a time (a leaky-bucket cadence) instead of releasing a thundering herd
        the instant a single token accrues.
        """
        async with self._lock:
            while True:
                now = self._time()
                elapsed = now - self._updated
                if elapsed > 0:
                    self._tokens = min(
                        self._capacity, self._tokens + elapsed * self._rate
                    )
                    self._updated = now
                if self._tokens >= 1.0 - _RATE_LIMITER_TOKEN_EPSILON:
                    self._tokens -= 1.0
                    return
                # Sleep just long enough for the next whole token to accrue.
                await self._sleep((1.0 - self._tokens) / self._rate)

    @asynccontextmanager
    async def slot(
        self, *, admit: Callable[[], None] | None = None
    ) -> AsyncIterator[None]:
        """Acquire one rate-limited slot for a single outbound request.

        When a concurrency cap is configured the semaphore slot is acquired
        *first* and the token is consumed only immediately before the wrapped
        block runs. Consuming the token *after* the concurrency gate (not
        before it) is what makes the token bucket pace actual request *starts*:
        a caller blocked on the semaphore holds no token, so when a slot frees
        it cannot fire instantly in a burst — it must still wait for the bucket.
        The semaphore slot is then held for the duration of the wrapped block,
        so at most ``max_concurrency`` requests are in flight at once.

        *admit*, when given, runs once the concurrency slot is held and before
        a token is waited for; an exception it raises releases the slot and
        propagates. It is how a caller whose request became pointless while it
        queued (its target started refusing it) leaves at once, instead of
        waiting for the bucket only to be turned away.
        """
        self._in_use += 1
        try:
            if self._sem is None:
                if admit is not None:
                    admit()
                await self._acquire_token()
                yield
                return
            async with self._sem:
                if admit is not None:
                    admit()
                await self._acquire_token()
                yield
        finally:
            self._in_use -= 1


# key -> shared RateLimiter
_RATE_LIMITER_REGISTRY: dict[str, RateLimiter] = {}


def _evict_rate_limiters_if_full() -> None:
    """Bound ``_RATE_LIMITER_REGISTRY`` before inserting a new entry.

    Evicts the least-recently-refilled (idle-est) ~10 % of limiters when the
    registry is full. An evicted key simply rebuilds a fresh limiter the next
    time it is used. A limiter a caller is inside (``in_use``: waiting for a
    slot or holding one) is never a victim, whatever its last refill says -
    the next caller for its key would build a second limiter beside it and
    double that key's concurrency. With every limiter busy nothing is
    evicted, so the registry may pass its cap by the busy ones.
    """
    if len(_RATE_LIMITER_REGISTRY) < _RATE_LIMITER_REGISTRY_MAX:
        return
    idle = [item for item in _RATE_LIMITER_REGISTRY.items() if not item[1].in_use]
    victims = sorted(idle, key=lambda kv: kv[1].last_refill)[
        : max(1, _RATE_LIMITER_REGISTRY_MAX // 10)
    ]
    for key, _ in victims:
        _RATE_LIMITER_REGISTRY.pop(key, None)


def get_shared_rate_limiter(
    key: str,
    *,
    rate: float,
    capacity: float,
    max_concurrency: int | None = None,
) -> RateLimiter:
    """Return the process-wide shared :class:`RateLimiter` for *key*.

    The first call for a *key* creates the limiter with the supplied config;
    later calls return that same instance (their config arguments are ignored),
    so every concurrent caller resolving to the same key shares one token
    bucket. *key* must be opaque and secret-free — build it from
    :func:`app_token_cache_key` when it has to encode a credential (e.g. an API
    token identifying the account whose rate cap is being respected).

    Lookup/create is synchronous and free of ``await`` so it cannot interleave
    on the event loop — two concurrent callers can never create duplicate
    limiters for the same key.
    """
    limiter = _RATE_LIMITER_REGISTRY.get(key)
    if limiter is not None:
        return limiter
    _evict_rate_limiters_if_full()
    limiter = RateLimiter(rate=rate, capacity=capacity, max_concurrency=max_concurrency)
    _RATE_LIMITER_REGISTRY[key] = limiter
    return limiter


# Type alias – a refresh callback returns the new access token or None.
RefreshFn = Callable[[], Awaitable[str | None]]


# ---------------------------------------------------------------------------
# Safe request helper – wraps the try/except + error-formatting pattern that
# every provider tool function repeats.
# ---------------------------------------------------------------------------


def _parse_retry_after(value: str | None) -> float:
    """Parse a ``Retry-After`` response header value into seconds.

    Per RFC 7231 §7.1.3 the header may be either a delay-seconds integer
    or an HTTP-date string. We only honour the numeric form; HTTP-date
    values fall back to ``0.0`` so the retry loop relies on jitter and is
    never interrupted by a ``ValueError`` from :func:`float`.

    Defensive handling:
      * Non-finite values (``NaN``, ``inf``, ``-inf``) — ``asyncio.sleep``
        raises ``ValueError`` on ``NaN`` and ``inf`` would park the worker
        forever, so they collapse to ``0.0``.
      * Negative values — would silently sleep ``0`` anyway, normalise.
      * Excessively large values — a misconfigured provider or hostile
        proxy returning e.g. ``Retry-After: 86400`` (24 h) would otherwise
        block a worker for the full duration. Clamp to
        :data:`_RATE_LIMIT_MAX_DELAY` so the upper bound on a single
        sleep stays predictable.
    """
    if not value:
        return 0.0
    try:
        parsed = float(value)
    except (ValueError, TypeError):
        return 0.0
    if not math.isfinite(parsed) or parsed <= 0:
        return 0.0
    return min(parsed, _RATE_LIMIT_MAX_DELAY)


def _log_request_warning(
    provider: str,
    method: str,
    log_path: str,
    err: str,
    *,
    label: str,
) -> None:
    """Centralised WARNING log for failed integration requests.

    All ``safe_request`` failure log sites route through here so that the
    log format stays consistent and there is a single audited call site
    that emits the request path. ``log_path`` is always
    ``urlparse(url).path`` (query string and fragment already stripped),
    and ``err`` is a human-readable message describing the failure or
    transient retry — for the HTTP-error / network-error / exhausted-
    retries paths it is produced by :func:`format_http_error` or
    :func:`format_network_error`, and on the 429-retry path it is a
    locally-formatted ``"retry N/M in Xs"`` string. None of those
    formats contain credentials.
    """
    logger.warning(
        "%s integration %s (%s %s): %s",
        provider,
        label,
        method.upper(),
        log_path,
        err,
    )


def _record_tool_http_call(provider: str, **figures: Any) -> None:
    """Hand one integration HTTP call's figures to the tool span it runs under.

    Lazy import, like the rate-limit note below; never raises: a telemetry
    failure must not reach the request.
    """
    try:
        from app.services.langfuse_tracing import record_tool_http_call

        record_tool_http_call(provider, **figures)
    except Exception:
        logger.debug("Could not record an integration HTTP call on the tool span", exc_info=True)


async def _note_integration_rate_limit() -> None:
    # Lazy: providers import this module long before app.services is loaded.
    try:
        from app.services.assignment_dispatch import note_rate_limited

        await note_rate_limited("integration")
    except Exception:
        logger.debug("Could not report an integration 429 to assignment dispatch", exc_info=True)


async def safe_request(
    client: httpx.AsyncClient | AutoRefreshClient,
    method: str,
    url: str,
    provider: str,
    action: str,
    *,
    expected_statuses: set[int] | None = None,
    retry_statuses: frozenset[int] | set[int] | None = None,
    max_total_seconds: float | None = None,
    **kwargs: Any,
) -> tuple[httpx.Response | None, str | None]:
    """Execute an HTTP request with standardised error handling and 429 retry.

    Returns ``(response, None)`` on a successful (2xx) response and
    ``(None, error_string)`` when the request fails (non-2xx status or
    network error). The *error_string* is suitable for returning directly to
    the LLM as the tool result.

    **Opt-in failed response**: when the caller passes ``expected_statuses``
    (signalling it wants to branch on the outcome — e.g. tell a benign
    403/404 probe apart from a real 401/5xx), a non-2xx response is returned
    as ``(response, error_string)`` so the caller can read
    ``resp.status_code``. Without ``expected_statuses`` the historical
    ``(None, error_string)`` contract is preserved, because many callers use
    ``resp is None`` (or a truthiness check) as their failure signal
    (``validate_connection``, ``get_user_info``, …) and would otherwise treat
    a 4xx/5xx as success. Network errors always return
    ``(None, error_string)`` (no response exists).

    **Rate-limit retry**: when the provider returns HTTP 429 the call is
    retried up to :data:`_RATE_LIMIT_MAX_RETRIES` times using *decorrelated
    jitter* anchored at the ``Retry-After`` floor — the actual sleep is
    ``random.uniform(retry_after, retry_after + cap)`` which draws from
    the closed interval ``[retry_after, retry_after + cap]`` (both
    endpoints are reachable modulo float rounding, matching the Python
    :func:`random.uniform` contract), where
    ``cap = min(_RATE_LIMIT_MAX_DELAY, base * 2^attempt)``. Anchoring at
    the floor honours the provider's hint; spreading uniformly above it
    de-correlates concurrent callers (so an N-way retry storm never
    reconverges to the same wake-up instant) and prevents the
    thundering-herd pattern described in the AWS Architecture Blog. The
    final 429 (after all retries are exhausted) is reported with an
    ``"after N retries"`` log label so the exhaustion path is visible in
    logs.

    **Transient-error retry (opt-in)**: when the caller passes
    ``retry_statuses`` (typically :data:`TRANSIENT_RETRY_STATUSES` =
    502/503/504), responses with those statuses are retried with the same
    decorrelated-jitter backoff as 429s — including honouring a
    ``Retry-After`` header, which RFC 9110 permits on 503. Only opt in
    idempotent calls (reads, queries): a mutation could be applied twice
    when the first attempt succeeded behind a failing gateway.

    Parameters
    ----------
    client:
        An ``httpx.AsyncClient`` or :class:`AutoRefreshClient` instance
        (must already be inside its ``async with`` context).
    method:
        HTTP verb – ``"get"``, ``"post"``, ``"patch"``, ``"put"``, ``"delete"``.
    url:
        The request URL.
    provider:
        Human-readable provider name forwarded to :func:`format_http_error`.
    action:
        Short description of the attempted action.
    expected_statuses:
        Optional set of non-success status codes that are *expected* for this
        call (e.g. ``{404}`` when probing a resource that may legitimately not
        exist before falling back to another path). When the response status is
        in this set, the standard warning log is suppressed — the error string
        is still returned so the caller can branch on it. Keeps benign,
        high-frequency misses (e.g. resolving meetings the user didn't organize)
        out of the warning stream. Passing this set also opts the call into
        receiving the failed ``httpx.Response`` (instead of ``None``) on *any*
        non-2xx status, so the caller can inspect ``resp.status_code``. Pass an
        **empty set** to opt into receiving the failed response while keeping
        every non-2xx status in the warning log — use this when the caller
        wants to branch on a genuine error (e.g. GitHub's 405 merge failure)
        rather than silence a benign probe miss.
    retry_statuses:
        Optional set of transient non-2xx statuses to retry with backoff in
        addition to 429 (use :data:`TRANSIENT_RETRY_STATUSES` for the
        standard 502/503/504 gateway set). Leave ``None`` (the default) for
        non-idempotent calls.
    max_total_seconds:
        Optional budget for the whole call: a retry whose wait would end
        past it is not made, and the response in hand is answered as if the
        retries were exhausted. For a caller whose own timeout bounds the
        call, not one attempt.
    **kwargs:
        Passed through to the underlying HTTP method (``headers``, ``json``,
        ``params``, ``content``, etc.).
    """
    log_path = urlparse(url).path
    started = time.monotonic()
    # What the call did, recorded on the tool span it runs under:
    # telemetry only, never a change to the request or its outcome.
    seen = {"5xx": 0, "429": 0}

    def _note(status: int | None, attempt: int, *, network_error: bool = False) -> None:
        _record_tool_http_call(
            provider,
            ms=(time.monotonic() - started) * 1000.0,
            status=status,
            retries=attempt,
            count_5xx=seen["5xx"],
            count_429=seen["429"],
            network_error=network_error,
        )

    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            # Dispatch through ``request()`` rather than ``getattr(client, method)``
            # so a request body (``json``/``content``/``data``) can ride on *any*
            # verb. httpx's ``get``/``delete``/``head``/``options`` convenience
            # methods do NOT accept a ``json=`` keyword and raise
            # ``TypeError: get() got an unexpected keyword argument 'json'`` — which
            # silently broke every GET tool that forwarded a (possibly ``None``)
            # body, and made bodied DELETEs (e.g. HackerOne's cancel-disclosure)
            # impossible. ``request()`` accepts the body params uniformly and is
            # exactly what the verb helpers delegate to internally, so this is a
            # behaviour-preserving change for every call that already worked.
            resp: httpx.Response = await client.request(method.upper(), url, **kwargs)
        except httpx.HTTPError as exc:
            err = format_network_error(exc, provider, action)
            _log_request_warning(provider, method, log_path, err, label="network error")
            _note(None, attempt, network_error=True)
            return None, err

        if resp.status_code >= 500:
            seen["5xx"] += 1
        is_rate_limited = resp.status_code == 429
        seen["429"] += int(is_rate_limited)
        if is_rate_limited:
            # The assignment whose run met it halves its concurrency for a while.
            await _note_integration_rate_limit()
        is_transient = bool(retry_statuses and resp.status_code in retry_statuses)
        has_retries_left = attempt < _RATE_LIMIT_MAX_RETRIES

        if (is_rate_limited or is_transient) and has_retries_left:
            # ``_parse_retry_after`` already clamps to _RATE_LIMIT_MAX_DELAY
            # and sanitises NaN/inf/negative inputs, so ``retry_after`` is
            # always a non-negative finite number bounded by the same cap
            # as the exponential backoff.
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            cap = min(_RATE_LIMIT_MAX_DELAY, _RATE_LIMIT_BASE_DELAY * (2**attempt))
            # Decorrelated jitter: anchor at the Retry-After floor but draw
            # uniformly above it so N concurrent callers with the same
            # provider-supplied delay never wake at the same instant.
            wait = random.uniform(retry_after, retry_after + cap)  # noqa: S311
            # Retry jitter for backoff; not used for secrets or security decisions.
            if max_total_seconds is not None and (
                time.monotonic() - started + wait > max_total_seconds
            ):
                has_retries_left = False
        if (is_rate_limited or is_transient) and has_retries_left:
            _log_request_warning(
                provider,
                method,
                log_path,
                f"retry {attempt + 1}/{_RATE_LIMIT_MAX_RETRIES} in {wait:.1f}s",
                label=(
                    "rate limit (429)"
                    if is_rate_limited
                    else f"transient error ({resp.status_code})"
                ),
            )
            await asyncio.sleep(wait)
            continue

        if not resp.is_success:
            err = format_http_error(resp, provider, action)
            # Suppress the warning for statuses the caller flagged as expected
            # (e.g. a 404 probe before falling back to another path). The error
            # string is still returned so the caller can branch on it.
            if not (expected_statuses and resp.status_code in expected_statuses):
                # When ``is_rate_limited`` / ``is_transient`` is True at this
                # point the retries are necessarily exhausted
                # (``has_retries_left`` is False), so surface that explicitly
                # in the log so operators can spot sustained throttling or a
                # genuinely down upstream without crawling per-attempt lines.
                if is_rate_limited:
                    label = (
                        f"rate limit exceeded after {_RATE_LIMIT_MAX_RETRIES} retries"
                    )
                elif is_transient:
                    label = (
                        f"transient error ({resp.status_code}) persisted "
                        f"after {_RATE_LIMIT_MAX_RETRIES} retries"
                    )
                else:
                    label = "HTTP error"
                _log_request_warning(provider, method, log_path, err, label=label)
            _note(resp.status_code, attempt)
            # Only callers that opted in by passing ``expected_statuses`` (i.e.
            # they explicitly want to branch on the outcome) receive the failed
            # response so they can read ``resp.status_code``. Every other caller
            # keeps the historical ``(None, err)`` contract — many providers use
            # ``resp is None`` / a truthiness check as their failure signal
            # (validate_connection, get_user_info, …) and would otherwise treat
            # a 4xx/5xx as success. Network errors (handled above) always return
            # None because no response exists.
            if expected_statuses is not None:
                return resp, err
            return None, err
        _note(resp.status_code, attempt)
        return resp, None

    # Unreachable: every iteration either ``continue``s or ``return``s.
    # Kept as a defensive guard so a future refactor cannot silently fall
    # through to ``None`` without the type checker noticing.
    raise RuntimeError("safe_request retry loop exited unexpectedly")


# ---------------------------------------------------------------------------
# List / pagination formatting – used by virtually every list/search tool.
# ---------------------------------------------------------------------------


def plural_noun(noun: str) -> str:
    """The English plural of *noun*, inflecting its last word.

    ``entity`` -> ``entities``, ``KEV entry`` -> ``KEV entries``, ``address``
    -> ``addresses``, ``index`` -> ``indexes``, ``match`` -> ``matches``,
    ``key`` -> ``keys``; anything else takes an ``s``. A noun whose plural is
    not on its last word (``group with IOCs``) is spelled by its caller.
    """
    if not noun:
        return noun
    lowered = noun.lower()
    if lowered.endswith("y") and len(noun) > 1 and lowered[-2] not in "aeiou":
        return f"{noun[:-1]}ies"
    if lowered.endswith(("s", "x", "z", "ch", "sh")):
        return f"{noun}es"
    return f"{noun}s"


def format_entity_list(
    items: list[dict[str, Any]],
    formatter: Callable[[dict[str, Any]], str],
    *,
    noun: str = "item",
    total: int | None = None,
    empty_msg: str | None = None,
    plural: str | None = None,
) -> str:
    """Format a list of entities with a consistent header and body.

    Returns *empty_msg* (or ``"No {nouns} found."``) when *items* is empty,
    otherwise ``"Found N {noun or nouns}:\\n…"``. When *total* exceeds what
    *items* holds, the header says how many are shown (``"Found 30145
    entities, showing 2:"``), so the caller never reads a page as the whole.

    Parameters
    ----------
    items:
        The list of raw dicts to format.
    formatter:
        A callable that turns a single dict into a one-line string.
    noun:
        Singular entity name used in the header (e.g. ``"company"``).
    total:
        Explicit total count (useful when paginating).  Falls back to
        ``len(items)``.
    empty_msg:
        Custom empty-state message.
    plural:
        The plural of *noun* when :func:`plural_noun` cannot form it.
    """
    nouns = plural or plural_noun(noun)
    if not items:
        return empty_msg or f"No {nouns} found."
    count = total if total is not None else len(items)
    shown = f", showing {len(items)}" if count > len(items) else ""
    header = f"Found {count} {noun if count == 1 else nouns}{shown}:"
    body = "\n".join(formatter(item) for item in items)
    return f"{header}\n{body}"


def clip_text(text: str, limit: int | None, read_call: str = "") -> str:
    """*text* whole when it fits *limit* (or *limit* is ``None``), else cut and marked.

    A listing that shows the head of a long text never cuts it silently: the
    cut falls on a word boundary and ends with ``... [truncated, N more
    characters: read it in full with <read_call>]``, *read_call* being the
    exact call that returns the whole text (left out when empty). A cut
    never drops more than half of what *limit* allows to reach a boundary
    (a long URL, text without spaces).
    """
    if limit is None or len(text) <= limit:
        return text
    head = text[:limit]
    if head and not (head[-1].isspace() or text[limit].isspace()):
        words = head.rsplit(None, 1)
        if len(words) == 2 and len(words[0]) >= limit // 2:
            head = words[0]
    head = head.rstrip()
    where = f": read it in full with {read_call}" if read_call else ""
    return f"{head} ... [truncated, {len(text) - len(head)} more characters{where}]"


def format_pagination_hint(
    cursor: str | None,
    param_name: str = "after",
) -> str:
    """Return a pagination hint string, or ``""`` if there is no next page.

    Produces text like::

        [More results available — pass after="abc123"]
    """
    if not cursor:
        return ""
    return f'\n[More results available — pass {param_name}="{cursor}"]'


# ---------------------------------------------------------------------------
# Short-lived-JWT freshness — proactive re-mint before expiry
# ---------------------------------------------------------------------------

#: Re-mint a short-lived bearer JWT this many seconds BEFORE it expires, rather
#: than waiting for the peer to reject it. A cross-platform XTM JWT
#: (``_XTM_JWT_TTL_MINUTES`` = 10 min) has a usable life that is short relative
#: to an autonomous run, so a purely reactive refresh guarantees a burst of
#: rejections every ~10 min - one per in-flight call, since the orchestrator
#: fires callbacks with ``asyncio.gather``. Refreshing a couple of minutes early
#: keeps every call on a valid token, so those bursts never happen. Generous
#: enough to absorb clock skew between the two platforms and a slow in-flight
#: call.
JWT_PROACTIVE_REFRESH_SKEW_SECONDS = 120.0


class RefreshableHeaders(dict):
    """Auth headers that optionally carry a token re-mint callback.

    Platform-managed integrations (OpenAEV / OpenCTI / OpenCRQ) authenticate
    with a short-lived (``_XTM_JWT_TTL_MINUTES`` = 10 min) cross-platform JWT
    minted ONCE at tool-build time. A long autonomous run outlives that TTL,
    after which every call to the peer platform fails - so the run can neither
    progress nor settle itself. Two mechanisms keep the token valid, both
    mutating THIS dict in place so every later call on the shared instance sees
    the new token:

    * PROACTIVE (:func:`ensure_fresh_token`, the common case): the provider's
      request helper re-mints the JWT just BEFORE it expires, so a well-behaved
      call never sends an expired token and the peer never has to reject it.
      Concurrent callers coalesce on :attr:`refresh_lock` so one expiry window
      triggers exactly one re-mint, not one per in-flight ``asyncio.gather``
      call.
    * REACTIVE (:class:`AutoRefreshClient`, the backstop): if a call still races
      an expiry (or the peer revokes early), a 401/403 re-mints and retries once.

    A plain-dict headers value (no callback) keeps the original single-shot
    behaviour, so nothing changes for a static-API-key integration.
    """

    def __init__(
        self, *args: Any, refresh_fn: RefreshFn | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.refresh_fn = refresh_fn
        # Lazily-bound (no running loop needed at construction on 3.10+); serialises
        # concurrent proactive re-mints so a gather() burst mints ONE fresh token.
        self.refresh_lock = asyncio.Lock()


def bearer_seconds_remaining(headers: Any) -> float | None:
    """Seconds until the ``Authorization: Bearer`` JWT in *headers* expires.

    Peeks the JWT ``exp`` claim WITHOUT verifying the signature - this is our own
    freshly minted token and the only question is timing, never trust. Returns
    ``None`` for anything that is not a parseable JWT with a numeric ``exp`` (an
    opaque/static API key, a malformed value): such a token has no known expiry,
    so proactive refresh is skipped and only the reactive backstop applies.
    """
    auth = headers.get("Authorization") if hasattr(headers, "get") else None
    if not isinstance(auth, str) or not auth.startswith("Bearer "):
        return None
    parts = auth[len("Bearer ") :].split(".")
    if len(parts) != 3:
        return None
    try:
        # base64url without padding -> pad to a multiple of 4 before decoding.
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)):
            return None
        return float(exp) - time.time()
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return None


async def ensure_fresh_token(headers: Any, refresh_fn: RefreshFn) -> None:
    """Proactively re-mint the JWT in *headers* if it is within the refresh skew.

    Coalesced and double-checked: the first caller into the expiry window takes
    :attr:`RefreshableHeaders.refresh_lock`, re-mints once and updates the shared
    ``Authorization`` header in place; callers that were waiting on the lock
    re-check the (now-refreshed) token and return without minting again. A
    re-mint failure is swallowed - the request proceeds on the current token and
    the reactive :class:`AutoRefreshClient` still covers a genuine expiry.
    """
    remaining = bearer_seconds_remaining(headers)
    if remaining is None or remaining > JWT_PROACTIVE_REFRESH_SKEW_SECONDS:
        return
    lock = getattr(headers, "refresh_lock", None)
    if lock is None:
        return
    async with lock:
        # Re-check under the lock: a peer call may have just refreshed it.
        remaining = bearer_seconds_remaining(headers)
        if remaining is not None and remaining > JWT_PROACTIVE_REFRESH_SKEW_SECONDS:
            return
        try:
            new_token = await refresh_fn()
        except Exception:
            # Never fail a call over a proactive re-mint - the reactive
            # AutoRefreshClient still covers a genuine expiry on a 401/403.
            logger.warning(
                "Proactive token refresh failed; proceeding on current token",
                exc_info=True,
            )
            return
        if new_token:
            headers["Authorization"] = f"Bearer {new_token}"


# ---------------------------------------------------------------------------
# AutoRefreshClient – httpx wrapper with automatic 401 retry
# ---------------------------------------------------------------------------


class AutoRefreshClient:
    """httpx-like async context-manager client with automatic 401 retry.

    Usage inside a provider ``build_tools``::

        def build_tools(token, executor, refresh_fn=None):
            headers = {"Authorization": f"Bearer {token}", ...}

            def _client():
                return AutoRefreshClient(headers, refresh_fn)

            async def some_tool():
                async with _client() as client:
                    resp = await client.get(url)
                    ...
    """

    # Shared connection pool limits — reused across all AutoRefreshClient
    # instances to cap total outgoing connections to external APIs.
    # max_connections: hard cap on total open sockets.
    # max_keepalive_connections: idle sockets kept alive for reuse.
    _LIMITS = httpx.Limits(
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=30,
    )

    def __init__(
        self,
        headers: dict[str, str],
        refresh_fn: RefreshFn | None = None,
        timeout: int = 30,
        *,
        follow_redirects: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: ssl.SSLContext | bool = True,
        event_hooks: dict[str, list[Any]] | None = None,
        mounts: dict[str, httpx.AsyncBaseTransport | None] | None = None,
    ) -> None:
        self._headers = headers
        self._refresh_fn = refresh_fn
        self._timeout = timeout
        self._follow_redirects = follow_redirects
        self._transport = transport
        self._verify = verify
        self._event_hooks = event_hooks
        self._mounts = mounts
        self._client: httpx.AsyncClient | None = None
        self._refreshed = False  # Only attempt one refresh per client instance

    # -- context manager --------------------------------------------------

    async def __aenter__(self) -> AutoRefreshClient:
        self._client = egress_client(
            timeout=self._timeout,
            limits=self._LIMITS,
            follow_redirects=self._follow_redirects,
            verify=self._verify,
            event_hooks=self._event_hooks,
            **({"transport": self._transport} if self._transport is not None else {}),
            **({"mounts": self._mounts} if self._mounts is not None else {}),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._client:
            await self._client.aclose()

    # -- HTTP verbs -------------------------------------------------------

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("headers", self._headers)
        return await self._request("get", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("headers", self._headers)
        return await self._request("post", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("headers", self._headers)
        return await self._request("patch", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("headers", self._headers)
        return await self._request("put", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("headers", self._headers)
        return await self._request("delete", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Verb-agnostic request entry point (mirrors ``httpx.AsyncClient.request``).

        ``safe_request`` dispatches through this method so a request body can be
        carried on any verb (including GET/DELETE) with the same 401/403 refresh
        semantics as the dedicated verb helpers.
        """
        kwargs.setdefault("headers", self._headers)
        return await self._request(method, url, **kwargs)

    @asynccontextmanager
    async def stream(
        self, method: str, url: str, **kwargs: Any
    ) -> AsyncIterator[httpx.Response]:
        """``httpx.AsyncClient.stream`` with the same single 401/403 refresh.

        The body is left unread, so a download can stop at a size cap
        (``file_delivery.fetch_file``). Only a request sent with this client's
        own token is refreshed and retried: one sent without it, or with
        another credential (a pre-authenticated URL), never gets the token.
        """
        if self._client is None:
            raise RuntimeError("Use AutoRefreshClient as an async context manager.")
        headers = kwargs.setdefault("headers", self._headers)
        sent = [
            str(value) for key, value in (headers or {}).items() if str(key).lower() == "authorization"
        ]
        carries_token = sent == [self._headers.get("Authorization", "")] and bool(sent[0])
        http_method = method.upper()
        async with self._client.stream(http_method, url, **kwargs) as resp:
            if (
                resp.status_code not in (401, 403)
                or not carries_token
                or not await self.refresh_once()
            ):
                yield resp
                return
        kwargs["headers"] = {
            **{key: value for key, value in headers.items() if str(key).lower() != "authorization"},
            "Authorization": self._headers["Authorization"],
        }
        self._client.cookies.clear()
        async with self._client.stream(http_method, url, **kwargs) as retried:
            yield retried

    # -- token re-mint ----------------------------------------------------

    async def refresh_once(self) -> bool:
        """Re-mint the bearer token once for this client instance.

        Returns ``True`` when the shared headers dict was updated with a new
        token (so the caller should retry), ``False`` when there is nothing to
        retry with: no refresh callback, this instance already spent its single
        refresh, or the callback came back empty.

        :meth:`_request` calls this on a 401/403. Providers whose upstream
        reports auth failures *inside* a 200 response — OpenCTI answers an
        expired token with an ``AUTH_REQUIRED`` entry in the GraphQL ``errors``
        array — call it directly, then re-issue the request once. The single-shot
        budget is shared between both paths, so one client instance never mints
        more than one token however the failure surfaced.
        """
        if self._refreshed or not self._refresh_fn:
            return False
        new_token = await self._refresh_fn()
        if not new_token:
            logger.warning("Token refresh returned nothing — not retrying")
            return False
        # Mutate the shared headers dict so *all* closures see the update
        self._headers["Authorization"] = f"Bearer {new_token}"
        self._refreshed = True
        return True

    # -- internal ---------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Execute *method* on *url*; on 401/403 refresh and retry once."""
        if self._client is None:
            raise RuntimeError("Use AutoRefreshClient as an async context manager.")
        # Route every verb through ``request()`` so a body (``json``/``content``)
        # can ride on GET/DELETE without httpx's verb helpers raising a TypeError.
        http_method = method.upper()

        resp: httpx.Response = await self._client.request(http_method, url, **kwargs)

        if resp.status_code not in (401, 403):
            return resp

        # --- token looks expired — try to refresh -------------------------
        # Log the path only, like ``safe_request``: a provider may carry a
        # credential in the query string (``?apikey=``, a signed URL).
        log_path = urlparse(str(url)).path
        logger.info("Got %s from %s — attempting token refresh", resp.status_code, log_path)
        if not await self.refresh_once():
            return resp  # Already tried, no callback, or the re-mint came back empty

        # Also patch the headers going into the retry (may be a spread copy)
        if "headers" in kwargs:
            kwargs["headers"] = {
                **kwargs["headers"],
                "Authorization": self._headers["Authorization"],
            }

        logger.info("Token refreshed — retrying %s %s", http_method, log_path)
        # Re-authenticate as a CLEAN, cookie-less bearer request. The failed
        # attempt may have accumulated a cookie in the shared client jar: an
        # auth-required response can carry ``Set-Cookie`` (e.g. a server that
        # opens a session on its 401), and this one client is reused across the
        # 401 and its retry. Replaying that cookie on the retry can defeat a
        # server-side "bearer request without cookies" CSRF exemption and turn
        # the retry into a 403 - the exact failure that stopped long OpenAEV
        # autonomous runs from recording events / authoring steps once their
        # 10-minute JWT expired mid-run. A bearer/API-key client is stateless, so
        # dropping any cookie the failed attempt left behind is always safe.
        self._client.cookies.clear()
        return await self._client.request(http_method, url, **kwargs)
