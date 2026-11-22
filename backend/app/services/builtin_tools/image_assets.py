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

"""``fetch_image_asset`` - a public image becomes a conversation file in one call.

Before this tool an agent that needed a logo from the web drove the headless
browser to the URL, ran ``canvas.toDataURL()`` in an ``evaluate`` call and
got a 200 KB base64 blob back into its context - six calls, and a result the
model cannot read. The tool downloads the image over the SAME outbound policy
as ``web_fetch`` (``validate_outbound_url_async`` on the URL and on every
redirect hop - http(s) only, globally routable addresses only - and every
connection pinned to a checked address, ``guarded_client_options``), bounded by
:data:`MAX_IMAGE_ASSET_BYTES` and an allow-list of image MIME types, types a
raster by its magic number rather than by the header the server sent
(:func:`sniff_raster_type`), sanitises an SVG through the one template-asset
SVG path, stores the bytes as a
conversation / run file through :func:`persist_tool_artifact` (the single
persistence path of every tool-produced file, byte-identical downloads reuse
their row) and answers with the ``file_id``, the dimensions and the visual
summary line - the handle every image-taking block, ``profiles`` /
``logo_grid`` / ``logo_wall`` / ``image``, and ``attach_template_asset``
accept. A logo's trim box is read here and stored with the file.

Downloads are paced per host: a deck brief naming twenty logos arrives
as ONE parallel batch, and seventeen simultaneous requests to
upload.wikimedia.org were all answered HTTP 429. At most
:data:`MAX_CONCURRENT_DOWNLOADS_PER_HOST` run at once against one host, and a
host that answers 429 is refused without a request until its ``Retry-After``
(:func:`record_host_throttle`) - reported as ``ToolResult.target_throttled`` so
the loop never withdraws the tool, and with it every other host. A host is one
key whatever the URL spells (:func:`host_key`). The pace and the throttle
memory are process-wide on purpose: every run on the process leaves through
the same address, so one user's 429 closes the host for all of them; each
process (a replica, the worker) learns on its own.
"""

from __future__ import annotations

import functools
import ipaddress
import mimetypes
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from app.core.utils import format_exception_message
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._shared import format_file_size, logger, persist_tool_artifact

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.tools.base import FileContext

#: Ceiling on one fetched image. A logo, a portrait or a diagram fits many
#: times over; a file above this is not an asset for a slide.
MAX_IMAGE_ASSET_BYTES = 5 * 1024 * 1024

#: The image types a slide or a template can carry, keyed by the MIME type the
#: server declares (parameters stripped). ``image/jpg`` is the common
#: misspelling of ``image/jpeg``.
ALLOWED_IMAGE_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}

#: The icon types the download also takes (a vendor's ``favicon.ico`` is
#: often its only public logo). A slide cannot carry ICO, so the bytes are
#: converted to PNG (:func:`ico_to_png`) before anything is stored. The
#: registered ``.ico`` type is read from the standard library's built-in
#: table (no system file), beside the common ``image/x-icon``.
ICON_IMAGE_TYPES: dict[str, str] = {
    mime: ".ico" for mime in ("image/x-icon", mimetypes.MimeTypes().guess_type("icon.ico")[0]) if mime
}
_ICO_MAGIC = b"\x00\x00\x01\x00"
#: Past this an icon is not decoded (an ICO frame is at most 256x256; a PNG
#: frame may claim more).
_MAX_ICON_PIXELS = 1024 * 1024

#: HTTP timeout for the download (seconds).
FETCH_TIMEOUT_SECONDS = 30

#: At most this many downloads in flight against ONE host, and new ones start
#: at most :data:`DOWNLOAD_STARTS_PER_SECOND_PER_HOST` a second after the first
#: few. Process-wide (the shared integration limiter): every run on the process
#: leaves through the same address, and that address is what a host throttles.
MAX_CONCURRENT_DOWNLOADS_PER_HOST = 3
DOWNLOAD_STARTS_PER_SECOND_PER_HOST = 2.0

