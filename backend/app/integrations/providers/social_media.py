# SPDX-License-Identifier: Apache-2.0

"""The media of social posts: what a post publishes, what a read post carries.

Shared by the LinkedIn, X, Facebook and Bluesky tools. A post tool takes files
of the run by ``file_id`` (:func:`read_post_media`, over
``file_delivery.read_pipe_file``) and checks every one against the platform's
documented type, size, duration and count limits (:class:`MediaLimits`) BEFORE
anything is uploaded, so a file the platform would refuse is refused here with
the limit it breaks. Every raster image a post publishes carries the visual
summary on the result. A read tool lists the media a post carries with
the links the platform gives, and a download tool fetches one of them
(:func:`fetch_public_media`: a signed CDN URL, never sent the account's token,
every hop under the outbound URL policy) into a file of the run with
``deliver_file``.
"""

from __future__ import annotations

import asyncio
import io
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlsplit

import httpx

from app.core.egress import egress_client
from app.integrations.file_content import file_id_list
from app.integrations.file_delivery import (
    FetchedFile,
    FetchError,
    fetch_file,
    file_content_type,
    read_pipe_file,
    uploaded_file_answer,
)
from app.integrations.http import PLATFORM_USER_AGENT
from app.tools.base import FileContext

MIB = 1024 * 1024

#: Largest post media a download tool stores as a file of the run, the cap of
#: the other integration downloads (Microsoft, Google, Slack, GitHub); past it
#: the answer keeps the link.
MAX_MEDIA_DOWNLOAD_BYTES = 100 * MIB

_MAX_REDIRECTS = 5
_DOWNLOAD_TIMEOUT_SECONDS = 120

#: Timeout of one upload request (a video part, a photo): a large part on a
#: slow link takes longer than the default API timeout.
UPLOAD_TIMEOUT_SECONDS = 120

#: How long a post tool waits for an uploaded video to finish processing
#: before it publishes: well inside the loop's per-tool timeout.
VIDEO_PROCESSING_WAIT_SECONDS = 150

#: The wait between two processing checks; a module attribute so tests run
#: without sleeping.
pause = asyncio.sleep

_DOCUMENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)


@dataclass(frozen=True)
class MediaRule:
    """What a platform accepts for one kind of media, as its documentation states it."""

    types: frozenset[str]
    #: ``JPG, PNG or GIF`` - the accepted formats as the refusal names them.
    types_label: str
    #: ``None`` when the platform documents no byte limit for the kind.
    max_bytes: int | None
    size_label: str = ""
    min_bytes: int = 0
    max_pixels: int | None = None
    max_frames: int | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None

    def accepts(self, content_type: str) -> bool:
        return content_type in self.types or (
            "video/*" in self.types and content_type.startswith("video/")
        )


@dataclass(frozen=True)
class MediaLimits:
    """The media one post (or one message) of a platform may carry.

    Either up to ``max_images`` images, or ONE video, ONE document or ONE
    animated GIF: every platform here refuses to mix them in a post.
    """

    platform: str
    image: MediaRule | None = None
    max_images: int = 1
    #: What carries the media, as a refusal names it: ``post``, ``comment``, ``message``.
    unit: str = "post"
    video: MediaRule | None = None
    document: MediaRule | None = None
    #: A platform that files an animated GIF apart from images (X) sets this;
    #: elsewhere an animated GIF is an image.
    animated_gif: MediaRule | None = None

    @property
    def max_files(self) -> int:
        return self.max_images if self.image is not None else 1

    @property
    def upload_ceiling(self) -> int:
        """The largest file any rule accepts: what :func:`read_pipe_file` reads up to.

        A kind with no documented byte limit is read up to the download cap.
        """
        rules = [r for r in (self.image, self.video, self.document, self.animated_gif) if r]
        return max(r.max_bytes or MAX_MEDIA_DOWNLOAD_BYTES for r in rules)

    def rule_for(self, kind: str) -> MediaRule | None:
        return {
            "image": self.image,
            "animated_gif": self.animated_gif,
            "video": self.video,
            "document": self.document,
        }[kind]

    def accepted(self) -> str:
        parts = []
        if self.image is not None:
            count = "an image" if self.max_images == 1 else f"up to {self.max_images} images"
            parts.append(f"{count} ({self.image.types_label}{_size_part(self.image)})")
        if self.animated_gif is not None:
            parts.append(f"one animated GIF ({_size_part(self.animated_gif).lstrip(', ')})")
        if self.video is not None:
            parts.append(f"one video ({self.video.types_label}{_size_part(self.video)})")
        if self.document is not None:
            parts.append(f"one document ({self.document.types_label}{_size_part(self.document)})")
        return ", or ".join(parts)


