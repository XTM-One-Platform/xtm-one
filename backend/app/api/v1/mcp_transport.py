# SPDX-License-Identifier: Apache-2.0

"""The MCP Streamable HTTP transport of XTM One's MCP servers.

One implementation for ``/mcp/user``, ``/mcp/global``, ``/mcp/opencti``,
``/mcp/openaev``, ``/mcp/opencrq`` and ``/mcp/xtm``: each endpoint
authenticates and gates the request its own way, then hands the body to
:func:`read_messages`, dispatches every message it returns through
:func:`dispatch_all` and answers with :func:`respond`.

The servers are **dual-era** (2026-07-28, "Backward Compatibility with
Initialization-Based Versions"):

- **Modern** (:data:`MODERN_PROTOCOL_VERSIONS`, 2026-07-28): no handshake. A
  request is modern when its ``params._meta`` names a protocol version or its
  ``MCP-Protocol-Version`` header names one that is not a legacy revision. It
  must carry ``io.modelcontextprotocol/protocolVersion`` and
  ``io.modelcontextprotocol/clientCapabilities`` (else 400, ``-32602``), the
  ``MCP-Protocol-Version``, ``Mcp-Method`` and, for ``tools/call``,
  ``Mcp-Name`` headers agreeing with the body (else 400 ``HeaderMismatch``,
  ``-32020``); an unknown version is a 400 ``UnsupportedProtocolVersionError``
  (``-32022``) listing :data:`SUPPORTED_PROTOCOL_VERSIONS`, an unknown method a
  404. Every result carries ``resultType`` and the server's identity in
  ``_meta``, ``tools/list`` its caching hints. ``server/discover`` and
  ``subscriptions/listen`` are answered here for every endpoint; ``ping`` and
  ``initialize`` do not exist in this era.
- **Legacy** (:data:`LEGACY_PROTOCOL_VERSIONS`): ``initialize`` negotiates one
  of them (:func:`negotiate_protocol_version` echoes a supported one and
  answers the latest legacy revision otherwise), and every later request is
  served as before.

What else the specification asks of a server, and where it is met:

- **``MCP-Protocol-Version``** (2025-06-18): a value this server does not
  support is a 400 ``UnsupportedProtocolVersionError`` - except on a legacy
  ``initialize``, where the version is negotiated, as the reference SDKs do;
  no header means 2025-03-26, as the specification says. A supported revision
  is accepted in either era, so a 2026-07-28 notification, client response or
  batch is read like any other body: a 202, a 202, a "batches are not part of
  MCP 2026-07-28" 400.
- **Batches**: required by 2025-03-26, removed by 2025-06-18 - accepted for a
  request speaking 2025-03-26 (bounded by :data:`MAX_BATCH_MESSAGES`, and by
  :data:`MAX_BATCH_TOOL_CALLS` for the calls that run tools; an ``initialize``
  is never part of one) and refused with a 400 otherwise. Each element is
  answered on its own - one that fails is a ``-32603`` next to the others'
  answers - and a client matches the answers by ``id``: invalid elements come
  first.
- **Notifications and responses** a client sends get no JSON-RPC answer and are
  never dispatched: a body holding nothing else is a ``202 Accepted``. A
  ``notifications/*`` method sent WITH an id is a request, answered ``-32601``.
- **Envelope**: ``jsonrpc`` must be ``"2.0"``; anything else is ``-32600``.
- **``Origin``** (DNS rebinding): :func:`require_valid_origin` refuses, with a
  403, a web origin that is neither the platform's own (``BASE_URL``,
  ``FRONTEND_URL``) nor one of ``CORS_ORIGINS`` (``*`` there admits every
  origin, as it does for CORS). Clients that send no
  ``Origin`` - every desktop, CLI and hosted client - are not concerned. A
  configured origin that cannot be read is logged once, by name.
- **``serverInfo``**: ``title`` (2025-06-18) and ``description`` (2025-11-25)
  only to a client that negotiated a revision defining them; a modern request
  speaks a revision defining both.
- **``GET`` / ``DELETE``**: these servers open no SSE stream and keep no session,
  so both are a 405 with ``Allow: POST`` (:func:`add_unsupported_method_routes`),
  in both eras.
- **Tool errors** (SEP-1303, 2025-11-25): invalid arguments and failed calls are
  tool execution errors (:func:`tool_error_result`, ``isError``), which the model
  can read and correct; an unknown tool stays a protocol error.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.csp import origin_source
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Revisions without a handshake: every request declares its version.
MODERN_PROTOCOL_VERSIONS: tuple[str, ...] = ("2026-07-28",)

#: Revisions that open with ``initialize``, newest first.
LEGACY_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26")

#: Every revision these servers speak, newest first.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = MODERN_PROTOCOL_VERSIONS + LEGACY_PROTOCOL_VERSIONS

LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

#: The revision ``initialize`` answers a client asking for one it does not
#: negotiate: the handshake only exists up to the latest legacy revision.
LATEST_LEGACY_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[0]

#: The revision a request without ``MCP-Protocol-Version`` speaks (2025-06-18,
#: "Protocol Version Header": the server SHOULD assume 2025-03-26).
DEFAULT_PROTOCOL_VERSION = "2025-03-26"

#: The revisions that still carry JSON-RPC batches (2025-06-18 removed them).
_BATCH_PROTOCOL_VERSIONS = frozenset({"2025-03-26"})

#: The most messages one batch may carry. A ``tools/call`` on ``/mcp/user``
#: runs a whole agent loop, and a batch runs its calls one after the other in
#: one HTTP request.
MAX_BATCH_MESSAGES = 16

#: The most ``tools/call`` requests one batch may carry. On ``/mcp/user`` and
#: ``/mcp/global`` each runs a whole agent loop, one after the other, in one
#: HTTP request holding one concurrency slot and one database session: the
#: message cap above bounds messages, this one bounds the work.
MAX_BATCH_TOOL_CALLS = 4

PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
METHOD_HEADER = "Mcp-Method"
NAME_HEADER = "Mcp-Name"

#: Reserved ``_meta`` keys of the modern revision.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"

#: JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: Codes the MCP specification reserves (-32020 .. -32099).
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

#: Caching hints of the modern revision. A tool list depends on the caller's
#: visibility, so it is ``private`` and kept short; the discovery result is
#: the same for every caller of an endpoint.
TOOLS_LIST_TTL_MS = 60_000
DISCOVER_TTL_MS = 3_600_000

#: Methods of the handshake era a modern request cannot name.
_LEGACY_ONLY_METHODS = frozenset({"initialize", "ping", "logging/setLevel"})

_BASE64_SENTINEL_PREFIX = "=?base64?"
_BASE64_SENTINEL_SUFFIX = "?="

#: The revision that added each optional ``serverInfo`` field.
_SERVER_INFO_FIELD_SINCE = {"title": "2025-06-18", "description": "2025-11-25"}


class MCPRequest(BaseModel):
    """JSON-RPC 2.0 request envelope used by MCP Streamable HTTP."""

    # Checked on the raw message (``_parse_element``): a missing or other
    # value is an invalid request. The default serves messages built here.
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] | None = None

    @property
    def is_notification(self) -> bool:
        """A message without an id asks for no answer (JSON-RPC 2.0 §4.1)."""
        return self.id is None


def jsonrpc_response(id: int | str | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def jsonrpc_error(
    id: int | str | None, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": error}


def tool_result(req_id: int | str | None, text: str, *, is_error: bool = False) -> dict[str, Any]:
    """A ``tools/call`` result carrying one text block."""
    return jsonrpc_response(
        req_id, {"content": [{"type": "text", "text": text or ""}], "isError": is_error}
    )


def tool_error_result(req_id: int | str | None, message: str) -> dict[str, Any]:
    """A tool execution error: in the result, where the model reads it (SEP-1303)."""
    return tool_result(req_id, message, is_error=True)


# ── Initialization ───────────────────────────────────────────────────────────


def negotiate_protocol_version(params: dict[str, Any] | None) -> str:
    """The revision to answer ``initialize`` with: a legacy one, always."""
    requested = (params or {}).get("protocolVersion")
    if isinstance(requested, str) and requested in LEGACY_PROTOCOL_VERSIONS:
        return requested
    return LATEST_LEGACY_PROTOCOL_VERSION


@dataclass(frozen=True)
class ServerIdentity:
    """What a server says it is, in ``initialize``, ``server/discover`` and ``_meta``."""

    name: str
    description: str

    def implementation(self, version: str = LATEST_PROTOCOL_VERSION) -> dict[str, Any]:
        """The MCP ``Implementation`` object, as *version* defines it.

        ``title`` (2025-06-18) and ``description`` (2025-11-25) only for a
        revision defining them: a strict client validates ``serverInfo``
        against its own revision. A modern request speaks the latest.
        """
        info: dict[str, Any] = {"name": self.name, "version": settings.app_version}
        optional = {"title": self.name, "description": self.description}
        for key, since in _SERVER_INFO_FIELD_SINCE.items():
            if version >= since:  # ISO dates compare as strings
                info[key] = optional[key]
        return info


def initialize_result(
    req_id: int | str | None,
    params: dict[str, Any] | None,
    *,
    name: str,
    description: str,
) -> dict[str, Any]:
    """The ``initialize`` answer of a tools-only server.

    ``name`` keeps the value every client has seen so far; ``title`` (2025-06-18)
    and ``description`` (2025-11-25) are what a client displays, sent only to a
    client that negotiated a revision defining them.
    """
    version = negotiate_protocol_version(params)
    return jsonrpc_response(
        req_id,
        {
            "protocolVersion": version,
            "capabilities": _CAPABILITIES,
            "serverInfo": ServerIdentity(name, description).implementation(version),
        },
    )


#: Tools only, and no change notifications: a tool list is read again once
#: its ``ttlMs`` has passed.
_CAPABILITIES: dict[str, Any] = {"tools": {"listChanged": False}}


def discover_result(req_id: int | str | None, identity: ServerIdentity) -> dict[str, Any]:
    """The ``server/discover`` answer (2026-07-28): versions, capabilities, identity."""
    return jsonrpc_response(
        req_id,
        {
            "resultType": "complete",
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": _CAPABILITIES,
            "_meta": {META_SERVER_INFO: identity.implementation()},
            "ttlMs": DISCOVER_TTL_MS,
            "cacheScope": "public",
        },
    )


# ── Origin ───────────────────────────────────────────────────────────────────

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _ip_literal_origin(value: str) -> str | None:
    """``scheme://[v6]:port`` / ``scheme://v4:port`` of an IP-literal URL, else ``None``."""
    try:
        parts = urlsplit(value.strip())
        hostname, port = parts.hostname, parts.port
        address = ipaddress.ip_address(hostname or "")
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{scheme}://{host}{f':{port}' if port else ''}"


