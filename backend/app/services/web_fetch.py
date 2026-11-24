# SPDX-License-Identifier: Apache-2.0

"""Shared web fetching and HTML-to-text extraction utilities.

This module is the **single source of truth** for fetching web pages and
converting HTML to clean text.  It is used by:

- The ``web_fetch`` built-in tool (agents can read web page content)
- The KB crawl endpoint (``POST /knowledge-bases/{id}/documents/crawl``)
- The website sync service (``sync_website()`` in ``kb_sync.py``)

Two extraction strategies are supported:

1. **Trafilatura** (default): High-quality main-content extraction that
   strips navigation, ads, and boilerplate.  Best for articles and docs.
2. **BeautifulSoup fallback**: Simpler tag-stripping approach.  Used when
   trafilatura cannot extract content or when raw extraction is preferred.

Both ``httpx`` (async HTTP), ``trafilatura``, and ``beautifulsoup4`` are
already project dependencies — no new packages required.

SSRF policy
-----------

All outbound fetches performed by this module (and by the browser tool,
which reuses :func:`validate_outbound_url`) enforce a strict URL policy:

- Only ``http`` and ``https`` schemes are allowed.
- Every hostname is resolved via DNS and **all** resolved addresses must
  pass the public-only destination policy
  (``app.core.destination_policy.forbidden_address_reason``).  This rejects
  private ranges, loopback, link-local and cloud metadata endpoints, the
  shared address space ``100.64.0.0/10``, reserved, multicast and other
  non-globally-routable blocks.
- :func:`fetch_and_extract` connects to the address that was checked, never
  to the name again (``app.core.pinned_request.send_pinned``): the name goes
  in the ``Host`` header and as the TLS server name, so a short-TTL DNS
  answer cannot pass the check and then point the connection inward (DNS
  rebinding).  Its client ignores environment proxies (``trust_env=False``).
- Redirects are **never** auto-followed.  :func:`fetch_and_extract`
  follows at most :data:`MAX_REDIRECTS` hops manually; every hop is
  resolved, checked and pinned the same way before it is requested, so a
  public URL cannot 302 into an internal service.
- The connection itself is pinned to a checked address
  (:func:`guarded_transport`): the host is resolved once, every address is
  checked, and the socket connects to a checked address. A DNS answer that
  changes between the check and the connection (rebinding) therefore
  cannot redirect the request. When an HTTP proxy is configured in the
  environment, a request the proxy carries is checked before it is sent
  instead (the proxy makes the connection), while a host ``NO_PROXY``
  exempts is still connected to directly, pinned
  (:func:`guarded_client_options`).

A second, looser policy exists for callers whose job is to reach the
operator's own network — importing an OpenAPI document from an intranet
host, then calling the API it describes (``allow_private=True``).  It is the
operator-network policy of :mod:`app.core.destination_policy` (the one
classification the A2A guard and the OAuth token endpoint guard use too):
private-network ranges (RFC 1918, IPv6 ULA, the ``100.64.0.0/10`` shared
space) are admitted, loopback, link-local, multicast, unspecified and
reserved addresses are refused, and so are the cloud metadata endpoints
(``METADATA_ADDRESSES``) whatever range they sit in.  Loopback stays refused
because it is the platform itself: an import must never be able to read the
backend's own unauthenticated ports.  A refusal under this policy never names
the address a host resolved to: whoever types a URL must not learn how
internal names resolve.

:func:`fetch_url_bytes` is the raw-bytes sibling of
:func:`fetch_and_extract`: same redirect re-validation, plus a hard byte
ceiling enforced while streaming (so neither a lying ``Content-Length``
nor a decompression bomb can exceed it).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import ipaddress
import logging
import re
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpcore
import httpx

from app.core.destination_policy import IPAddress, forbidden_address_reason
from app.core.egress import current_route
from app.core.pinned_request import pinned_client, resolve_pinned_address, send_pinned
from app.core.pinned_transport import PolicyRefusal
from app.core.utils import format_exception_message
from app.integrations.http import PLATFORM_USER_AGENT

logger = logging.getLogger(__name__)

# ── Trafilatura log management ────────────────────────────────────────────────

# Task-local URL context injected into trafilatura log records by
# _TrafilaturaFilter.  Always holds the REDACTED form (see
# _redact_url_for_log) so a raw URL with credentials or query tokens can
# never reach a log record through this path.
_traf_current_url: contextvars.ContextVar[str | None] = contextvars.ContextVar("_traf_current_url", default=None)


def _redact_url_for_log(url: str | None) -> str | None:
    """Return a log-safe copy of *url*: scheme + host[:port] + path only.

    Strips userinfo (``user:pass@``), the query string (API keys, signed
    tokens), and the fragment before the URL is embedded in a log record —
    the same principle as the integrations HTTP layer, which logs only
    ``urlparse(url).path``.  The host and path are kept because they are
    what makes a fetch failure traceable.

    Never raises (this runs on logging paths); returns ``None`` when the
    URL is empty, cannot be parsed, is not an absolute http(s) URL, or has
    no hostname — anything else (``file:///…``, ``https:///path``, relative
    paths) would reconstruct into a misleading non-URL string and could
    leak non-web paths into logs.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        # Only absolute http(s) URLs with a hostname can be redacted into a
        # meaningful "scheme + host[:port] + path" form.  urlparse lowercases
        # the scheme, so no .lower() is needed.
        if parsed.scheme not in ("http", "https"):
            return None
        host = parsed.hostname or ""
        if not host:
            return None
        # ``urlparse`` strips the brackets from IPv6 literals — re-add them
        # so the reconstructed URL stays valid (``https://[2001:db8::1]/x``).
        if ":" in host:
            host = f"[{host}]"
        # ``parsed.port`` raises ValueError for malformed ports — caught below.
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        redacted = urlunparse((parsed.scheme, host, parsed.path, "", "", ""))
        return redacted or None
    except Exception:
        return None


