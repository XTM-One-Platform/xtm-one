# SPDX-License-Identifier: Apache-2.0

"""Shared utility functions used across the application.

Provides text-processing and parsing helpers that are used by multiple
modules (API routes, services, migrations).  Keep imports at module level to
stdlib so this can be imported anywhere without circular issues; the few
helpers that need ``settings`` import it inside the function body.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
import unicodedata
import uuid as _uuid
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache
from itertools import islice, pairwise
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# HTTP Content-Disposition helper (safe against header injection)
# ---------------------------------------------------------------------------


def build_content_disposition(filename: str) -> str:
    """Build a safe ``attachment; filename=…`` Content-Disposition header.

    Filenames travelling through this codebase can originate from
    user uploads (knowledge-base documents, chat attachments) or from
    AI-generated output (built-in ``generate_file`` tool, custom-tool
    ``$output_files``). All of those paths are *attacker-influenceable*,
    so the raw filename must NEVER be interpolated directly into the
    header — that opens a CRLF header-injection vector and breaks
    quoting on names containing ``"`` or non-ASCII characters.

    This helper:

    * **Strips CR / LF** so a filename of
      ``foo.csv\r\nSet-Cookie: …`` can't smuggle additional headers.
    * Emits an **ASCII-safe quoted form** (``filename="…"``) for
      legacy clients (RFC 6266 §4.1 fallback), with backslashes and
      double-quotes escaped per RFC 6266 / RFC 7230.
    * Adds an **RFC 5987 ``filename*=UTF-8''…`` variant** so modern
      browsers receive the original Unicode filename via a
      percent-encoded UTF-8 form.

    Use everywhere a ``Content-Disposition`` header is built from a
    persisted filename: KB document downloads, chat file downloads,
    message-scoped chat downloads, etc.
    """
    safe_filename = filename.replace("\r", " ").replace("\n", " ")
    ascii_filename = safe_filename.encode("ascii", "replace").decode("ascii")
    ascii_filename = ascii_filename.replace("\\", "\\\\").replace('"', '\\"')
    utf8_filename = quote(safe_filename, safe="")
    return f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{utf8_filename}'


# ---------------------------------------------------------------------------
# HTTP conditional requests (If-None-Match)
# ---------------------------------------------------------------------------

#: One entity tag of an ``If-None-Match`` list: ``"opaque"`` or ``W/"opaque"``.
_ENTITY_TAG_RE = re.compile(r'(?:W/)?"([^"]*)"')


def if_none_match_hits(header: str | None, etag: str) -> bool:
    """Whether an ``If-None-Match`` header names the representation tagged *etag*.

    RFC 9110 section 13.1.2: ``*`` matches any current representation, and a
    list matches with the WEAK comparison (a ``W/`` prefix on either side is
    ignored), so a cache that downgraded the tag still revalidates. An absent
    or blank header, or one holding no entity tag, never matches.
    """
    if not header:
        return False
    if header.strip() == "*":
        return True
    wanted = _ENTITY_TAG_RE.fullmatch(etag.strip())
    if wanted is None:
        return False
    return wanted.group(1) in _ENTITY_TAG_RE.findall(header)


def external_base(request) -> str:
    """``scheme://host[/base-path]`` this deployment is reached at, for absolute URLs.

    Derived from the request itself - ``request.base_url`` already carries
    the URL path prefix the platform is served under (``BASE_PATH``),
    because the base-path middleware sets ``root_path`` on every scope.
    Behind a TLS-terminating proxy the scheme
    is taken from ``X-Forwarded-Proto`` — but only when the operator opted into
    ``trust_proxy_headers``, mirroring the platform's existing trust model.

    A deployment that terminates TLS *without* trusting proxy headers would
    otherwise emit ``http://`` URLs for a site reached over HTTPS — a broken
    link at best (Chromium silently cancels the mixed-content download)
    and a self-contradiction at worst (the A2A card handing every partner a
    plaintext endpoint). So a request-derived base that is
    still plain HTTP is upgraded to the operator's canonical ``BASE_URL`` when
    that is HTTPS for the SAME hostname. Never the reverse (an HTTPS request
    is never downgraded), and never across hostnames — a split-horizon
    deployment reached under another name keeps the base the caller actually
    used.

    Use this anywhere an absolute self-URL is handed to an external consumer:
    runner manifests, A2A agent cards and card URLs.
    """
    from app.core.config import settings
    from app.core.security_options import security_option

    base = str(request.base_url).rstrip("/")
    if security_option("trust_proxy_headers"):
        proto = forwarded_proto_claim(request)
        if proto in ("http", "https") and "://" in base:
            base = proto + base[base.index("://") :]
    if base.startswith("http://"):
        try:
            configured = urlsplit(settings.base_url or "")
            request_parts = urlsplit(base)
        except ValueError:
            # urlsplit refuses unbalanced IPv6 brackets. A typoed BASE_URL (or
            # a hostile Host header reaching the raw request) must degrade to
            # the request-derived base - these endpoints are unauthenticated,
            # so raising here would 500 the response for every caller.
            return base
        request_host = request_parts.hostname
        if (
            configured.scheme == "https"
            and configured.hostname
            and request_host
            and request_host.lower() == configured.hostname.lower()
        ):
            # Only the scheme (and the canonical netloc) is taken from the
            # configured URL; the path prefix stays the request's own.
            return f"https://{configured.netloc}{request_parts.path}"
    return base


def forwarded_scheme(forwarded_proto: str | None) -> str | None:
    """Leftmost scheme of an X-Forwarded-Proto chain, lowercased, else None.

    The parser behind :func:`forwarded_proto_claim`, which is how a request's
    header is read; its callers decide whether to trust it and what an
    uninterpretable value means.
    """
    if not forwarded_proto:
        return None
    return forwarded_proto.split(",")[0].strip().lower() or None


def forwarded_proto_claim(request) -> str | None:
    """What ``X-Forwarded-Proto`` claims: ``"https"``, ``"http"``, ``"other"``, or ``None``.

    The one reading of the header; ``None`` means the request makes no claim.
    Whether to BELIEVE the claim is the caller's trust decision - uvicorn
    applies it itself for a peer ``FORWARDED_ALLOW_IPS`` names, the platform
    only under ``trust_proxy_headers``: :func:`external_base` takes ``http`` /
    ``https`` from it, :func:`is_secure_transport` treats any claim as
    authoritative, and the unconfirmed-transport report names it without
    believing it. ``"other"`` stands for anything else, never the raw text.
    """
    raw = request.headers.get("x-forwarded-proto", "")
    if not raw:
        return None
    scheme = forwarded_scheme(raw)
    return scheme if scheme in ("http", "https") else "other"


def is_secure_transport(request, *, allow_test_peers: bool = False) -> bool:
    """Whether this request arrived over a connection we can confirm encrypted.

    Read from the forwarded protocol when the deployment says its proxy
    rewrites the header, and from the request's own scheme otherwise. Trusting
    ``X-Forwarded-Proto`` unconditionally would let any client assert the
    connection was encrypted simply by sending the header.

    ``allow_test_peers`` admits ``test_only_insecure_transport_peers``. Only
    the refusals pass it - the A2A surface (``require_secure_transport``) and,
    under ``credential_transport_strict``, runner enrollment and the MCP OAuth
    registration and token endpoints (``refuses_credential_transport``)
    - since the end-to-end suite reaches them from the Docker bridge network. A caller
    that only reports a transport must not pass it: a setting that exists for
    the tests must never silence a report.
    """
    from app.core.config import settings
    from app.core.security_options import security_option

    if security_option("trust_proxy_headers"):
        # Any claim, not only a parsable one: a claim that parses to nothing
        # (whitespace, a bare comma) is still a claim, and an uninterpretable
        # one must read as "not confirmed encrypted".
        claim = forwarded_proto_claim(request)
        if claim is not None:
            return claim == "https"
    if (request.url.scheme or "").lower() == "https":
        return True
    # No address stands in for the scheme unless the deployment says so: a
    # proxy on the same host makes every request arrive from 127.0.0.1.
    allow_loopback = security_option("a2a_allow_insecure_loopback")
    loopback = settings.debug or allow_loopback
    # The test peers extend the end-to-end stack's own loopback exemption and
    # count only alongside it (the settings refuse them without its variable).
    peers = settings.test_only_insecure_transport_peers if allow_test_peers and allow_loopback else []
    if not (loopback or peers):
        return False
    # An address exemption reads the real socket peer only.
    if client_address_is_forwarded(request):
        return False
    host = request_client_ip(request) or ""
    if loopback and host in ("127.0.0.1", "::1"):
        return True
    return bool(peers) and _peer_in_networks(host, tuple(peers))


@lru_cache(maxsize=8)
def _parsed_networks(networks: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    # Entries are canonical networks (the settings validator normalises them),
    # so the strict parse cannot raise.
    return tuple(ipaddress.ip_network(net) for net in networks)


def _peer_in_networks(host: str, networks: tuple[str, ...]) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in net for net in _parsed_networks(networks))


_ADDRESS_PORT = re.compile(r"\d{1,5}", re.ASCII)

#: Length of the audit trail's ``ip_address`` column (``AuditLog``, pinned by
#: a test): a longer client address fails the audited write.
CLIENT_ADDRESS_MAX_LENGTH = 45

#: What a proxy writes in ``X-Forwarded-For`` for a client it hides (Squid's
#: ``forwarded_for off``, RFC 7239's ``unknown``): not an address, not forged.
_HIDDEN_CLIENT = "unknown"

#: A zone index as an interface name or number spells it (RFC 6874's ZoneID).
#: Python accepts any other character in one, a newline or markup included.
_ZONE_ID = re.compile(r"[A-Za-z0-9._~-]+", re.ASCII)

_forged_address_reported = False


def canonical_ip(value: str | None) -> str | None:
    """``value`` as a canonical IP address, else ``None``.

    Accepts the forms uvicorn's own proxy parser does - a bare address,
    ``IPv4:port``, ``[IPv6]``, ``[IPv6]:port`` - and drops the port and, once
    the whole address has validated, its IPv6 zone index (meaningless for a
    remote client, and unbounded in length); an IPv4-mapped IPv6 address is
    read as its IPv4 address, and the result is compressed and lowercase, so
    one client is one string. Anything else (``unknown``, a hostname, free
    text, a malformed zone) is ``None``. :func:`client_address` decides which
    form a reader gets.
    """
    if not value:
        return None
    host = value.strip()
    if host.startswith("["):
        host, closed, port = host[1:].partition("]")
        if not closed:
            return None
        if port and not (port.startswith(":") and _ADDRESS_PORT.fullmatch(port[1:])):
            return None
    elif host.count(":") == 1:
        # A bare IPv6 address has at least two colons.
        host, _, port = host.partition(":")
        if not _ADDRESS_PORT.fullmatch(port):
            return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.version == 6:
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address.scope_id:
            address = ipaddress.IPv6Address(int(address))
    return str(address)


def client_address(value: str | None) -> str | None:
    """A client address as a reader keys, logs and stores it, else ``None``.

    Every client address goes through this: :func:`request_client_ip` for the
    forwarded hop and the peer, the access log for its two fields. An address
    in any form :func:`canonical_ip` accepts comes back as it was read,
    whitespace trimmed - the answer earlier releases gave, so a proxy's
    ``IP:port``, brackets, zone index or case keeps its rate-limit key and
    audit value - unless the ``client_ip_strict`` security option
    (``CLIENT_IP_STRICT``, or Settings > Access and security) asks for the canonical form
    (one client, one string), or the value is longer than the audit trail's
    column, which used to fail the audited write, or its zone index holds a
    character no interface name has: both are canonical in both modes. The
    ``unknown`` a proxy writes for a client it hides is kept as read unless
    strict. Anything else is not an address and only a client writes it:
    ``None``, warned about once per process.
    """
    from app.core.security_options import security_option

    raw = (value or "").strip()
    if not raw:
        return None
    canonical = canonical_ip(raw)
    if canonical is None:
        if raw.lower() == _HIDDEN_CLIENT:
            return None if security_option("client_ip_strict") else raw
        _report_forged_address()
        return None
    if security_option("client_ip_strict") or len(raw) > CLIENT_ADDRESS_MAX_LENGTH or not _plain_zone(raw):
        return canonical
    return raw


def _plain_zone(raw: str) -> bool:
    """Whether the zone index of ``raw``, an address :func:`canonical_ip` accepts, if any, is an interface's."""
    _, percent, zone = raw.partition("%")
    if not percent:
        return True
    if raw.startswith("["):
        zone = zone.partition("]")[0]
    return _ZONE_ID.fullmatch(zone) is not None


