# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""MCP client manager – connects to MCP servers and exposes their tools.

Supports three transports:
  * **stdio**           – spawns a local subprocess (e.g. ``npx -y @modelcontextprotocol/server-github``).
  * **sse**             – connects to a remote HTTP+SSE endpoint (legacy).
  * **streamable_http** – connects via the modern Streamable HTTP transport (recommended for remote servers).

**Lazy connections**: When an MCP server has cached ``discovered_tools`` in the
database, the manager registers tool definitions without opening a connection.
The actual transport connection is established lazily on the first
``execute_tool()`` call.  This avoids the overhead of connecting to every MCP
server on every chat message / assignment run when most messages don't invoke MCP
tools at all.

Usage inside a skill::

    manager = MCPManager()
    # Fast path: use DB-cached tools (no connection yet)
    manager.register_cached_server(server_config, cached_tools)
    # Slow path (fallback when no cache): connect immediately
    await manager.connect_server(server_config)

    result  = await manager.execute_tool(tc) # ToolResult (connects lazily)
    await manager.disconnect_all()
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession as _SDKClientSession
from mcp import StdioServerParameters
from mcp import types as mcp_types
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from app.core.cancellation import uncancel_during_cleanup
from app.core.egress import EgressRoute, egress_client, egress_route
from app.core.utils import format_exception_message
from app.services.code_interpreter import _build_sandbox_env, _sanitize_output
from app.tools.base import (
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolResult,
    is_auth_or_permission_error,
    is_rate_limit_error,
)

logger = logging.getLogger(__name__)


async def runtime_egress_route(config: MCPServerRuntime) -> EgressRoute | None:
    """The route of a server's requests: ``None`` when it is reached directly.

    Read on a session of its own when the connection opens, so a group renamed
    or narrowed meanwhile applies at once. A lookup that fails raises: a routed
    server is never reached directly instead.
    """
    group_id = config.relay_group_id
    if not group_id:
        return None
    from app.core.database import async_session
    from app.services.relays import load_egress_routes

    key = config.server_id or config.name
    async with async_session() as db:
        routes = await load_egress_routes(db, [("mcp_server", key, group_id)])
    return routes.get(str(key)) or EgressRoute(
        group_id=str(group_id), source_type="mcp_server", source_id=str(key)
    )


def mcp_http_client_factory(route: EgressRoute | None) -> Any:
    """The MCP SDK's ``httpx_client_factory``, pinned to *route*.

    The SDK makes its requests from tasks of its own transport, outside the
    caller's context, so the route is bound on the client rather than read
    from the context. Same defaults as the SDK's ``create_mcp_http_client``,
    except that a direct server is held to the MCP server URL policy on every
    connection (``app.tools.mcp_destinations.guarded_client``, which also
    decides whether an outbound proxy is used).

    No ``follow_redirects``: the transports send every request with redirect
    following off and follow a redirect themselves only when it stays on the
    endpoint's origin and keeps the request method (mcp 1.30); any other one
    fails the connection, which :class:`MCPRedirectNotFollowedError` names.
    """
    from mcp.shared._httpx_utils import (
        MCP_DEFAULT_SSE_READ_TIMEOUT,
        MCP_DEFAULT_TIMEOUT,
    )

    from app.tools.mcp_destinations import guarded_client

    def factory(
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: Any = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "timeout": timeout
            or httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
        }
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        if route is not None:
            # The relay connects, from the customer's network, and checks its
            # own allow-list.
            return egress_client(route=route, **kwargs)
        return guarded_client(route=None, **kwargs)

    return factory


def _client_kwargs(route: EgressRoute | None) -> dict[str, Any]:
    """The transport kwargs of a remote server: routed, or held to the URL policy."""
    return {"httpx_client_factory": mcp_http_client_factory(route)}


def _is_cancel_scope_runtime_error(exc: RuntimeError) -> bool:
    """Return True for anyio cross-task cancel-scope teardown RuntimeErrors."""
    message = str(exc).lower()
    return "cancel scope" in message and (
        "different task" in message or "attempted to exit" in message
    )


def _is_teardown_noise_runtime_error(exc: RuntimeError) -> bool:
    """Return True for ``RuntimeError`` patterns that are documented teardown
    noise from the MCP SDK / anyio / CPython async-generator machinery —
    safe to swallow regardless of caller intent.

    Two distinct patterns surface during MCP connection lifecycle:

    1. ``cancel scope`` cross-task teardown (anyio) — see
       :func:`_is_cancel_scope_runtime_error`.
    2. ``athrow()`` / ``aclose(): asynchronous generator is already
       running`` (CPython) — raised when multiple cleanup attempts
       converge on the same generator simultaneously (mass teardown,
       race conditions). The OS / event loop reclaims the underlying
       resources regardless, so propagating them just produces log
       noise without any actionable signal.
    """
    if _is_cancel_scope_runtime_error(exc):
        return True
    message = str(exc).lower()
    return "asynchronous generator is already running" in message


# Exception class names (httpx / httpcore) that mean "could not reach the
# server". Matched by name so we don't hard-import httpcore just for isinstance.
_CONNECT_ERROR_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "ConnectionRefusedError",
        "TimeoutException",
        "ReadTimeout",
        "PoolTimeout",
        "TimeoutError",
    }
)

# JSON-RPC error code the MCP SDK sets on the ``McpError`` it raises for
# every request still pending when the transport closes (for instance during
# ``initialize()``). An operational failure (server shut the connection,
# endpoint gone away), not a code bug, so it gets the same concise WARNING
# treatment as network-level errors. Read from the error's typed code, never
# from its message (invariant 18, #5303).
_MCP_CONNECTION_CLOSED_CODE = mcp_types.CONNECTION_CLOSED


def _is_mcp_connection_closed(exc: BaseException) -> bool:
    """True when *exc* is the SDK's ``McpError`` for a closed transport."""
    if type(exc).__name__ != "McpError":
        return False
    error = getattr(exc, "error", None)
    return getattr(error, "code", None) == _MCP_CONNECTION_CLOSED_CODE


class MCPAuthorizationRequiredError(ConnectionError):
    """The MCP endpoint answered ``401`` — it needs an OAuth authorization.

    Carries the RFC 9728 ``resource_metadata`` URL from the
    ``WWW-Authenticate`` challenge when the server supplied one. That URL is
    *authoritative*: RFC 9728 lets it point at a host or path no well-known
    probe derived from the MCP URL would ever reach, so following the
    challenge is the only discovery path guaranteed to work.

    A ``ConnectionError`` subclass so the existing operational-failure
    handling (concise WARNING, circuit-breaker accounting) treats it like any
    other reachable-but-unusable endpoint rather than a code fault.
    """

    def __init__(self, message: str, *, resource_metadata: str | None = None):
        super().__init__(message)
        self.resource_metadata = resource_metadata


# ``WWW-Authenticate: Bearer resource_metadata="https://…", error="…"`` —
# RFC 9728 §5.1. Quoted form only: that is what the RFC specifies and what
# every MCP server emits, and a permissive unquoted pattern would happily
# swallow the following parameter.
_RESOURCE_METADATA_PATTERN = re.compile(
    r'resource_metadata\s*=\s*"([^"]+)"', re.IGNORECASE
)


def _extract_auth_challenge(exc: BaseException) -> tuple[bool, str | None] | None:
    """Find a ``401`` in *exc*, returning ``(True, resource_metadata_url|None)``.

    The MCP SDK runs its transport inside an anyio task group, so an HTTP
    error raised during ``initialize()`` reaches the caller wrapped in a
    ``BaseExceptionGroup`` (and often behind a ``CancelledError``). Recursing
    through the group leaves and the ``__cause__`` / ``__context__`` chain is
    what makes the underlying ``httpx`` 401 findable at all — without it the
    caller only ever sees "unhandled errors in a TaskGroup".

    Returns ``None`` when no 401 is present.
    """
    seen: set[int] = set()

    def _walk(node: BaseException | None) -> tuple[bool, str | None] | None:
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))

        response = getattr(node, "response", None)
        if response is not None and getattr(response, "status_code", None) == 401:
            headers = getattr(response, "headers", None) or {}
            try:
                challenge = headers.get("www-authenticate", "") or ""
            except Exception:
                challenge = ""
            match = _RESOURCE_METADATA_PATTERN.search(challenge)
            return True, (match.group(1) if match else None)

        for sub in getattr(node, "exceptions", None) or ():
            found = _walk(sub)
            if found:
                return found
        return _walk(node.__cause__) or _walk(node.__context__)

    return _walk(exc)


# A redirect target is the remote server's text: it is quoted, never followed,
# and quoted short.
_REDIRECT_LOCATION_MAX_CHARS = 300


class MCPRedirectNotFollowedError(ConnectionError):
    """The MCP endpoint redirects where its transport does not follow.

    Since mcp 1.30 the remote transports follow a redirect only when it stays
    on the endpoint's origin and keeps the request method; any other one (apex
    to ``www``, an endpoint moved to another host, a 301/302 on a POST, an
    https endpoint sent to plain http) fails the request with an
    ``httpx.HTTPStatusError`` for the redirect response. ``location`` is where
    it pointed, as :func:`_unfollowed_redirect` shows it. A
    ``ConnectionError``, so every caller already treats it as a failed
    connection.
    """

    def __init__(self, message: str, *, location: str):
        super().__init__(message)
        self.location = location


def _unfollowed_redirect(exc: BaseException) -> tuple[int, str] | None:
    """Find a redirect response the transport did not follow in *exc*.

    Returns ``(status_code, location)``, else ``None``. The location is the
    SDK's ``redirect_location`` (no userinfo, query or fragment, which can carry
    state), with a token-shaped path segment masked
    (:func:`app.core.utils.redact_secret_text`), cut to
    ``_REDIRECT_LOCATION_MAX_CHARS``. The failure reaches ``connect()`` inside
    the transport's task group, so the group leaves and the ``__cause__`` /
    ``__context__`` chain are walked, as in :func:`_extract_auth_challenge`.
    """
    from mcp.shared._httpx_utils import redirect_location

    from app.core.utils import redact_secret_text

    seen: set[int] = set()

    def _walk(node: BaseException | None) -> tuple[int, str] | None:
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        if isinstance(node, httpx.HTTPStatusError) and node.response.is_redirect:
            target = redirect_location(node.response)
            if target is not None:
                location = str(redact_secret_text(str(target)))
                if len(location) > _REDIRECT_LOCATION_MAX_CHARS:
                    location = f"{location[:_REDIRECT_LOCATION_MAX_CHARS]}…"
                return node.response.status_code, location
        for sub in getattr(node, "exceptions", None) or ():
            found = _walk(sub)
            if found:
                return found
        return _walk(node.__cause__) or _walk(node.__context__)

    return _walk(exc)


async def diagnose_auth_after_connect_failure(
    config: MCPServerRuntime,
) -> dict[str, Any] | None:
    """Ask a failed remote connection whether the real answer was "401".

    Returns an ``auth_required`` result, or ``None`` to let the original
    failure stand.

    The cancellation dance here is the crux. anyio's transport scope delivers a
    ``CancelledError`` to *this* task, so the probe cannot await until that
    cancellation is lifted. And when the probe confirms a 401 the cancellation
    must **not** be re-applied: it came from the SDK's internal scope rather
    than from our caller, we have just turned it into an ordinary return value,
    and leaving it pending would cancel the caller's next await — which is the
    endpoint's own database write.

    When the probe finds nothing, the lifted cancellation *is* restored, so a
    genuine cancellation still ends the task at its next checkpoint.
    """
    if config.transport not in ("sse", "streamable_http") or not config.url:
        return None

    task = asyncio.current_task()
    lifted = 0
    while task is not None and task.cancelling() > 0:
        task.uncancel()
        lifted += 1

    challenge: tuple[bool, str | None] | None = None
    try:
        challenge = await probe_auth_challenge(config.url, _credential_headers(config))
    except BaseException:  # a probe must never mask the original failure
        challenge = None

    if challenge is None:
        if lifted and task is not None:
            task.cancel()
        return None

    _, resource_metadata = challenge
    logger.warning(
        "MCP server '%s' requires authorization (HTTP 401)%s",
        config.name,
        f"; resource metadata: {resource_metadata}" if resource_metadata else "",
    )
    return {
        "status": "auth_required",
        "message": f"MCP server '{config.name}' requires authorization",
        "resource_metadata": resource_metadata,
    }