# Matches http(s) URLs embedded in log message text.  Trafilatura logs the
# raw ``url=`` argument in its own messages (e.g. ``LOGGER.error("empty HTML
# tree: %s", url)`` in ``trafilatura.core``), so the rendered message can
# carry userinfo / query tokens even though the injected ``[url=…]`` suffix
# is already redacted.
_URL_IN_MSG_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _redact_urls_in_text(text: str) -> str:
    """Redact every http(s) URL embedded in *text* via :func:`_redact_url_for_log`.

    URLs that cannot be parsed at all are replaced with ``[redacted-url]``
    rather than kept, because an unparseable URL cannot be proven free of
    credentials.  Idempotent: redacting an already-redacted URL is a no-op.
    """
    return _URL_IN_MSG_RE.sub(lambda m: _redact_url_for_log(m.group(0)) or "[redacted-url]", text)


class _TrafilaturaFilter(logging.Filter):
    """Downgrade known trafilatura ERROR messages and inject caller URL context.

    Trafilatura emits ERROR-level log records for expected failure modes
    (JS-rendered pages, anti-bot responses, non-parseable HTML) that are not
    genuine platform errors.  This filter:

    1. Downgrades records whose message matches a known non-critical pattern
       from ERROR → WARNING so they do not pollute the platform error stream.
    2. Redacts URLs embedded in the message itself — trafilatura logs the raw
       ``url=`` argument in several of its own messages, which would otherwise
       leak userinfo / query tokens into the log stream.
    3. Appends the URL currently being processed (stored in the task-local
       *_traf_current_url* ContextVar by :func:`_extract_with_trafilatura`)
       so every log line is traceable to a specific fetch call.
    """

    # Trafilatura message substrings that indicate expected, non-actionable
    # failure modes (JS-rendered content, anti-bot pages, empty trees).
    _EXPECTED_PATTERNS: frozenset[str] = frozenset(
        [
            "empty HTML tree",
            "parsed tree length",
            "wrong data type",
        ]
    )

    def filter(self, record: logging.LogRecord) -> bool:
        url = _traf_current_url.get()
        # Render the original formatted message before we mutate args.
        try:
            msg = record.getMessage()
        except Exception:
            # Malformed record (msg/args mismatch). Filters run unprotected at
            # the logging call site — raising here would propagate into the
            # fetch path — so pass the record through untouched and let the
            # handler's own formatting error handling deal with it.
            return True
        if record.levelno >= logging.ERROR and any(p in msg for p in self._EXPECTED_PATTERNS):
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        # Redact raw URLs trafilatura embedded in the message itself (it logs
        # the ``url=`` argument verbatim), then append the caller context.
        # Both steps are idempotent, so a second pass over the same record —
        # from a duplicated filter registration or a handler-level copy — is
        # a no-op: redacting a redacted URL changes nothing and the ``[url=``
        # guard prevents a second suffix.
        clean = _redact_urls_in_text(msg)
        if url and "[url=" not in clean:
            clean = f"{clean} [url={url}]"
        if clean != msg or record.args:
            # Clear args so handlers don't re-format the rendered message.
            record.msg = clean
            record.args = ()
        return True