def _report_forged_address() -> None:
    global _forged_address_reported
    if _forged_address_reported:
        return
    _forged_address_reported = True
    logging.getLogger(__name__).warning(
        "Ignored a client address that is not an IP address (an X-Forwarded-For hop, or a peer "
        "uvicorn copied from one): only a client writes such a value, so the request is keyed "
        "on the peer instead, or on no address. Logged once per process."
    )


def first_forwarded_hop(forwarded_for: str | None) -> str | None:
    """Leftmost (original-client) hop of an X-Forwarded-For chain, else None.

    Callers decide *whether* to trust the header (``trust_proxy_headers``);
    this only parses it. The trailing entries are proxy hops. The hop comes
    back through :func:`client_address`, so ``None`` when it is not an
    address: behind a proxy that appends to the header, it is the text the
    client sent.
    """
    if not forwarded_for:
        return None
    return client_address(forwarded_for.partition(",")[0])


def request_client_ip(request) -> str | None:
    """The client address a request came from, under the platform's trust model.

    ``X-Forwarded-For`` is read only when the
    ``trust_proxy_headers`` security option is on, and then its leftmost hop wins (the
    caller-controlled start of the chain behind a proxy that appends to it,
    which is why ``FORWARDED_ALLOW_IPS`` is recommended instead). Otherwise - or when that hop is not an IP
    address - the peer address is used, which is the proxy-corrected client
    when ``FORWARDED_ALLOW_IPS`` names the proxy, since uvicorn rewrites
    ``scope["client"]`` before the app runs. Both go through
    :func:`client_address`: an IP address as it was read (canonical under
    ``client_ip_strict``), never text that is not an address; ``None`` when
    there is none to report.
    """
    from app.core.security_options import security_option

    if security_option("trust_proxy_headers"):
        hop = first_forwarded_hop(request.headers.get("x-forwarded-for"))
        if hop:
            return hop
    client = getattr(request, "client", None)
    return client_address(client.host) if client else None


def client_address_is_forwarded(request) -> bool:
    """Whether the client address may come from ``X-Forwarded-For`` rather than the socket.

    True whenever the request carries the header, whatever the settings:
    uvicorn rewrites ``scope["client"]`` from it for any peer
    ``FORWARDED_ALLOW_IPS`` trusts (every peer under ``*``) without checking
    the value is an address, and :func:`request_client_ip` prefers it under
    ``trust_proxy_headers``. Such a request's client address is not known to
    be its socket peer, so nothing may grant trust by address to it;
    otherwise :func:`request_client_ip` is that peer.
    """
    return "x-forwarded-for" in request.headers


# ---------------------------------------------------------------------------
# Exception formatting helper (logging)
# ---------------------------------------------------------------------------


def format_exception_message(exc: BaseException) -> str:
    """Return a non-empty, human-readable one-line reason for *exc*.

    ``str(exc)`` is empty for many exception types that show up constantly
    in production logs — ``asyncio.TimeoutError()``, ``CancelledError()``,
    several ``httpx`` transport errors, any ``Exception()`` raised without
    arguments — which produced log lines like
    ``"Failed to refresh token for X (notion): "`` carrying zero
    diagnostic value.

    Rules:

    * When ``str(exc)`` is non-empty, return it unchanged (existing log
      output is preserved, alerting on message content keeps working).
    * When it is empty, fall back to the exception class name
      (``"TimeoutError"``), and — if the exception was chained from
      another one — append the cause so the root reason survives
      (``"RetryError (caused by ConnectTimeout)"``).

    Use this helper whenever an exception message is interpolated into a
    log line (``logger.warning("...: %s", format_exception_message(e))``).
    It intentionally does NOT replace ``exc_info=True`` /
    ``logger.exception()`` — tracebacks should still be recorded for
    unexpected failures.
    """
    # ``str(exc)`` was previously evaluated lazily inside the logging
    # handler (where a raising ``__str__`` is contained by
    # ``Handler.handleError``); this helper runs at the call site, so it
    # must never let a broken ``__str__`` escape into the except block
    # that was logging it.
    try:
        msg = str(exc).strip()
    except Exception:
        msg = ""
    if msg:
        return msg
    name = type(exc).__name__
    # Mirror traceback chaining semantics: explicit ``raise ... from X``
    # wins; the implicit context is used unless suppressed via ``from None``.
    cause = exc.__cause__
    if cause is None and not exc.__suppress_context__:
        cause = exc.__context__
    if cause is not None and cause is not exc:
        try:
            cause_msg = str(cause).strip() or type(cause).__name__
        except Exception:
            cause_msg = type(cause).__name__
        return f"{name} (caused by {cause_msg})"
    return name


# ---------------------------------------------------------------------------
# Graceful-shutdown drain cancellation detection
# ---------------------------------------------------------------------------

# uvicorn's ``Server._wait_tasks_to_complete`` cancels the ASGI tasks that
# are still running when ``--timeout-graceful-shutdown`` expires with
# ``task.cancel(msg="Task cancelled, timeout graceful shutdown exceeded")``.
# Matching on the distinctive tail (instead of the full sentence) keeps the
# detection resilient to minor upstream wording changes while staying
# specific enough to never match an application-provided cancel message
# that merely mentions "graceful shutdown". Shared with the
# ``DemoteRoutineShutdownDrainLogs`` filter in ``app.core.logging`` so the
# middleware and the log filter can never drift apart.
GRACEFUL_SHUTDOWN_CANCEL_HINT = "timeout graceful shutdown exceeded"