async def probe_auth_challenge(
    url: str, headers: dict[str, str] | None = None
) -> tuple[bool, str | None] | None:
    """Ask the endpoint directly whether it is refusing us for lack of a token.

    Needed because the MCP SDK runs its transport in an anyio task group that
    consumes the HTTP error itself and cancels the pending request: the caller
    of ``initialize()`` can see a bare ``CancelledError`` with no status code
    anywhere in the chain. That is exactly why an unauthorized MCP server used
    to surface as an inscrutable transport failure — the 401 never survived the
    trip. One deliberate request on the failure path recovers it. It follows
    redirects as the transports do (``request_within_origin``).

    Returns ``(True, resource_metadata_url|None)`` on 401, else ``None``.
    """

    if not url:
        return None
    request_headers = dict(headers or {})
    request_headers.setdefault("Accept", "application/json, text/event-stream")
    request_headers.setdefault("Content-Type", "application/json")
    # A well-formed ``initialize`` rather than junk: a server that authenticates
    # before parsing answers 401 either way, but one that validates first should
    # not be handed a malformed body just so we can read its status line.
    body = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "xtm-one-probe", "version": "1.0"},
        },
    }
    try:
        # The configured URL, so the same policy as the connection it
        # diagnoses, and the same redirect rule: a redirect is followed
        # only within the endpoint's origin and when it keeps the method, so a
        # 401 from a host the transport never reaches is not reported.
        from mcp.shared._httpx_utils import request_within_origin

        from app.tools.mcp_destinations import guarded_client

        async with guarded_client(timeout=10.0) as client:
            response = await request_within_origin(
                client, "POST", url, json=body, headers=request_headers
            )
    except Exception:
        return None

    if response.status_code != 401:
        return None
    challenge = response.headers.get("www-authenticate", "") or ""
    match = _RESOURCE_METADATA_PATTERN.search(challenge)
    return True, (match.group(1) if match else None)


def _credential_headers(config: MCPServerRuntime) -> dict[str, str]:
    """The headers a request of this connection carries, the bearer token included.

    An OAuth server's token is not in the static headers but set by the
    connection's auth hook (``services/mcp_oauth_tokens.py``), which may
    have replaced it since the runtime was built: a direct request sends the
    token the hook holds now, never the one the connection was opened with.
    """
    headers = dict(config.headers or {})
    token = getattr(config.auth, "token", None)
    if isinstance(token, str) and token:
        headers = {k: v for k, v in headers.items() if str(k).lower() != "authorization"}
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _pending_authorization_metadata(config: MCPServerRuntime) -> str | None:
    """PRM URL when a *successful* connection still has no usable credential.

    Returns ``None`` — meaning "nothing pending" — whenever the runtime holds a
    credential: an ``Authorization`` header, exactly what every real tool call
    will send, or the auth hook ``build_mcp_runtime`` gives an OAuth server
    holding a token. Their presence is the precise test for "we hold a
    credential", independent of how the server was configured.
    """
    if config.transport not in ("sse", "streamable_http") or not config.url:
        return None
    if any(str(k).lower() == "authorization" for k in _credential_headers(config)):
        return None

    from app.services.mcp_oauth_discovery import find_protected_resource_metadata

    try:
        return await find_protected_resource_metadata(config.url)
    except Exception as exc:  # never fail a connectivity test over a probe
        logger.debug(
            "Protected-resource probe failed for '%s': %s",
            config.name,
            format_exception_message(exc),
        )
        return None


def _connect_error_detail(exc: BaseException) -> str | None:
    """Return a concise reason string if *exc* is a network/connect failure.

    MCP SSE / streamable-HTTP transports surface the underlying httpx/httpcore
    connect error wrapped in an anyio ``BaseExceptionGroup``, so we recurse into
    the group's leaves. A server that is down or unreachable is an operational
    condition (bad URL, offline endpoint), not a code fault — callers use this
    to log a concise WARNING instead of an ERROR with a multi-frame traceback,
    which otherwise spams the logs on every retry. Returns ``None`` when *exc*
    is not a recognisable connect failure (those keep the full traceback).

    Also recognises the SDK's ``McpError`` whose error code is
    ``CONNECTION_CLOSED``: the MCP SDK raises it when the remote endpoint drops
    the connection during ``initialize()``. This is an operational failure
    (server down/misconfigured), not a code bug.
    """
    subs = getattr(exc, "exceptions", None)
    if subs:  # ExceptionGroup / BaseExceptionGroup
        for sub in subs:
            detail = _connect_error_detail(sub)
            if detail:
                return detail
        return None
    if isinstance(exc, (ConnectionError, TimeoutError)) or (
        type(exc).__name__ in _CONNECT_ERROR_NAMES
    ):
        message = str(exc).strip() or type(exc).__name__
        return f"{type(exc).__name__}: {message}"
    # McpError with the CONNECTION_CLOSED code: the MCP SDK raises it when the
    # remote server closes the transport during initialize(). Treat it as an
    # operational failure (same as a TCP connect error) so the log stays a
    # WARNING instead of a full ERROR traceback on every run.
    if _is_mcp_connection_closed(exc):
        message = str(exc).strip() or "Connection closed"
        return f"{type(exc).__name__}: {message}"
    return None


# ---------------------------------------------------------------------------
# Persistent MCP connection-failure tracking (Redis-backed, fail-open)
#
# Tracks consecutive connection failures per MCP server across agent runs.
# After _MCP_PERSISTENT_FAILURE_THRESHOLD consecutive failures the manager
# escalates from WARNING to ERROR so the issue is distinguishable from the
# "background noise" of a single transient blip.  On the next successful
# connect the counter is cleared.  All Redis operations fail open so a Redis
# outage never prevents the agent loop from running.
#
# A second key (mcp:first_failure_at:{server_key}) captures the Unix epoch
# of the *first* failure in the current outage window.  The timestamp is
# written once per outage and every subsequent failure refreshes its TTL
# (mirroring the counter's sliding window), so the window only resets after
# 48 h of silence — never mid-outage.  It lets us surface duration-aware
# messages ("unreachable for 3 days") in both operator logs and the agent's
# system-prompt Tool Availability Notice.
# ---------------------------------------------------------------------------

_MCP_FAILURE_KEY_PREFIX = "mcp:conn_failures:"
_MCP_FIRST_FAILURE_KEY_PREFIX = "mcp:first_failure_at:"
_MCP_FAILURE_KEY_TTL_SECS = 48 * 3600  # auto-expire after 48 h of silence
_MCP_PERSISTENT_FAILURE_THRESHOLD = 3  # escalate to ERROR after N consecutive failures
# After the initial escalation, re-alert only every Nth consecutive failure.
# Logging ERROR on *every* failure past the threshold would reintroduce the
# per-run error noise this module's WARNING downgrade exists to avoid, while
# never re-alerting would let a multi-day outage vanish from the error stream
# after a single line.  With the circuit breaker below, failures are counted
# at most once per _MCP_CIRCUIT_TTL_SECS cooldown window (circuit-skipped
# attempts don't increment the counter), so during a sustained outage the
# ERROR reminder fires at most every 20 counted failures — at least ~100
# minutes apart, and less often when connection attempts themselves are
# sparse (e.g. infrequent scheduled runs).
_MCP_FAILURE_REALERT_EVERY = 20


def _should_escalate_mcp_failure(consecutive: int) -> bool:
    """Return True when the consecutive-failure count warrants an ERROR log.

    Fires once when ``_MCP_PERSISTENT_FAILURE_THRESHOLD`` is first reached,
    then every ``_MCP_FAILURE_REALERT_EVERY`` failures thereafter.  ``0``
    (the fail-open sentinel from ``_record_mcp_connection_failure``) never
    escalates.
    """
    if consecutive < _MCP_PERSISTENT_FAILURE_THRESHOLD:
        return False
    excess = consecutive - _MCP_PERSISTENT_FAILURE_THRESHOLD
    return excess % _MCP_FAILURE_REALERT_EVERY == 0


def format_failure_duration(hours: int) -> str:
    """Return a human-readable duration string for a sustained MCP outage.

    Used in operator logs and end-user Tool Availability Notices (see
    ``builtin_tools.loaders.format_mcp_unavailable_prompt``) to surface how
    long a server has been unreachable (e.g. "3 days", "26 hours").
    """
    if hours >= 48:
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''}"


# Atomic Lua, single round-trip per operational connect failure:
# (a) increment the consecutive-failure counter;
# (b) write the first-failure timestamp once per outage window (the value is
#     never overwritten while the key lives — a plain ``SET … NX EX`` would
#     only apply the expiry on the first write, letting the key expire
#     mid-outage for outages longer than the TTL, the exact case the
#     duration feature exists for);
# (c) unconditionally refresh BOTH keys' TTLs, so counter and timestamp
#     share a true sliding window: they reset only after
#     _MCP_FAILURE_KEY_TTL_SECS of silence, never mid-outage.  The
#     unconditional EXPIRE also guarantees neither key can end up without a
#     TTL (crash / Redis OOM between SET and EXPIRE — same concern as
#     app/core/rate_limit.py).
# KEYS[1] = consecutive-failure counter, KEYS[2] = first-failure timestamp,
# ARGV[1] = current epoch, ARGV[2] = TTL seconds.  Returns the new count.
_MCP_RECORD_FAILURE_LUA = """
local current = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
if redis.call('EXISTS', KEYS[2]) == 1 then
    redis.call('EXPIRE', KEYS[2], ARGV[2])
else
    redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
end
return current
"""

# Counter-only variant (no first-failure timestamp): used by the tool-level
# auth-failure tracking below, which has no duration-aware messaging.
_MCP_FAILURE_INCR_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
else
    local ttl = redis.call('TTL', KEYS[1])
    if ttl < 0 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