def _web_origin(value: object) -> str | None:
    """``scheme://host[:port]`` of an http(s) URL, the default port left out.

    A host name as :func:`origin_source` reads it, or an IP literal (an IPv6
    one included, which ``origin_source`` does not take).
    """
    source = origin_source(value)
    if source is None and isinstance(value, str):
        source = _ip_literal_origin(value)
    if source is None:
        return None
    scheme, _, rest = source.partition("://")
    host, sep, port = rest.rpartition(":")
    if not sep or host.endswith(":") or "]" in port:
        return source  # no port (an IPv6 host holds colons of its own)
    if int(port) == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return source


#: Configured origins already reported as unreadable (logged once each).
_UNREADABLE_ORIGINS_LOGGED: set[str] = set()


def _configured_origin(value: object) -> str | None:
    origin = _web_origin(value)
    if origin is None and isinstance(value, str) and value and value not in _UNREADABLE_ORIGINS_LOGGED:
        _UNREADABLE_ORIGINS_LOGGED.add(value)
        logger.warning(
            "Ignoring %r as an origin allowed to call the MCP servers: it is not an http(s) "
            "origin this platform can read (BASE_URL, FRONTEND_URL or CORS_ORIGINS)",
            value[:200],
        )
    return origin


def allowed_origins() -> frozenset[str] | None:
    """The web origins a browser may call these servers from; ``None`` for any.

    ``CORS_ORIGINS=*`` admits every origin, for CORS and here alike: an
    operator who opened the platform to every page has made that choice once.
    """
    cors_origins = settings.cors_origins or []
    if "*" in cors_origins:
        return None
    candidates = [settings.base_url, settings.frontend_url, *cors_origins]
    return frozenset(origin for origin in map(_configured_origin, candidates) if origin)