def is_graceful_shutdown_cancellation(exc: BaseException) -> bool:
    """True when *exc* is uvicorn's bounded-drain task cancellation.

    The cancel message travels as ``CancelledError.args`` from the await
    point where the cancellation was delivered.  When the error is re-raised
    from a ``finally`` block (or wrapped by task-group internals) the
    original message can end up on the ``__context__`` chain instead, so the
    chain is walked too (cycle-safe).
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if any(
            isinstance(arg, str) and GRACEFUL_SHUTDOWN_CANCEL_HINT in arg
            for arg in current.args
        ):
            return True
        current = current.__context__
    return False


def is_statement_timeout(exc: BaseException) -> bool:
    """Whether *exc*, or an exception it was raised from, is a cancelled database statement.

    PostgreSQL's ``statement_timeout`` (SQLSTATE 57014, asyncpg's
    ``QueryCanceledError``, wrapped by SQLAlchemy as ``DBAPIError.orig``).
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ == "QueryCanceledError" or getattr(current, "sqlstate", None) == "57014":
            return True
        if "canceling statement due to statement timeout" in str(current):
            return True
        orig = getattr(current, "orig", None)
        if isinstance(orig, BaseException) and id(orig) not in seen:
            current = orig
            continue
        current = current.__cause__ or current.__context__
    return False


# ---------------------------------------------------------------------------
# Null-byte sanitization (tracing + ORM JSON payloads)
# ---------------------------------------------------------------------------

# Leaves ``strip_null_bytes`` returns as they are, recognised by their EXACT
# type inside the container loops so the many scalars of a payload cost no
# recursive call; a subclass still takes the call, whose answer is the same.
_NUL_FREE_LEAF_TYPES = frozenset({int, float, bool, type(None)})


def strip_null_bytes(data: Any) -> Any:
    """Recursively strip ``\\u0000`` null bytes from all strings in a nested structure.

    PostgreSQL rejects the null byte in text and JSONB columns with
    ``UntranslatableCharacterError``, and the Langfuse ingestion API rejects
    payloads containing it with an opaque 400 Bad Request.
    Tool results (e.g. GitHub GraphQL response bodies) occasionally embed raw
    null bytes, so every tracing sink sanitizes payloads with this helper
    before emission — the local PG sink (``services/tracing/local_sink.py``)
    and the Langfuse span emitters (``services/langfuse_tracing.py``) — and
    the async engine's ``json_serializer`` (``core/database.py``) runs it on
    every ORM JSON column write, so a NUL in an external payload can never
    be persisted as a ``\\u0000`` escape that later breaks ``json -> jsonb``
    reads such as the ``list_runs`` cross-run search.

    Copy-on-write: strings without null bytes and containers whose contents
    are unchanged are returned as the *same* object, and a replacement
    container is only allocated once the first changed element is found — the
    common clean-case costs a single traversal with no allocations.  Tuples
    are always converted to lists — they serialize to JSON arrays anyway, and
    a list result keeps both sinks' downstream JSON encoding uniform.

    Dict keys are sanitized only when they are strings (the only JSON key
    type); non-string keys pass through unchanged so hashable keys can never
    be converted into an unhashable container (e.g. tuple → list).

    Payloads are JSON-derived (tool results, LLM messages), so reference
    cycles cannot occur and nesting depth is bounded far below the
    interpreter recursion limit — same recursion profile as the sibling
    sanitizers (``_strip_media_content``, ``_truncate_deep``).
    """
    return _sanitize_strings(data, False)


def sanitize_json_strings(data: Any) -> Any:
    """:func:`strip_null_bytes`, and every lone UTF-16 surrogate replaced by U+FFFD.

    A request body's ``"\\ud800"`` escape decodes to a Python string holding a
    lone surrogate, which ``json.dumps`` writes back as that escape. A
    ``json`` column stores it verbatim, and every later read that makes
    PostgreSQL de-escape the document - a ``::jsonb`` cast, ``->``, ``->>`` -
    refuses the row and aborts the whole statement (``core/json_sql.py``).
    The engine's ``json_serializer`` and its flush hook (``core/database.py``)
    run this on every ORM JSON write, so no such value is stored again.

    A high surrogate immediately followed by a low one is a valid pair
    (``json.dumps`` writes it as the escape pair PostgreSQL decodes) and is
    kept. Copy-on-write like :func:`strip_null_bytes`: an ASCII string costs
    one flag test, and a payload with nothing to replace is returned as the
    same object.
    """
    return _sanitize_strings(data, True)


#: A surrogate code point: the cheap test before the exact one.
SURROGATE_RE = re.compile("[\ud800-\udfff]")
#: A surrogate with no partner: a high one not followed by a low one, or a
#: low one not preceded by a high one. The one definition, also read by the
#: refusal of ``core/lone_surrogates.py``.
LONE_SURROGATE_RE = re.compile(
    "[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]"
)


def storable_text(text: str) -> str:
    """*text* as a PostgreSQL text value can hold it.

    A NUL is stripped, and a UTF-16 surrogate, which UTF-8 cannot encode (the
    driver refused the whole statement), is repaired: a high surrogate directly
    followed by a low one becomes the character the pair encodes, any other
    one U+FFFD. The engine applies it to every bound string and to every
    flushed string attribute; a string needing neither is returned as is.

    Surrogates first: two halves a NUL separates are not a pair.
    """
    if not text.isascii() and SURROGATE_RE.search(text):
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if "\u0000" in text:
        text = text.replace("\u0000", "")
    return text


def _clean_string(text: str, surrogates: bool) -> str:
    # Surrogates first: two halves a NUL separates are not a pair.
    if (
        surrogates
        and not text.isascii()
        and SURROGATE_RE.search(text)
        and LONE_SURROGATE_RE.search(text)
    ):
        text = LONE_SURROGATE_RE.sub("\ufffd", text)
    if "\u0000" in text:
        text = text.replace("\u0000", "")
    return text


def _sanitize_strings(data: Any, surrogates: bool) -> Any:
    """The walk behind :func:`strip_null_bytes` and :func:`sanitize_json_strings`.

    A string that cannot need cleaning (no NUL, and ASCII when *surrogates*)
    is decided inline, without a call.
    """
    if isinstance(data, str):
        if "\u0000" in data or (surrogates and not data.isascii()):
            return _clean_string(data, surrogates)
        return data
    if isinstance(data, dict):
        out: dict | None = None
        for idx, (key, value) in enumerate(data.items()):
            new_key = (
                _clean_string(key, surrogates)
                if isinstance(key, str)
                and ("\u0000" in key or (surrogates and not key.isascii()))
                else key
            )
            value_type = type(value)
            if value_type is str:
                new_value = (
                    _clean_string(value, surrogates)
                    if "\u0000" in value or (surrogates and not value.isascii())
                    else value
                )
            elif value_type in _NUL_FREE_LEAF_TYPES:
                new_value = value
            else:
                new_value = _sanitize_strings(value, surrogates)
            if out is None:
                if new_key is key and new_value is value:
                    continue
                # First change found — copy the already-verified prefix.
                out = dict(islice(data.items(), idx))
            out[new_key] = new_value
        return data if out is None else out
    if isinstance(data, tuple):
        return [_sanitize_strings(item, surrogates) for item in data]
    if isinstance(data, list):
        items: list | None = None
        for idx, item in enumerate(data):
            item_type = type(item)
            if item_type is str:
                new_item = (
                    _clean_string(item, surrogates)
                    if "\u0000" in item or (surrogates and not item.isascii())
                    else item
                )
            elif item_type in _NUL_FREE_LEAF_TYPES:
                new_item = item
            else:
                new_item = _sanitize_strings(item, surrogates)
            if items is None:
                if new_item is item:
                    continue
                items = data[:idx]
            items.append(new_item)
        return data if items is None else items
    return data


def null_non_finite_numbers(data: Any) -> Any:
    """Recursively replace ``NaN``/``Infinity`` floats with ``None``.

    JSON cannot spell either, so PostgreSQL rejects both in ``json`` and
    ``jsonb`` columns (``invalid input syntax for type json``) and the whole
    transaction with it. ``json.loads`` accepts the bare tokens and
    ``json.dumps`` writes them back, so a value from an external payload
    reaches the database looking like a number. The entry points are
    those of ``strip_null_bytes`` (webhook bodies, integration events, anything
    decoded with ``json.loads`` into an ORM JSON column), so both run in the
    engine's ``json_serializer`` rather than at each caller.

    ``None``, not ``0`` or a string: a non-finite value is not a measurement,
    and null is how the platform spells "not measured".

    Only values need it: ``json.dumps`` stringifies a float key to ``"NaN"``,
    an ordinary string and valid JSON.

    Copy-on-write like ``strip_null_bytes``: unchanged containers are returned
    as the same object, so the clean case costs one traversal and no
    allocations.
    """
    if isinstance(data, float):
        return None if not math.isfinite(data) else data
    if isinstance(data, dict):
        out: dict | None = None
        for idx, (key, value) in enumerate(data.items()):
            new_value = null_non_finite_numbers(value)
            if out is None:
                if new_value is value:
                    continue
                out = dict(islice(data.items(), idx))
            out[key] = new_value
        return data if out is None else out
    if isinstance(data, tuple):
        return [null_non_finite_numbers(item) for item in data]
    if isinstance(data, list):
        items: list | None = None
        for idx, item in enumerate(data):
            new_item = null_non_finite_numbers(item)
            if items is None:
                if new_item is item:
                    continue
                items = data[:idx]
            items.append(new_item)
        return data if items is None else items
    return data