end
return current
"""


async def _record_mcp_connection_failure(server_key: str) -> int:
    """Record one operational connect failure for an MCP server in Redis.

    A single atomic Lua round-trip increments the consecutive-failure
    counter AND sets (first failure of the window) or TTL-refreshes
    (subsequent failures, timestamp preserved) the first-failure timestamp
    used for duration-aware messages.

    *server_key* is the server's UUID string (``MCPServerRuntime.server_id``)
    or the server name as a fallback.  Returns the new consecutive failure
    count, or ``0`` on Redis unavailability (fail-open).
    """
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        count = await redis.eval(
            _MCP_RECORD_FAILURE_LUA,
            2,
            f"{_MCP_FAILURE_KEY_PREFIX}{server_key}",
            f"{_MCP_FIRST_FAILURE_KEY_PREFIX}{server_key}",
            str(time.time()),
            _MCP_FAILURE_KEY_TTL_SECS,
        )
        return int(count)
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot track MCP failure count for '%s': %s",
            server_key,
            format_exception_message(exc),
        )
        return 0


async def get_mcp_failure_hours(server_key: str) -> int | None:
    """Return whole hours elapsed since the first recorded connection failure.

    Returns ``None`` when the first-failure key is absent (outage window
    expired or the server has not yet failed) or when Redis is unavailable
    (fail-open).  Callers use this to surface duration-aware messages such as
    "unreachable for 3 days" in both operator logs and end-user notices.
    """
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        val = await redis.get(f"{_MCP_FIRST_FAILURE_KEY_PREFIX}{server_key}")
        if val is None:
            return None
        first_ts = float(val)
        return max(0, int((time.time() - first_ts) / 3600))
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot get MCP failure duration for '%s': %s",
            server_key,
            format_exception_message(exc),
        )
        return None


async def _reset_mcp_connection_failures(server_key: str) -> None:
    """Reset the consecutive failure counter and first-failure timestamp after a successful connection."""
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        await redis.delete(
            f"{_MCP_FAILURE_KEY_PREFIX}{server_key}",
            f"{_MCP_FIRST_FAILURE_KEY_PREFIX}{server_key}",
        )
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot reset MCP failure state for '%s': %s",
            server_key,
            format_exception_message(exc),
        )  # Fail open — a stale counter causes at most one extra log line.


# ---------------------------------------------------------------------------
# Persistent MCP tool-level auth-failure tracking (Redis-backed, fail-open)
#
# Tracks consecutive auth failures at the tool-call level per MCP server
# across agent runs.  After _MCP_AUTH_FAILURE_THRESHOLD consecutive auth
# failures the manager logs an additional ERROR so operators can
# distinguish a permanently-broken credential (e.g. expired GitHub PAT)
# from the per-run tool-error noise that already logs at ERROR via
# logger.exception().
#
# Unlike the connection circuit breaker, auth failures do NOT short-circuit
# the server: the server itself may be healthy and the token may be rotated
# at any time.  A successful tool call resets the counter immediately so a
# rotated credential clears the alert without waiting for the TTL.
#
# All Redis operations fail-open so a Redis outage never blocks tool calls.
# ---------------------------------------------------------------------------

_MCP_AUTH_FAILURE_KEY_PREFIX = "mcp:auth_failures:"
_MCP_AUTH_FAILURE_TTL_SECS = 72 * 3600  # auto-expire after 72 h of silence
_MCP_AUTH_FAILURE_THRESHOLD = 3  # escalate to ERROR after N consecutive auth failures
# Re-alert every Nth failure past the threshold — matches the connection
# failure re-alert pattern so operators see one periodic reminder during a
# sustained credential outage without flooding the error stream.
_MCP_AUTH_FAILURE_REALERT_EVERY = 10


def _should_escalate_mcp_auth_failure(consecutive: int) -> bool:
    """Return True when the consecutive auth-failure count warrants an extra ERROR log.

    Fires once when ``_MCP_AUTH_FAILURE_THRESHOLD`` is first reached,
    then every ``_MCP_AUTH_FAILURE_REALERT_EVERY`` failures thereafter.
    ``0`` (the fail-open sentinel from ``_record_mcp_auth_failure``) never
    escalates.
    """
    if consecutive < _MCP_AUTH_FAILURE_THRESHOLD:
        return False
    excess = consecutive - _MCP_AUTH_FAILURE_THRESHOLD
    return excess % _MCP_AUTH_FAILURE_REALERT_EVERY == 0


async def _record_mcp_auth_failure(server_key: str) -> int:
    """Increment the consecutive auth-failure counter for a server in Redis.

    Returns the new consecutive count, or ``0`` on Redis unavailability
    (fail-open).
    """
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        redis_key = f"{_MCP_AUTH_FAILURE_KEY_PREFIX}{server_key}"
        count = await redis.eval(
            _MCP_FAILURE_INCR_LUA, 1, redis_key, _MCP_AUTH_FAILURE_TTL_SECS
        )
        return int(count)
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot track MCP auth failure count for '%s': %s",
            server_key,
            format_exception_message(exc),
        )
        return 0


async def _reset_mcp_auth_failures(server_key: str) -> None:
    """Reset the consecutive auth-failure counter after a successful tool call."""
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        await redis.delete(f"{_MCP_AUTH_FAILURE_KEY_PREFIX}{server_key}")
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot reset MCP auth failure count for '%s': %s",
            server_key,
            format_exception_message(exc),
        )  # Fail open — a stale counter causes at most one extra log line.


async def _get_mcp_auth_failure_count(server_key: str) -> int:
    """Return the current consecutive auth-failure count for *server_key*.

    Used at run-start to pre-warn the LLM about persistently broken
    credentials before it wastes iterations calling broken tools.
    Returns ``0`` on Redis unavailability (fail-open).
    """
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        val = await redis.get(f"{_MCP_AUTH_FAILURE_KEY_PREFIX}{server_key}")
        return int(val) if val is not None else 0
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot read MCP auth failure count for '%s': %s",
            server_key,
            format_exception_message(exc),
        )
        return 0


#: The remediation of a credential failure on a ``per_user`` MCP server.
_PER_USER_REMEDIATION = (
    "This MCP server uses each person's own account: the person this run acts as "
    "must connect (or reconnect) their account on the MCP server's page."
)


def _auth_escalation_fix_hint(is_company_managed: bool | None, per_user: bool = False) -> str:
    """Return the operator remediation sentence for the auth-failure escalation.

    Mirrors the ownership-driven wording of :func:`_build_mcp_error_message`:
    ``True`` escalates to an administrator, ``False`` keeps the self-service
    wording, and ``None`` (runtime built without a DB record) falls back to a
    neutral conditional hint. On a ``per_user`` server the account is the
    acting person's own, whoever manages the server.
    """
    if per_user:
        return _PER_USER_REMEDIATION
    if is_company_managed is True:
        return (
            "This MCP server is company-managed — an administrator must "
            "re-authorize or rotate the credential in the MCP server settings."
        )
    if is_company_managed is False:
        return "Re-authorize or rotate the credential in the MCP server settings."
    return (
        "Re-authorize or rotate the credential in the MCP server settings "
        "(ask an administrator if the server is company-managed)."
    )


# ---------------------------------------------------------------------------
# MCP connection circuit breaker (Redis-backed, fail-open)
#
# After _MCP_PERSISTENT_FAILURE_THRESHOLD consecutive connection failures the
# circuit breaker "opens": subsequent connect() calls are skipped immediately
# (no subprocess spawn or network timeout) so the agent loop is not stalled
# waiting for an endpoint that is known to be unreachable.
#
# The circuit stays open for _MCP_CIRCUIT_TTL_SECS (5 minutes) via Redis TTL.
# After that window the key expires ("half-open"), and the next connect()
# attempt is allowed through.  A successful connection resets both the failure
# counter and the circuit key; a failed attempt re-opens the circuit with a
# fresh TTL.
#
# All Redis operations fail-open: a Redis outage never prevents connections.
# ---------------------------------------------------------------------------

_MCP_CIRCUIT_KEY_PREFIX = "mcp:circuit_open:"
_MCP_CIRCUIT_TTL_SECS = 5 * 60  # 5-minute cooldown when circuit is open

# Overall bound (seconds) on one MCP connection attempt: transport connect +
# session initialize() + list_tools().  Without it a hanging endpoint (TCP
# accepts but never answers initialize — a half-dead SSE server, a stdio
# subprocess that starts but never speaks) blocks the chat setup phase or a
# lazy first-use connect INDEFINITELY, and the circuit breaker never opens
# because a hang never *fails*.  Generous enough for a cold ``npx``
# stdio server that must download its package on first run.  A timeout is
# classified as an operational connect failure (``_connect_error_detail``
# recognises TimeoutError), so it counts toward the circuit breaker.
_MCP_CONNECT_TIMEOUT_SECS = 60


class _MCPCircuitOpenError(ConnectionError):
    """Raised when the circuit breaker is open for an MCP server.

    This is an operational failure (the server has been persistently
    unreachable); callers treat it identically to any other connection
    error (skip the server, return an error ToolResult, etc.).
    """


async def _is_mcp_circuit_open(server_key: str) -> bool:
    """Return True when the circuit breaker is open for *server_key*.

    Fails open (returns False) when Redis is unavailable so a Redis
    outage never prevents connection attempts.
    """
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        return bool(await redis.exists(f"{_MCP_CIRCUIT_KEY_PREFIX}{server_key}"))
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot check MCP circuit state for '%s': %s",
            server_key,
            format_exception_message(exc),
        )
        return False  # fail-open: allow the connection attempt


async def _open_mcp_circuit(server_key: str) -> None:
    """Open (or refresh) the circuit breaker for *server_key*.

    Sets a Redis key with a TTL of _MCP_CIRCUIT_TTL_SECS.  Calling this
    when the key already exists resets the TTL, extending the cooldown
    after each additional failure.
    """
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        await redis.set(
            f"{_MCP_CIRCUIT_KEY_PREFIX}{server_key}",
            "1",
            ex=_MCP_CIRCUIT_TTL_SECS,
        )
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot open MCP circuit for '%s': %s",
            server_key,
            format_exception_message(exc),
        )  # Fail-open: worst case, the cooldown is not enforced this cycle.


async def _close_mcp_circuit(server_key: str) -> None:
    """Close the circuit breaker for *server_key* after a successful connection."""
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        await redis.delete(f"{_MCP_CIRCUIT_KEY_PREFIX}{server_key}")
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot close MCP circuit for '%s': %s",
            server_key,
            format_exception_message(exc),
        )  # Fail-open: stale circuit key expires on its own TTL.


def _build_mcp_env(user_env: dict[str, str] | None) -> dict[str, str]:
    """Build a safe environment for stdio MCP subprocesses.

    Starts from a minimal safe base (PATH, HOME, etc.) and layers the
    user-configured env vars on top.  This ensures MCP subprocesses
    never inherit backend secrets (DATABASE_URL, SECRET_KEY, etc.)
    from the parent process.
    """
    safe = _build_sandbox_env()
    if user_env:
        safe.update(user_env)
    return safe


def _resolve_stdio_command(command: str, args: list[Any]) -> tuple[str, list[str]]:
    """Resolve stdio command with compatibility fallbacks.

    On some runtimes, ``uv`` is installed but the ``uvx`` shim is missing.
    In that case, translate ``uvx <args...>`` to ``uv tool run <args...>``.

    Persisted configs may carry stray whitespace (e.g. ``"uvx "``), which
    would make PATH lookup fail on an otherwise-installed command.  Trim
    the command and each arg, and drop args that are blank after trimming
    so no empty argv tokens reach the subprocess.

    Imported configs come from untyped JSON bundles, so values may not be
    strings at all: a non-string command is rejected with a clear config
    error, and non-string args (e.g. a bare port number) are coerced via
    ``str()`` — argv tokens must be strings before reaching the subprocess.
    """
    if not isinstance(command, str):
        raise ValueError(
            "Cannot start MCP server: the configured stdio 'command' must "
            f"be a string, got {type(command).__name__}."
        )
    command = command.strip()
    args = [
        stripped
        for arg in args
        if (stripped := (arg if isinstance(arg, str) else str(arg)).strip())
    ]

    if not command:
        raise ValueError(
            "Cannot start MCP server: the configured stdio 'command' is "
            "empty or whitespace-only. Set it to an executable name or path."
        )

    if shutil.which(command):
        return command, args

    if command == "uvx" and shutil.which("uv"):
        logger.info("MCP stdio command 'uvx' not found, falling back to 'uv tool run'")
        return "uv", ["tool", "run", *args]

    # ``shutil.which`` returns ``None`` for two distinct failure modes:
    #
    #   * a bare command name (e.g. ``my-mcp-server``) that isn't on
    #     ``PATH`` — actionable fix: install the command, mount it into
    #     the container, or set ``PATH``;
    #   * an explicit path (absolute like ``/usr/local/bin/server`` or
    #     relative like ``./bin/server``) that doesn't exist or doesn't
    #     have the executable bit set — actionable fix: check the path
    #     and ``chmod +x``.
    #
    # Tailor the error message to the configured shape so the LLM and
    # the operator both see actionable guidance instead of a generic
    # "not in PATH" message that's wrong half the time.  We use the same
    # path-detection rule ``shutil.which`` uses internally
    # (``os.sep``/``os.altsep`` membership) so the two stay in sync.
    is_explicit_path = os.sep in command or (
        os.altsep is not None and os.altsep in command
    )
    if is_explicit_path:
        raise FileNotFoundError(
            f"Cannot start MCP server: command '{command}' does not exist or "
            "is not executable. Check that the path is correct and that the "
            "file has the executable bit set."
        )
    raise FileNotFoundError(
        f"Cannot start MCP server: command '{command}' was not found in PATH. "
        "Install the command in the runtime/container or provide an absolute executable path."
    )


# ---------------------------------------------------------------------------
# Stderr capture for stdio MCP servers
#
# MCP stdio servers write their own log messages (e.g. "GitHub MCP Server
# running on stdio") to stderr.  By default these bypass the Python logging
# system and appear as raw text, breaking structured (JSON) log output.
#
# _StderrCapture creates a real OS pipe (so the subprocess has a valid file
# descriptor to write to), reads from it in a daemon thread, and re-emits
# each line through the Python logger so structlog can format it as JSON.
# ---------------------------------------------------------------------------


class _StderrCapture:
    """Capture a subprocess's stderr and re-emit lines via Python logging."""

    def __init__(self, server_name: str) -> None:
        self._server_name = server_name
        read_fd, write_fd = os.pipe()
        self._read_file = os.fdopen(read_fd, "r", errors="replace")
        self.errlog = os.fdopen(write_fd, "w")
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background reader thread."""
        self._thread = threading.Thread(
            target=self._read_loop,
            daemon=True,
            name=f"mcp-stderr-{self._server_name}",
        )
        self._thread.start()

    def close_parent_write_end(self) -> None:
        """Close the parent's copy of the write fd after subprocess creation.

        The child process holds its own dup of the fd.  Closing the parent's
        copy ensures the reader sees EOF once the child exits.
        """
        with contextlib.suppress(OSError):
            self.errlog.close()

    def _read_loop(self) -> None:
        try:
            for line in self._read_file:
                stripped = line.rstrip("\n\r")
                if stripped:
                    logger.info(
                        "MCP server '%s' stderr: %s",
                        self._server_name,
                        stripped,
                    )
        except (OSError, ValueError):
            pass  # Expected when stderr pipe closes at process exit
        finally:
            with contextlib.suppress(OSError):
                self._read_file.close()

    def stop(self) -> None:
        """Close the write end (if still open) and join the reader thread."""
        self.close_parent_write_end()
        if self._thread:
            self._thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Tool-description enrichment helpers
#
# LLMs are significantly better at honouring required parameters when the
# information is present in the *description* (natural language) rather than
# only in the JSON Schema ``required`` array.  These helpers append a concise
# "Required parameters" line so the model sees them upfront.
# ---------------------------------------------------------------------------


def _format_param_hint(name: str, prop: dict[str, Any]) -> str:
    """Format a single parameter into a concise hint string.

    Example outputs:
        ``owner (string): The account owner of the repository``
        ``page (integer)``
    """
    ptype = prop.get("type", "any")
    desc = prop.get("description", "")
    if desc:
        return f"{name} ({ptype}): {desc}"
    return f"{name} ({ptype})"


def _enrich_tool_description(description: str, schema: dict[str, Any]) -> str:
    """Append required-parameter hints to a tool description.

    If the schema declares ``required`` fields, a block like this is appended::

        Required parameters:
        - owner (string): The account owner of the repository
        - repo (string): The name of the repository
    """
    required = schema.get("required", [])
    if not required:
        return description

    properties = schema.get("properties", {})
    lines: list[str] = []
    for param_name in required:
        prop = properties.get(param_name, {})
        lines.append(f"  - {_format_param_hint(param_name, prop)}")

    suffix = "\n\nRequired parameters:\n" + "\n".join(lines)
    return description.rstrip() + suffix


def _build_missing_params_message(
    tool_name: str,
    missing: list[str],
    schema: dict[str, Any],
) -> str:
    """Build a detailed error message for missing required parameters.

    Includes parameter types and descriptions so the LLM can self-correct
    on the very next attempt.
    """
    properties = schema.get("properties", {})
    parts = [f"Tool '{tool_name}' is missing required parameters:"]
    for param_name in missing:
        prop = properties.get(param_name, {})
        parts.append(f"  - {_format_param_hint(param_name, prop)}")
    parts.append(
        "\nYou MUST provide ALL required parameters listed above. "
        "Re-call this tool with the missing parameters included."
    )
    return "\n".join(parts)


def _build_type_error_message(
    tool_name: str,
    mismatches: list[str],
) -> str:
    """Build an error message for parameter type mismatches."""
    parts = [f"Tool '{tool_name}' has parameter type errors:"]
    parts.extend(mismatches)
    parts.append(
        "\nFix the parameter types as described above and re-call this tool. "
        'Arrays must be JSON arrays (e.g. ["value"]), not plain strings.'
    )
    return "\n".join(parts)


def _build_mcp_error_message(
    tool_name: str,
    exc: Exception,
    schema: dict[str, Any] | None,
    is_company_managed: bool | None = None,
    per_user: bool = False,
) -> str:
    """Build a detailed, actionable error message from an MCP execution failure.

    Root causes the LLM cannot fix by changing parameters are surfaced
    directly and return early WITHOUT the parameter reference: rate
    limiting (stop retrying and wait) and auth/permission failures
    (re-authorize or rotate the credential).  Only genuine
    parameter-shaped failures include the tool's parameter reference so
    the LLM can self-correct on the very next attempt.

    *is_company_managed* is the server's actual ownership flag
    (``MCPServer.is_company_managed``) so the credential remediation is
    driven by real entity ownership instead of hedged guesses: ``True``
    escalates to an administrator, ``False`` keeps the self-service
    wording, and ``None`` (runtime built without a DB record) falls back
    to a neutral conditional hint.
    """
    raw_error = str(exc)
    parts = [f"Tool '{tool_name}' failed with error: {raw_error}"]

    # The auth classifier excludes rate-limit messages internally, so
    # GitHub's secondary-rate-limit phrasing ("Permission Denied: You have
    # exceeded a secondary rate limit…") always lands in this branch.
    if is_rate_limit_error(raw_error):
        parts.append(
            "\nDetected rate limiting: the upstream API has temporarily "
            "blocked further requests. Stop immediate retries and wait a few "
            "minutes before trying again to avoid extending the lockout. "
            "Changing the parameters will not help."
        )
        return "\n".join(parts)

    if is_auth_or_permission_error(raw_error):
        if per_user:
            remediation = _PER_USER_REMEDIATION[0].lower() + _PER_USER_REMEDIATION[1:]
        elif is_company_managed is True:
            remediation = (
                "this MCP server is company-managed, so only an "
                "administrator can fix it — ask an administrator to "
                "re-authorize or rotate the credential in the MCP server "
                "settings."
            )
        elif is_company_managed is False:
            remediation = (
                "re-authorize or rotate the credential in the MCP server "
                "settings."
            )
        else:
            remediation = (
                "re-authorize or rotate the credential in the MCP server "
                "settings (if the server is company-managed and you cannot "
                "edit those settings, ask an administrator)."
            )
        parts.append(
            "\nDetected an authentication/permission failure. Some APIs "
            "report a missing resource with this same wording, so first "
            "verify the target resource (owner, repository, project, path, "
            "query) exists and is spelled correctly. If the target is "
            "correct, the credential configured for this MCP server is "
            f"likely invalid, expired, or missing access — {remediation} "
            "Do not blindly retry the same call."
        )
        return "\n".join(parts)

    if schema:
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if properties:
            param_hints = []
            for name, prop in properties.items():
                hint = _format_param_hint(name, prop)
                if name in required:
                    hint += " [REQUIRED]"
                param_hints.append(f"  - {hint}")
            if param_hints:
                parts.append("\nTool parameter reference:")
                parts.extend(param_hints)

    parts.append(
        "\nCarefully review the error above and fix the parameters before retrying. "
        "Ensure all REQUIRED parameters are provided with correct types "
        '(arrays must be JSON arrays like ["value"], not plain strings).'
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Lightweight runtime config (built from the DB model at execution time)
# ---------------------------------------------------------------------------


@dataclass
class MCPServerRuntime:
    """Runtime configuration for a single MCP server connection.

    Built from the ``MCPServer`` ORM model right before a skill executes,
    with secrets already decrypted.
    """

    name: str
    transport: str  # "stdio" | "sse" | "streamable_http"

    # stdio transport fields
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None

    # sse / streamable_http transport fields
    url: str | None = None
    headers: dict[str, str] | None = None
    # Authentication hook handed to the transport (``httpx.Auth``): for an
    # OAuth server, the one that keeps its stored token fresh for the whole
    # connection (``services/mcp_oauth_tokens.py``). It sets the
    # ``Authorization`` header on every request, over the one in ``headers``.
    auth: httpx.Auth | None = None

    # Stable unique identifier (MCPServer.id as string) used as the Redis key
    # for persistent failure tracking across agent runs.  Callers that build
    # the runtime without a DB record (e.g. public_chat.py) may omit this;
    # the failure counter then falls back to the server name.
    server_id: str | None = None

    # Actual ownership flag (``MCPServer.is_company_managed``) so error
    # remediation can be driven by real entity ownership: company-managed
    # credentials are admin-only, user-owned ones are self-service.
    # ``None`` = unknown (runtime built without a DB record) — error
    # messages then fall back to neutral conditional wording.
    is_company_managed: bool | None = None

    # The relay group the server is reached through (``MCPServer.relay_group_id``);
    # ``None`` = direct. Resolved to its route when the connection opens.
    relay_group_id: str | None = None

    # A ``per_user`` OAuth server (``MCPServer.credential_mode``): the token is
    # the acting person's own, so a credential failure is theirs to fix.
    per_user: bool = False
    # Whose failures the cross-run counters track on a ``per_user`` server (the
    # acting person's id, ``anonymous`` without one): one person's missing
    # account never opens the circuit for everyone. ``None`` = the server's.
    failure_scope: str | None = None

    @property
    def failure_key(self) -> str:
        """Redis key component for cross-run failure tracking.

        The stable DB UUID when available, the server name otherwise, with the
        acting person appended on a ``per_user`` server.
        Single source of truth shared by :class:`MCPConnection` and
        ``builtin_tools.loaders.load_mcp_tools`` so the counter, circuit
        breaker, and first-failure timestamp always address the same keys.
        """
        key = self.server_id or self.name
        return f"{key}:user:{self.failure_scope}" if self.failure_scope else key

    @property
    def server_key(self) -> str:
        """The server itself (id, else name), whoever acts: what a resource link and a route name."""
        return self.server_id or self.name


class ClientSession(_SDKClientSession):
    """The SDK session without its check of a tool's structured result.

    ``call_tool`` validates ``structuredContent`` against the server's own
    ``outputSchema`` with ``jsonschema.validate``: every ``pattern`` of that
    schema runs through the backtracking ``re`` on the event loop, and a
    result the schema refuses, or a ``$ref`` that does not resolve within the
    schema document (mcp 1.30 retrieves no other document), fails the call.
    The platform reads only ``result.content``, never ``structuredContent``,
    so the check guards nothing it uses.
    """

    async def _validate_tool_result(self, name: str, result: Any) -> None:
        return None

    async def read_resource(
        self, uri: Any, read_timeout_seconds: timedelta | None = None
    ) -> mcp_types.ReadResourceResult:
        """``resources/read``, bounded by *read_timeout_seconds* like ``call_tool``."""
        return await self.send_request(
            mcp_types.ClientRequest(
                mcp_types.ReadResourceRequest(params=mcp_types.ReadResourceRequestParams(uri=uri))
            ),
            mcp_types.ReadResourceResult,
            request_read_timeout_seconds=read_timeout_seconds,
        )


# ---------------------------------------------------------------------------
# Files in MCP tool results
# ---------------------------------------------------------------------------

_ARTIFACT_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "application/pdf": ".pdf"}


def _artifact_filename(uri: str, mime_type: str, tool_name: str) -> str:
    """The name of a file an MCP tool returned: its URI's last segment, when it has one.

    The extension is what the text extraction reads the file by, so a name
    without one gets the extension of its type.
    """
    from urllib.parse import unquote, urlparse

    ext = _ARTIFACT_EXTENSIONS.get(mime_type) or mimetypes.guess_extension(mime_type or "") or ""
    name = ""
    with contextlib.suppress(ValueError):
        name = unquote(urlparse(uri).path.rsplit("/", 1)[-1]).strip()
    if not name or name in (".", ".."):
        return f"mcp_{tool_name}_{uuid.uuid4().hex[:8]}{ext}"
    return name if "." in name else f"{name}{ext}"


def _is_resource_link(block: Any) -> bool:
    """A ``resource_link`` content block: a URI the server points at, no content."""
    if getattr(block, "type", None) == "resource_link":
        return True
    return bool(getattr(block, "uri", None)) and not (
        getattr(block, "text", None) or getattr(block, "data", None)
    )


def _resource_link_line(block: Any, config: MCPServerRuntime) -> str:
    uri = str(getattr(block, "uri", "") or "")
    name = getattr(block, "title", None) or getattr(block, "name", None) or uri
    details = [str(v) for v in (getattr(block, "mimeType", None),) if v]
    size = getattr(block, "size", None)
    if isinstance(size, int):
        details.append(f"{size:,} bytes")
    suffix = f" ({', '.join(details)})" if details else ""
    description = getattr(block, "description", None)
    tail = f" - {description}" if description else ""
    return (
        f"[Resource link: {name}{suffix} - {uri}{tail} - from MCP server '{config.name}', not "
        f'downloaded; {READ_RESOURCE_TOOL_NAME}(uri, server="{config.server_key}") stores it as a file]'
    )


#: The tool that reads a resource an MCP tool linked. A
#: ``resource_link`` points at a resource the server did not send, often
#: because it is large: reading it is the agent's call, one link at a time,
#: never an automatic fetch of every link a result lists.
READ_RESOURCE_TOOL_NAME = "read_mcp_resource"

#: How long one ``resources/read`` may take.
_RESOURCE_READ_TIMEOUT = timedelta(seconds=60)

#: The most contents of one ``resources/read`` that are stored (a resource
#: may answer with several, e.g. the files of a folder).
_MAX_RESOURCE_CONTENTS = 10


def resource_read_tool_definition() -> ToolDefinition:
    """The definition of :data:`READ_RESOURCE_TOOL_NAME`, offered with a run's MCP tools."""
    return ToolDefinition(
        name=READ_RESOURCE_TOOL_NAME,
        description=(
            "Bring the file behind a '[Resource link: ...]' line of an MCP tool result into "
            "the conversation: reads it from the MCP server that linked it (MCP resources/read, "
            "over that server's own connection) and stores it as a file - a file_id any tool "
            "that takes a file accepts, a download card, and its content on this result. Only "
            "a link an MCP tool returned in this run can be read."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "The link's URI, exactly as shown"},
                "server": {
                    "type": "string",
                    "description": "The server given on the link line (needed only when two servers linked the same URI)",
                },
            },
            "required": ["uri"],
        },
        source="mcp:resources",
    )


