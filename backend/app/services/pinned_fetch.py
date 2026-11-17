# SPDX-License-Identifier: Apache-2.0

"""Fetch a URL someone typed, connected to the address the policy checked.

A built pack's free HTTP source is a URL template its author wrote, run by
whoever runs the pack: the host is hostile until proven otherwise. Every
request goes through the shared pinned sender (`app.core.pinned_request`):
the host is resolved once, every address must be public
(`destination_policy.forbidden_address_reason`, metadata endpoints refused
whatever the policy), and the request goes to that address with the original
name as `Host` and as the TLS server name, so the certificate is still checked
against the host the template names. Its client keeps no connection for reuse:
a redirect to another name on the same address opens a new
connection, whose certificate is checked against that name.

What else a hostile server cannot decide here: proxies from the environment
(`trust_env=False`), how long it may keep the connection (`timeout`), how many
bytes are read (a streamed, uncompressed read capped on raw bytes), what kind
of document is taken (text, JSON or XML only), and where a redirect leads
(each hop resolved, checked and pinned the same way, a bounded number of
times).

A URL is parsed once, by `httpx.URL` (:func:`fetched_url`), the parser the
sender connects with: the host the pack picker names
(`investigation_packs._fetched_host`) and the credential refusal read the
same parse, so no two parsers can disagree on which host is meant.

A refusal raises `PinnedFetchRefusedError` with the precise reason, for the logs
only: what reaches the caller is generic, so the fetch is never an oracle on
which internal names resolve where.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from app.core.pinned_request import PinnedDestinationError, pinned_client, send_pinned

#: Redirects followed before the fetch gives up.
MAX_REDIRECTS = 3
#: Raw bytes read from a response before reading stops (`capped`).
MAX_BYTES = 512 * 1024
#: Media types taken: a free source is read as text, never rendered.
ACCEPTED_TYPES = ("text/", "application/json", "application/xml", "application/ld+json")
ACCEPTED_SUFFIXES = ("+json", "+xml")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
USER_AGENT = "XTM-One-DeepInvestigation/1.0"


class PinnedFetchRefusedError(Exception):
    """The fetch was refused or failed; ``reason`` is for the logs only."""

    def __init__(self, reason: str, *, kind: str = "refused", status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        #: refused (policy), unreachable, timeout, status, type, too_many_redirects
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class PinnedResponse:
    url: str
    status_code: int
    content_type: str
    text: str
    capped: bool


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def accepted_type(content_type: str) -> bool:
    media = _media_type(content_type)
    return media.startswith(ACCEPTED_TYPES) or media.endswith(ACCEPTED_SUFFIXES)


def fetched_url(url: str | httpx.URL) -> httpx.URL:
    """*url* as the fetch parses it: `httpx.URL`, the parser `send_pinned`
    connects with, so the host checked here is the host connected to. http or
    https, a host, no credentials; anything else is refused before any
    resolution."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise PinnedFetchRefusedError(f"invalid URL: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise PinnedFetchRefusedError(f"scheme {parsed.scheme!r} is not http or https")
    if not parsed.raw_host:
        raise PinnedFetchRefusedError("no host")
    if parsed.userinfo:
        raise PinnedFetchRefusedError("credentials in the URL")
    return parsed


def fetched_host(url: httpx.URL) -> str:
    """The host of a :func:`fetched_url` as it is sent in `Host`: the ASCII
    (IDNA) form, an IPv6 literal bracketed, the port when it is not the
    scheme's default."""
    host = url.raw_host.decode("ascii")
    named = f"[{host}]" if ":" in host else host
    return named if url.port is None else f"{named}:{url.port}"


async def _read(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    buf = bytearray()
    async for chunk in response.aiter_raw():
        room = max_bytes - len(buf)
        if len(chunk) > room:
            buf += chunk[:room]
            return bytes(buf), True
        buf += chunk
    return bytes(buf), False


async def fetch_pinned(url: str, *, timeout: float = 20.0, max_bytes: int = MAX_BYTES) -> PinnedResponse:
    """GET ``url`` under the policy above; the response text, decoded.

    ``timeout`` bounds the whole fetch - resolution, every redirect and the
    read - not each operation: httpx's own timeout is per read, which a server
    sending a byte at a time would never trip."""
    try:
        async with asyncio.timeout(timeout):
            return await _fetch(url, timeout=timeout, max_bytes=max_bytes)
    except TimeoutError as exc:
        raise PinnedFetchRefusedError(f"{url!r} not read in {timeout}s", kind="timeout") from exc


async def _fetch(url: str, *, timeout: float, max_bytes: int) -> PinnedResponse:
    current = fetched_url(url)
    # Uncompressed: a compressed body read raw would be garbage, and
    # decompressing it would let the server decide how many bytes are held.
    headers = {"Accept-Encoding": "identity", "User-Agent": USER_AGENT}
    async with pinned_client(timeout=timeout, headers=headers) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                response = await send_pinned(client, "GET", current, stream=True)
            except PinnedDestinationError as exc:
                raise PinnedFetchRefusedError(exc.reason, kind=exc.kind) from exc
            except (httpx.TimeoutException, TimeoutError) as exc:
                raise PinnedFetchRefusedError(f"no response from {current!r} in {timeout}s", kind="timeout") from exc
            except httpx.HTTPError as exc:
                raise PinnedFetchRefusedError(f"{current!r}: {exc!r}"[:300], kind="unreachable") from exc
            try:
                if response.status_code in _REDIRECTS and "location" in response.headers:
                    try:
                        target = current.join(response.headers["location"])
                    except httpx.InvalidURL as exc:
                        raise PinnedFetchRefusedError(f"{current!r} redirected to an invalid URL: {exc}") from exc
                    current = fetched_url(target)
                    continue
                if response.status_code >= 400:
                    raise PinnedFetchRefusedError(
                        f"{current!r} answered {response.status_code}", kind="status", status=response.status_code
                    )
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise PinnedFetchRefusedError(f"{current!r} sent a {encoding!r} body", kind="type")
                content_type = response.headers.get("content-type", "")
                if not accepted_type(content_type):
                    raise PinnedFetchRefusedError(f"{current!r} sent {content_type!r}", kind="type")
                try:
                    body, capped = await _read(response, max_bytes)
                except (httpx.TimeoutException, TimeoutError) as exc:
                    raise PinnedFetchRefusedError(f"{current!r} stalled while sending", kind="timeout") from exc
                except httpx.HTTPError as exc:
                    raise PinnedFetchRefusedError(f"{current!r}: {exc!r}"[:300], kind="unreachable") from exc
                return PinnedResponse(
                    url=str(current),
                    status_code=response.status_code,
                    content_type=content_type,
                    text=body.decode(response.encoding or "utf-8", errors="replace"),
                    capped=capped,
                )
            finally:
                await response.aclose()
    raise PinnedFetchRefusedError(f"more than {MAX_REDIRECTS} redirects from {url!r}", kind="too_many_redirects")