# ---------------------------------------------------------------------------
# Span tool-arguments normalization (tracing payloads)
# ---------------------------------------------------------------------------


def coerce_tool_arguments_object(value: Any) -> dict[str, Any] | None:
    """Normalize span ``tool_arguments`` into a JSON object (or ``None``).

    The span schema declares ``tool_arguments: dict | None``, but legacy call
    paths and legacy rows can carry strings (already-serialized JSON or opaque
    text) or other scalars.  This helper is the single normalization contract
    shared by the write path (``langfuse_tracing``), the Postgres API
    serializer (``api/v1/traces``), and the ClickHouse read store, so every
    surface agrees on the same shape:

    * ``None`` and empty / whitespace-only strings -> ``None`` (no arguments).
    * dicts -> returned unchanged.
    * strings holding a JSON object -> parsed dict.
    * any other string -> ``{"raw": <original string>}``.
    * any other value (list, scalar) -> ``{"raw": <JSON-encoded value>}``.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {"raw": value}
        if isinstance(parsed, dict):
            return parsed
        return {"raw": value}
    try:
        return {"raw": json.dumps(value, default=str, ensure_ascii=False)}
    except Exception:
        return {"raw": repr(value)}


# ---------------------------------------------------------------------------
# XTM One STIX extension
# ---------------------------------------------------------------------------

#: Stable UUID for the XTM One STIX 2.1 extension definition.
#: Used wherever XTM One embeds custom properties into STIX objects/bundles
#: (e.g. ``extraction_errors`` in Filigran AI tool results).
XTM_ONE_STIX_EXTENSION_ID = (
    "extension-definition--5765b2bc-77e1-4629-b6e0-f1b4a2ed3e4e"
)

# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug.

    Strips accents, lowercases, removes non-word characters, and collapses
    whitespace/hyphens into single hyphens.

    >>> slugify("My Agent Name!")
    'my-agent-name'
    >>> slugify("Héllo Wörld")
    'hello-world'
    """
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text or "agent"


_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")


def validate_slug(slug: str) -> str:
    """Validate and normalize a user-provided slug.

    Raises ``ValueError`` if the slug is empty, too long, or contains
    characters outside ``[a-z0-9-]``.
    """
    slug = slug.strip().lower()
    if not slug:
        raise ValueError("Slug cannot be empty")
    if len(slug) > 200:
        raise ValueError("Slug cannot exceed 200 characters")
    if not _SLUG_PATTERN.match(slug):
        raise ValueError(
            "Slug must contain only lowercase letters, numbers, and hyphens, and cannot start or end with a hyphen"
        )
    return slug


# ---------------------------------------------------------------------------
# Exact-match key normalization (work items + shared state)
# ---------------------------------------------------------------------------

#: Max length of an exact-match key. Mirrors the ``String(200)`` column on both
#: ``work_items.correlation_key`` and ``shared_states.key`` (both models import
#: this constant) so the write-side normalizer and the DB columns can never drift.
CORRELATION_KEY_MAX_LENGTH = 200


def normalize_correlation_key(value: str | None) -> str | None:
    """Normalize an exact-match key for storage and lookup.

    Exact-match keys are compared with ``==`` (the work-item ``correlation_key``
    and the shared-state ``key``), so stray leading/trailing whitespace would
    silently break "same key" semantics, and a value longer than
    :data:`CORRELATION_KEY_MAX_LENGTH` would raise on commit against the
    ``String(200)`` column. Strips surrounding whitespace and bounds the length;
    returns ``None`` for an empty / whitespace-only value so callers persist SQL
    ``NULL`` (and equality filters treat it as "no filter").

    Apply on BOTH the write path (create / update / upsert) and the read path
    (equality filters, bulk existence checks) so a caller passing an
    unnormalized key still matches the normalized value already stored.

    >>> normalize_correlation_key("  inc-8948  ")
    'inc-8948'
    >>> normalize_correlation_key("   ") is None
    True
    """
    if value is None:
        return None
    cleaned = value.strip()[:CORRELATION_KEY_MAX_LENGTH]
    return cleaned or None


# ---------------------------------------------------------------------------
# Delay parsing
# ---------------------------------------------------------------------------


def parse_delay(delay_str: str | None) -> int:
    """Parse a human-friendly delay string into seconds.

    Supported units: ``s`` (seconds), ``m`` (minutes), ``h`` (hours),
    ``d`` (days).  Returns ``0`` for ``None`` or unrecognised input.

    >>> parse_delay("10m")
    600
    >>> parse_delay("1h")
    3600
    >>> parse_delay(None)
    0
    """
    if not delay_str:
        return 0
    match = re.match(r"^(\d+)(s|m|h|d)$", delay_str.strip())
    if not match:
        return 0
    value, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers.get(unit, 0)


# ---------------------------------------------------------------------------
# Markdown → Slack mrkdwn conversion
# ---------------------------------------------------------------------------


def markdown_to_slack(text: str) -> str:
    """Convert standard Markdown to Slack's *mrkdwn* format.

    Handles the most common differences between the two formats:

    * Bold:          ``**text**``  → ``*text*``
    * Bold (alt):    ``__text__``  → ``*text*``
    * Strikethrough: ``~~text~~``  → ``~text~``
    * Links:         ``[text](url)``  → ``<url|text>``
    * Images:        ``![alt](url)``  → ``<url|alt>``
    * Headers:       ``# heading``    → ``*heading*``

    Code blocks (fenced and inline) are preserved as-is since Slack uses
    the same backtick syntax.  Bullet lists (``- item``) are left
    untouched — Slack renders them acceptably.

    >>> markdown_to_slack("**Hello** world")
    '*Hello* world'
    >>> markdown_to_slack("[click](https://x.com)")
    '<https://x.com|click>'
    >>> markdown_to_slack("# Title")
    '*Title*'
    >>> markdown_to_slack("~~removed~~")
    '~removed~'
    """
    if not text:
        return text

    # 1. Protect fenced and inline code from transformation.
    code_blocks: list[str] = []

    def _protect_code(m: re.Match[str]) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    result = re.sub(r"```[\s\S]*?```", _protect_code, text)
    result = re.sub(r"`[^`\n]+`", _protect_code, result)

    # 2. Images ![alt](url) → <url|alt>  (must come before links)
    result = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"<\2|\1>", result)

    # 3. Links [text](url) → <url|text>
    result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", result)

    # 4. Headers: # … → *…* (Slack has no header syntax)
    result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)

    # 5. Bold **text** / __text__ → *text*
    result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
    result = re.sub(r"__(.+?)__", r"*\1*", result)

    # 6. Strikethrough ~~text~~ → ~text~
    result = re.sub(r"~~(.+?)~~", r"~\1~", result)

    # 7. Restore protected code blocks.
    for i, block in enumerate(code_blocks):
        result = result.replace(f"\x00CODE{i}\x00", block)

    return result


# ---------------------------------------------------------------------------
# UUID helpers
# ---------------------------------------------------------------------------


def safe_uuid(value: str | _uuid.UUID | None) -> _uuid.UUID | None:
    """Parse *value* as a UUID, returning ``None`` for non-UUID strings.

    Useful for handling identifiers that may be a real UUID or a sentinel
    like ``"anonymous"``. A ``UUID`` is returned as it is.

    >>> safe_uuid("550e8400-e29b-41d4-a716-446655440000")
    UUID('550e8400-e29b-41d4-a716-446655440000')
    >>> safe_uuid(_uuid.UUID("550e8400-e29b-41d4-a716-446655440000"))
    UUID('550e8400-e29b-41d4-a716-446655440000')
    >>> safe_uuid("anonymous") is None
    True
    >>> safe_uuid(None) is None
    True
    """
    if isinstance(value, _uuid.UUID):
        return value
    if not value:
        return None
    try:
        return _uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


_GITHUB_URL_PREFIXES = (
    "https://github.com/",
    "http://github.com/",
    "github.com/",
)


def normalize_github_repo(repo: str) -> str:
    """Extract ``owner/repo`` from a full GitHub URL or return as-is.

    Handles formats like:

    - ``https://github.com/owner/repo``
    - ``https://github.com/owner/repo.git``
    - ``http://github.com/owner/repo/tree/main/subdir``
    - ``github.com/owner/repo``
    - ``owner/repo``  (already correct)

    Uses string operations instead of regex to avoid ReDoS.

    >>> normalize_github_repo("https://github.com/acme/widgets.git")
    'acme/widgets'
    >>> normalize_github_repo("owner/repo")
    'owner/repo'
    """
    repo = repo.strip().rstrip("/")
    lower = repo.lower()
    for prefix in _GITHUB_URL_PREFIXES:
        if lower.startswith(prefix):
            repo = repo[len(prefix) :]
            break
    else:
        return repo
    if repo.endswith(".git"):
        repo = repo[:-4]
    parts = repo.split("/")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return repo