#: How long a host that answered HTTP 429 is left alone when it gave no usable
#: ``Retry-After``, and the longest a ``Retry-After`` may hold it - a bad
#: header must not take a host away for a day.
DEFAULT_HOST_THROTTLE_SECONDS = 300
MAX_HOST_THROTTLE_SECONDS = 900
_MAX_THROTTLED_HOSTS = 256
#: Longest delay-seconds value read as a number; a longer one is a wait past
#: the ceiling anyway, and converting it could hit Python's int-string limit.
_MAX_RETRY_AFTER_DIGITS = 9
_MAX_CONTENT_LENGTH_DIGITS = 15

_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_SVG_ROOT_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)
_SVG_ATTR_RE = re.compile(r'\b(width|height|viewBox)\s*=\s*"([^"]*)"', re.IGNORECASE)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: The extensions a caller's ``name`` may carry that the DECODED type replaces
#: when the two disagree (``logo.png`` for JPEG bytes is stored as ``logo.jpg``,
#: never ``logo.png.jpg``).
_IMAGE_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico")


def sniff_raster_type(data: bytes) -> str | None:
    """The allow-listed RASTER type the bytes' magic number spells, or ``None``.

    The stored MIME type comes from here, never from the ``Content-Type`` a
    server declared: a PNG header over JPEG bytes is common (a CDN rewriting
    formats behind one URL), and a file labelled with the wrong type is a
    picture the export and the download route describe wrongly. A format
    Pillow decodes but the allow-list does not carry (BMP, TIFF, ICO) is
    ``None`` - the slide cannot carry it either.
    """
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def ico_to_png(data: bytes) -> bytes | None:
    """The largest frame of an ICO file as PNG bytes, or ``None``.

    CPU-bound (call it through ``run_parse``). Decoded as ICO only, under
    :data:`_MAX_ICON_PIXELS`; any decoding failure is ``None``.
    """
    import io

    from PIL import Image

    if not data.startswith(_ICO_MAGIC):
        return None
    try:
        with Image.open(io.BytesIO(data), formats=["ICO"]) as opened:
            width, height = opened.size
            if width < 1 or height < 1 or width * height > _MAX_ICON_PIXELS:
                return None
            opened.load()
            out = io.BytesIO()
            opened.convert("RGBA").save(out, format="PNG")
            return out.getvalue()
    except Exception:
        return None


class ImageAssetError(ValueError):
    """The fetch could not produce an image asset; the message is the tool's answer."""


@dataclass(frozen=True)
class HostThrottle:
    """A host that answered HTTP 429, and until when it is left alone."""

    host: str
    since: datetime
    until: datetime
    deadline: float
    retry_after: bool


class HostThrottledError(ImageAssetError):
    """The host is throttling the platform: the 429 just received, or one still in force."""

    def __init__(self, message: str, throttle: HostThrottle) -> None:
        super().__init__(message)
        self.throttle = throttle


_THROTTLED_HOSTS: dict[str, HostThrottle] = {}


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """The seconds a ``Retry-After`` header asks for, or ``None`` when it asks nothing usable.

    Both RFC 9110 forms: delay-seconds (``120``) and an HTTP-date. Zero, a date
    already past and an unparseable value are ``None`` (the caller's default
    applies); a longer wait than :data:`MAX_HOST_THROTTLE_SECONDS` is cut to it.
    Never raises: the header is the server's, and delay-seconds is ASCII digits
    only (``str.isdigit`` also accepts U+00B2, one latin-1 byte ``int`` refuses).
    """
    text = (value or "").strip()
    if not text:
        return None
    if text.isascii() and text.isdigit():
        seconds = float(int(text)) if len(text) <= _MAX_RETRY_AFTER_DIGITS else float(MAX_HOST_THROTTLE_SECONDS)
    else:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if seconds <= 0:
        return None
    return min(seconds, float(MAX_HOST_THROTTLE_SECONDS))


def host_throttle(host: str) -> HostThrottle | None:
    """The throttle still in force on *host*, or ``None`` (an expired one is dropped)."""
    entry = _THROTTLED_HOSTS.get(host)
    if entry is None:
        return None
    if entry.deadline <= time.monotonic():
        _THROTTLED_HOSTS.pop(host, None)
        return None
    return entry