def _resource_contents_size(contents: Any, limit: int) -> int:
    """The stored size of one ``resources/read`` content, read before it is decoded.

    A text longer than *limit* characters is over it whatever its encoding,
    so it is not encoded to be measured.
    """
    blob = getattr(contents, "blob", None)
    if blob:
        return len(blob) * 3 // 4 if isinstance(blob, str) else len(blob)
    text = getattr(contents, "text", None)
    if not isinstance(text, str):
        return 0
    return len(text) if len(text) > limit else len(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Single MCP connection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPArtifactScope:
    """Who a binary MCP tool result belongs to.

    The same scope the run's ``generate_file`` persists with, so a downloaded
    file is born linked to its user / conversation / assignment run: the next
    turn's ``FileContext`` finds it, the run lists it, a HITL resume restores it.
    """

    user_id: str | None = None
    conversation_id: str | None = None
    assignment_run_id: str | None = None


class MCPConnection:
    """Manages a (possibly lazy) connection to one MCP server.

    When constructed with *cached_tools*, the connection is **deferred** —
    no subprocess or HTTP connection is opened until a tool is actually
    invoked via :meth:`execute_tool`.  This makes tool *discovery* nearly
    free when the DB cache is populated.
    """

    def __init__(
        self,
        config: MCPServerRuntime,
        cached_tools: list[ToolDefinition] | None = None,
        artifact_scope: MCPArtifactScope | None = None,
    ) -> None:
        self.config = config
        self.artifact_scope = artifact_scope or MCPArtifactScope()
        self._session: ClientSession | None = None
        self._exit_stack = AsyncExitStack()
        self._tools: list[ToolDefinition] = cached_tools or []
        self._connected: bool = False
        self._stderr_capture: _StderrCapture | None = None
        self._has_open_contexts: bool = False
        # Redis key for cross-run persistent failure tracking (stable DB
        # UUID when available, server name otherwise) — see
        # MCPServerRuntime.failure_key.
        self._failure_key: str = config.failure_key
        # The resource links this server's tools returned in this run, by URI:
        # the only URIs ``read_resource_link`` reads.
        self._resource_links: dict[str, Any] = {}

    # -- lifecycle -----------------------------------------------------------

    async def _close_exit_stack_best_effort(
        self,
        reason: str,
        *,
        suppress_cancelled: bool = True,
        suppress_unexpected_runtime: bool = True,
        hand_back_failure: bool = False,
    ) -> Exception | None:
        """Close entered async contexts without masking the original error.

        Any other error the close raises is logged and absorbed, or with
        *hand_back_failure* returned unlogged: ``connect()`` reads it as the
        failure the transport cancelled the task to report.

        Documented teardown noise (``cancel scope`` cross-task exits AND
        CPython ``athrow()/aclose(): asynchronous generator is already
        running`` races) is ALWAYS swallowed regardless of the flags:
        those are MCP-SDK / anyio lifecycle artifacts that do not
        indicate data loss.

        The two ``suppress_*`` flags control how a ``CancelledError`` /
        ``RuntimeError`` raised by ``self._exit_stack.aclose()`` itself
        (NOT the surrounding caller's cancellation) is handled:

        * ``suppress_cancelled=True`` (the default — used by both
          ``connect()`` and ``disconnect()``) absorbs an aclose-side
          ``CancelledError``.  Re-raising it would mask whatever real
          exception the caller is already propagating: ``connect()`` is
          inside its own ``except asyncio.CancelledError:`` handler and
          will re-raise the original cancellation after we return, so
          letting a second cancellation bubble up here is pointless
          noise.  ``suppress_cancelled=False`` is reserved for a
          hypothetical caller that wants to OBSERVE the aclose-side
          cancellation separately.
        * ``suppress_unexpected_runtime=False`` re-raises
          ``RuntimeError`` that does NOT match the teardown-noise
          patterns above (callers like ``disconnect()`` use this so a
          real session aclose() bug surfaces instead of getting
          silently absorbed).

        Cancellation-resilient cleanup
        ------------------------------
        When the surrounding task is already cancelled (e.g. ``connect``
        is racing a parent-task ``Task.cancel()``), a plain
        ``await self._exit_stack.aclose()`` would observe the pending
        cancellation on the first ``await`` inside ``aclose`` and bail
        out before the entered contexts actually unwind — leaking the
        ``stdio_client`` subprocess and the ``ClientSession`` we just
        opened.  We use the Python 3.11+ ``Task.uncancel`` /
        ``Task.cancel`` pattern to temporarily clear the pending
        cancellation while ``aclose`` runs, then re-trigger it so the
        caller's own ``raise`` at the top of its ``except`` block still
        sees a cancelled task.

        The log wording is "Suppressed …" ONLY when the exception is
        actually absorbed by this helper. When the flags re-raise, a
        neutral "while … (propagating)" line is emitted instead so
        operators reading the logs don't see a misleading "Suppressed
        CancelledError" right before a CancelledError pops out the top.
        """
        if not self._has_open_contexts:
            return None
        failure: Exception | None = None
        # ── Cancellation-resilient cleanup primitive ──────────────────
        # If the surrounding task is already being cancelled, a plain
        # ``await self._exit_stack.aclose()`` would observe the pending
        # cancellation on its first checkpoint and bail before the
        # entered contexts unwind — leaking the ``stdio_client``
        # subprocess and the ``ClientSession``.  ``uncancel_during_cleanup``
        # lifts the pending cancellation for the duration of ``aclose``
        # and re-applies it on exit so the caller's own ``raise`` still
        # sees a cancelled task.  (Defined in ``app.core.cancellation``
        # alongside ``run_cleanup_to_completion`` — the shielded-task helper
        # ``get_db`` uses for the same class of teardown problem.)
        async with uncancel_during_cleanup():
            try:
                await self._exit_stack.aclose()
            except asyncio.CancelledError:
                if suppress_cancelled:
                    logger.debug(
                        "Suppressed CancelledError while %s MCP server '%s'",
                        reason,
                        self.config.name,
                    )
                else:
                    # About to re-raise — neutral wording matches reality.
                    logger.debug(
                        "CancelledError while %s MCP server '%s' (propagating)",
                        reason,
                        self.config.name,
                    )
                    raise
            except RuntimeError as exc:
                msg = str(exc) or type(exc).__name__
                if _is_teardown_noise_runtime_error(exc):
                    logger.debug(
                        "Suppressed RuntimeError while %s MCP server '%s': %s",
                        reason,
                        self.config.name,
                        msg,
                    )
                elif suppress_unexpected_runtime:
                    logger.warning(
                        "MCP cleanup runtime error while %s server '%s': %s",
                        reason,
                        self.config.name,
                        msg,
                    )
                else:
                    logger.warning(
                        "MCP cleanup runtime error while %s server '%s' "
                        "(propagating): %s",
                        reason,
                        self.config.name,
                        msg,
                    )
                    raise
            except Exception as exc:
                if hand_back_failure:
                    failure = exc
                else:
                    logger.warning(
                        "MCP cleanup error while %s server '%s': %s",
                        reason,
                        self.config.name,
                        format_exception_message(exc),
                    )
            finally:
                self._exit_stack = AsyncExitStack()
                self._session = None
                self._connected = False
                self._has_open_contexts = False
        return failure

    async def connect(self, *, force: bool = False) -> None:
        """Establish connection, initialise session, discover tools.

        *force* bypasses the circuit-breaker check.  Pass ``force=True``
        from explicit test/refresh flows (:meth:`MCPManager.test_connection`)
        so that a user-initiated connectivity test is never silently skipped.
        """
        if self._connected:
            return
        # ── Circuit-breaker guard ────────────────────────────────────────
        # When the server has been persistently unreachable the circuit is
        # "open" and we skip the connection attempt immediately — no
        # subprocess spawn or network timeout stalls the agent loop.  The
        # circuit auto-expires after _MCP_CIRCUIT_TTL_SECS; on expiry the
        # next call is allowed through ("half-open").  ``force=True``
        # bypasses this so an explicit test/refresh always reaches the wire.
        if not force and await _is_mcp_circuit_open(self._failure_key):
            logger.debug(
                "MCP server '%s' circuit breaker open — skipping connection "
                "attempt (cooling down after repeated failures)",
                self.config.name,
            )
            raise _MCPCircuitOpenError(
                f"MCP server '{self.config.name}' circuit breaker open — "
                f"cooling down after repeated failures"
            )
        task = asyncio.current_task()
        # The cancellations this task already carried before the attempt: they
        # are never the transport's to withdraw (see the CancelledError branch).
        cancellations_before = task.cancelling() if task is not None else 0
        try:
            # Bound the WHOLE attempt (transport connect + initialize +
            # list_tools): a half-dead endpoint that accepts the connection
            # but never answers would otherwise hang the caller forever —
            # and a hang never *fails*, so the circuit breaker would never
            # open.  ``asyncio.timeout`` cancels in place (no child
            # task), preserving the task affinity the anyio-based transport
            # context managers require (see the CancelledError note below).
            async with asyncio.timeout(_MCP_CONNECT_TIMEOUT_SECS):
                route = await runtime_egress_route(self.config)
                if self.config.transport == "stdio":
                    if route is not None:
                        raise ValueError(
                            "A stdio MCP server runs on the platform itself and cannot be "
                            "routed through a relay group"
                        )
                    if not self.config.command:
                        raise ValueError("stdio transport requires a 'command'")

                    # Route subprocess stderr through structured logging so
                    # that messages like "GitHub MCP Server running on stdio"
                    # appear as JSON instead of raw text.
                    self._stderr_capture = _StderrCapture(self.config.name)
                    self._stderr_capture.start()
                    resolved_command, resolved_args = _resolve_stdio_command(
                        self.config.command, self.config.args
                    )

                    read, write, *_ = await self._exit_stack.enter_async_context(
                        stdio_client(
                            server=StdioServerParameters(
                                command=resolved_command,
                                args=resolved_args,
                                env=_build_mcp_env(self.config.env),
                            ),
                            errlog=self._stderr_capture.errlog,
                        )
                    )

                    # Close parent's copy of the write fd — the child process
                    # holds its own dup.  This ensures the reader thread sees
                    # EOF once the subprocess exits.
                    self._stderr_capture.close_parent_write_end()
                elif self.config.transport == "sse":
                    # Trim like the stdio command: persisted configs may carry
                    # stray whitespace that would otherwise be sent to the
                    # server (encoded as %20) and fail the connection.
                    # Imported configs are untyped JSON, so a non-string url
                    # is treated as missing instead of raising an
                    # AttributeError on .strip().
                    raw_url = self.config.url
                    url = raw_url.strip() if isinstance(raw_url, str) else ""
                    if not url:
                        raise ValueError("sse transport requires a 'url'")
                    read, write, *_ = await self._exit_stack.enter_async_context(
                        sse_client(
                            url=url,
                            headers=self.config.headers or {},
                            auth=self.config.auth,
                            **_client_kwargs(route),
                        )
                    )
                elif self.config.transport == "streamable_http":
                    raw_url = self.config.url
                    url = raw_url.strip() if isinstance(raw_url, str) else ""
                    if not url:
                        raise ValueError(
                            "streamable_http transport requires a 'url'"
                        )
                    read, write, *_ = await self._exit_stack.enter_async_context(
                        streamablehttp_client(
                            url=url,
                            headers=self.config.headers or {},
                            auth=self.config.auth,
                            **_client_kwargs(route),
                        )
                    )
                else:
                    raise ValueError(
                        f"Unsupported MCP transport: {self.config.transport}"
                    )
                self._has_open_contexts = True

                self._session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await self._session.initialize()
                self._connected = True

                # Discover tools (refreshes any cached definitions)
                response = await self._session.list_tools()
            self._tools = []
            for tool in response.tools:
                schema = getattr(
                    tool, "inputSchema", {"type": "object", "properties": {}}
                )
                self._tools.append(
                    ToolDefinition(
                        name=tool.name,
                        description=_enrich_tool_description(
                            tool.description or "", schema
                        ),
                        input_schema=schema,
                        source=f"mcp:{self.config.name}",
                    )
                )
            logger.info(
                "Connected to MCP server '%s' – discovered %d tool(s)",
                self.config.name,
                len(self._tools),
            )
            await _reset_mcp_connection_failures(self._failure_key)
            await _close_mcp_circuit(self._failure_key)
        except asyncio.CancelledError:
            # Ensure async context managers entered above are always closed in the
            # same task that opened them. Without this explicit close, partially
            # opened stdio_client generators can be finalized later by GC in a
            # different task, triggering anyio cancel-scope RuntimeError noise.
            delivered = task.cancelling() if task is not None else 0
            failure = await self._close_exit_stack_best_effort(
                "connecting to", hand_back_failure=True
            )
            if self._stderr_capture:
                self._stderr_capture.stop()
                self._stderr_capture = None
            if failure is None or task is None:
                raise
            # The transport's task group raised a failure on close: one of its
            # tasks failed (the first POST of a ``streamable_http`` connection
            # runs in one), and the group cancelled this task to report it.
            # That cancellation is the transport's, not the caller's:
            # anyio would withdraw it when its scope exits, but the close above
            # ran with it lifted and re-applied it. Withdraw it here, so the
            # caller's next await (the route's own database write) is not
            # cancelled, and report the failure as any other failed attempt.
            # Only what arrived during the attempt is withdrawn: a cancellation
            # carried before it, or arriving during the close, still stands (a
            # caller's cancel landing in the same instant as the failure cannot
            # be told apart and is withdrawn with it).
            for _ in range(max(0, delivered - cancellations_before)):
                task.uncancel()
            await self._report_connect_failure(failure)
            raise failure from None
        except Exception as exc:
            await self._close_exit_stack_best_effort("connecting to")
            # Clean up stderr capture on connection failure
            if self._stderr_capture:
                self._stderr_capture.stop()
                self._stderr_capture = None
            await self._report_connect_failure(exc)
            raise

    async def _report_connect_failure(self, exc: Exception) -> None:
        """Log and count a failed connection attempt.

        Raises :class:`MCPAuthorizationRequiredError` when *exc* carries a 401,
        :class:`MCPRedirectNotFollowedError` when it carries a redirect the
        transport did not follow; otherwise returns and the caller re-raises
        *exc*.
        """
        # A 401 is not "the server is broken" — it is the server telling us
        # to go and get a token. Surface it as its own error carrying the
        # RFC 9728 pointer, so the caller can say "authorize this server"
        # instead of relaying an opaque TaskGroup failure (the
        # raw upstream message reached the UI).
        challenge = _extract_auth_challenge(exc)
        if challenge is not None:
            _, resource_metadata = challenge
            logger.warning(
                "MCP server '%s' requires authorization (HTTP 401)%s",
                self.config.name,
                (
                    f"; resource metadata: {resource_metadata}"
                    if resource_metadata
                    else ""
                ),
            )
            raise MCPAuthorizationRequiredError(
                f"MCP server '{self.config.name}' requires authorization",
                resource_metadata=resource_metadata,
            ) from exc

        # A redirect the transport does not follow is a URL to fix,
        # not a code fault: a WARNING naming where it points, no traceback,
        # counted like an unreachable server (every attempt meets it again).
        # The caller gets that location (redacted) and a fixed sentence,
        # never the SDK's wrapped TaskGroup failure.
        redirect = _unfollowed_redirect(exc)
        if redirect is not None:
            status_code, location = redirect
            logger.warning(
                "MCP server '%s' redirects to %r (HTTP %d), which its transport "
                "does not follow (another origin, or a changed request method); "
                "skipping its tools for this run",
                self.config.name,
                location,
                status_code,
            )
            await self._count_operational_failure()
            raise MCPRedirectNotFollowedError(
                f"MCP server '{self.config.name}' redirects to {location}, which "
                "its connection does not follow. If that is the intended server, "
                "save the server with that URL.",
                location=location,
            ) from exc

        connect_detail = _connect_error_detail(exc)
        if connect_detail is not None:
            # Server unreachable / down — operational, not a code fault.
            # Concise WARNING (no multi-frame traceback) so an offline or
            # misconfigured MCP endpoint doesn't spam ERROR tracebacks on
            # every retry. The caller still handles the raise and skips the
            # server's tools for this run.
            logger.warning(
                "Failed to connect to MCP server '%s' — %s (server "
                "unreachable; skipping its tools for this run)",
                self.config.name,
                connect_detail,
            )
            await self._count_operational_failure()
        else:
            logger.error(
                "Failed to connect to MCP server '%s'", self.config.name, exc_info=exc
            )

    async def _count_operational_failure(self) -> None:
        """Count an operational connection failure; open the circuit at the threshold."""
        # Persistent-failure tracking lives HERE (not only in the
        # manager's eager connect_server path) so the lazy
        # cached-tools path — ``execute_tool`` connecting on first
        # use — counts too.  Only *operational* connect failures
        # (unreachable, or a redirect not followed) are recorded: code
        # bugs and config errors already log a full ERROR traceback on
        # every attempt, so counting them would just skew the
        # counter and emit a misleading "verify endpoint
        # reachability" message.  Cancellation is handled earlier
        # and deliberately not recorded either: a cancelled parent
        # task says nothing about server health.
        # Escalation is rate-limited: once at the threshold, then a
        # periodic reminder — never on every failed run (that would
        # reintroduce the ERROR-per-run noise this fix removes).
        # One atomic call records BOTH the consecutive count and the
        # first-failure timestamp (single Redis round-trip).
        consecutive = await _record_mcp_connection_failure(self._failure_key)
        # Open (or refresh) the circuit breaker once the threshold
        # is reached.  Subsequent connect() calls are skipped for
        # _MCP_CIRCUIT_TTL_SECS so the agent loop is not stalled
        # on a known-unreachable server every single run.
        if consecutive >= _MCP_PERSISTENT_FAILURE_THRESHOLD:
            await _open_mcp_circuit(self._failure_key)
        if _should_escalate_mcp_failure(consecutive):
            hours = await get_mcp_failure_hours(self._failure_key)
            if hours is not None and hours >= 24:
                duration_str = format_failure_duration(hours)
                logger.error(
                    "MCP server '%s' has been unreachable for %s "
                    "(%d consecutive failure(s)). Verify the server "
                    "configuration and endpoint reachability — this is "
                    "a sustained outage. The server's tools will be "
                    "unavailable until connectivity is restored. "
                    "Connection attempts will pause for %d minute(s) "
                    "to reduce resource waste.",
                    self.config.name,
                    duration_str,
                    consecutive,
                    _MCP_CIRCUIT_TTL_SECS // 60,
                )
            else:
                logger.error(
                    "MCP server '%s' has failed to connect %d consecutive "
                    "time(s). Verify the server configuration and endpoint "
                    "reachability — the server's tools will be unavailable "
                    "until connectivity is restored. Connection attempts will "
                    "pause for %d minute(s) to reduce resource waste.",
                    self.config.name,
                    consecutive,
                    _MCP_CIRCUIT_TTL_SECS // 60,
                )

    async def disconnect(self) -> None:
        if not self._connected:
            return  # Nothing to disconnect (lazy connection never opened)
        try:
            # ``suppress_cancelled=True`` matches the existing contract
            # locked by ``test_disconnect_suppresses_cancelled_error``:
            # the stdio transport raises ``CancelledError``
            # when the subprocess wait() is interrupted by the cancel
            # scope teardown — that's expected teardown noise, not a
            # signal callers need to observe.
            # ``suppress_unexpected_runtime=False`` so real bugs (e.g.
            # a session aclose() that genuinely failed) still surface
            # up to the caller instead of being silently absorbed.
            # The helper still suppresses the documented teardown
            # patterns (``cancel scope``, ``asynchronous generator is
            # already running``) regardless of these flags.
            await self._close_exit_stack_best_effort(
                "disconnecting",
                suppress_cancelled=True,
                suppress_unexpected_runtime=False,
            )
        finally:
            # CRITICAL: stderr-capture teardown lives in ``finally`` so
            # it ALSO runs when the helper re-raises an unexpected
            # ``RuntimeError`` — otherwise the stderr reader thread /
            # pipe FDs leak on every disconnect that hits a real
            # exception. ``_connected`` is similarly cleared here as a
            # belt-and-braces guarantee in case the helper short-
            # circuited via the ``_has_open_contexts`` guard (tests
            # that pre-date this PR rely on this post-condition).
            self._connected = False
            if self._stderr_capture:
                self._stderr_capture.stop()
                self._stderr_capture = None

    # -- tool access ---------------------------------------------------------

    @property
    def tools(self) -> list[ToolDefinition]:
        return list(self._tools)

    def has_tool(self, name: str) -> bool:
        return any(t.name == name for t in self._tools)

    def _get_tool_schema(self, tool_name: str) -> dict[str, Any] | None:
        """Return the ``input_schema`` for a named tool, or ``None``."""
        for t in self._tools:
            if t.name == tool_name:
                return t.input_schema
        return None

    def _validate_required_params(self, call: ToolCall) -> str | None:
        """Check that all ``required`` parameters are present in the call.

        Returns a detailed error message if any are missing, or ``None``
        if validation passes.  This avoids a full MCP round-trip for
        obviously invalid invocations.

        Also catches required parameters that are present but set to
        ``None`` — MCP servers reject these as "undefined".
        """
        schema = self._get_tool_schema(call.name)
        if not schema:
            return None
        required = schema.get("required", [])
        if not required:
            return None
        provided = set(call.arguments.keys()) if call.arguments else set()
        missing = [p for p in required if p not in provided]
        if call.arguments:
            for p in required:
                if p in provided and call.arguments[p] is None and p not in missing:
                    missing.append(p)
        if not missing:
            return None
        return _build_missing_params_message(call.name, missing, schema)

    def _validate_param_types(self, call: ToolCall) -> str | None:
        """Pre-validate parameter types against the JSON Schema.

        Catches common LLM mistakes (e.g. sending a string where an
        array is expected) *before* hitting the MCP server, returning a
        detailed error the LLM can act on immediately.
        """
        schema = self._get_tool_schema(call.name)
        if not schema or not call.arguments:
            return None
        properties = schema.get("properties", {})

        mismatches: list[str] = []
        for param_name, value in call.arguments.items():
            if value is None:
                continue
            prop = properties.get(param_name)
            if not prop:
                continue
            expected = prop.get("type")
            if not expected:
                continue

            if expected == "array" and not isinstance(value, list):
                example = f'["{value}"]' if isinstance(value, str) else f"[{value}]"
                mismatches.append(
                    f"  - {param_name}: expected array, got {type(value).__name__}. "
                    f"Wrap in an array, e.g. {example}"
                )
            elif expected == "object" and not isinstance(value, dict):
                mismatches.append(
                    f"  - {param_name}: expected object, got {type(value).__name__}"
                )
            elif expected == "integer" and not isinstance(value, int):
                mismatches.append(
                    f"  - {param_name}: expected integer, got {type(value).__name__}"
                )
            elif expected == "number" and not isinstance(value, (int, float)):
                mismatches.append(
                    f"  - {param_name}: expected number, got {type(value).__name__}"
                )
            elif expected == "boolean" and not isinstance(value, bool):
                mismatches.append(
                    f"  - {param_name}: expected boolean, got {type(value).__name__}"
                )

        if not mismatches:
            return None
        return _build_type_error_message(call.name, mismatches)

    async def _store_artifact(self, filename: str, raw: bytes, mime_type: str) -> Any:
        """Persist a file an MCP tool returned, with the run's scope."""
        from app.services.builtin_tools._shared import persist_tool_artifact

        scope = self.artifact_scope
        return await persist_tool_artifact(
            user_id=scope.user_id or "",
            filename=filename,
            raw_bytes=raw,
            content_type=mime_type,
            conversation_id=scope.conversation_id,
            assignment_run_id=scope.assignment_run_id,
            source="mcp",
        )

    async def _embedded_resource_part(
        self, tool_name: str, resource: Any, file_refs: list, *, fallback_mime: str = ""
    ) -> str:
        """Store an embedded resource (``type: "resource"``) as a file of the run.

        A text resource is redacted like any other MCP text before it is
        stored; the stored file rides the result, so its content is read by
        the model within the usual caps. When it cannot be stored, a text
        resource is answered inline instead. *fallback_mime* is the type used
        when the resource declares none (a resource link's).
        """
        uri = str(getattr(resource, "uri", "") or "")
        blob = getattr(resource, "blob", None)
        text = getattr(resource, "text", None)
        mime_type = getattr(resource, "mimeType", None) or fallback_mime or ""
        if blob:
            try:
                raw = base64.b64decode(blob) if isinstance(blob, str) else bytes(blob)
            except (ValueError, TypeError):
                return f"[Resource: {uri} - its content could not be decoded]"
            mime_type = mime_type or mimetypes.guess_type(uri)[0] or "application/octet-stream"
        elif isinstance(text, str):
            text = _sanitize_output(text)
            raw = text.encode("utf-8")
            mime_type = mime_type or mimetypes.guess_type(uri)[0] or "text/plain"
        else:
            return f"[Resource: {uri} (no content)]" if uri else ""
        filename = _artifact_filename(uri, mime_type, tool_name)
        try:
            ref = await self._store_artifact(filename, raw, mime_type)
        except Exception as exc:
            logger.warning(
                "Failed to store MCP resource %s: %s", uri, format_exception_message(exc)
            )
            if isinstance(text, str) and not blob:
                return f"[Resource: {uri} ({mime_type})]\n{text}"
            return f"[Resource: {uri} ({mime_type}, {len(raw):,} bytes) - it could not be stored]"
        file_refs.append(ref)
        origin = f" from {uri}" if uri else ""
        return (
            f"[Resource: {filename} ({mime_type}, {len(raw):,} bytes){origin} "
            f"- file_id={ref.file_id} [[FILE:{ref.file_id}]]]"
        )

    def linked_resource(self, uri: str) -> bool:
        """Whether a tool of this server returned a resource link to *uri* in this run."""
        return uri in self._resource_links

    async def read_resource_link(self, call: ToolCall, uri: str) -> ToolResult:
        """Read a resource a tool of this server linked and store it as a file of the run.

        Only a URI this server's own tools linked is read, over this session
        and its credentials. Bounded: a link declaring a size over the stored
        file limit is not read, the read times out after
        ``_RESOURCE_READ_TIMEOUT``, at most ``_MAX_RESOURCE_CONTENTS`` contents
        are stored, and none past the limit in all. The bytes RECEIVED are
        the transport's: the SDK parses every message of the session before
        the session hands it over, a tool result and a notification the
        server sends unasked alike, so a cap on this one request would bound
        nothing a server cannot send in another message.
        """
        from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES

        link = self._resource_links.get(uri)
        if link is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"MCP server '{self.config.name}' linked no resource '{uri}' in this run.",
                is_error=True,
            )
        limit_mb = MAX_UPLOAD_SIZE_BYTES // 1024 // 1024
        declared = getattr(link, "size", None)
        if isinstance(declared, int) and declared > MAX_UPLOAD_SIZE_BYTES:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"'{uri}' is {declared:,} bytes, over the {limit_mb} MB file limit: it "
                    "was not read. Its link is the way to reach it."
                ),
                is_error=True,
            )
        if not self._connected:
            try:
                await self.connect()
            except Exception as exc:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Failed to connect to MCP server '{self.config.name}': {format_exception_message(exc)}",
                    is_error=True,
                )
        if self._session is None:
            return ToolResult(tool_call_id=call.id, content="MCP session not connected", is_error=True)
        try:
            result = await self._session.read_resource(uri, read_timeout_seconds=_RESOURCE_READ_TIMEOUT)
        except Exception as exc:
            logger.warning(
                "MCP resources/read failed on server '%s': %s",
                self.config.name,
                format_exception_message(exc),
            )
            return ToolResult(
                tool_call_id=call.id,
                content=f"MCP server '{self.config.name}' could not read '{uri}': {format_exception_message(exc)}",
                is_error=True,
            )
        contents = list(getattr(result, "contents", None) or [])
        parts: list[str] = []
        file_refs: list = []
        stored_bytes = 0
        for item in contents[:_MAX_RESOURCE_CONTENTS]:
            size = _resource_contents_size(item, MAX_UPLOAD_SIZE_BYTES)
            item_uri = str(getattr(item, "uri", "") or uri)
            if stored_bytes + size > MAX_UPLOAD_SIZE_BYTES:
                parts.append(f"[Resource: {item_uri} - not stored: over the {limit_mb} MB file limit]")
                continue
            stored_bytes += size
            part = await self._embedded_resource_part(
                READ_RESOURCE_TOOL_NAME, item, file_refs, fallback_mime=getattr(link, "mimeType", None) or ""
            )
            if part:
                parts.append(part)
        if len(contents) > _MAX_RESOURCE_CONTENTS:
            parts.append(
                f"[{len(contents) - _MAX_RESOURCE_CONTENTS} more content(s) of '{uri}' not stored: "
                f"at most {_MAX_RESOURCE_CONTENTS} are kept per read]"
            )
        if not parts:
            return ToolResult(
                tool_call_id=call.id,
                content=f"MCP server '{self.config.name}' returned no content for '{uri}'.",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id, content=_sanitize_output("\n".join(parts)), files=file_refs or None
        )

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        """Execute a tool call on this MCP server.

        Lazily connects if needed (cached-tool path).

        **Client-side pre-validation**: Before hitting the MCP server,
        required parameters and types are checked locally.  If any are
        invalid, an informative error (with types + descriptions) is
        returned *immediately* — no subprocess spawn or network call needed.
        """
        # ── Pre-validate required parameters (fast, no I/O) ──────────
        validation_error = self._validate_required_params(call)
        if validation_error:
            logger.warning(
                "MCP tool '%s' pre-validation failed on server '%s': missing required params",
                call.name,
                self.config.name,
            )
            return ToolResult(
                tool_call_id=call.id,
                content=validation_error,
                is_error=True,
            )

        # ── Pre-validate parameter types (fast, no I/O) ──────────────
        type_error = self._validate_param_types(call)
        if type_error:
            logger.warning(
                "MCP tool '%s' pre-validation failed on server '%s': type mismatch",
                call.name,
                self.config.name,
            )
            return ToolResult(
                tool_call_id=call.id,
                content=type_error,
                is_error=True,
            )

        # ── Lazy connect: establish connection only when actually needed ──
        if not self._connected:
            try:
                await self.connect()
            except Exception as exc:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Failed to connect to MCP server '{self.config.name}': {exc}",
                    is_error=True,
                )
        if self._session is None:
            return ToolResult(
                tool_call_id=call.id, content="MCP session not connected", is_error=True
            )
        try:
            result = await self._session.call_tool(call.name, call.arguments)

            parts: list[str] = []
            file_refs: list = []
            if result.content:
                for block in result.content:
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(text)
                    # Handle binary/blob content from MCP tools
                    data = getattr(block, "data", None)
                    if data and isinstance(data, (bytes, str)):
                        try:
                            raw = (
                                base64.b64decode(data)
                                if isinstance(data, str)
                                else data
                            )
                            mime_type = (
                                getattr(block, "mimeType", None)
                                or "application/octet-stream"
                            )
                            filename = _artifact_filename("", mime_type, call.name)
                            ref = await self._store_artifact(filename, raw, mime_type)
                            file_refs.append(ref)
                            parts.append(
                                f"[Binary content: {filename} ({mime_type}, {len(raw):,} bytes) "
                                f"- file_id={ref.file_id} [[FILE:{ref.file_id}]]]"
                            )
                        except Exception as bin_err:
                            logger.warning(
                                "Failed to store MCP binary content: %s", format_exception_message(bin_err)
                            )
                    resource = getattr(block, "resource", None)
                    if resource is not None:
                        part = await self._embedded_resource_part(
                            call.name, resource, file_refs
                        )
                        if part:
                            parts.append(part)
                    elif _is_resource_link(block):
                        self._resource_links[str(block.uri)] = block
                        parts.append(_resource_link_line(block, self.config))
            content = "\n".join(parts) if parts else ""
            content = _sanitize_output(content)
            # Successful call — reset the cross-run auth-failure counter so
            # a rotated credential is recognised immediately.
            await _reset_mcp_auth_failures(self._failure_key)
            return ToolResult(
                tool_call_id=call.id, content=content, files=file_refs or None
            )
        except Exception as exc:
            logger.exception(
                "MCP tool '%s' execution failed on server '%s'",
                call.name,
                self.config.name,
            )
            # Track persistent auth failures across agent runs so a broken
            # credential (e.g. expired token) escalates to a high-signal
            # ERROR after several consecutive failures, making it easy to
            # distinguish from transient tool errors in operator dashboards.
            if is_auth_or_permission_error(str(exc)):
                consecutive = await _record_mcp_auth_failure(self._failure_key)
                if _should_escalate_mcp_auth_failure(consecutive):
                    logger.error(
                        "MCP server '%s' has failed with an authentication error "
                        "%d consecutive time(s) across agent runs — the credential "
                        "is likely expired, revoked, or missing. %s",
                        self.config.name,
                        consecutive,
                        _auth_escalation_fix_hint(self.config.is_company_managed, self.config.per_user),
                    )
            schema = self._get_tool_schema(call.name)
            return ToolResult(
                tool_call_id=call.id,
                content=_build_mcp_error_message(
                    call.name, exc, schema, self.config.is_company_managed, self.config.per_user
                ),
                is_error=True,
            )