# Module-level singleton (NOT lazily created): two concurrent first calls
# racing a lazy ``if _traf_filter is None`` could each create an instance,
# and ``Logger.addFilter`` only dedupes the SAME instance — the loser's
# copy would run too and stamp ``[url=…]`` twice on every record.
_traf_filter = _TrafilaturaFilter()

# Logger names carrying the known noisy emitters: ``trafilatura.core``
# logs "empty HTML tree", ``trafilatura.utils`` logs "parsed tree
# length … wrong data type"; the parent covers records logged on the
# package logger itself.  Filters do NOT propagate to child loggers
# (unlike handlers), so each emitter must be listed explicitly.
_TRAF_LOGGER_NAMES = ("trafilatura", "trafilatura.core", "trafilatura.utils")


def _install_trafilatura_filter() -> None:
    """Install the URL-injection / level-downgrade filter on trafilatura's loggers.

    Idempotent: the module-level singleton :class:`_TrafilaturaFilter` is
    registered at most once per logger (``Logger.addFilter`` skips duplicate
    instances), so repeated calls from concurrent tasks are safe.
    """
    for name in _TRAF_LOGGER_NAMES:
        logging.getLogger(name).addFilter(_traf_filter)


# ── Constants ────────────────────────────────────────────────────────────────

USER_AGENT = PLATFORM_USER_AGENT
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_CHARS = 80_000
MAX_REDIRECTS = 5

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class DestinationRefusedError(ValueError):
    """A URL the outbound policy refuses, or whose host does not resolve."""


class DestinationUnresolvedError(DestinationRefusedError):
    """A URL whose host does not resolve (so nothing could be judged or reached)."""


@dataclass
class FetchResult:
    """The result of fetching and extracting a web page.

    Attributes:
        url: The final URL after any redirects.
        title: Page title (from ``<title>`` tag), or *None*.
        text: Extracted plain-text content.
        content_type: The ``Content-Type`` header from the response.
        status_code: HTTP status code.
        char_count: Length of *text*.
        truncated: Whether *text* was truncated to fit *max_chars*.
        body: The response body as received - what the ``web_fetch`` tool
            keeps as a file of the conversation.
        headers: The final response's headers, names lowercased - what a
            Deep Investigation site fingerprint reads besides the body
            (``server: LiteSpeed``, a hosting panel's own header).
        body_capped: Whether reading stopped at ``max_bytes`` - the body
            then holds the first ``max_bytes`` bytes of a longer response.
    """

    url: str
    title: str | None
    text: str
    content_type: str
    status_code: int
    char_count: int
    truncated: bool = False
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    body_capped: bool = False


@dataclass(frozen=True)
class FetchedBytes:
    """A raw response body fetched by :func:`fetch_url_bytes`.

    Attributes:
        url: The final URL after any redirects.
        content: The (content-decoded) body, never longer than the ceiling.
        content_type: The ``Content-Type`` header from the response.
        status_code: HTTP status code.
    """

    url: str
    content: bytes
    content_type: str
    status_code: int


# ── Public API ───────────────────────────────────────────────────────────────


async def _get_following_validated_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    allow_private: bool,
    stream: bool,
) -> httpx.Response:
    """GET *url*, following at most :data:`MAX_REDIRECTS` re-validated hops.

    The single redirect loop of this module: every ``Location`` is validated
    against the SSRF policy BEFORE the next request is issued, so a permitted
    URL cannot 302 into a refused one. With *stream* the returned response is
    open (the caller reads and closes it); intermediate redirect responses are
    always closed here.
    """

    async def _send(target: str) -> httpx.Response:
        if stream:
            return await client.send(client.build_request("GET", target), stream=True)
        return await client.get(target)

    safe_url = await validate_outbound_url_async(url, allow_private=allow_private)
    resp = await _send(safe_url)
    hops = 0
    while resp.status_code in _REDIRECT_STATUSES and "location" in resp.headers:
        next_url = urljoin(str(resp.request.url), resp.headers["location"])
        if stream:
            await resp.aclose()
        hops += 1
        if hops > MAX_REDIRECTS:
            raise ValueError(f"Too many redirects (more than {MAX_REDIRECTS}) while fetching: {url}")
        safe_url = await validate_outbound_url_async(next_url, allow_private=allow_private)
        resp = await _send(safe_url)
    return resp