def require_valid_origin(request: Request) -> None:
    """Refuse a request a web page of another origin sent (403).

    No ``Origin``: not a browser, nothing to check. An ``Origin`` that is not a
    web origin (a browser extension, an application scheme) is not a page a
    DNS-rebinding attack can serve either. ``null`` is: a sandboxed frame of
    any page sends it.
    """
    raw = request.headers.get("origin")
    if raw is None:
        return
    value = raw.strip()
    origins = allowed_origins()
    if origins is None:
        return
    if value.lower() == "null":
        allowed = False
    else:
        scheme = value.partition("://")[0].lower()
        if scheme not in ("http", "https"):
            return
        origin = _web_origin(value)
        allowed = origin is not None and origin in origins
    if not allowed:
        # INFO: any caller can send this before authentication. ``%r`` keeps a
        # hostile header from forging a log line.
        logger.info(
            "Refused an MCP request from origin %r (not BASE_URL, FRONTEND_URL or CORS_ORIGINS)",
            value[:100],
        )
        raise HTTPException(
            status_code=403,
            detail="This origin may not call this MCP server. Add it to CORS_ORIGINS to allow it.",
        )


# ── Messages ─────────────────────────────────────────────────────────────────


@dataclass
class IncomingMessages:
    """The JSON-RPC messages of one POST body, in order.

    ``requests`` are dispatched; ``errors`` are answers already decided for
    elements that were not valid requests (a batch keeps its other elements).
    ``modern`` says the body is one request of a handshake-free revision.
    """

    batch: bool
    protocol_version: str
    requests: list[MCPRequest] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    modern: bool = False