# ---------------------------------------------------------------------------
# MCP Manager – aggregates multiple MCP connections
# ---------------------------------------------------------------------------


class MCPManager(ToolExecutor):
    """Manages connections to multiple MCP servers.

    Implements :class:`ToolExecutor` so it can be plugged directly into
    :class:`CompositeToolExecutor`.
    """

    def __init__(self, artifact_scope: MCPArtifactScope | None = None) -> None:
        self.artifact_scope = artifact_scope
        self._connections: list[MCPConnection] = []
        self._failed_servers: list[str] = []
        self._failed_server_durations: dict[str, int] = {}

    # -- failed-server tracking ----------------------------------------------

    def record_failed_server(self, name: str, *, hours: int | None = None) -> None:
        """Record a server name that failed its eager connect attempt.

        Called by ``load_mcp_tools`` when a connect attempt raises so the
        caller can surface the unavailability in the agent's system prompt
        and produce a visible acknowledgement in the run output rather than
        silently missing tools.  Lazy (cached-tools) connect failures are
        NOT recorded here — they surface at tool-call time as an error
        result the LLM already sees.

        *hours* is the number of whole hours elapsed since the first failure
        in the current outage window (from ``get_mcp_failure_hours``).  Any
        non-``None`` value is stored; the >= 24 h display threshold is
        enforced by ``format_mcp_unavailable_prompt`` when the Tool
        Availability Notice is rendered, not here.
        """
        if name not in self._failed_servers:
            self._failed_servers.append(name)
        if hours is not None:
            self._failed_server_durations[name] = hours

    @property
    def failed_servers(self) -> list[str]:
        """Names of MCP servers that failed to connect during this run."""
        return list(self._failed_servers)

    @property
    def failed_server_durations(self) -> dict[str, int]:
        """Mapping of server name → hours since first failure for failed servers.

        Only populated for servers that have been failing for at least one
        measurable period (i.e. ``hours`` was available when
        :meth:`record_failed_server` was called).  Callers pass this to
        :func:`format_mcp_unavailable_prompt` to surface duration-aware
        notices such as "unreachable for 3 days".
        """
        return dict(self._failed_server_durations)

    async def auth_broken_servers(self) -> list[str]:
        """Return names of servers with persistent auth failures.

        Reads the cross-run Redis auth-failure counter for every registered
        connection.  Servers at or above :data:`_MCP_AUTH_FAILURE_THRESHOLD`
        are returned so the caller can include a proactive notice in the
        agent's system prompt — preventing wasted iterations on tools that
        are known to be credential-broken before the first call even fires.
        All Redis reads are fail-open (returns an empty list on error).
        """
        if not self._connections:
            return []
        counts = await asyncio.gather(
            *(
                _get_mcp_auth_failure_count(conn.config.failure_key)
                for conn in self._connections
            )
        )
        return [
            conn.config.name
            for conn, count in zip(self._connections, counts, strict=True)
            if count >= _MCP_AUTH_FAILURE_THRESHOLD
        ]

    # -- lifecycle -----------------------------------------------------------

    async def connect_server(
        self, config: MCPServerRuntime, *, force: bool = False
    ) -> list[ToolDefinition]:
        """Connect to one MCP server and return its tools.

        Persistent failure tracking (Redis counter + ERROR escalation)
        happens inside :meth:`MCPConnection.connect` so the lazy
        cached-tools path — where ``execute_tool`` connects on first
        use — is covered by the same counter.

        *force* bypasses the circuit breaker (see
        :meth:`MCPConnection.connect`).  Pass ``force=True`` from
        explicit user-initiated flows (e.g. the admin test-tool
        endpoint) so they always reach the wire even during a cooldown.
        """
        conn = MCPConnection(config, artifact_scope=self.artifact_scope)
        await conn.connect(force=force)
        self._connections.append(conn)
        return conn.tools

    def register_cached_server(
        self,
        config: MCPServerRuntime,
        cached_tools: list[dict[str, Any]],
    ) -> list[ToolDefinition]:
        """Register tools from DB cache **without** connecting.

        The connection is deferred until a tool is actually invoked via
        :meth:`execute_tool`.  This avoids subprocess/network overhead
        when the LLM doesn't end up calling any MCP tools.

        *cached_tools* is the ``MCPServer.discovered_tools`` JSON list,
        each item having ``name``, ``description``, and ``input_schema``.
        """
        tools = []
        for t in cached_tools:
            schema = t.get("input_schema", {"type": "object", "properties": {}})
            tools.append(
                ToolDefinition(
                    name=t["name"],
                    description=_enrich_tool_description(
                        t.get("description", ""), schema
                    ),
                    input_schema=schema,
                    source=f"mcp:{config.name}",
                )
            )
        conn = MCPConnection(
            config, cached_tools=tools, artifact_scope=self.artifact_scope
        )
        self._connections.append(conn)
        logger.info(
            "Registered %d cached tool(s) from MCP server '%s' (lazy connection)",
            len(tools),
            config.name,
        )
        return tools

    async def disconnect_all(self) -> None:
        for conn in self._connections:
            try:
                await conn.disconnect()
            except (Exception, asyncio.CancelledError) as exc:
                logger.warning(
                    "Error disconnecting MCP server '%s': %s",
                    conn.config.name,
                    format_exception_message(exc),
                )
        self._connections.clear()

    # -- tool access ---------------------------------------------------------

    def resource_tools(self) -> list[ToolDefinition]:
        """:data:`READ_RESOURCE_TOOL_NAME`, for a run that carries MCP tools; register it with this manager."""
        return [resource_read_tool_definition()] if self._connections else []

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        """Route a tool call to the correct MCP server.

        :data:`READ_RESOURCE_TOOL_NAME` is the platform's: its definition
        replaces a server tool of the same name in the composite, so the call
        is never routed to that server.
        """
        if call.name == READ_RESOURCE_TOOL_NAME:
            return await self._read_resource_link(call)
        for conn in self._connections:
            if conn.has_tool(call.name):
                return await conn.execute_tool(call)
        return ToolResult(
            tool_call_id=call.id,
            content=f"No MCP server provides tool '{call.name}'",
            is_error=True,
        )

    async def _read_resource_link(self, call: ToolCall) -> ToolResult:
        """Read a resource link on the connection whose tool returned it."""
        arguments = call.arguments or {}
        uri = str(arguments.get("uri") or "").strip()
        server = str(arguments.get("server") or "").strip()
        if not uri:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: uri is required - the URI of a '[Resource link: ...]' line.",
                is_error=True,
            )
        # A server is named by its id (the one the link line gives) or its
        # name, which two bound servers may share.
        holders = [
            conn
            for conn in self._connections
            if conn.linked_resource(uri) and (not server or server in (conn.config.server_key, conn.config.name))
        ]
        if len(holders) > 1:
            servers = ", ".join(sorted(f'"{conn.config.server_key}" ({conn.config.name})' for conn in holders))
            return ToolResult(
                tool_call_id=call.id,
                content=f"Several MCP servers linked '{uri}': pass server= one of {servers}.",
                is_error=True,
            )
        if not holders:
            where = f" on server '{server}'" if server else ""
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"No MCP tool linked '{uri}'{where} in this run: {READ_RESOURCE_TOOL_NAME} reads "
                    "only a resource link an MCP tool returned. Call the tool that lists it, then "
                    "read the URI of its '[Resource link: ...]' line."
                ),
                is_error=True,
            )
        return await holders[0].read_resource_link(call, uri)

    # -- testing helper ------------------------------------------------------

    async def test_connection(self, config: MCPServerRuntime) -> dict[str, Any]:
        """Test connectivity to a server and return its tool list (then disconnect).

        Always bypasses the circuit breaker (``force=True``) so that an
        explicit user-initiated test is never silently skipped — a
        successful test also resets the failure counter and closes the
        circuit, restoring normal tool availability immediately.

        Every request of the test - the probes included - goes through the
        server's relay group when it is routed through one.
        """
        with egress_route(await runtime_egress_route(config)):
            return await self._test_connection(config)

    async def _test_connection(self, config: MCPServerRuntime) -> dict[str, Any]:
        conn = MCPConnection(config)
        try:
            await conn.connect(force=True)
            tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in conn.tools
            ]
            result: dict[str, Any] = {"status": "ok", "tools": tools}

            # Connecting and listing tools proves nothing about whether calls
            # will be accepted: plenty of remote MCP servers answer
            # ``initialize`` / ``tools/list`` to anyone and gate ``tools/call``
            # behind a bearer token. Reporting a plain success there is what
            # produced the contradiction users hit — a green test followed by
            # "Authorization required" on every call.
            pending = await _pending_authorization_metadata(config)
            if pending is not None:
                result["status"] = "auth_required"
                result["resource_metadata"] = pending
            return result
        except MCPAuthorizationRequiredError as exc:
            return {
                "status": "auth_required",
                "message": str(exc),
                "resource_metadata": exc.resource_metadata,
            }
        except MCPRedirectNotFollowedError as exc:
            # The endpoint answered, with a redirect: no 401 to probe for.
            return {
                "status": "error",
                "message": str(exc),
                "redirect_location": exc.location,
            }
        except BaseException as exc:
            # Deliberately broad, and deliberately catching BaseException: the
            # MCP SDK can report an unauthorized endpoint as a *CancelledError*
            # from inside its transport task group. ``connect()`` turns that
            # cancellation into the failure behind it when the group raises
            # one on close; one that still arrives here carried no
            # such failure, so narrowing to Exception would let the single
            # most useful diagnosis escape as an opaque failure. Ask the
            # endpoint directly before deciding this was a plain error.
            diagnosis = await diagnose_auth_after_connect_failure(config)
            if diagnosis is not None:
                return diagnosis
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"status": "error", "message": str(exc)}
        finally:
            await conn.disconnect()