def is_valid_uuid(value: str) -> bool:
    """Return ``True`` if *value* is a parseable UUID hex string.

    >>> is_valid_uuid("550e8400-e29b-41d4-a716-446655440000")
    True
    >>> is_valid_uuid("not-a-uuid")
    False
    """
    return safe_uuid(value) is not None


def is_zero_norm_vector(vec: Iterable[float]) -> bool:
    """Return ``True`` when *vec* has zero L2 norm (unusable for similarity).

    Some embedding providers degrade to a well-shaped vector of zeros
    instead of raising (or returning an empty list) on failure. A zero-norm
    vector is useless for cosine similarity — pgvector's ``<=>`` operator
    yields NaN distances against it — so every embedding write path must
    treat it as a failed embedding. Single source of truth for the norm
    check used by ``document_processor``, ``builtin_tools.store_knowledge``
    and the persistent-memory embedding helpers.

    The degenerate empty vector also has zero norm and therefore returns
    ``True`` — deliberately, so a caller relying on this check alone never
    persists an unusable vector. Callers that want a *distinct* log message
    for the empty/``None`` case (all current call sites do) must check
    emptiness *before* calling this helper.

    >>> is_zero_norm_vector([0.0, 0.0, 0.0])
    True
    >>> is_zero_norm_vector([])
    True
    >>> is_zero_norm_vector([0.1, 0.0])
    False
    """
    return sum(float(v) * float(v) for v in vec) < 1e-12


# ---------------------------------------------------------------------------
# Cron expression validation
# ---------------------------------------------------------------------------


def validate_cron_expression(cron_expr: object) -> str:
    """Validate a cron expression and return its canonical stripped form.

    Raises ``ValueError`` with a clear message when the expression is missing,
    not a string, empty, or unparseable by ``croniter``; a value that is present
    but unusable is named in the message. This is the single source of truth
    used by:

    * The assignment Pydantic schemas (`POST` / `PUT` API validation)
    * The agent builder tools (`create_assignment` / `update_assignment`)
    * The agent import endpoint

    The first two reach it through :func:`validate_schedule_trigger_config`.
    Centralising the check ensures every save path rejects invalid crons
    instead of persisting them and crashing the scheduler at runtime.

    >>> validate_cron_expression("0 9 * * 1-5")
    '0 9 * * 1-5'
    >>> validate_cron_expression("*/15 * * 1")
    Traceback (most recent call last):
        ...
    ValueError: Invalid cron expression '*/15 * * 1': ...
    """
    if not isinstance(cron_expr, str) or not cron_expr.strip():
        got = "" if cron_expr is None else f", got {cron_expr!r}"
        raise ValueError(
            f"Schedule trigger requires a non-empty 'cron' expression in trigger_config{got}."
        )
    stripped = cron_expr.strip()
    # Import lazily so this module stays import-cheap for code paths that
    # don't need cron parsing (migrations, slug helpers, etc.).
    from croniter import CroniterBadCronError, croniter

    try:
        croniter(stripped)
    except (CroniterBadCronError, ValueError) as exc:
        raise ValueError(f"Invalid cron expression '{cron_expr}': {exc}") from exc
    return stripped


def validate_schedule_trigger_config(trigger_type: object, trigger_config: object) -> None:
    """Refuse a ``schedule`` trigger whose config the scheduler cannot run.

    The one rule shared by the save paths that see the whole trigger: the
    assignment create / update schemas and the agent-builder
    ``create_assignment`` / ``update_assignment`` tools, so the API and the
    tools cannot drift. A ``schedule`` needs an object ``trigger_config`` whose
    ``cron`` passes :func:`validate_cron_expression`; any other trigger type
    passes untouched. Raises ``ValueError`` with the message to surface.

    >>> validate_schedule_trigger_config("manual", {})
    >>> validate_schedule_trigger_config("schedule", {"cron": "0 9 * * 1-5"})
    >>> validate_schedule_trigger_config("schedule", {})
    Traceback (most recent call last):
        ...
    ValueError: Schedule trigger requires a non-empty 'cron' expression in trigger_config.
    """
    if trigger_type != "schedule":
        return
    if not isinstance(trigger_config, dict):
        raise ValueError("trigger_config must be an object for schedule triggers.")
    validate_cron_expression(trigger_config.get("cron"))
    if "run_on_create" in trigger_config and not isinstance(trigger_config["run_on_create"], bool):
        raise ValueError("trigger_config.run_on_create must be true or false.")


def schedule_trigger_error(trigger_type: object, trigger_config: object) -> str | None:
    """Return the refusal for a trigger AS IT WILL BE SAVED, else ``None``.

    For the agent-builder assignment tools, which see the stored row. The
    scheduler pauses a schedule whose cron it cannot parse and silently skips one
    with no cron, so either would be an "updated" answer for a schedule that
    never fires. The message names the bad value and the expected format.
    """
    try:
        validate_schedule_trigger_config(trigger_type, trigger_config)
    except ValueError as exc:
        return (
            f"{exc} Expected trigger_config like '{{\"cron\": \"0 9 * * 1-5\"}}' - "
            "a cron of 5 fields: minute hour day-of-month month day-of-week - "
            "or a trigger_type other than 'schedule'."
        )
    return None


def cron_is_valid(cron_expr: object) -> bool:
    """Return ``True`` when *cron_expr* is a non-empty string parseable by croniter.

    The non-raising counterpart of :func:`validate_cron_expression`, for
    read-only callers (e.g. the schedule endpoint) that want to decide whether
    to compute a next-run time without catching an exception.

    >>> cron_is_valid("*/5 * * * *")
    True
    >>> cron_is_valid("nope")
    False
    >>> cron_is_valid(None)
    False
    """
    if not isinstance(cron_expr, str) or not cron_expr.strip():
        return False
    from croniter import CroniterBadCronError, croniter

    try:
        croniter(cron_expr.strip())
    except (CroniterBadCronError, ValueError):
        return False
    return True


def schedule_timezone(trigger_config: object) -> tuple[tzinfo, str | None]:
    """Return the zone a schedule trigger's cron is read in, and a warning.

    ``trigger_config.timezone`` is an IANA name (``Europe/Paris``). Absent or
    empty: UTC, no warning (the behaviour of every schedule without a zone).
    Present but unknown to the zone database: UTC and the warning
    ``timezone '<value>' not recognised, UTC used``, which the schedule
    endpoint and ``get_assignment`` show so a wrong zone is visible. Never
    raises: a bad zone must not pause a schedule that fired before.

    >>> schedule_timezone({"cron": "0 7 * * *"})
    (datetime.timezone.utc, None)
    >>> schedule_timezone({"timezone": "Europe/Paris"})[0].key
    'Europe/Paris'
    >>> schedule_timezone({"timezone": "Mars/Olympus"})[1]
    "timezone 'Mars/Olympus' not recognised, UTC used"
    """
    raw = trigger_config.get("timezone") if isinstance(trigger_config, dict) else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return UTC, None
    if not isinstance(raw, str):
        return UTC, f"timezone {raw!r} not recognised, UTC used"
    name = raw.strip()
    if name.upper() in {"UTC", "Z", "ETC/UTC"}:
        return UTC, None
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError):
        return UTC, f"timezone {name!r} not recognised, UTC used"


def _wall_time_instant(wall: datetime, zone: tzinfo) -> datetime:
    """The UTC instant a naive local wall time *wall* stands for in *zone*.

    A wall time a daylight saving change repeats is its FIRST occurrence
    (``fold=0``), so a repeated local hour fires once. A wall time the change
    skips is the first valid instant after the gap (the transition itself),
    so a non-existent local hour fires once, at the next valid instant.
    """
    first = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if first.astimezone(zone).replace(tzinfo=None) == wall:
        return first
    # In a gap: fold=0 reads it with the offset before the change, fold=1 with
    # the one after; the transition lies between the two instants. Gaps are
    # whole minutes, so walk them minute by minute to the first instant whose
    # wall time is at or past *wall*.
    later = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
    lo, hi = min(first, later), max(first, later)
    instant = lo.replace(second=0, microsecond=0)
    while instant < hi and instant.astimezone(zone).replace(tzinfo=None) < wall:
        instant += timedelta(minutes=1)
    return instant