def _size_part(rule: MediaRule) -> str:
    return f", up to {rule.size_label}" if rule.size_label else ""


@dataclass
class PostMedia:
    """One file of the run, checked for a post.

    *filename* is the stored name, a third party's text: it is sent to the
    platform with the bytes and never written in an answer, which names the
    file by its *file_id* (:attr:`label`).
    """

    data: bytes
    filename: str
    content_type: str
    #: ``image``, ``animated_gif``, ``video`` or ``document``.
    kind: str
    width: int | None = None
    height: int | None = None
    frames: int = 1
    seconds: float | None = None
    #: The ``ImageSummary`` of a raster image (``None`` when undecodable).
    summary: Any = None
    raster: bool = False
    alt_text: str = ""
    #: The ``file_id`` of the file of the run, how every answer names it.
    file_id: str = ""

    @property
    def label(self) -> str:
        """``file <file_id>``: the file as an answer, an error or an action label names it."""
        return f"file {self.file_id}" if self.file_id else "the file"

    def summary_line(self) -> str:
        from app.services.image_summary import format_visual_summary

        return f"{self.label}: {format_visual_summary(self.summary)}"


def media_kinds(files: list[PostMedia]) -> str:
    """``2 images`` / ``1 video`` - how a post's media reads in an answer."""
    counts: dict[str, int] = {}
    for f in files:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    labels = {"image": "image", "animated_gif": "animated GIF", "video": "video", "document": "document"}
    return ", ".join(f"{n} {labels.get(k, k)}{'s' if n > 1 else ''}" for k, n in counts.items())


def summary_lines(files: list[PostMedia]) -> list[str]:
    """The visual summary of every raster image a post publishes."""
    return [f.summary_line() for f in files if f.raster]


def with_summaries(text: str, files: list[PostMedia]) -> str:
    lines = summary_lines(files)
    return "\n".join([text, *lines]) if lines else text


def media_type(data: bytes, declared: str, filename: str) -> str:
    """The canonical MIME type of *data*: its magic bytes first, then what was declared, then its name."""
    from app.services.builtin_tools.image_assets import sniff_raster_type

    raster = sniff_raster_type(data)
    if raster:
        return raster
    head = data[:16]
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/quicktime" if head[8:12] == b"qt  " else "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return file_content_type(declared, filename or "")


def _boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """The ISO BMFF boxes between *start* and *end*: ``(type, body start, box end)``."""
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos + header, pos + size
        pos += size


def mp4_geometry(data: bytes) -> tuple[int | None, int | None, float | None]:
    """``(width, height, seconds)`` of an MP4 / MOV, read from its ``moov`` box.

    The display size of the first video track (``tkhd``, swapped for a 90 or
    270 degree rotation) and the movie duration (``mvhd``); ``None`` for what
    the file does not say. Reads box headers only, never the media.
    """
    width = height = None
    seconds = None
    for kind, body, end in _boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for sub, sub_body, sub_end in _boxes(data, body, end):
            if sub == b"mvhd" and sub_end - sub_body >= 32:
                version = data[sub_body]
                if version == 1:
                    timescale, duration = struct.unpack(">IQ", data[sub_body + 20 : sub_body + 32])
                else:
                    timescale, duration = struct.unpack(">II", data[sub_body + 12 : sub_body + 20])
                if timescale:
                    seconds = duration / timescale
            elif sub == b"trak" and width is None:
                for box, box_body, box_end in _boxes(data, sub_body, sub_end):
                    if box != b"tkhd":
                        continue
                    offset = 88 if data[box_body] == 1 else 76
                    if box_end - box_body < offset + 8:
                        continue
                    w, h = struct.unpack(">II", data[box_body + offset : box_body + offset + 8])
                    w, h = w >> 16, h >> 16
                    if w and h:
                        matrix = box_body + offset - 36
                        a, b = struct.unpack(">ii", data[matrix : matrix + 8])
                        if a == 0 and b != 0:
                            w, h = h, w
                        width, height = w, h
        break
    return width, height, seconds