async def fetch_url_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: int = DEFAULT_TIMEOUT,
    allow_private: bool = False,
    accept: str | None = None,
) -> FetchedBytes:
    """Fetch *url*'s raw body under the module SSRF policy, bounded to *max_bytes*.

    The ceiling is enforced on the decoded stream, chunk by chunk: a declared
    ``Content-Length`` above it is refused before reading, and a body that
    grows past it (no or a lying ``Content-Length``, or a compressed payload
    that inflates) is abandoned as soon as it crosses it.

    Raises:
        ValueError: The URL or a redirect target is refused by the policy,
            the redirect chain is too long, or the body exceeds *max_bytes*.
        httpx.HTTPStatusError: The final response is a 4xx/5xx.
        httpx.HTTPError: Connection failure or timeout.
    """
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        headers=headers,
        **guarded_client_options(allow_private=allow_private),
    ) as client:
        resp = await _get_following_validated_redirects(client, url, allow_private=allow_private, stream=True)
        try:
            resp.raise_for_status()
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise ValueError(_too_large_message(max_bytes))
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(_too_large_message(max_bytes))
                chunks.append(chunk)
        finally:
            await resp.aclose()
    return FetchedBytes(
        url=str(resp.url),
        content=b"".join(chunks),
        content_type=resp.headers.get("content-type", ""),
        status_code=resp.status_code,
    )


def _too_large_message(max_bytes: int) -> str:
    if max_bytes >= 1024 * 1024 and max_bytes % (1024 * 1024) == 0:
        limit = f"{max_bytes // (1024 * 1024)} MB"
    else:
        limit = f"{max_bytes} bytes"
    return f"The response exceeds the maximum size of {limit}."