def record_host_throttle(host: str, retry_after: str | None) -> HostThrottle:
    """Remember that *host* answered HTTP 429 and return the throttle in force.

    Only ever extends a throttle already held: a parallel download's shorter
    ``Retry-After`` must not reopen the host early.
    """
    now = datetime.now(UTC)
    now_mono = time.monotonic()
    asked = retry_after_seconds(retry_after, now=now)
    seconds = asked if asked is not None else float(DEFAULT_HOST_THROTTLE_SECONDS)
    current = host_throttle(host)
    if current is not None and current.deadline >= now_mono + seconds:
        return current
    if current is None and len(_THROTTLED_HOSTS) >= _MAX_THROTTLED_HOSTS:
        for key in [k for k, v in _THROTTLED_HOSTS.items() if v.deadline <= now_mono]:
            del _THROTTLED_HOSTS[key]
        if len(_THROTTLED_HOSTS) >= _MAX_THROTTLED_HOSTS:
            del _THROTTLED_HOSTS[min(_THROTTLED_HOSTS, key=lambda k: _THROTTLED_HOSTS[k].deadline)]
    entry = HostThrottle(
        host=host,
        since=current.since if current is not None else now,
        until=now + timedelta(seconds=seconds),
        deadline=now_mono + seconds,
        retry_after=asked is not None,
    )
    _THROTTLED_HOSTS[host] = entry
    return entry


def _clock(moment: datetime, *, round_up: bool = False) -> str:
    if round_up and (moment.second or moment.microsecond):
        moment = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return moment.strftime("%H:%M UTC")


_ELSEWHERE = "Take the image from another source (its owner's own website or press kit) or go on without it."


def _throttled_now_message(throttle: HostThrottle, url: str) -> str:
    reason = (
        "as its Retry-After asks"
        if throttle.retry_after
        else f"it gave no Retry-After, so the platform waits {DEFAULT_HOST_THROTTLE_SECONDS // 60} min"
    )
    return (
        f"{throttle.host} answered HTTP 429 (Too Many Requests) for {url}: it is throttling downloads "
        f"from this platform, and nothing was stored. Every fetch from {throttle.host} is refused "
        f"without a request until about {_clock(throttle.until, round_up=True)} ({reason}). {_ELSEWHERE}"
    )


def _still_throttled_message(throttle: HostThrottle, url: str) -> str:
    return (
        f"not fetched: {url} - {throttle.host} has been throttling downloads from this platform since "
        f"{_clock(throttle.since)} (HTTP 429), and every fetch from it is refused without a request "
        f"until about {_clock(throttle.until, round_up=True)}. {_ELSEWHERE}"
    )


def _refuse_if_throttled(host: str, url: str) -> None:
    throttle = host_throttle(host)
    if throttle is not None:
        raise HostThrottledError(_still_throttled_message(throttle, url), throttle)


def host_key(url: str) -> str:
    """The key one server is paced and throttled under, whatever *url* spells.

    Lowercase and IDNA-encoded (a Unicode name is its ``xn--`` form), without
    the dot of a fully-qualified name, an IP literal in its canonical form, and
    without the port: a throttle is the server's, whatever port it answered on.
    Never raises; *url* has already passed the outbound policy.
    """
    import httpx

    try:
        host = httpx.URL(url).raw_host.decode("ascii")
    except (httpx.InvalidURL, ValueError):
        host = urlparse(url).hostname or ""
    host = host.rstrip(".").lower()
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host


def _host_slot(host: str, *, admit: Callable[[], None]) -> AbstractAsyncContextManager[None]:
    """One paced download slot on *host*. *admit* runs once the slot is held and
    before its start is waited for (:meth:`RateLimiter.slot`)."""
    from app.integrations.http import get_shared_rate_limiter

    return get_shared_rate_limiter(
        f"fetch_image_asset:{host}",
        rate=DOWNLOAD_STARTS_PER_SECOND_PER_HOST,
        capacity=MAX_CONCURRENT_DOWNLOADS_PER_HOST,
        max_concurrency=MAX_CONCURRENT_DOWNLOADS_PER_HOST,
    ).slot(admit=admit)