def _probe_image(data: bytes) -> tuple[int, int, int] | None:
    """``(width, height, frames)`` read from the image header; CPU-light, off-loop."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as opened:
            width, height = opened.size
            frames = max(1, int(getattr(opened, "n_frames", 1)))
    except Exception:
        return None
    return width, height, frames


async def _describe(media: PostMedia) -> None:
    """Fill the geometry and, for a raster image, the visual summary."""
    if media.kind == "video":
        if media.content_type in ("video/mp4", "video/quicktime"):
            media.width, media.height, media.seconds = mp4_geometry(media.data)
        return
    if not media.content_type.startswith("image/"):
        return
    from app.core.parse_offload import run_parse
    from app.services.image_summary import looks_like_raster_image, summarize_image

    media.raster = looks_like_raster_image(media.data, media.content_type)
    if not media.raster:
        return
    media.summary = await summarize_image(media.data)
    if media.summary is not None:
        media.width, media.height, media.frames = (
            media.summary.width,
            media.summary.height,
            media.summary.frames,
        )
        return
    probed = await run_parse(_probe_image, media.data)
    if probed is not None:
        media.width, media.height, media.frames = probed


def _mb(size: int) -> str:
    return f"{round(size / MIB, 1)} MB"


def _check(media: PostMedia, rule: MediaRule, limits: MediaLimits, noun: str) -> str | None:
    """The refusal *media* earns under *rule*, or ``None`` when the platform accepts it.

    The refusal names the file by its id; its stored name and type follow in
    a quarantine block (:func:`uploaded_file_answer`).
    """
    platform = limits.platform
    problem = ""
    size = len(media.data)
    pixels = (media.width or 0) * (media.height or 0)
    if not rule.accepts(media.content_type):
        problem = f"is of a type {platform} does not accept for one {noun} (it accepts {rule.types_label})"
    elif rule.max_bytes is not None and size > rule.max_bytes:
        problem = (
            f"is {_mb(size)} ({size:,} bytes), over the {rule.size_label} {platform} accepts "
            f"for one {noun}"
        )
    elif size < rule.min_bytes:
        problem = f"is {size:,} bytes; {platform} accepts at least {rule.min_bytes // 1024} KB for one {noun}"
    elif rule.max_pixels and pixels >= rule.max_pixels:
        problem = (
            f"is {media.width}x{media.height} px ({pixels:,} pixels); {platform} accepts "
            f"images of less than {rule.max_pixels:,} pixels"
        )
    elif rule.max_frames and media.frames > rule.max_frames:
        problem = f"has {media.frames} frames; {platform} accepts up to {rule.max_frames}"
    elif media.seconds is not None and rule.min_seconds is not None and media.seconds < rule.min_seconds:
        problem = f"lasts {media.seconds:.1f} s; {platform} accepts at least {rule.min_seconds:g} s for one {noun}"
    elif media.seconds is not None and rule.max_seconds is not None and media.seconds > rule.max_seconds:
        problem = (
            f"lasts {media.seconds / 60:.1f} min; {platform} accepts at most "
            f"{rule.max_seconds / 60:g} min for one {noun}"
        )
    if not problem:
        return None
    return uploaded_file_answer(
        f"Error: {media.label} {problem}. Nothing has been published.",
        target=platform,
        filename=media.filename,
        content_type=media.content_type,
    )


def alt_texts(value: Any, count: int) -> list[str]:
    """One alt text per file, in the order of the file ids; a single string is the first file's."""
    if value is None:
        items: list[Any] = []
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    texts = [str(item).strip() if item is not None else "" for item in items[:count]]
    return texts + [""] * (count - len(texts))


async def read_post_media(
    file_context: FileContext | None,
    file_ids: Any,
    limits: MediaLimits,
    *,
    alt_text: Any = None,
) -> list[PostMedia] | str:
    """The files of the run *file_ids* names, checked against *limits*; the refusal otherwise.

    Nothing is uploaded here: a caller that gets a list publishes it, a caller
    that gets a string answers with it and has published nothing.
    """
    ids = file_id_list(file_ids)
    if not ids:
        return []
    if len(ids) > limits.max_files:
        return (
            f"Error: {len(ids)} files given; one {limits.platform} {limits.unit} carries "
            f"{limits.accepted()}. Nothing has been published."
        )
    files: list[PostMedia] = []
    for file_id, text in zip(ids, alt_texts(alt_text, len(ids)), strict=True):
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=limits.upload_ceiling, target=limits.platform
        )
        if isinstance(piped, str):
            return piped
        data, filename, declared = piped
        content_type = media_type(data, declared, filename)
        if content_type.startswith("video/"):
            kind = "video"
        elif content_type in _DOCUMENT_TYPES:
            kind = "document"
        elif content_type.startswith("image/"):
            kind = "image"
        else:
            kind = ""
        media = PostMedia(
            data=data,
            filename=filename,
            content_type=content_type,
            kind=kind,
            alt_text=text,
            file_id=str(file_id).strip(),
        )
        if kind:
            await _describe(media)
            if kind == "image" and content_type == "image/gif" and media.frames > 1 and limits.animated_gif:
                media.kind = "animated_gif"
        rule = limits.rule_for(media.kind) if media.kind else None
        if rule is None:
            return uploaded_file_answer(
                f"Error: {media.label} is of a type {limits.platform} does not take; it accepts "
                f"{limits.accepted()}. Nothing has been published.",
                target=limits.platform,
                filename=filename,
                content_type=content_type,
            )
        noun = "animated GIF" if media.kind == "animated_gif" else media.kind
        if refusal := _check(media, rule, limits, noun):
            return refusal
        files.append(media)
    if any(f.kind != "image" for f in files) and len(files) > 1:
        return (
            f"Error: one {limits.platform} {limits.unit} carries {limits.accepted()} - not "
            f"{media_kinds(files)} together. Nothing has been published."
        )
    return files