def _bad_request(
    code: int, message: str, *, req_id: int | str | None = None, data: Any = None
) -> JSONResponse:
    """A body this server cannot accept: an HTTP 400 carrying a JSON-RPC error."""
    return JSONResponse(status_code=400, content=jsonrpc_error(req_id, code, message, data))


def _unsupported_version(version: str, *, req_id: int | str | None = None) -> JSONResponse:
    """The 400 ``UnsupportedProtocolVersionError``: what is supported, what was asked."""
    return _bad_request(
        UNSUPPORTED_PROTOCOL_VERSION,
        "Unsupported protocol version",
        req_id=req_id,
        data={"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": version[:40]},
    )


def read_protocol_version(request: Request, *, initializing: bool = False) -> str | JSONResponse:
    """The revision a body read outside the modern request path speaks, or the 400.

    Any supported revision: a 2026-07-28 notification, client response or batch
    reaches here (only a request with an id and a method takes the modern
    path), and refusing it as "unsupported" would contradict the ``supported``
    list of that very refusal. A batch is then refused as a batch, a
    notification or a response answered 202.

    An ``initialize`` is never refused for its header and always negotiates a
    legacy revision: the handshake exists up to the latest legacy one.
    """
    header = request.headers.get(PROTOCOL_VERSION_HEADER)
    if header is None:
        return DEFAULT_PROTOCOL_VERSION
    version = header.strip()
    if initializing and version not in LEGACY_PROTOCOL_VERSIONS:
        return DEFAULT_PROTOCOL_VERSION
    if version in SUPPORTED_PROTOCOL_VERSIONS:
        return version
    return _unsupported_version(version)