async def fetch_and_extract(
    url: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout: int = DEFAULT_TIMEOUT,
    use_trafilatura: bool = True,
    max_bytes: int | None = None,
) -> FetchResult:
    """Fetch a URL and extract its text content.

    This is the main entry point used by the built-in ``web_fetch`` tool
    and the KB crawl/sync pipeline.

    Args:
        url: The URL to fetch.
        max_chars: Maximum characters to return (default 80 000).
            Content exceeding this is truncated with a notice.
        timeout: HTTP request timeout in seconds (default 30).
        use_trafilatura: When True (default), use trafilatura for
            high-quality content extraction.  Falls back to
            BeautifulSoup if trafilatura returns nothing.
        max_bytes: When set, the body is streamed uncompressed and reading
            stops once it holds this many bytes (``body_capped``), so the
            size of a hostile server's response never decides how much a
            fetch holds in memory. Redirect bodies are not read at all.

    Returns:
        A :class:`FetchResult` with the extracted text and metadata.

    Raises:
        ValueError: If the URL (or any redirect target) uses a forbidden
            scheme, does not resolve, resolves to an address the policy
            refuses, or the redirect chain exceeds :data:`MAX_REDIRECTS` hops.
        httpx.HTTPStatusError: If the server returns a 4xx/5xx response.
        httpx.ConnectError: If the connection fails.
        httpx.TimeoutException: If the request times out.
    """
    # Every request - the first and each redirect hop - is resolved once,
    # checked and sent to the checked address (`send_pinned`). The relay
    # route that `validate_outbound_url` defers to does not apply: this
    # client never goes through a relay, so the check is never skipped.
    # Redirects are followed manually so that every hop is checked before it
    # is requested; their bodies are never read. The client is HTTP/1.1 only,
    # keeps no connection and ignores environment proxies (`pinned_client`).
    async with pinned_client(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        resp = await _send(client, url, max_bytes)
        try:
            hops = 0
            while resp.status_code in _REDIRECT_STATUSES and "location" in resp.headers:
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise ValueError(f"Too many redirects (more than {MAX_REDIRECTS}) while fetching: {url}")
                next_url = urljoin(str(resp.url), resp.headers["location"])
                await resp.aclose()
                resp = await _send(client, next_url, max_bytes)
            if max_bytes is None:
                # Read before judging the status: an `HTTPStatusError` carries
                # a response whose body can be read, as it did before streaming.
                await resp.aread()
                resp.raise_for_status()
                content, body_capped, page_text = resp.content, False, resp.text
            else:
                # Under a cap an error answer is not read at all: the cap
                # holds whatever the status.
                resp.raise_for_status()
                content, body_capped = await _read_capped(resp, max_bytes)
                page_text = content.decode(resp.encoding or "utf-8", errors="replace")
        finally:
            await resp.aclose()

    final_url = str(resp.url)
    content_type = resp.headers.get("content-type", "")

    # Non-HTML content — return raw text (capped)
    if not is_html_media_type(content_type):
        raw = page_text[:max_chars]
        return FetchResult(
            url=final_url,
            title=None,
            text=raw,
            content_type=content_type,
            status_code=resp.status_code,
            char_count=len(raw),
            truncated=len(page_text) > max_chars,
            body=content,
            headers=_response_headers(resp),
            body_capped=body_capped,
        )

    html = page_text

    # Extraction (trafilatura / BeautifulSoup) is CPU-bound and can take
    # seconds on multi-MB pages — run it on the dedicated parse executor so
    # it never blocks the event loop.
    from app.core.parse_offload import run_parse

    text, title = await run_parse(lambda: extract_from_html(html, url=final_url, use_trafilatura=use_trafilatura))

    # Truncate if needed
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[Content truncated — exceeded max length]"
        truncated = True

    return FetchResult(
        url=final_url,
        title=title,
        text=text,
        content_type=content_type,
        status_code=resp.status_code,
        char_count=len(text),
        truncated=truncated,
        body=content,
        headers=_response_headers(resp),
        body_capped=body_capped,
    )


async def _send(client: httpx.AsyncClient, url: str, max_bytes: int | None) -> httpx.Response:
    """One pinned GET (`send_pinned`), streamed; asked uncompressed under a byte cap."""
    headers = {"Accept-Encoding": "identity"} if max_bytes is not None else None
    return await send_pinned(client, "GET", url, headers=headers, stream=True)


async def _read_capped(resp: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """The first ``max_bytes`` bytes of a streamed body, and whether there was more.

    Decoded bytes are counted, so a server that compresses anyway cannot
    inflate past the cap either.
    """
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        room = max_bytes - len(buf)
        if len(chunk) > room:
            buf += chunk[:room]
            return bytes(buf), True
        buf += chunk
    return bytes(buf), False


def _response_headers(resp: httpx.Response) -> dict[str, str]:
    """The response headers with lowercased names; repeated ones joined."""
    headers: dict[str, str] = {}
    for name, value in resp.headers.multi_items():
        key = name.lower()
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    return headers


def is_html_media_type(content_type: str | None) -> bool:
    """Whether a ``Content-Type`` names an HTML page - the one test a response is extracted as a page by.

    The media type is read as HTTP defines it (before any parameter,
    case-insensitive): ``Text/HTML; charset=UTF-8`` is a page, and a header
    that merely contains ``text/html`` among other words is not.
    """
    return (content_type or "").split(";", 1)[0].strip().lower() == "text/html"


def extract_from_html(
    html: str,
    *,
    url: str | None = None,
    use_trafilatura: bool = True,
) -> tuple[str, str | None]:
    """Extract text and title from an HTML string (no HTTP fetch).

    Useful when you already have the HTML (e.g. from a prior request).

    Returns:
        ``(text, title)`` tuple.
    """
    title = _extract_title(html)
    text = ""

    if use_trafilatura:
        text = _extract_with_trafilatura(html, url=url)

    if not text:
        text = _extract_with_beautifulsoup(html)

    return text, title


#: The elements a sanitised page drops whole (``sanitize_html``): what runs, styles or fetches
#: something, never the text a fixture is kept for.
SANITIZE_DROPPED_ELEMENTS = (
    "script",
    "style",
    "noscript",
    "svg",
    "img",
    "picture",
    "video",
    "audio",
    "source",
    "track",
    "iframe",
    "frame",
    "object",
    "embed",
    "canvas",
    "link",
    "template",
)


def sanitize_html(body: bytes) -> bytes:
    """*body*, an HTML page, without its active and media elements, as UTF-8.

    Drops every element of :data:`SANITIZE_DROPPED_ELEMENTS` with its content and every
    ``on*`` event attribute, so a page fetched to the workspace is a test fixture as it is
    (``web_fetch`` ``sanitize``). The text, the headings and the structure stay.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body, "html.parser")
    for element in soup.find_all(SANITIZE_DROPPED_ELEMENTS):
        element.decompose()
    for element in soup.find_all(True):
        for name in [attribute for attribute in element.attrs if attribute.lower().startswith("on")]:
            del element.attrs[name]
    return soup.decode().encode("utf-8")


def validate_outbound_url(url: str, *, allow_private: bool = False) -> str:
    """Validate a URL against the module SSRF policy and return a sanitized copy.

    This is the single shared validator for all agent-driven outbound
    requests (``web_fetch`` tool, KB crawl/sync, and the browser tool).

    Rejects non-http(s) schemes and hostnames where **any** DNS-resolved
    address fails the public-only destination policy
    (``destination_policy.forbidden_address_reason``): private ranges,
    loopback, link-local/cloud metadata, shared address space
    ``100.64.0.0/10``, reserved ranges, etc.  Returns a reconstructed URL
    from parsed components so callers can use the return value directly.
    The refusal does not name the address (it is logged): the message is
    never an oracle on where an internal name points.

    ``allow_private=True`` selects the operator-network policy described in
    the module docstring: private ranges are admitted, loopback, link-local,
    multicast, reserved and cloud-metadata addresses are still refused.

    Raises:
        DestinationRefusedError: A :class:`ValueError` with a readable message
            when the URL is not allowed.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise DestinationRefusedError(f"URL is not valid: {exc}") from None
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise DestinationRefusedError(f"URL scheme must be http or https, got: {scheme or 'empty'}")

    if not hostname:
        raise DestinationRefusedError("URL must have a hostname")

    if current_route() is not None:
        # Routed through a relay group: the destination is meant to be a
        # private address, and the relay checks it against its own list.
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL must not carry credentials")
        return urlunparse((scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

    if not allow_private:
        # The same resolution and policy `fetch_and_extract` pins its
        # requests to. A caller that connects by name afterwards (the
        # browser) still resolves again; `fetch_and_extract` does not.
        resolve_pinned_address(hostname)
    else:
        try:
            infos = socket.getaddrinfo(hostname, None, family=0, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as e:
            raise DestinationUnresolvedError(f"Cannot resolve hostname: {hostname}") from e

        resolved = False
        for info in infos:
            sockaddr = info[4]
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(str(ip_str).split("%", 1)[0])
            except ValueError:
                continue
            resolved = True
            if not _is_safe_address(ip, allow_private=True):
                raise DestinationRefusedError(
                    f"URL host {hostname} is a loopback, link-local, reserved or "
                    "cloud-metadata address, which this platform does not call"
                )
        if not resolved:
            raise DestinationUnresolvedError(f"Hostname did not resolve to any IP address: {hostname}")

    return urlunparse(
        (
            scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


async def validate_outbound_url_async(url: str, *, allow_private: bool = False) -> str:
    """Async variant of :func:`validate_outbound_url`.

    The SSRF check resolves the hostname with a *blocking*
    ``socket.getaddrinfo`` call; a slow or unresponsive resolver would
    stall the whole event loop for its duration. Async
    callers (the browser tool, file delivery, image assets) must use this
    variant, which delegates the resolution to a worker thread.
    """
    return await asyncio.to_thread(functools.partial(validate_outbound_url, url, allow_private=allow_private))


# ── Connection pinning ───────────────────────────────────────────────────────


async def _admitted_addresses(host: str, port: int, *, allow_private: bool) -> list[str]:
    """Resolve *host* once and return its addresses, all checked against the policy."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise httpcore.ConnectError(f"Cannot resolve hostname: {host}") from exc
    addresses: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if not _is_safe_address(ip, allow_private=allow_private):
            raise PolicyRefusal(f"Connection refused: {host} resolves to an address this platform does not call.")
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise httpcore.ConnectError(f"Hostname did not resolve to any IP address: {host}")
    return addresses


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _host_and_port(url: httpx.URL) -> tuple[str, int | None]:
    """*url*'s host (its ASCII form, as httpcore connects to it) and port, the default spelled out."""
    return url.raw_host.decode("ascii").lower(), url.port or _DEFAULT_PORTS.get(url.scheme)


def _private_hosts(private_origins: Iterable[str]) -> frozenset[tuple[str, int | None]]:
    """The ``(host, port)`` pairs of *private_origins*; an origin httpx cannot
    parse, or that is not an http(s) origin with a host, is left out."""
    hosts: set[tuple[str, int | None]] = set()
    for origin in private_origins:
        try:
            url = httpx.URL(origin)
            if url.scheme in _DEFAULT_PORTS and url.raw_host:
                hosts.add(_host_and_port(url))
        except (httpx.InvalidURL, UnicodeError):
            continue
    return frozenset(hosts)


class _PolicyNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolves, checks and connects in one step.

    httpcore hands the backend the request's hostname; the backend connects
    to one of its checked addresses instead. TLS is started afterwards with
    the hostname as server name, so certificates are still verified for it.
    A host and port of *private_hosts* is judged by the operator-network
    policy even when the backend is public-only.
    """

    def __init__(
        self, *, allow_private: bool, private_hosts: frozenset[tuple[str, int | None]] = frozenset()
    ) -> None:
        self._allow_private = allow_private
        self._private_hosts = private_hosts
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        allow_private = self._allow_private or (host.lower(), port) in self._private_hosts
        addresses = await _admitted_addresses(host, port, allow_private=allow_private)
        error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        raise error or httpcore.ConnectError(f"Could not connect to {host}")

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: object = None
    ) -> httpcore.AsyncNetworkStream:
        raise PolicyRefusal("Outbound requests cannot use a Unix socket.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """An ``httpx`` transport whose connections go only to checked addresses."""

    def __init__(
        self,
        *,
        allow_private: bool,
        verify: ssl.SSLContext | bool = True,
        limits: httpx.Limits | None = None,
        private_origins: Iterable[str] = (),
    ) -> None:
        limits = limits or httpx.Limits()
        # Certificate authorities as httpx reads them (SSL_CERT_FILE / DIR).
        ssl_context = httpx.create_ssl_context(verify=verify)
        super().__init__(verify=ssl_context, limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=_PolicyNetworkBackend(
                allow_private=allow_private, private_hosts=_private_hosts(private_origins)
            ),
        )


def _environment_proxies() -> dict[str, str | None]:
    """The environment's proxy mounts as httpx reads them (``{}`` without a proxy).

    ``URL pattern -> proxy URL``, and ``None`` for a pattern ``NO_PROXY``
    exempts. httpx's own reading, so the proxy a request goes through here is
    the one it would go through with the default transport.
    """
    from httpx._utils import get_environment_proxies

    return get_environment_proxies()


def guarded_transport(
    *,
    allow_private: bool,
    verify: ssl.SSLContext | bool = True,
    limits: httpx.Limits | None = None,
    private_origins: Iterable[str] = (),
) -> GuardedTransport:
    """The pinned transport for the requests a client connects to directly."""
    return GuardedTransport(
        allow_private=allow_private, verify=verify, limits=limits, private_origins=private_origins
    )


def _destination_check(
    *, allow_private: bool, private_origins: Iterable[str] = ()
) -> Callable[[httpx.Request], Awaitable[None]]:
    """A request hook that refuses, before sending, a URL the policy refuses.

    It raises what the pinned transport raises (``httpx.ConnectError``), so
    callers handle a refusal the same way with or without a proxy.
    """
    private_hosts = _private_hosts(private_origins)

    async def check(request: httpx.Request) -> None:
        operator = allow_private or _host_and_port(request.url) in private_hosts
        try:
            await validate_outbound_url_async(str(request.url), allow_private=operator)
        except ValueError as exc:
            raise httpx.ConnectError(str(exc), request=request) from None

    return check


def guarded_client_options(
    *,
    allow_private: bool,
    verify: ssl.SSLContext | bool = True,
    limits: httpx.Limits | None = None,
    private_origins: Iterable[str] = (),
    event_hooks: Mapping[str, Iterable[Callable[..., Any]]] | None = None,
) -> dict[str, object]:
    """``httpx.AsyncClient`` keyword arguments that guard every destination.

    Every connection the client makes itself is pinned to a checked address
    (see above). Behind an environment proxy, the requests the proxy carries
    go through it (the proxy makes the connection), and each request's URL -
    every redirect hop included - is checked before it is sent; a host
    ``NO_PROXY`` exempts is connected to directly, and so still pinned. A
    custom *verify* (a client certificate's context) is presented through
    the proxy too.

    *private_origins* (``scheme://host[:port]``) are origins an operator
    configured, such as a self-hosted server on the private network: a
    request to one of them is held to the operator-network policy (private
    ranges admitted; loopback, link-local and metadata still refused) even
    when *allow_private* is False. The caller's own *event_hooks* go in the
    returned ``event_hooks``, ahead of the check above.
    """
    private_origins = tuple(private_origins)
    transport = guarded_transport(
        allow_private=allow_private, verify=verify, limits=limits, private_origins=private_origins
    )
    options: dict[str, object] = {"transport": transport}
    hooks = {name: list(callbacks) for name, callbacks in (event_hooks or {}).items()}
    proxies = _environment_proxies()
    if proxies:
        # A client given a transport ignores the environment's proxies: they
        # are mounted here, and the patterns NO_PROXY exempts (``None``) fall
        # back to the pinned transport.
        options["mounts"] = {
            pattern: None
            if proxy is None
            else httpx.AsyncHTTPTransport(proxy=proxy, verify=verify, limits=limits or httpx.Limits())
            for pattern, proxy in proxies.items()
        }
        hooks.setdefault("request", []).append(
            _destination_check(allow_private=allow_private, private_origins=private_origins)
        )
    if hooks:
        options["event_hooks"] = hooks
    return options


# ── Private helpers ──────────────────────────────────────────────────────────


def _is_safe_address(ip: IPAddress, *, allow_private: bool = False) -> bool:
    """Whether *ip* passes the destination policy.

    Public-only by default (``destination_policy.forbidden_address_reason``);
    with *allow_private*, the operator-network policy described in the module
    docstring. IPv4-mapped and NAT64 (``64:ff9b::/96``) addresses are judged
    by the IPv4 address they carry, so ``64:ff9b::7f00:1`` - which a NAT64
    gateway delivers to ``127.0.0.1`` - is refused under both.
    """
    return forbidden_address_reason(ip, allow_private=allow_private) is None


def _extract_title(html: str) -> str | None:
    """Extract the page title from HTML using a lightweight regex."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip() or None
    return None


def _extract_with_trafilatura(html: str, *, url: str | None = None) -> str:
    """Extract main content using trafilatura.

    Trafilatura strips navigation, ads, sidebars, and boilerplate to
    isolate the main article/document content.  Returns an empty string
    if extraction fails.

    Trafilatura's internal loggers emit ERROR-level messages for expected
    failure modes (JS-rendered pages, anti-bot responses, empty trees).
    :func:`_install_trafilatura_filter` downgrades those to WARNING and
    injects the caller URL via *_traf_current_url* so failures are
    traceable to a specific fetch call without polluting the error stream.
    """
    try:
        import trafilatura

        _install_trafilatura_filter()
        # Store the redacted form so log records can never carry userinfo
        # or query tokens (the raw url still goes to trafilatura itself,
        # which uses it for date/metadata heuristics, not logging).
        _tok = _traf_current_url.set(_redact_url_for_log(url))
        try:
            text = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                output_format="txt",
                url=url,
            )
        finally:
            _traf_current_url.reset(_tok)

        return (text or "").strip()
    except Exception as exc:
        safe_url = _redact_url_for_log(url)
        url_info = f" [url={safe_url}]" if safe_url else ""
        logger.warning("Trafilatura extraction failed%s: %s", url_info, format_exception_message(exc))
        return ""


def _extract_with_beautifulsoup(html: str) -> str:
    """Extract text from HTML by stripping non-content tags.

    Simpler than trafilatura but works on more page structures.
    Removes script, style, nav, footer, and header elements.
    """
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # Collapse excessive blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception as exc:
        logger.warning("BeautifulSoup extraction failed: %s", format_exception_message(exc))
        return ""