def next_cron_run(
    cron_expr: object,
    after: datetime | None,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> datetime | None:
    """Return the next time a cron expression fires after a base time.

    Single source of truth for "when does this schedule next run", shared by the
    background scheduler (``autonomous._process_scheduled_assignments``), the
    read-only schedule endpoint (``GET /assignments/schedule``) and the
    ``get_assignment`` tool, so the next-run time shown can never drift from the
    tick that actually fires:

    * ``after`` is the assignment's ``last_triggered_at`` — the base the
      scheduler advances from. When it is ``None`` (never run), the next fire is
      computed from ``now`` so the UI shows the upcoming occurrence.
    * ``tz`` is the zone the cron's fields are read in (``schedule_timezone``
      of the trigger config). ``None`` or UTC keeps the plain UTC computation.
      Any other zone evaluates the cron on the zone's wall clock, daylight
      saving included (see ``_wall_time_instant`` for the skipped and repeated
      hours), and the result is returned in UTC.

    Returns ``None`` for a missing / non-string / unparseable expression instead
    of raising, so read-only callers can treat it as "no next run".

    >>> next_cron_run("nope", None) is None
    True
    >>> next_cron_run("0 9 * * *", None) is None
    False
    >>> next_cron_run("0 * * * *", datetime(2026, 1, 1, 9, 0, tzinfo=UTC)).hour
    10
    >>> next_cron_run("0 7 * * *", datetime(2026, 10, 31, 0, 0, tzinfo=UTC),
    ...               tz=ZoneInfo("Europe/Paris")).isoformat()
    '2026-10-31T06:00:00+00:00'
    """
    if not isinstance(cron_expr, str) or not cron_expr.strip():
        return None
    from croniter import CroniterBadCronError, croniter

    base = after or now or datetime.now(UTC)
    try:
        if tz is None or tz is UTC:
            return croniter(cron_expr.strip(), base).get_next(datetime)
        base_utc = base.astimezone(UTC)
        # Iterate on the zone's naive wall clock (croniter never sees an
        # offset, so a daylight saving change cannot skew its arithmetic),
        # then map each wall time to its instant. A candidate at or before
        # the base (the second reading of a repeated hour, a skipped time
        # mapped onto an instant already fired) is passed over: once only.
        wall = base_utc.astimezone(tz).replace(tzinfo=None)
        itr = croniter(cron_expr.strip(), wall)
        for _ in range(1000):
            instant = _wall_time_instant(itr.get_next(datetime), tz)
            if instant > base_utc:
                return instant
        return None
    except (CroniterBadCronError, ValueError, TypeError, AttributeError):
        return None


def previous_cron_run(
    cron_expr: object,
    at: datetime,
    tz: tzinfo | None = None,
) -> datetime | None:
    """Return the latest time a cron expression fires at or before *at*.

    The cron slot a late fire serves (#2562): ``next_cron_run`` read
    backwards, with the same zone rules (the cron's fields read on *tz*'s wall
    clock, daylight saving included, the result in UTC). ``None`` for a
    missing / non-string / unparseable expression.

    >>> previous_cron_run("*/15 * * * *", datetime(2026, 11, 27, 5, 42, 17, tzinfo=UTC)).isoformat()
    '2026-11-27T05:30:00+00:00'
    >>> previous_cron_run("*/15 * * * *", datetime(2026, 11, 27, 5, 30, tzinfo=UTC)).isoformat()
    '2026-11-27T05:30:00+00:00'
    >>> previous_cron_run("0 7 * * *", datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
    ...                   tz=ZoneInfo("Europe/Paris")).isoformat()
    '2026-10-31T06:00:00+00:00'
    """
    if not isinstance(cron_expr, str) or not cron_expr.strip():
        return None
    from croniter import CroniterBadCronError, croniter

    # One second past *at* (croniter drops microseconds), so a slot exactly
    # at *at* is its own answer.
    base_utc = at.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=1)
    try:
        if tz is None or tz is UTC:
            previous: datetime = croniter(cron_expr.strip(), base_utc).get_prev(datetime)
            return previous
        wall = base_utc.astimezone(tz).replace(tzinfo=None)
        itr = croniter(cron_expr.strip(), wall)
        for _ in range(1000):
            instant = _wall_time_instant(itr.get_prev(datetime), tz)
            if instant < base_utc:
                return instant
        return None
    except (CroniterBadCronError, ValueError, TypeError, AttributeError):
        return None


def schedule_base(assignment: object) -> datetime | None:
    """The instant a schedule assignment's cron advances from.

    The later of ``last_triggered_at`` (the last fire) and
    ``schedule_resumed_at`` (the last time it was enabled again), so enabling
    a schedule never makes it due at once: the next fire is the first cron
    occurrence after the resume, with no catch-up of the occurrences missed
    while it was off or never ran. When neither is set (an assignment created
    enabled that never ran): ``None``, due at the first tick (the creation
    auto-run), unless its ``trigger_config`` says ``run_on_create: false``,
    whose anchor is ``created_at`` so its first fire is the first cron
    occurrence after creation. Read with ``getattr`` so lightweight doubles
    still answer.

    >>> from types import SimpleNamespace as N
    >>> t1 = datetime(2026, 11, 1, tzinfo=UTC); t2 = datetime(2026, 11, 5, tzinfo=UTC)
    >>> schedule_base(N(last_triggered_at=t1, schedule_resumed_at=t2)) == t2
    True
    >>> schedule_base(N(last_triggered_at=t2, schedule_resumed_at=None)) == t2
    True
    >>> schedule_base(N()) is None
    True
    >>> schedule_base(N(trigger_config={"run_on_create": False}, created_at=t1)) == t1
    True
    """
    stamps = [
        value
        for value in (
            getattr(assignment, "last_triggered_at", None),
            getattr(assignment, "schedule_resumed_at", None),
        )
        if isinstance(value, datetime)
    ]
    if stamps:
        return max(stamps)
    config = getattr(assignment, "trigger_config", None)
    created_at = getattr(assignment, "created_at", None)
    if isinstance(config, dict) and config.get("run_on_create") is False and isinstance(created_at, datetime):
        return created_at
    return None


def describe_schedule_next_run(
    trigger_config: object,
    after: datetime | None,
    now: datetime | None = None,
) -> dict[str, str | None]:
    """The next fire of a schedule trigger as the UI and tools show it.

    Returns ``next_run_at`` (UTC, ISO 8601), ``timezone`` (the zone the cron
    is read in, ``UTC`` when none or unknown), ``next_run_local`` (the same
    instant as a wall time in that zone, with its offset) and
    ``timezone_warning`` (set when the configured zone is not recognised).
    Computed with :func:`next_cron_run`, the function the scheduler fires on.

    >>> d = describe_schedule_next_run(
    ...     {"cron": "0 7 * * *", "timezone": "Europe/Paris"},
    ...     datetime(2026, 10, 31, 0, 0, tzinfo=UTC))
    >>> d["next_run_at"], d["next_run_local"]
    ('2026-10-31T06:00:00+00:00', '2026-10-31T07:00:00+01:00')
    """
    config = trigger_config if isinstance(trigger_config, dict) else {}
    zone, warning = schedule_timezone(config)
    nxt = next_cron_run(config.get("cron"), after, now, tz=zone)
    nxt_utc = nxt.astimezone(UTC) if nxt else None
    return {
        "next_run_at": nxt_utc.isoformat() if nxt_utc else None,
        "timezone": getattr(zone, "key", "UTC"),
        "next_run_local": nxt_utc.astimezone(zone).isoformat() if nxt_utc else None,
        "timezone_warning": warning,
    }


def describe_schedule_history(assignment: object) -> dict[str, str | None]:
    """What a schedule assignment last did, as the ``get_assignment`` tool shows it.

    ``last_triggered_at`` (the last fire) and ``schedule_resumed_at`` (the last
    time it was enabled again) in UTC ISO 8601, and ``skipped_slot``: the latest
    cron occurrence after the last fire (or the creation) and at or before the
    resume, which never ran because the schedule was off and the resume does
    not catch up (:func:`schedule_base`). ``None`` for each value not set.

    >>> from types import SimpleNamespace as N
    >>> t1 = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
    >>> t2 = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    >>> h = describe_schedule_history(N(trigger_config={"cron": "0 7 * * *"},
    ...     last_triggered_at=t1, schedule_resumed_at=t2))
    >>> h["skipped_slot"]
    '2026-11-03T07:00:00+00:00'
    >>> describe_schedule_history(N(trigger_config={"cron": "0 7 * * *"},
    ...     last_triggered_at=t2, schedule_resumed_at=t1))["skipped_slot"] is None
    True
    >>> describe_schedule_history(N())["last_triggered_at"] is None
    True
    """

    def _stamp(name: str) -> datetime | None:
        value = getattr(assignment, name, None)
        return value.astimezone(UTC) if isinstance(value, datetime) else None

    last = _stamp("last_triggered_at")
    resumed = _stamp("schedule_resumed_at")
    skipped: datetime | None = None
    floor = last or _stamp("created_at")
    config = getattr(assignment, "trigger_config", None)
    cron = config.get("cron") if isinstance(config, dict) else None
    if resumed is not None and (last is None or resumed > last) and isinstance(cron, str) and cron.strip():
        from croniter import CroniterBadCronError, croniter

        zone, _ = schedule_timezone(config)
        try:
            if zone is None or zone is UTC:
                prev = croniter(cron.strip(), resumed).get_prev(datetime).astimezone(UTC)
            else:
                wall = resumed.astimezone(zone).replace(tzinfo=None)
                prev = _wall_time_instant(croniter(cron.strip(), wall).get_prev(datetime), zone)
            if floor is None or prev > floor:
                skipped = prev
        except (CroniterBadCronError, ValueError, TypeError, AttributeError):
            skipped = None
    return {
        "last_triggered_at": last.isoformat() if last else None,
        "schedule_resumed_at": resumed.isoformat() if resumed else None,
        "skipped_slot": skipped.isoformat() if skipped else None,
    }


# ---------------------------------------------------------------------------
# Webhook secret helpers (SEC-5)
# ---------------------------------------------------------------------------