def is_modern_request(request: Request) -> bool:
    """Whether the request's header names a handshake-free revision.

    Read before the body (authentication runs first): a modern client sends
    the header on every request.
    """
    header = request.headers.get(PROTOCOL_VERSION_HEADER)
    return header is not None and header.strip() in MODERN_PROTOCOL_VERSIONS


def _is_client_response(message: dict[str, Any]) -> bool:
    """A JSON-RPC response a client sends (to a server request): no answer."""
    return "method" not in message and ("result" in message or "error" in message)


def _parse_element(message: Any, incoming: IncomingMessages) -> None:
    if not isinstance(message, dict):
        incoming.errors.append(jsonrpc_error(None, INVALID_REQUEST, "Invalid request"))
        return
    if _is_client_response(message):
        return
    if message.get("jsonrpc") != "2.0":
        message_id = message.get("id")
        incoming.errors.append(
            jsonrpc_error(message_id if isinstance(message_id, int | str) else None, INVALID_REQUEST, "Invalid request")
        )
        return
    try:
        incoming.requests.append(MCPRequest(**message))
    except (ValidationError, TypeError):
        message_id = message.get("id")
        if not isinstance(message_id, int | str):
            message_id = None
        incoming.errors.append(jsonrpc_error(message_id, INVALID_REQUEST, "Invalid request"))


def _request_meta(message: dict[str, Any]) -> dict[str, Any] | None:
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta if isinstance(meta, dict) else None


def decode_header_value(value: str) -> str | None:
    """A mirrored header value, its Base64 sentinel form decoded; ``None`` if malformed."""
    if value.startswith(_BASE64_SENTINEL_PREFIX) and value.endswith(_BASE64_SENTINEL_SUFFIX):
        encoded = value[len(_BASE64_SENTINEL_PREFIX) : -len(_BASE64_SENTINEL_SUFFIX)]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    return value


def _read_modern(request: Request, message: dict[str, Any], version: str) -> IncomingMessages | JSONResponse:
    """One request of a handshake-free revision, validated as the transport requires."""
    req_id = message.get("id") if isinstance(message.get("id"), int | str) else None
    meta = _request_meta(message) or {}
    method = message.get("method")

    meta_version = meta.get(META_PROTOCOL_VERSION)
    if not isinstance(meta_version, str):
        return _bad_request(
            INVALID_PARAMS, f"Missing _meta {META_PROTOCOL_VERSION}", req_id=req_id
        )
    header = request.headers.get(PROTOCOL_VERSION_HEADER)
    if header is None or header.strip() != meta_version:
        return _bad_request(
            HEADER_MISMATCH,
            f"Header mismatch: {PROTOCOL_VERSION_HEADER} must equal _meta {META_PROTOCOL_VERSION}",
            req_id=req_id,
        )
    if not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict):
        return _bad_request(
            INVALID_PARAMS, f"Missing _meta {META_CLIENT_CAPABILITIES}", req_id=req_id
        )
    if request.headers.get(METHOD_HEADER) != method:
        return _bad_request(
            HEADER_MISMATCH,
            f"Header mismatch: {METHOD_HEADER} must equal the request method",
            req_id=req_id,
        )
    if method == "tools/call":
        name_header = request.headers.get(NAME_HEADER)
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        decoded = decode_header_value(name_header) if name_header is not None else None
        if decoded is None or decoded != params.get("name"):
            return _bad_request(
                HEADER_MISMATCH,
                f"Header mismatch: {NAME_HEADER} must equal the tool name",
                req_id=req_id,
            )

    incoming = IncomingMessages(batch=False, protocol_version=version, modern=True)
    _parse_element(message, incoming)
    if incoming.errors:
        return _bad_request(INVALID_REQUEST, "Invalid request", req_id=req_id)
    return incoming