def normalize_asset_filename(name: str | None, url: str, content_type: str) -> str:
    """The stored filename: the caller's *name* (or the URL's last segment),
    made safe and given the extension of the MIME type when it has none."""
    candidate = (name or "").strip()
    if not candidate:
        candidate = urlparse(url).path.rsplit("/", 1)[-1] or "image"
    candidate = _SAFE_NAME_RE.sub("-", candidate).strip("-.") or "image"
    extension = ALLOWED_IMAGE_TYPES.get(content_type, "")
    lowered = candidate.lower()
    if extension and not lowered.endswith(extension) and not (extension == ".jpg" and lowered.endswith(".jpeg")):
        # A name spelling ANOTHER image type (the URL said .png, the bytes are
        # JPEG) is re-typed rather than double-suffixed.
        for known in _IMAGE_EXTENSIONS:
            if lowered.endswith(known):
                candidate = candidate[: -len(known)] or "image"
                break
        candidate = f"{candidate}{extension}"
    return candidate[:200]


def declared_image_type(content_type: str | None) -> str | None:
    """The allow-listed MIME type a response declares, or ``None``."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return base if base in ALLOWED_IMAGE_TYPES else None


def svg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width x height an SVG declares (``width`` / ``height``, else ``viewBox``)."""
    head = data[:4096].decode("utf-8", errors="ignore")
    root = _SVG_ROOT_RE.search(head)
    if root is None:
        return None
    attrs = {key.lower(): value for key, value in _SVG_ATTR_RE.findall(root.group(0))}

    def _px(value: str | None) -> int | None:
        if not value:
            return None
        match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(px)?\s*$", value)
        return int(float(match.group(1))) if match else None

    width, height = _px(attrs.get("width")), _px(attrs.get("height"))
    if width and height:
        return width, height
    box = attrs.get("viewbox")
    if box:
        parts = box.replace(",", " ").split()
        if len(parts) == 4:
            try:
                return int(float(parts[2])), int(float(parts[3]))
            except ValueError:
                return None
    return None


async def download_public_image(
    url: str,
    *,
    allowed_types: Mapping[str, str] = ALLOWED_IMAGE_TYPES,
    max_bytes: int = MAX_IMAGE_ASSET_BYTES,
    noun: str = "image",
) -> tuple[bytes, str, str]:
    """Download *url* under the outbound policy. Returns ``(bytes, mime, final url)``.

    *allowed_types*, *max_bytes* and *noun* (the word the refusals use) let
    another media kind share the same guarded, paced download: a deck's
    ``video`` block imports an MP4 / WebM link through it.

    Raises :class:`ImageAssetError` with the message the model reads for every
    refusal: a URL the policy blocks, a redirect chain too long, a non-image
    content type, a body over the ceiling, an HTTP failure - and
    :class:`HostThrottledError` for a host that answers HTTP 429 or is
    still throttling the platform, checked on every hop before the host's
    paced slot is asked for, once it is held, and once its start is granted
    (a 429 may land while the call queues). A hop gives its slot back before
    the next host's is asked for, so a download never holds one host while it
    waits for another.
    """
    import httpx

    from app.services.web_fetch import USER_AGENT, guarded_client_options, validate_outbound_url_async

    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        raise ImageAssetError("url must start with http:// or https://")
    try:
        safe_url = await validate_outbound_url_async(url.strip())
    except ValueError as exc:
        raise ImageAssetError(f"url refused: {exc}") from None

    try:
        # Each connection goes to an address the policy checked when it opens
        # (DNS rebinding), every redirect hop included.
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT, "Accept": f"{noun}/*"},
            **guarded_client_options(allow_private=False),
        ) as client:
            hops = 0
            while True:
                host = host_key(safe_url)
                admit = functools.partial(_refuse_if_throttled, host, safe_url)
                admit()
                async with _host_slot(host, admit=admit):
                    admit()
                    async with client.stream("GET", safe_url) as resp:
                        if resp.status_code not in _REDIRECT_STATUSES or "location" not in resp.headers:
                            return await _read_image(
                                resp, host, safe_url, allowed_types=allowed_types, max_bytes=max_bytes, noun=noun
                            )
                        next_url = urljoin(str(resp.request.url), resp.headers["location"])
                hops += 1
                if hops > _MAX_REDIRECTS:
                    raise ImageAssetError(f"too many redirects (more than {_MAX_REDIRECTS})")
                try:
                    safe_url = await validate_outbound_url_async(next_url)
                except ValueError as exc:
                    raise ImageAssetError(f"redirect refused: {exc}") from None
    except ImageAssetError:
        raise
    except httpx.HTTPError as exc:
        raise ImageAssetError(f"could not download {safe_url}: {format_exception_message(exc)}") from None