def ensure_webhook_secret(
    trigger_type: str | None,
    trigger_config: dict | None,
) -> dict | None:
    """Auto-generate a ``webhook_secret`` for webhook-triggered assignments.

    Assignment webhook endpoints (``POST /api/hooks/assignments/{id}``) are
    otherwise authenticated by URL path only (SEC-5).  When the caller did
    not provide a secret, generate a strong one server-side so every new
    webhook assignment is born with signature verification material.  This
    is the single source of truth used by every Assignment creation path:
    the REST create/update endpoints, agent import, agent duplicate,
    create-from-template, public catalog clone, and the agent builder tool.

    Returns a new dict (never mutates the input) so SQLAlchemy JSON change
    detection works on updates.  A no-op passthrough for non-webhook
    triggers and for configs that already carry a secret.
    """
    if trigger_type != "webhook":
        return trigger_config
    config = dict(trigger_config or {})
    if not config.get("webhook_secret"):
        # Import lazily to keep module import cost minimal.
        import secrets as _secrets

        config["webhook_secret"] = _secrets.token_urlsafe(32)
    return config


def strip_webhook_secret(trigger_config: dict | None) -> dict | None:
    """Return a copy of ``trigger_config`` without the ``webhook_secret``.

    Used by every surface that serialises an assignment's trigger config
    for someone other than its manager: agent export bundles, public
    catalog downloads, and cross-user copies (catalog clone, duplicate of a
    company-managed agent).  The key is removed entirely (not masked) so an
    exported bundle can never carry a live credential and a re-import never
    persists a placeholder string as the secret.
    """
    if not trigger_config or "webhook_secret" not in trigger_config:
        return trigger_config
    return {k: v for k, v in trigger_config.items() if k != "webhook_secret"}


# ---------------------------------------------------------------------------
# MCP server arguments
# ---------------------------------------------------------------------------

_SECRET_ARG_PREFIX_RE = re.compile(r"^(ghp_|gho_|ghs_|sk-|xox[bpas]-|AKIA|eyJ)")
_SECRET_ARG_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_REDACTED = "***"
_NAME_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")
_NAME_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_AUTH_SCHEME_RE = re.compile(r"\b(Bearer|Basic|Token)\s+\S+", re.IGNORECASE)
#: Words of a name (a flag, a ``NAME=value``, a query parameter) that say its
#: value is a credential. A name ending with a reference word names WHERE a
#: credential is (``--token-file``, ``TOKEN_URL``, ``AWS_ACCESS_KEY_ID``), and
#: one with a setting word sizes something (``--max-tokens``, ``TOKEN_TTL``).
_SECRET_NAME_SUBSTRINGS = ("password", "passwd", "secret")
_SECRET_NAME_WORDS = frozenset({"token", "apikey", "credential", "credentials"})
_SECRET_NAME_PAIRS = frozenset(
    {("api", "key"), ("access", "key"), ("private", "key"), ("auth", "key")}
)
_SECRET_WHOLE_NAMES = frozenset({"key", "auth", "pass", "pat", "sig"})
_REFERENCE_NAME_WORDS = frozenset(
    {"file", "path", "dir", "url", "uri", "endpoint", "env", "var", "id", "name", "type", "header"}
)
_SETTING_NAME_WORDS = frozenset(
    {"max", "min", "limit", "count", "length", "size", "budget", "timeout", "ttl", "expiry"}
)


def mask_secret_arg(arg: Any) -> Any:
    """Mask an MCP command-line argument that looks like a token or a secret.

    A known token prefix (``ghp_``, ``sk-``, ``xoxb-``, ``AKIA``, a JWT, ...)
    or a bare alphanumeric string over 30 characters keeps its first
    characters only - how the MCP API shows a server's arguments.
    """
    if not isinstance(arg, str):
        return arg
    if _SECRET_ARG_PREFIX_RE.match(arg):
        return arg[:6] + "***"
    if len(arg) > 30 and _SECRET_ARG_TOKEN_RE.match(arg):
        return arg[:4] + "***"
    return arg


def _names_a_secret(name: str) -> bool:
    words = [
        word.lower()
        for word in _NAME_SEPARATOR_RE.split(_NAME_CAMEL_RE.sub(r"\1_\2", name))
        if word
    ]
    if (
        not words
        or words[-1] in _REFERENCE_NAME_WORDS
        or any(word in _SETTING_NAME_WORDS for word in words)
    ):
        return False
    if len(words) == 1 and words[0] in _SECRET_WHOLE_NAMES:
        return True
    return (
        any(part in word for word in words for part in _SECRET_NAME_SUBSTRINGS)
        or any(word in _SECRET_NAME_WORDS for word in words)
        or any(pair in _SECRET_NAME_PAIRS for pair in pairwise(words))
    )


def _redact_query_pair(pair: str) -> str:
    name, sep, value = pair.partition("=")
    if not sep or not value:
        return pair
    plain = unquote(value)
    if _names_a_secret(unquote(name)) or mask_secret_arg(plain) != plain:
        return f"{name}={_REDACTED}"
    return pair


def _redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, host = netloc.rpartition("@")
        user, colon, password = userinfo.partition(":")
        if colon and password:
            netloc = f"{user}:{_REDACTED}@{host}"
        elif not colon and mask_secret_arg(user) != user:
            netloc = f"{_REDACTED}@{host}"
    path = "/".join(
        _REDACTED if _SECRET_ARG_PREFIX_RE.match(segment) else segment
        for segment in parts.path.split("/")
    )
    query = "&".join(_redact_query_pair(pair) for pair in parts.query.split("&"))
    if (netloc, path, query) == (parts.netloc, parts.path, parts.query):
        return value
    return urlunsplit((parts.scheme, netloc, path, query, parts.fragment))


def redact_secret_text(value: Any) -> Any:
    """An MCP server argument or URL with the credential it carries masked,
    for a surface that hands the server to ANOTHER user (every Filigran AI
    Catalog page, download and copy): a token-shaped value
    (:func:`mask_secret_arg`), the value of a secret-named ``NAME=value``, a
    URL's password, token path segment and secret query values, and the
    token of an ``Authorization`` scheme. Anything else is returned as is."""
    if not isinstance(value, str):
        return value
    masked = mask_secret_arg(value)
    if masked != value:
        return masked
    name, sep, rest = value.partition("=")
    if sep and name and "://" not in name and not any(ch.isspace() for ch in name):
        if rest and _names_a_secret(name):
            return f"{name}={_REDACTED}"
        redacted = redact_secret_text(rest)
        return value if redacted == rest else f"{name}={redacted}"
    if "://" in value:
        return _redact_url(value)
    return _AUTH_SCHEME_RE.sub(lambda m: f"{m.group(1)} {_REDACTED}", value)


def redact_secret_args(args: Any) -> Any:
    """``args`` through :func:`redact_secret_text`, plus the value that
    follows a secret-named flag (``--token <value>``); anything that is not a
    list (``None`` included) is returned unchanged."""
    if not isinstance(args, list):
        return args
    out: list = []
    after_secret_flag = False
    for arg in args:
        if after_secret_flag and isinstance(arg, str) and not arg.startswith("-"):
            out.append(_REDACTED)
            after_secret_flag = False
            continue
        after_secret_flag = (
            isinstance(arg, str)
            and arg.startswith("-")
            and "=" not in arg
            and _names_a_secret(arg)
        )
        out.append(redact_secret_text(arg))
    return out


class MaskedArgumentError(ValueError):
    """A masked value sent back cannot be put back to one stored value."""


def _shown_keys(args: list) -> list:
    """What tells each argument of a list apart as the MCP API shows it: the
    argument itself, or - for the bare ``***`` that hides the value of every
    secret-named flag alike - the argument before it, with it. ``None`` for an
    entry that is not a string."""
    keys: list = []
    for i, arg in enumerate(args):
        if not isinstance(arg, str):
            keys.append(None)
        elif arg == _REDACTED:
            before = args[i - 1] if i else None
            keys.append((before if isinstance(before, str) else None, arg))
        else:
            keys.append(arg)
    return keys


