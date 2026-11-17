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

"""Bounded thumbnails of template assets.

The deck editor's picture picker and a template's Assets tab draw every image
asset as a small tile; downloading each asset's full file for that made opening
either one cost the whole library. The thumbnail route
(``GET /template-containers/{id}/assets/{asset_id}/thumbnail``) answers the same
visibility question as the download and hands back at most ``size`` px on the
long edge:

* a raster asset - any ``image/*`` type but SVG, ``image/jpg`` included - is
  decided by its bytes: its first frame is decoded IN FULL by one of the
  raster decoders a browser paints (:data:`THUMBNAIL_DECODERS`, never another
  Pillow plugin, whatever the row declares), turned upright by its EXIF
  orientation, resized with its aspect and transparency kept and encoded as
  PNG. A small image is decoded and encoded too, so bytes that do not decode
  are a refusal, never a successful thumbnail;
* an SVG is passed through as the download serves it (it is a document, never
  resized, never rendered inline by this route);
* anything else, a raster that does not decode, or one holding more than
  ``MAX_THUMBNAIL_SOURCE_PIXELS`` (read from its header, before any pixel is
  allocated), is refused (``ThumbnailRefusedError``).

A raster render holds a process-wide gate (:func:`thumbnail_render_gate`) from
its storage read to its PNG and decodes off the event loop on a pool of its own
(:func:`render_thumbnail_async`): a burst of cold tiles (the Assets tab asks for
every image at once) waits as coroutines, not as downloaded blobs, and never
queues a document parse behind it, whatever ``PARSE_THREAD_POOL_MAX_WORKERS``
says. Caching follows the file-response contract
(``services/file_responses.py``): a strong ``ETag`` derived from the row, never
the bytes - the download's validator, the stored size and digest, the size asked
for and the rendering - with the lifetime the contract gives a template asset
(``private, no-cache``: it is replaced in place under the same id),
``Vary: Authorization`` and ``nosniff``; a matching ``If-None-Match`` is a
``304`` answered after the authorisation read and before any object-store read.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.utils import build_content_disposition
from app.services.file_responses import INLINE_FILE_CONTENT_TYPES, file_cache_control, file_etag

if TYPE_CHECKING:
    from PIL import Image

#: The long edges a tile asks for: the picker's 36 px tile and the Assets tab's
#: 80 px one at 1x and 2x density, and a larger preview. A fixed set keeps the
#: validators few and the work bounded.
THUMBNAIL_SIZES: tuple[int, ...] = (64, 128, 256)
DEFAULT_THUMBNAIL_SIZE = 128
#: A decompression-bomb ceiling, checked on the header before any pixel is
#: decoded (a 40-megapixel photo is the largest asset a brand template holds).
MAX_THUMBNAIL_SOURCE_PIXELS = 40_000_000
#: The only Pillow decoders a thumbnail runs: PNG, JPEG, GIF, WebP and BMP, and
#: AVIF where this Pillow build has it. A picture the download opens in another
#: format (TIFF, ICO) has no thumbnail and is refused like any undecodable file.
THUMBNAIL_DECODERS: tuple[str, ...] = ("PNG", "JPEG", "GIF", "WEBP", "BMP", "AVIF")
THUMBNAIL_CONTENT_TYPE = "image/png"
SVG_CONTENT_TYPE = "image/svg+xml"
#: Bumped when the rendering changes, so a browser holding an older thumbnail
#: of an unchanged asset revalidates to the new one.
THUMBNAIL_RENDER_VERSION = "2"
#: Decoded modes whose colour profile still describes the pixels once they are
#: RGB or RGBA; a CMYK or grey profile would be wrong on the converted pixels.
_RGB_MODES = frozenset({"RGB", "RGBA", "RGBX", "P", "PA"})
#: A display profile is a few KB; a larger one is not carried into a tile.
_MAX_KEPT_PROFILE_BYTES = 64 * 1024
#: What Pillow raises on bytes it cannot decode (a fuzz of the five raster
#: formats raised nothing else); ``DecompressionBombError`` is added at runtime.
_DECODE_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError, SyntaxError, EOFError)


_render_gate: asyncio.Semaphore | None = None
_render_executor: ThreadPoolExecutor | None = None


class ThumbnailRefusedError(ValueError):
    """The asset has no thumbnail: not an image, undecodable, or too large to decode."""


def _render_workers() -> int:
    # One fewer than the document parse pool (2 by default), at least one.
    return max(1, settings.parse_thread_pool_max_workers - 1)


def thumbnail_render_gate() -> asyncio.Semaphore:
    """The gate a raster thumbnail holds from its storage read to its PNG.

    As wide as the thumbnail pool. Created on first use, in the serving loop.
    """
    global _render_gate
    if _render_gate is None:
        _render_gate = asyncio.Semaphore(_render_workers())
    return _render_gate


async def render_thumbnail_async(data: bytes, size: int) -> bytes:
    """:func:`render_thumbnail` off the event loop, on the thumbnail pool.

    Never the document parse pool (``run_parse``): with
    ``PARSE_THREAD_POOL_MAX_WORKERS=1`` a cold asset grid would otherwise hold
    its only worker. A render is bounded by ``MAX_THUMBNAIL_SOURCE_PIXELS``, so
    a thread still busy at shutdown ends within seconds.
    """
    global _render_executor
    if _render_executor is None:
        _render_executor = ThreadPoolExecutor(max_workers=_render_workers(), thread_name_prefix="xtmone-thumbnail")
    return await asyncio.get_running_loop().run_in_executor(_render_executor, render_thumbnail, data, size)


def thumbnail_kind(content_type: str | None) -> str | None:
    """``"raster"``, ``"svg"`` or ``None`` (no thumbnail, refused before storage is read)."""
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    if kind == SVG_CONTENT_TYPE:
        return "svg"
    if kind.startswith("image/"):
        return "raster"
    return None


def thumbnail_etag(asset: object, size: int) -> str:
    """The strong ``ETag`` of *asset*'s thumbnail at *size* - from the row, never the bytes."""
    material = "\0".join(
        (
            file_etag(asset),
            str(getattr(asset, "size", None) or 0),
            getattr(asset, "sha256", None) or "",
            str(size),
            THUMBNAIL_RENDER_VERSION,
        )
    )
    return f'"{hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]}"'