async def _read_image(
    resp: httpx.Response,
    host: str,
    url: str,
    *,
    allowed_types: Mapping[str, str] = ALLOWED_IMAGE_TYPES,
    max_bytes: int = MAX_IMAGE_ASSET_BYTES,
    noun: str = "image",
) -> tuple[bytes, str, str]:
    """The media a final (non-redirect) answer carries, or the refusal it amounts to."""
    if resp.status_code == 429:
        throttle = record_host_throttle(host, resp.headers.get("retry-after"))
        logger.warning(
            "fetch_image_asset: %s answered HTTP 429 - refusing it until %s (%s)",
            host,
            throttle.until.isoformat(timespec="seconds"),
            "Retry-After" if throttle.retry_after else "default",
        )
        raise HostThrottledError(_throttled_now_message(throttle, url), throttle)
    if resp.status_code >= 400:
        raise ImageAssetError(f"the server answered HTTP {resp.status_code} for {url}")
    declared_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    mime = declared_type if declared_type in allowed_types else None
    if mime is None:
        article = "an" if noun[:1] in "aeiou" else "a"
        raise ImageAssetError(
            f"{url} is not {article} {noun} the platform accepts (Content-Type "
            f"{resp.headers.get('content-type', 'missing')!r}; accepted: "
            f"{', '.join(sorted(allowed_types))})"
        )
    declared = resp.headers.get("content-length") or ""
    # The server's header: ASCII digits only and a bounded length before int();
    # an absurd value is left to the streaming count below.
    if (
        declared.isascii()
        and declared.isdigit()
        and len(declared) <= _MAX_CONTENT_LENGTH_DIGITS
        and int(declared) > max_bytes
    ):
        raise ImageAssetError(
            f"the {noun} is {format_file_size(int(declared))}, above the {format_file_size(max_bytes)} ceiling"
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ImageAssetError(f"the {noun} is larger than the {format_file_size(max_bytes)} ceiling")
        chunks.append(chunk)
    return b"".join(chunks), mime, str(resp.url)


def build_fetch_image_asset_tool(
    *,
    db: AsyncSession | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
    executor: CallableToolExecutor | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build ``fetch_image_asset``. Without a persistence context the tool is
    still defined (the catalog lists it) and refuses at call time. *executor*
    lets a suite builder register the handler on its own executor."""

    async def _fetch_image_asset(
        url: str,
        name: str | None = None,
        file_context: FileContext | None = None,
    ) -> ToolResult:
        from app.services.image_geometry import image_geometry
        from app.services.image_summary import format_visual_summary, summarize_image
        from app.services.svg_sanitizer import SvgSanitizeError, sanitize_template_asset

        if db is None or not (user_id or conversation_id or assignment_run_id):
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: fetch_image_asset needs a conversation or run to store the "
                    "file in, and this run has none."
                ),
                is_error=True,
            )
        try:
            data, mime, final_url = await download_public_image(
                url, allowed_types={**ALLOWED_IMAGE_TYPES, **ICON_IMAGE_TYPES}
            )
        except HostThrottledError as exc:
            return ToolResult(tool_call_id="", content=f"Error: {exc}", is_error=True, target_throttled=True)
        except ImageAssetError as exc:
            return ToolResult(tool_call_id="", content=f"Error: {exc}", is_error=True)

        summary = None
        if data.startswith(_ICO_MAGIC):
            from app.core.parse_offload import run_parse

            converted = await run_parse(ico_to_png, data)
            if converted is None:
                return ToolResult(
                    tool_call_id="",
                    content=f"Error: the icon at {final_url} does not decode as an ICO file; nothing was stored.",
                    is_error=True,
                )
            data, mime = converted, "image/png"
        if mime == "image/svg+xml":
            try:
                data = sanitize_template_asset(data, mime, "asset.svg")
            except SvgSanitizeError as exc:
                return ToolResult(
                    tool_call_id="",
                    content=f"Error: {final_url} is not a valid SVG document: {exc}",
                    is_error=True,
                )
            dimensions = svg_dimensions(data)
        else:
            # Typed by the bytes FIRST, decoded second: the magic number
            # decides whether Pillow's decoder set is run at all (a TIFF or a
            # PSD under an image/png header never reaches it), and it is the
            # type the file is stored under - a declared image/png over JPEG
            # bytes has pixels and the wrong header.
            decoded_type = sniff_raster_type(data)
            if decoded_type is None:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"Error: the bytes at {final_url} are not one of the image types a slide "
                        f"can carry ({', '.join(sorted(ALLOWED_IMAGE_TYPES))}); the server declared "
                        f"{mime}. Nothing was stored."
                    ),
                    is_error=True,
                )
            # Decoded, not trusted: a PNG header over a truncated or bomb body
            # has the right magic number and no pixels the platform will show.
            summary = await summarize_image(data)
            if summary is None:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"Error: the bytes at {final_url} are not the {mime} image the server "
                        "declared (they do not decode as an image); nothing was stored."
                    ),
                    is_error=True,
                )
            mime = decoded_type
            dimensions = (summary.width, summary.height)

        filename = normalize_asset_filename(name, final_url, mime)
        # Stored with the file, so a wall of fetched logos is trimmed from its
        # first save instead of waiting on the bounded backfill.
        geometry = await image_geometry(data, mime)
        try:
            file_ref = await persist_tool_artifact(
                db=db,
                user_id=user_id or "",
                filename=filename,
                raw_bytes=data,
                content_type=mime,
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                file_context=file_context,
                source="agent_download",
                image_meta=geometry,
                fetched=True,
            )
        except Exception as exc:
            logger.warning(
                "fetch_image_asset: storing %s failed: %s", final_url, format_exception_message(exc), exc_info=True
            )
            return ToolResult(
                tool_call_id="",
                content=f"Error: the image was downloaded but could not be stored: {format_exception_message(exc)}",
                is_error=True,
            )

        size_line = f"{dimensions[0]}x{dimensions[1]} px" if dimensions else "dimensions unknown"
        summary_line = (
            "Visual summary: SVG (vector), colours are in the markup."
            if mime == "image/svg+xml"
            else format_visual_summary(summary)
        )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Fetched {file_ref.filename} ({mime}, {format_file_size(len(data))}, {size_line}) "
                f"from {final_url} as file_id {file_ref.file_id}.\n"
                f"{summary_line}\n"
                "Use the file_id where a block takes an image (image, logo_grid / logo_wall, a profiles "
                "line's logo_file_id or photo_file_id) or bind it to a template with "
                "attach_template_asset so it is reusable across decks. Save what it shows "
                "with annotate_file if you will need to recall it."
            ),
        )

    executor = executor if executor is not None else CallableToolExecutor()
    executor.register("fetch_image_asset", _fetch_image_asset)

    definition = ToolDefinition(
        name="fetch_image_asset",
        description=(
            "Download a public image (PNG, JPEG, WebP, GIF, SVG; ICO as PNG; up to 5 MB) "
            "from a URL and store it as a file of this conversation in ONE call - a logo, "
            "a portrait, a diagram for a slide or a template. "
            "Returns the file_id, the dimensions and the visual summary. Never drive "
            "the browser to an image URL and read it back through evaluate: that "
            "returns base64 you cannot use."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The public http(s) URL of the image file itself (not of a page showing it).",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Filename to store it under (e.g. 'anssi-logo.svg'); the extension of the "
                        "image type is added when missing. Defaults to the URL's last segment."
                    ),
                },
            },
            "required": ["url"],
        },
        source="builtin:web",
    )
    return [definition], executor
