# SPDX-License-Identifier: Apache-2.0

"""Who a client of the MCP authorization server is.

Two kinds of ``client_id`` reach ``/mcp/oauth/authorize`` and ``/mcp/oauth/token``:

- **A registered client** (``mcp-...``): a row of ``mcp_oauth_clients``, made by
  Dynamic Client Registration (RFC 7591) or for an A2A partner.
- **A Client ID Metadata Document** (CIMD, MCP 2025-11-25, SEP-991): the
  ``client_id`` is an HTTPS URL, and the JSON document served there describes
  the client - its name and the redirect URIs it may use. Claude, VS Code and
  other hosted clients use one whenever the authorization server advertises it
  (``client_id_metadata_document_supported``): no registration call, so no
  registration rate limit and no row per connection.

:func:`resolve_client_metadata` fetches and validates a document:

- **The fetch** resolves the host ONCE, refuses it unless every address it
  resolves to is public (``web_fetch._is_safe_address``, the platform's
  outbound policy), and connects to that validated address - TLS still
  verified against the host name - so a host answering a public address to
  the check and a private one to the connection (DNS rebinding) reaches
  nothing. No proxy from the environment, no redirect, an identity encoding:
  at most :data:`MAX_DOCUMENT_BYTES` raw bytes of an ``application/json``
  body, all within :data:`FETCH_TIMEOUT_SECONDS`.
- **The budgets**: a cache miss spends the caller's budget of
  :data:`FETCHES_PER_MINUTE`, the host's of :data:`HOST_FETCHES_PER_MINUTE` and
  the platform's of :data:`PLATFORM_FETCHES_PER_MINUTE`, refused while Redis
  cannot count them, so the unauthenticated authorization endpoint cannot be
  turned into a fetcher of arbitrary URLs, nor fill Redis from many addresses.
  A client already known never depends on them: once its entry is past its
  ``max-age``, an exhausted budget or an unreachable document serves the
  entry it has (for up to :data:`STALE_GRACE_SECONDS` more) instead of a 429.
- **The answer**: one constant ``Invalid client metadata document``. Why a
  document was refused is logged, never answered: the answer would tell a
  caller what the platform resolves a host name to, or whether an address
  answered.
- **The document** must name its own URL as ``client_id``, carry a
  ``client_name`` (MCP 2025-11-25, "Client ID Metadata Documents"; NFKC
  normalised, no control or formatting character), list redirect URIs this
  server accepts (:func:`redirect_uri_allowed`), and be a public client:
  ``token_endpoint_auth_method`` absent or ``none``.
- **The cache**: the fields kept of a valid document (a few kilobytes at
  most) are fresh for its ``Cache-Control`` ``max-age``, bounded by
  :data:`MIN_CACHE_SECONDS` / :data:`MAX_CACHE_SECONDS` (:data:`DEFAULT_CACHE_SECONDS`
  without one), then kept stale for :data:`STALE_GRACE_SECONDS`; a refusal for
  :data:`REFUSAL_CACHE_SECONDS`. With the platform's fetch budget, that bounds
  the entries Redis can hold at any time. The token endpoint reads the cached
  refusal of a client to refuse its refreshes (:func:`cached_refusal`).

The client it returns is a transient ``OAuthClient``, never added to a session:
a document stays the source of truth, and nothing grows the client table.

:func:`redirect_uri_matches` is the redirect rule of every client: an exact
match, or - for an ``http`` loopback redirect registered WITHOUT a port - a
match whatever its port (RFC 8252 §7.3). A native client such as Claude Code
declares ``http://localhost/callback`` and listens on an ephemeral port; a
registration that pinned a port, and an ``https`` one, keep the exact match.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, Request

from app.core.rate_limit import enforce_rate_limit
from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.models.oauth_client import OAuthClient

logger = logging.getLogger(__name__)

#: The longest ``client_id`` the authorization codes and tokens carry (the
#: ``mcp_oauth_clients.client_id`` column).
MAX_CLIENT_ID_LENGTH = 255

MAX_DOCUMENT_BYTES = 16 * 1024
#: The whole fetch - resolution, connection, every byte - not each read.
FETCH_TIMEOUT_SECONDS = 5.0
DEFAULT_CACHE_SECONDS = 3600
MIN_CACHE_SECONDS = 300
MAX_CACHE_SECONDS = 3600
REFUSAL_CACHE_SECONDS = 60
#: How long an entry past its ``max-age`` is still served when a refetch is
#: refused a budget or cannot reach the document.
STALE_GRACE_SECONDS = 24 * 3600
#: Documents fetched per client IP and minute (cache misses only).
FETCHES_PER_MINUTE = 20
#: Documents fetched per host and minute (cache misses only): one host - any
#: party can serve documents under as many paths as it likes - cannot spend
#: the whole platform budget alone.
HOST_FETCHES_PER_MINUTE = 10
#: Documents fetched per minute by the whole platform (cache misses only). A
#: client's document is fetched once an hour at most, so this is far above
#: what real clients need, and with :data:`MAX_CACHE_SECONDS` it bounds the
#: cache at a few thousand entries of a few kilobytes.
PLATFORM_FETCHES_PER_MINUTE = 60

MAX_REDIRECT_URIS = 10
MAX_URI_LENGTH = 512
MAX_CLIENT_NAME_LENGTH = 200

_CACHE_PREFIX = "mcp_oauth_cimd:"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_AGE = re.compile(r"(?:^|,)\s*max-age\s*=\s*(\d+)", re.IGNORECASE)


#: The one answer to a document this server does not accept.
CLIENT_METADATA_REFUSED = "Invalid client metadata document"
_UNREACHABLE = "its URL could not be fetched"


class ClientMetadataError(HTTPException):
    """A document that does not describe a client this server accepts (400).

    ``reason`` is logged, never answered (:data:`CLIENT_METADATA_REFUSED`).
    ``transient``: the document could not be reached, which says nothing of a
    client already known.
    """

    def __init__(self, reason: str, *, transient: bool = False) -> None:
        self.reason = reason
        self.transient = transient
        super().__init__(status_code=400, detail=CLIENT_METADATA_REFUSED)


# ── Redirect URIs ────────────────────────────────────────────────────────────


def redirect_uri_allowed(uri: str) -> bool:
    """Whether *uri* may receive an authorization code (OAuth 2.1 transport rule).

    Loopback callbacks (native apps) may use ``http`` or ``https``; every other
    host must use ``https``. The scheme is checked even for loopback hosts so an
    unexpected scheme like ``ftp://localhost/cb`` can't be registered or receive
    an authorization code.
    """
    try:
        parsed = urlsplit(uri)
        hostname = parsed.hostname
    except ValueError:
        return False
    if hostname in _LOOPBACK_HOSTS:
        return parsed.scheme in ("http", "https")
    return parsed.scheme == "https"


def _loopback_parts(uri: str) -> tuple[tuple[str, str, str], int | None] | None:
    """``((host, path, query), port)`` of an ``http`` loopback URI, else ``None``.

    RFC 8252 §7.3 describes ``http`` loopback redirects only.
    """
    try:
        parsed = urlsplit(uri)
        hostname = parsed.hostname
        port = parsed.port  # an unparsable port is no URI to match
    except ValueError:
        return None
    if parsed.scheme.lower() != "http" or hostname not in _LOOPBACK_HOSTS:
        return None
    if parsed.username or parsed.password:
        return None
    return (hostname, parsed.path or "/", parsed.query), port


def redirect_uri_matches(requested: str, registered: list[str] | None) -> bool:
    """Whether *requested* is one of the client's *registered* redirect URIs.

    Exact, except that an ``http`` loopback URI registered without a port
    matches whatever port is requested (RFC 8252 §7.3): the client listens on
    an ephemeral one. A registration that pinned a port keeps it. An empty
    registration list matches nothing, never "anything".
    """
    registered = [uri for uri in (registered or []) if isinstance(uri, str)]
    if requested in registered:
        return True
    wanted = _loopback_parts(requested)
    if wanted is None:
        return False
    for uri in registered:
        parts = _loopback_parts(uri)
        if parts is not None and parts[1] is None and parts[0] == wanted[0]:
            return True
    return False


# ── Client ID Metadata Documents ─────────────────────────────────────────────


def is_metadata_document_url(client_id: str) -> bool:
    """Whether *client_id* names a Client ID Metadata Document.

    An ``https`` URL with a host and a path, no credentials, query, fragment or
    dot segment, that fits :data:`MAX_CLIENT_ID_LENGTH` (the specification's
    rules for a document URL). ASCII only: the consent page names the client
    by this host, and a Unicode host can spell another one's lookalike. A
    registered ``client_id`` is never one.
    """
    if not isinstance(client_id, str) or not client_id.startswith("https://"):
        return False
    if not client_id.isascii():
        return False
    # Printable only: a control character would reach the logs of an
    # unauthenticated caller's request (terminal escapes, forged lines).
    if len(client_id) > MAX_CLIENT_ID_LENGTH or not client_id.isprintable() or " " in client_id:
        return False
    try:
        parsed = urlsplit(client_id)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path in ("", "/"):
        return False
    return not any(segment in (".", "..") for segment in parsed.path.split("/"))


def _cache_key(client_id: str) -> str:
    return _CACHE_PREFIX + hashlib.sha256(client_id.encode("utf-8")).hexdigest()


def _cache_seconds(cache_control: str | None) -> int:
    if cache_control and "no-store" in cache_control.lower():
        return MIN_CACHE_SECONDS
    match = _MAX_AGE.search(cache_control or "")
    seconds = int(match.group(1)) if match else DEFAULT_CACHE_SECONDS
    return max(MIN_CACHE_SECONDS, min(MAX_CACHE_SECONDS, seconds))


def _optional_string(document: dict[str, Any], key: str, limit: int) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ClientMetadataError(f"{key} must be a string of at most {limit} characters")
    return value.strip() or None


#: Unicode categories a client name may not carry: controls, formatting
#: characters (bidirectional overrides, zero-width), private use, surrogates,
#: line and paragraph separators - what makes a name read as another one.
_REFUSED_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Zl", "Zp"})


def _client_name(document: dict[str, Any]) -> str:
    """The document's ``client_name``, normalised; required (MCP 2025-11-25).

    It is the client's own claim, shown on the consent page after the host
    the document came from: NFKC-normalised, whitespace collapsed, and refused
    when it carries a character that changes how it reads.
    """
    value = document.get("client_name")
    if not isinstance(value, str) or len(value) > MAX_CLIENT_NAME_LENGTH:
        raise ClientMetadataError(f"client_name must be a string of at most {MAX_CLIENT_NAME_LENGTH} characters")
    name = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(c) in _REFUSED_NAME_CATEGORIES for c in name if c not in "\t\n\r"):
        raise ClientMetadataError("client_name carries control or formatting characters")
    name = " ".join(name.split())
    if not name:
        raise ClientMetadataError("client_name is required")
    return name


def validate_metadata_document(client_id: str, document: object) -> dict[str, Any]:
    """The client a document describes, or :class:`ClientMetadataError`.

    Returns the fields kept: ``client_name``, ``client_uri``, ``redirect_uris``.
    """
    if not isinstance(document, dict):
        raise ClientMetadataError("the document is not a JSON object")
    if document.get("client_id") != client_id:
        raise ClientMetadataError("its client_id is not the URL it is served at")

    redirect_uris = document.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise ClientMetadataError("redirect_uris must be a non-empty list")
    if len(redirect_uris) > MAX_REDIRECT_URIS:
        raise ClientMetadataError(f"redirect_uris lists more than {MAX_REDIRECT_URIS} URIs")
    for uri in redirect_uris:
        if not isinstance(uri, str) or len(uri) > MAX_URI_LENGTH or "#" in uri:
            raise ClientMetadataError("a redirect URI is not a URI without a fragment")
        if not redirect_uri_allowed(uri):
            raise ClientMetadataError("a redirect URI is neither HTTPS nor a loopback address")

    auth_method = document.get("token_endpoint_auth_method", "none")
    if auth_method != "none":
        raise ClientMetadataError("only public clients (token_endpoint_auth_method none) are accepted")
    grant_types = document.get("grant_types")
    if grant_types is not None and (
        not isinstance(grant_types, list) or "authorization_code" not in grant_types
    ):
        raise ClientMetadataError("grant_types must include authorization_code")
    response_types = document.get("response_types")
    if response_types is not None and (
        not isinstance(response_types, list) or "code" not in response_types
    ):
        raise ClientMetadataError("response_types must include code")

    return {
        "client_name": _client_name(document),
        "client_uri": _optional_string(document, "client_uri", MAX_URI_LENGTH),
        "redirect_uris": list(redirect_uris),
    }


def document_host(client_id: str) -> str:
    """The host a document is served from: what proves who its client is."""
    return urlsplit(client_id).hostname or client_id


def _transient_client(client_id: str, fields: dict[str, Any]) -> OAuthClient:
    """The client a document describes, as the authorization code paths read it.

    ``client_name`` (required by :func:`validate_metadata_document`) is what
    the client says it is; the consent page shows it as a claim, after the
    host the document came from (:func:`document_host`).
    """
    return OAuthClient(
        client_id=client_id,
        client_secret_hash=None,
        client_name=fields["client_name"],
        client_uri=fields.get("client_uri"),
        redirect_uris=fields["redirect_uris"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",  # noqa: S106 - an OAuth method name, not a secret
    )


async def _cached(client_id: str) -> dict[str, Any] | None:
    try:
        raw = await (await redis_client()).get(_cache_key(client_id))
    except Exception as exc:
        logger.warning("Client metadata cache unreadable: %s", format_exception_message(exc))
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def _remember(client_id: str, entry: dict[str, Any], seconds: int) -> None:
    try:
        await (await redis_client()).set(_cache_key(client_id), json.dumps(entry), ex=seconds)
    except Exception as exc:
        logger.warning("Client metadata cache unwritable: %s", format_exception_message(exc))


async def _getaddrinfo(host: str, port: int) -> list[tuple[Any, ...]]:
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def _public_address(host: str, port: int) -> str:
    """The one address the fetch connects to: every address of *host* must be public."""
    from app.services.web_fetch import _is_safe_address

    try:
        infos = await _getaddrinfo(host, port)
    except OSError as exc:
        logger.info("Client metadata document host %r does not resolve: %s", host, format_exception_message(exc))
        raise ClientMetadataError(_UNREACHABLE, transient=True) from exc
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
        except ValueError:
            continue
    unsafe = [str(address) for address in addresses if not _is_safe_address(address)]
    if not addresses or unsafe:
        logger.info("Client metadata document host %r refused: it resolves to %s", host, unsafe or "nothing")
        raise ClientMetadataError(_UNREACHABLE)
    return str(addresses[0])


def _json_media_type(content_type: str | None) -> bool:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


async def _fetch(client_id: str) -> tuple[object, str | None]:
    """The parsed document at *client_id* and its ``Cache-Control``."""
    import httpx

    from app.integrations.http import PLATFORM_USER_AGENT

    parsed = urlsplit(client_id)
    host = parsed.hostname or ""
    port = parsed.port or 443
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            address = await _public_address(host, port)
            netloc = f"[{address}]" if ":" in address else address
            pinned = urlunsplit(("https", f"{netloc}:{port}", parsed.path, "", ""))
            headers = {
                "Host": parsed.netloc,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": PLATFORM_USER_AGENT,
            }
            async with (
                httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client,
                client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": host}) as response,
            ):
                if response.status_code != 200:
                    logger.info("Client metadata document URL %s answered HTTP %s", client_id, response.status_code)
                    raise ClientMetadataError(_UNREACHABLE, transient=response.status_code >= 500)
                if not _json_media_type(response.headers.get("content-type")):
                    raise ClientMetadataError("the document is not served as application/json")
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise ClientMetadataError("the document is served encoded")
                try:
                    declared = int(response.headers.get("content-length") or 0)
                except ValueError:
                    declared = 0
                if declared > MAX_DOCUMENT_BYTES:
                    raise ClientMetadataError(f"the document is larger than {MAX_DOCUMENT_BYTES} bytes")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > MAX_DOCUMENT_BYTES:
                        raise ClientMetadataError(f"the document is larger than {MAX_DOCUMENT_BYTES} bytes")
                cache_control = response.headers.get("cache-control")
    except TimeoutError as exc:
        raise ClientMetadataError(_UNREACHABLE, transient=True) from exc
    except httpx.HTTPError as exc:
        logger.info("Client metadata document URL %s unreachable: %s", client_id, format_exception_message(exc))
        raise ClientMetadataError(_UNREACHABLE, transient=True) from exc
    try:
        return json.loads(bytes(body)), cache_control
    except ValueError as exc:
        raise ClientMetadataError("the document is not JSON") from exc


async def cached_refusal(client_id: str) -> bool:
    """Whether the cache holds a refusal of *client_id*'s document (no fetch).

    Read by the token endpoint: a client whose document was refused - removed,
    changed, or its domain handed over - gets no new token from a refresh.
    """
    entry = await _cached(client_id)
    return entry is not None and "refused" in entry


async def _spend_fetch_budgets(client_id: str, request: Request) -> None:
    """The budgets one fetch spends, refused while Redis cannot count them."""
    await enforce_rate_limit(
        request, "mcp-oauth-cimd-fetch", limit=FETCHES_PER_MINUTE, window_seconds=60, fail_closed=True
    )
    await enforce_rate_limit(
        None,
        "mcp-oauth-cimd-fetch-host",
        limit=HOST_FETCHES_PER_MINUTE,
        window_seconds=60,
        bucket=document_host(client_id).lower(),
        fail_closed=True,
    )
    await enforce_rate_limit(
        None,
        "mcp-oauth-cimd-fetch-platform",
        limit=PLATFORM_FETCHES_PER_MINUTE,
        window_seconds=60,
        bucket="platform",
        fail_closed=True,
    )


async def resolve_client_metadata(client_id: str, *, request: Request) -> OAuthClient:
    """The client the document at *client_id* describes.

    Raises :class:`ClientMetadataError` (400) for a document this server does
    not accept, and the rate limiter's 429 / 503 when no fetch may be spent and
    no entry of the client is left to serve.
    """
    entry = await _cached(client_id)
    stale: dict[str, Any] | None = None
    if entry is not None:
        if "refused" in entry:
            logger.info("Refused the client metadata document at %r (cached): %s", client_id, entry["refused"])
            raise ClientMetadataError(str(entry["refused"]))
        if entry.get("fresh_until", float("inf")) > time.time():
            return _transient_client(client_id, entry)
        stale = entry

    try:
        await _spend_fetch_budgets(client_id, request)
    except HTTPException as exc:
        if stale is None:
            raise
        logger.info("Serving the cached client metadata of %r: no fetch budget (%s)", client_id, exc.status_code)
        return _transient_client(client_id, stale)
    try:
        document, cache_control = await _fetch(client_id)
        fields = validate_metadata_document(client_id, document)
    except ClientMetadataError as exc:
        if stale is not None and exc.transient:
            logger.info("Serving the cached client metadata of %r: %s", client_id, exc.reason)
            return _transient_client(client_id, stale)
        logger.info("Refused the client metadata document at %r: %s", client_id, exc.reason)
        await _remember(client_id, {"refused": exc.reason}, REFUSAL_CACHE_SECONDS)
        raise
    seconds = _cache_seconds(cache_control)
    await _remember(client_id, {**fields, "fresh_until": time.time() + seconds}, seconds + STALE_GRACE_SECONDS)
    return _transient_client(client_id, fields)