def thumbnail_cache_headers(asset: object, etag: str) -> dict[str, str]:
    """The validator headers every thumbnail answer carries, the ``304`` included."""
    return {"ETag": etag, "Cache-Control": file_cache_control(asset), "Vary": "Authorization"}


def thumbnail_headers(asset: object, content_type: str, etag: str) -> dict[str, str]:
    """The headers of a ``200`` thumbnail: a raster image inline, an SVG as the download serves it."""
    disposition = (
        "inline"
        if content_type in INLINE_FILE_CONTENT_TYPES
        else build_content_disposition(getattr(asset, "filename", None) or "asset.svg")
    )
    return {
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        **thumbnail_cache_headers(asset, etag),
    }


def render_thumbnail(data: bytes, size: int) -> bytes:
    """PNG bytes of a raster asset, at most *size* px on its long edge.

    Blocking (Pillow): call :func:`render_thumbnail_async`. The first frame is decoded
    in full whatever its size, so truncated or corrupt bytes are refused here;
    it is then turned upright, resized (aspect and alpha kept) and encoded with
    no metadata but a colour profile that still describes its pixels.
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data), formats=_decoders()) as source:
            width, height = source.size
            if width * height > MAX_THUMBNAIL_SOURCE_PIXELS:
                raise ThumbnailRefusedError(f"the image is {width}x{height} px, too large to decode")
            # A JPEG decodes at a reduced scale, still twice the box (a no-op
            # for the other formats).
            source.draft(None, (size * 2, size * 2))
            source.load()
            profile = _kept_profile(source)
            frame = _as_rgb(source)
            frame.thumbnail((size, size), Image.Resampling.LANCZOS)
            _turn_upright(frame)
            out = io.BytesIO()
            frame.save(out, format="PNG", optimize=True, icc_profile=profile)
    except ThumbnailRefusedError:
        raise
    except (*_DECODE_ERRORS, Image.DecompressionBombError) as exc:
        raise ThumbnailRefusedError("the file is not an image this route can decode") from exc
    return out.getvalue()


def _decoders() -> list[str]:
    from PIL import Image

    Image.init()
    return [name for name in THUMBNAIL_DECODERS if name in Image.OPEN]


def _kept_profile(image: Image.Image) -> bytes | None:
    profile = image.info.get("icc_profile")
    if not profile or image.mode not in _RGB_MODES or len(profile) > _MAX_KEPT_PROFILE_BYTES:
        return None
    return profile


def _as_rgb(image: Image.Image) -> Image.Image:
    """*image* in RGB, or RGBA when it carries transparency: what the resample and the PNG keep exactly."""
    if image.mode.startswith("I"):
        # 16-bit grey (PNG): scaled to 8 bits, where a plain conversion clips it to white.
        image = image.convert("I").point(lambda value: value * (1 / 256)).convert("L")
    mode = "RGBA" if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info else "RGB"
    if image.mode == mode and "transparency" not in image.info:
        return image
    return image.convert(mode)


def _turn_upright(frame: Image.Image) -> None:
    """Apply *frame*'s EXIF orientation to its pixels, once: the PNG carries no EXIF.

    An EXIF block that cannot be read leaves the pixels as decoded, as a
    browser shows the original, rather than refusing the picture.
    """
    from PIL import ImageOps

    with contextlib.suppress(*_DECODE_ERRORS):
        ImageOps.exif_transpose(frame, in_place=True)


__all__ = [
    "DEFAULT_THUMBNAIL_SIZE",
    "MAX_THUMBNAIL_SOURCE_PIXELS",
    "SVG_CONTENT_TYPE",
    "THUMBNAIL_CONTENT_TYPE",
    "THUMBNAIL_DECODERS",
    "THUMBNAIL_SIZES",
    "ThumbnailRefusedError",
    "render_thumbnail",
    "render_thumbnail_async",
    "thumbnail_cache_headers",
    "thumbnail_etag",
    "thumbnail_headers",
    "thumbnail_kind",
    "thumbnail_render_gate",
]