def is_https(url: str) -> bool:
    try:
        return urlparse(url).scheme == "https" and bool(urlparse(url).hostname)
    except ValueError:
        return False


def host_is(url: str, *domains: str) -> bool:
    """True when *url* is HTTPS on one of *domains* or a subdomain of one."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host == d or host.endswith(f".{d}") for d in domains)


def https_origin(url: str) -> str:
    """``https://host[:port]`` of an HTTPS URL, ``""`` for anything else.

    The host is lowercased and the default port dropped, so two spellings of
    one origin compare equal; a URL carrying credentials, a bad port or any
    scheme but HTTPS has no origin, so it never compares equal to one.
    """
    try:
        parsed = urlsplit((url or "").strip())
        port = parsed.port
    except ValueError:
        return ""
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None:
        return ""
    if port == 0:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


async def check_public_url(url: str) -> str:
    """*url* checked against the outbound URL policy (``validate_outbound_url_async``).

    Raises ``ValueError`` for a URL that names a private, loopback, link-local
    or otherwise non-public address.
    """
    from app.services.web_fetch import validate_outbound_url_async

    return await validate_outbound_url_async(url)


class _RefusedHopError(Exception):
    """A request of a download the outbound URL policy refuses."""


async def fetch_public_media(
    url: str,
    *,
    source: str,
    max_bytes: int = MAX_MEDIA_DOWNLOAD_BYTES,
    what: str = "media",
    trusted_origin: str = "",
) -> FetchedFile | FetchError:
    """The file at a URL a platform served, or the answer to give instead.

    The URL is a signed CDN link or a public endpoint: it is fetched without
    the account's token, over HTTPS only, every request - redirects included -
    checked against the outbound URL policy first (:func:`check_public_url`),
    and streamed by ``fetch_file`` up to *max_bytes*. Every refusal keeps the
    link, and the ``FetchError`` keeps what caused it (over the cap, status).

    *trusted_origin* is the origin of a host the administrator configured (a
    self-hosted Bluesky PDS, which may sit on a private network): a request to
    exactly that origin (:func:`https_origin`) is not checked against the URL
    policy while every request before it went there too; its connection is
    still refused on a loopback, link-local or metadata address. A chain that
    starts elsewhere, or once left it, is checked at every hop - a foreign
    host redirecting to the trusted origin included.
    """
    url = (url or "").strip()
    if not is_https(url):
        return FetchError(f"Error: {source} gave no HTTPS link for this {what}.")

    trusted = https_origin(trusted_origin)
    on_trusted_origin = bool(trusted)

    async def check_hop(request: httpx.Request) -> None:
        nonlocal on_trusted_origin
        hop = str(request.url)
        if not is_https(hop):
            raise _RefusedHopError(f"{hop} is not HTTPS")
        on_trusted_origin = on_trusted_origin and https_origin(hop) == trusted
        if on_trusted_origin:
            return
        try:
            await check_public_url(hop)
        except ValueError as exc:
            raise _RefusedHopError(str(exc)) from None

    from app.services.web_fetch import guarded_client_options

    try:
        # Each connection goes to an address checked when it opens (DNS
        # rebinding): public only, the trusted origin under the
        # operator-network policy (private ranges admitted, loopback,
        # link-local and metadata refused).
        async with egress_client(
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            headers={"User-Agent": PLATFORM_USER_AGENT},
            **guarded_client_options(
                allow_private=False,
                private_origins=(trusted,) if trusted else (),
                event_hooks={"request": [check_hop]},
            ),
        ) as client:
            fetched = await fetch_file(
                client, "GET", url, provider=source, action=f"download the {what}", max_bytes=max_bytes
            )
    except _RefusedHopError as exc:
        return FetchError(f"Error: the {source} {what} link was refused ({exc}).")
    if isinstance(fetched, FetchError):
        return FetchError(
            f"{fetched} Download URL: {url}",
            status=fetched.status,
            too_large=fetched.too_large,
            size=fetched.size,
        )
    return fetched