def restore_masked_args(incoming: list[str], stored: Any) -> list[str]:
    """``incoming`` with each masked argument put back to the stored value it stands for.

    The MCP API shows a server's arguments through :func:`redact_secret_args`,
    so a client that edits another field and sends them back as it read them
    would otherwise overwrite every credential with its mask. An argument is
    recognised by how it was shown: its masked text (``ghp_xx***``,
    ``NAME=***``, ``--token=***``, a URL, ``Bearer ***``), or for the bare
    ``***`` after a secret-named flag that flag followed by ``***`` - so
    ``--token *** --client-secret ***`` keeps its two values apart, and each
    pair may move. A match gets its stored value back. When several distinct
    stored values are shown the same way, the n-th occurrence gets the n-th of
    them, which holds only while every one of them is still sent.

    :class:`MaskedArgumentError` is raised rather than saving the wrong value
    or a mask: when such a shared form is sent, but not as many times as it is
    stored (sent none of those times, every one of them was replaced), and,
    once the server has a masked argument, when an argument still
    carrying ``***`` matches no stored one (a ``***`` moved after another flag,
    a value edited around its mask). Anything else - a new or retyped value -
    is kept as sent.
    """
    if not isinstance(stored, list):
        return incoming
    shown = redact_secret_args(stored)
    shown_keys = _shown_keys(shown)
    masked = {
        key
        for key, raw, seen in zip(shown_keys, stored, shown, strict=True)
        if key is not None and seen != raw
    }
    if not masked:
        return incoming
    by_key: dict[Any, list[str]] = {}
    for key, raw in zip(shown_keys, stored, strict=True):
        if key in masked:
            by_key.setdefault(key, []).append(raw)
    incoming_keys = _shown_keys(incoming)
    for key, values in by_key.items():
        # None of them sent any more: every one was replaced, nothing to put back.
        if len(set(values)) > 1 and incoming_keys.count(key) not in (0, len(values)):
            raise MaskedArgumentError
    stored_values = {raw for raw in stored if isinstance(raw, str)}
    sent: dict[Any, int] = {}
    restored: list[str] = []
    for arg, key in zip(incoming, incoming_keys, strict=True):
        values = by_key.get(key)
        if values is None:
            if isinstance(arg, str) and _REDACTED in arg and arg not in stored_values:
                raise MaskedArgumentError
            restored.append(arg)
            continue
        occurrence = sent.get(key, 0)
        sent[key] = occurrence + 1
        restored.append(values[min(occurrence, len(values) - 1)])
    return restored


#: The URL fields of an MCP server the MCP API shows through
#: :func:`redact_secret_text`, with how a refusal names each one.
MCP_SERVER_URL_LABELS = {
    "url": "URL",
    "authorize_url": "authorization URL",
    "token_url": "token URL",
    "registration_url": "registration URL",
}


def restore_masked_text(incoming: Any, stored: Any) -> Any:
    """``incoming`` - an MCP server URL sent back - as the stored value when it
    is that value as :func:`redact_secret_text` shows it.

    :class:`MaskedArgumentError` when the stored value is masked and
    ``incoming`` still carries ``***`` without being its masked form (the host
    edited around a masked password): saving it would replace the credential
    with the mask. Anything else is kept as sent.
    """
    if not isinstance(incoming, str) or not isinstance(stored, str):
        return incoming
    shown = redact_secret_text(stored)
    if shown == stored:
        return incoming
    if incoming == shown:
        return stored
    if _REDACTED in incoming:
        raise MaskedArgumentError
    return incoming


# ---------------------------------------------------------------------------
# Chat in-flight turn marker
# ---------------------------------------------------------------------------


def chat_processing_key(conversation_id: object) -> str:
    """Redis key marking a conversation's in-flight chat turn.

    Single source of truth for the ``chat:processing:{conversation_id}``
    key format. Setters (the web chat's turn runner and the public chat
    widget) and readers (conversation ``is_processing`` probes and the
    stale-run sweep's agent_chat liveness probe) must all build the
    key through this helper — a silent format drift between the setter and
    the sweep would misclassify live chat turns as orphaned runs.
    """
    return f"chat:processing:{conversation_id}"


# ---------------------------------------------------------------------------
# Platform languages (i18n)
# ---------------------------------------------------------------------------

# "auto" = resolve from browser detection (platform level) or inherit the
# platform language (user level). Single source of truth for the backend;
# the frontend mirror lives in ``frontend/src/i18n/index.tsx``.
LANGUAGE_AUTO = "auto"

# Concrete UI locales the platform ships translations for.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en-us", "fr-fr", "es-es", "ja-jp")


def normalize_language(value: object) -> str | None:
    """Validate a platform/user language value.

    Returns the normalized (lowercased) locale code, ``LANGUAGE_AUTO``, or
    ``None`` when the value is not a supported language.
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized == LANGUAGE_AUTO or normalized in SUPPORTED_LANGUAGES:
        return normalized
    return None


# ---------------------------------------------------------------------------
# Text columns: copy names and written values
# ---------------------------------------------------------------------------


def _column_length(column: Any) -> int | None:
    return getattr(column.type, "length", None)


#: Code points that continue the character before them, besides the combining
#: marks (UAX #29 Extend and SpacingMark): zero-width (non-)joiners, emoji
#: skin-tone modifiers and tag characters, halfwidth katakana voiced marks,
#: Thai and Lao sara am, Hangul vowel and final jamo.
_CONTINUING_RANGES = (
    (0x200C, 0x200D),
    (0x1F3FB, 0x1F3FF),
    (0xE0020, 0xE007F),
    (0xFF9E, 0xFF9F),
    (0x0E33, 0x0E33),
    (0x0EB3, 0x0EB3),
    (0x1160, 0x11FF),
    (0xD7B0, 0xD7FF),
)
#: Code points that begin a character with the one after them (UAX #29 Prepend,
#: Unicode 16, the version of Python 3.14's ``unicodedata``).
_PREPEND_RANGES = (
    (0x0600, 0x0605),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x0D4E, 0x0D4E),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x111C2, 0x111C3),
    (0x113D1, 0x113D1),
    (0x1193F, 0x1193F),
    (0x11941, 0x11941),
    (0x11A3A, 0x11A3A),
    (0x11A84, 0x11A89),
    (0x11D46, 0x11D46),
    (0x11F02, 0x11F02),
)
_HANGUL_INITIALS = ((0x1100, 0x115F), (0xA960, 0xA97F))
_HANGUL_SYLLABLES = ((0xAC00, 0xD7A3),)
_REGIONAL_INDICATORS = ((0x1F1E6, 0x1F1FF),)


def _in_ranges(character: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    code = ord(character)
    return any(low <= code <= high for low, high in ranges)


def _splits_character(text: str, index: int) -> bool:
    """Whether ``text[:index]`` would end inside one user-perceived character."""
    before, after = text[index - 1], text[index]
    if unicodedata.category(after) in ("Mn", "Mc", "Me") or _in_ranges(after, _CONTINUING_RANGES):
        return True
    if before == "\u200d" or (before, after) == ("\r", "\n") or _in_ranges(before, _PREPEND_RANGES):
        return True
    # A virama links the next letter into a conjunct (Devanagari ksha).
    if unicodedata.combining(before) == 9 and unicodedata.category(after).startswith("L"):
        return True
    if _in_ranges(before, _HANGUL_INITIALS) and _in_ranges(after, _HANGUL_INITIALS + _HANGUL_SYLLABLES):
        return True
    if not _in_ranges(after, _REGIONAL_INDICATORS):
        return False
    pending = 0  # regional indicators pair into flags
    while index - pending > 0 and _in_ranges(text[index - pending - 1], _REGIONAL_INDICATORS):
        pending += 1
    return pending % 2 == 1


def fit_copy_name(original: str, column: Any, *, prefix: str = "", suffix: str = "") -> str:
    """*prefix* + *original* + *suffix*, fitted to the ``String(n)`` *column*.

    Only *original* is shortened, so a copy still reads as one (``Copy of ...``,
    ``... (copy)``); a name that fits is returned unchanged. PostgreSQL refuses
    a longer value at the INSERT, which the copy paths answered as a 500. It
    counts code points, like ``len``; the cut moves back to the start of the
    character it would split (an accent, a conjunct, an emoji sequence, a
    flag), and the spaces it leaves at the end of the kept part are dropped.
    A first character longer than the whole room has no start to move back
    to: it is cut on a code point rather than dropping the original name.
    """
    limit = _column_length(column)
    if limit is None:
        return f"{prefix}{original}{suffix}"
    room = max(limit - len(prefix) - len(suffix), 0)
    if len(original) <= room:
        return f"{prefix}{original}{suffix}"
    cut = room
    while cut > 0 and _splits_character(original, cut):
        cut -= 1
    kept = original[: cut or room]
    return f"{prefix}{kept.rstrip() or kept}{suffix}"


def text_column_error(column: Any, value: object, *, field: str | None = None) -> str | None:
    """Why *value* cannot be written to the text *column* as sent, else ``None``.

    ``None`` is left to the caller (the column's nullability or its default);
    anything else must be a string, and no longer than a ``String(n)`` column
    holds - PostgreSQL refuses the rest at the write. *field* names the value
    in the message when it is not the column's own name.
    """
    if value is None:
        return None
    name = field or column.name
    if not isinstance(value, str):
        return f"{name} must be a string, got {type(value).__name__}."
    limit = _column_length(column)
    if limit is not None and len(value) > limit:
        return f"{name} must be at most {limit} characters (got {len(value)})."
    return None


# ---------------------------------------------------------------------------
# Iterable batching
# ---------------------------------------------------------------------------


def chunked[T](seq: Sequence[T], size: int) -> Iterator[list[T]]:
    """Yield successive ``size``-length chunks of *seq* (the last may be shorter).

    A dependency-free, version-agnostic stand-in for ``itertools.batched`` (whose
    ``strict`` keyword is Python 3.13-only). Yields ``list`` slices so callers can
    feed each chunk straight into a SQLAlchemy ``IN (...)`` clause — primarily to
    keep bulk queries under the driver bind-parameter ceiling on large deletes.
    """
    if size < 1:
        raise ValueError("size must be >= 1")
    for start in range(0, len(seq), size):
        yield list(seq[start : start + size])