async def read_messages(request: Request) -> IncomingMessages | JSONResponse:
    """The messages of a POST body, or the 400 that refuses the body."""
    try:
        raw = await request.json()
    except Exception as exc:
        logger.warning("MCP request body is not JSON: %s", format_exception_message(exc))
        return _bad_request(PARSE_ERROR, "Parse error: invalid JSON-RPC request")

    if isinstance(raw, dict) and "id" in raw and "method" in raw:
        meta = _request_meta(raw)
        meta_version = meta.get(META_PROTOCOL_VERSION) if meta else None
        header = request.headers.get(PROTOCOL_VERSION_HEADER)
        header_version = header.strip() if header is not None else None
        named = meta_version if isinstance(meta_version, str) else header_version
        legacy_initialize = raw.get("method") == "initialize" and meta_version is None
        if named is not None and named not in LEGACY_PROTOCOL_VERSIONS and not legacy_initialize:
            if named not in SUPPORTED_PROTOCOL_VERSIONS:
                req_id = raw["id"] if isinstance(raw["id"], int | str) else None
                return _unsupported_version(named, req_id=req_id)
            return _read_modern(request, raw, named)

    initializing = isinstance(raw, dict) and raw.get("method") == "initialize"
    version = read_protocol_version(request, initializing=initializing)
    if isinstance(version, JSONResponse):
        return version

    if isinstance(raw, list):
        if version not in _BATCH_PROTOCOL_VERSIONS:
            return _bad_request(
                INVALID_REQUEST,
                f"JSON-RPC batches are not part of MCP {version}: send one message per request.",
            )
        if not raw:
            return _bad_request(INVALID_REQUEST, "Invalid request: empty batch")
        if len(raw) > MAX_BATCH_MESSAGES:
            return _bad_request(
                INVALID_REQUEST,
                f"A batch carries at most {MAX_BATCH_MESSAGES} messages.",
            )
        incoming = IncomingMessages(batch=True, protocol_version=version)
        for message in raw:
            _parse_element(message, incoming)
        if any(message.method == "initialize" for message in incoming.requests):
            return _bad_request(INVALID_REQUEST, "An initialize request must not be part of a batch.")
        calls = sum(1 for m in incoming.requests if m.method == "tools/call" and not m.is_notification)
        if calls > MAX_BATCH_TOOL_CALLS:
            return _bad_request(
                INVALID_REQUEST,
                f"A batch carries at most {MAX_BATCH_TOOL_CALLS} tools/call requests.",
            )
        return incoming

    incoming = IncomingMessages(batch=False, protocol_version=version)
    _parse_element(raw, incoming)
    if incoming.errors:
        return _bad_request(INVALID_REQUEST, "Invalid request")
    return incoming


Dispatch = Callable[[MCPRequest], Awaitable[dict[str, Any] | None]]


@dataclass
class StreamedAnswer:
    """An answer sent as an SSE stream: these messages, in order."""

    messages: list[dict[str, Any]]


def _subscription_answer(req_id: int | str, identity: ServerIdentity) -> StreamedAnswer:
    """``subscriptions/listen``: acknowledged with nothing, then closed gracefully.

    These servers emit no change notifications (``listChanged`` is false), so
    the acknowledgment agrees to no type and the subscription ends at once
    with its completion result, which tells the client it ended cleanly.
    """
    return StreamedAnswer(
        [
            {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {"_meta": {META_SUBSCRIPTION_ID: req_id}, "notifications": {}},
            },
            jsonrpc_response(
                req_id,
                {
                    "resultType": "complete",
                    "_meta": {
                        META_SUBSCRIPTION_ID: req_id,
                        META_SERVER_INFO: identity.implementation(),
                    },
                },
            ),
        ]
    )


def _modernize(answer: dict[str, Any], method: str, identity: ServerIdentity) -> dict[str, Any]:
    """A modern result: ``resultType``, the server's identity, caching hints."""
    if not isinstance(answer.get("result"), dict):
        return answer
    result = dict(answer["result"])
    result.setdefault("resultType", "complete")
    meta = dict(result.get("_meta") or {})
    meta.setdefault(META_SERVER_INFO, identity.implementation())
    result["_meta"] = meta
    if method == "tools/list":
        result.setdefault("ttlMs", TOOLS_LIST_TTL_MS)
        result.setdefault("cacheScope", "private")
    return {**answer, "result": result}


async def dispatch_all(
    incoming: IncomingMessages, dispatch: Dispatch, *, identity: ServerIdentity
) -> list[dict[str, Any] | StreamedAnswer]:
    """Dispatch every request in order; the answers, the invalid elements' first.

    A message without an id is never dispatched: these servers act on no
    notification, and a ``tools/call`` sent without one would run a tool whose
    result nobody can receive. ``server/discover`` is answered here in both
    eras (a server MUST implement it); a modern request also gets
    ``subscriptions/listen`` here, and never a handshake-era method. In a
    batch, an element whose dispatch fails is answered ``-32603`` and the
    others keep their answers - the earlier ones may have acted already, and a
    client that got nothing back would repeat them; a lone message's failure
    propagates as before.
    """
    answers: list[dict[str, Any] | StreamedAnswer] = list(incoming.errors)
    for message in incoming.requests:
        if message.is_notification:
            continue
        if message.method == "server/discover":
            answers.append(discover_result(message.id, identity))
            continue
        if incoming.modern:
            if message.method == "subscriptions/listen":
                answers.append(_subscription_answer(message.id, identity))
                continue
            if message.method in _LEGACY_ONLY_METHODS:
                answers.append(
                    jsonrpc_error(message.id, METHOD_NOT_FOUND, f"Method not found: {message.method}")
                )
                continue
        try:
            answer = await dispatch(message)
        except Exception as exc:
            if not incoming.batch:
                raise
            logger.exception("MCP dispatch failed for %r in a batch: %s", message.method[:80], format_exception_message(exc))
            answer = jsonrpc_error(message.id, INTERNAL_ERROR, "Internal error")
        if answer is not None:
            answers.append(_modernize(answer, message.method, identity) if incoming.modern else answer)
    return answers


def _sse(messages: list[dict[str, Any]]) -> Response:
    body = "".join(f"event: message\ndata: {json.dumps(message)}\n\n" for message in messages)
    return Response(
        content=body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def respond(incoming: IncomingMessages, answers: list[dict[str, Any] | StreamedAnswer]) -> Response:
    """The HTTP answer: ``202`` with nothing to say, the answer, or the batch's.

    A modern request naming a method this server does not implement is a 404
    (the JSON-RPC body tells it from a server that hosts no MCP endpoint).
    """
    if not answers:
        return Response(status_code=202)
    if not incoming.batch and isinstance(answers[0], StreamedAnswer):
        return _sse(answers[0].messages)
    content: Any = answers if incoming.batch else answers[0]
    status = 200
    if (
        incoming.modern
        and isinstance(content, dict)
        and (content.get("error") or {}).get("code") == METHOD_NOT_FOUND
    ):
        status = 404
    return JSONResponse(content=content, status_code=status)


# ── Methods other than POST ──────────────────────────────────────────────────


async def _method_not_allowed() -> Response:
    """No SSE stream to open, no session to end: only POST."""
    return Response(status_code=405, headers={"Allow": "POST"})


def add_unsupported_method_routes(router: APIRouter, *paths: str) -> None:
    """Answer ``GET`` and ``DELETE`` on each MCP endpoint path with a 405.

    Without these, a ``GET`` falls through to the SPA catch-all and is answered
    404, which a client reads as "no such server".
    """
    for path in paths:
        router.add_api_route(
            path,
            _method_not_allowed,
            methods=["GET", "DELETE"],
            include_in_schema=False,
        )
