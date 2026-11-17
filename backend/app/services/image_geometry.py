# SPDX-License-Identifier: Apache-2.0

"""The geometry of a stored image a deck needs before it draws it.

A logo file routinely carries transparent padding around its mark - an export
tool's artboard, a square canvas around a wordmark - and a renderer that sizes
the FILE sizes that padding too: a 40px logo with 25% margins on every side
draws a 20px mark. The ``logo_grid`` / ``logo_wall`` blocks therefore size the
mark, not the file: this module reads, once, when the bytes are stored

* ``w`` / ``h`` - the pixel size the image was analysed at (the file's own
  size for a raster, the rasterised size for an SVG): only their RATIO is
  read, together with the trim box;
* ``trim`` - ``[x0, y0, x1, y1]``, the box of the non-transparent pixels as
  fractions of the image, so the renderer crops the margins away and the
  logo's height is its mark's height;
* ``ink`` - the mean opacity inside that box (0-1): a solid seal covers most
  of its box, a thin wordmark a third of it, which is what optical balance
  weighs (``components/presentation/logo-sizing.ts``). Absent for an image
  with no transparency at all, whose "ink" is its background.

and stores it on the file (``FileAttachment.image_meta``), so a deck response
carries it for the logos it draws (``asset_meta``) and the renderer never has
to decode a pixel to lay a wall out - on screen, in the PDF and in the DOM the
editable PPTX is measured from, one reading.

:func:`image_tone` reads, through the same bounded decoding, how light an
image draws - the mean luminance of its ink and the proportions of its box -,
what tells a template asset's twin for a dark surface from its twin for a
light one (``video_studio.figure_twins``); it is never stored on the
file.

Computed off the event loop - a raster on the dedicated parse executor
(``run_parse``), an SVG render in the SVG worker process - and never
raising: an undecodable file gets no geometry and is drawn as the file it is.
The bytes are hostile input and every step is bounded: a raster is decoded
only as one of the four types a slide carries, typed by its magic number
(``sniff_raster_type``, as ``fetch_image_asset`` does), under a byte and a
pixel ceiling; an SVG is cleaned by the template sanitizer (``sanitize_svg``,
defusedxml), refused when it would multiply its shapes (nested ``<use>``,
markers, embedded images) - the reading every geometry was stored with,
kept as is so a stored trim box never moves - and rendered through the one
bounded rasterizer (``services/svg_raster.py``) INTO the analysis box,
never to a fixed width.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
from collections.abc import Iterable
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Bumped when the reading changes, so stored geometry can be told apart.
IMAGE_META_VERSION = 1
#: Alpha at or below this is margin, not mark: an export tool's 1-2 alpha haze
#: must not keep a margin alive, while an anti-aliased edge stays inside.
TRIM_ALPHA_THRESHOLD = 8
#: Long edge (px) the image is decoded / rasterised at to be read. The trim
#: box is a fraction, so a 512px reading is precise to 0.2% of the logo.
ANALYSIS_LONG_EDGE = 512
#: Past this the file is not read (a logo is kilobytes, not a photograph).
MAX_ANALYSIS_BYTES = 8 * 1024 * 1024
#: Past this the raster is not decoded (the decompression-bomb bound).
MAX_SOURCE_PIXELS = 40_000_000
#: A trim edge within this fraction of the image edge is the edge itself.
_EDGE_EPSILON = 0.002
#: The files a deck save reads back when their geometry is missing.
MAX_BACKFILL_PER_SAVE = 32
#: Wall-clock budget (seconds) of those reads: a save holds its deck's row
#: lock while they run, so a slow store costs the geometry, never the lock.
BACKFILL_BUDGET_SECONDS = 4.0
#: How many of those files are downloaded and read at once.
BACKFILL_CONCURRENCY = 4
#: Shapes an SVG may draw once every reference is expanded: a detailed logo
#: is a few thousand, and resvg draws ~100k shapes a second at 512 px.
MAX_SVG_EXPANDED_ELEMENTS = 50_000
#: What an SVG may not contain to be read: a marker is drawn once per vertex
#: of every path naming it, an embedded image is decoded at its own size -
#: both outside every bound here.
_SVG_UNBOUNDED_ELEMENTS = frozenset({"marker", "image"})
#: A reference to another element of the document. After the sanitizer these
#: live in presentation attributes and local ``href`` values only: it drops
#: every ``url(`` from ``style`` attributes and ``<style>`` blocks.
_URL_REFERENCE = re.compile(r"url\(\s*['\"]?#([^)'\"\s]+)")
#: Paint servers drawn as a shader: naming one draws no shape again.
_FLAT_REFERENCES = frozenset({"lineargradient", "radialgradient"})
#: The raster types a slide carries (``sniff_raster_type``), as Pillow names them.
_PIL_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}


def _looks_like_svg(data: bytes, content_type: str | None) -> bool:
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared == "image/svg+xml":
        return True
    head = data[:1024].decode("utf-8", errors="ignore").lstrip("\ufeff").lstrip().lower()
    return head.startswith("<svg") or (head.startswith("<?xml") and "<svg" in head)


def _raster_format(data: bytes) -> str | None:
    """The Pillow format of a raster a slide can carry, read from its magic number."""
    from app.services.builtin_tools.image_assets import sniff_raster_type

    return _PIL_FORMATS.get(sniff_raster_type(data) or "")


def _is_svg(data: bytes, content_type: str | None) -> bool:
    """An SVG by its head or its declared type - and never a raster's magic number."""
    return _raster_format(data) is None and _looks_like_svg(data, content_type)


def _local_tag(element: Any) -> str:
    return element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""


def _references(element: Any) -> list[str]:
    """The ids *element* draws again: a local ``href`` and every ``url(#id)``."""
    found: list[str] = []
    for name, value in element.attrib.items():
        if name.rsplit("}", 1)[-1].lower() == "href":
            value = value.strip()
            if value.startswith("#"):
                found.append(value[1:])
        elif "url(" in value:
            found.extend(_URL_REFERENCE.findall(value))
    return found


def _svg_expansion_within(root: Any, cap: int = MAX_SVG_EXPANDED_ELEMENTS) -> bool:
    """Whether *root* draws at most *cap* shapes once every reference is expanded.

    A ``<use>`` draws its target again, and so does a pattern, a mask, a clip
    path or a filter for every element naming it - nested ones multiply (ten
    uses of a group of ten uses of a group ...). Counted with each target's
    expansion memoized; a reference that reaches itself, a marker or an
    embedded image is out of bounds outright.
    """
    by_id: dict[str, Any] = {}
    for element in root.iter():
        if _local_tag(element) in _SVG_UNBOUNDED_ELEMENTS:
            return False
        ident = element.get("id")
        if ident and ident not in by_id:
            by_id[ident] = element
    memo: dict[int, int] = {}
    active: set[int] = set()

    def expanded(element: Any) -> int:
        key = id(element)
        if key in memo:
            return memo[key]
        if key in active:
            raise ValueError("a reference reaches itself")
        active.add(key)
        total = 0
        for node in element.iter():
            total += 1
            for ref in _references(node):
                target = by_id.get(ref)
                if target is not None and _local_tag(target) not in _FLAT_REFERENCES:
                    total += expanded(target)
            if total > cap:
                raise ValueError("too many shapes")
        active.discard(key)
        memo[key] = total
        return total

    try:
        return expanded(root) <= cap
    except (ValueError, RecursionError):
        return False


def _geometry_markup(data: bytes) -> bytes | None:
    """The SVG markup a geometry is read from, or ``None`` when it is not read.

    The template sanitizer's output, refused when its shapes are unbounded
    (:func:`_svg_expansion_within`): the reading which every stored
    geometry was taken with - kept exactly, so a logo's trim box never moves.
    Rendered by the one bounded rasterizer at resvg-py's default resolution, as
    it always was.
    """
    import defusedxml.ElementTree as DefusedET

    from app.services.svg_sanitizer import sanitize_svg

    try:
        clean = sanitize_svg(data)
        bounded = _svg_expansion_within(DefusedET.fromstring(clean))
    except Exception as exc:  # not well-formed, declares entities, or no SVG at all
        logger.debug("Image geometry: SVG refused: %s", format_exception_message(exc))
        return None
    if not bounded:
        logger.debug("Image geometry: SVG not read, its shapes are unbounded")
        return None
    return clean


def _fraction(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 4)


def analyze_image_geometry(data: bytes, content_type: str | None = None) -> dict[str, Any] | None:
    """``{"v", "w", "h", "trim"[, "ink"]}`` for a raster or SVG image, or ``None``.

    ``None`` for anything that is not an image this can read (and for a fully
    transparent one, which has no mark to size). Blocking: call it through
    :func:`image_geometry` from async code.
    """
    if not data or len(data) > MAX_ANALYSIS_BYTES:
        return None
    # Typed by the bytes first: the magic number decides whether Pillow runs at
    # all, and which ONE of its decoders may; the declared type only names an
    # SVG whose head does not say so. An SVG goes through the one bounded
    # rasterizer (``services/svg_raster.py``), fitted into the box.
    pil_format = _raster_format(data)
    if pil_format is not None:
        return _measure(data, (pil_format,))
    if not _looks_like_svg(data, content_type):
        return None
    from app.services.svg_raster import rasterize_svg

    clean = _geometry_markup(data)
    png = rasterize_svg(clean, box=ANALYSIS_LONG_EDGE, dpi=0) if clean else None
    return _measure(png, ("PNG",)) if png else None


def _decoded(raster: bytes, formats: tuple[str, ...]) -> tuple[Any, int, int] | None:
    """``(RGBA image fitted into the analysis box, width, height)`` of a raster decoded as one of *formats*: its
    first frame, under the pixel ceiling; ``None`` when it is not."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(raster), formats=formats) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_SOURCE_PIXELS:
                return None
            opened.seek(0)
            image = opened.convert("RGBA")
    except Exception as exc:
        logger.debug("Image geometry: not decodable: %s", format_exception_message(exc))
        return None
    image.thumbnail((ANALYSIS_LONG_EDGE, ANALYSIS_LONG_EDGE))
    return image, width, height


def _measure(raster: bytes, formats: tuple[str, ...]) -> dict[str, Any] | None:
    """The geometry of a raster decoded as one of *formats* (see :func:`analyze_image_geometry`)."""
    from PIL import ImageStat

    decoded = _decoded(raster, formats)
    if decoded is None:
        return None
    image, width, height = decoded
    aw, ah = image.size
    alpha = image.getchannel("A")
    low, _high = alpha.getextrema()
    meta: dict[str, Any] = {"v": IMAGE_META_VERSION, "w": width, "h": height}
    if low > TRIM_ALPHA_THRESHOLD:
        # No transparency: nothing to trim, and the "ink" would be the
        # file's background - optical balance leaves such a logo alone.
        meta["trim"] = [0.0, 0.0, 1.0, 1.0]
        return meta
    mask = alpha.point(lambda a: 255 if a > TRIM_ALPHA_THRESHOLD else 0)
    box = mask.getbbox()
    if box is None:
        return None
    # One analysis pixel of slack on each side: the edge of an anti-aliased
    # mark is never cropped by the renderer's rounding.
    left, top, right, bottom = max(0, box[0] - 1), max(0, box[1] - 1), min(aw, box[2] + 1), min(ah, box[3] + 1)
    trim = [_fraction(left / aw), _fraction(top / ah), _fraction(right / aw), _fraction(bottom / ah)]
    if trim[0] <= _EDGE_EPSILON:
        trim[0] = 0.0
    if trim[1] <= _EDGE_EPSILON:
        trim[1] = 0.0
    if trim[2] >= 1 - _EDGE_EPSILON:
        trim[2] = 1.0
    if trim[3] >= 1 - _EDGE_EPSILON:
        trim[3] = 1.0
    meta["trim"] = trim
    meta["ink"] = round(ImageStat.Stat(alpha.crop((left, top, right, bottom))).mean[0] / 255, 3)
    return meta


async def _svg_png(data: bytes) -> bytes | None:
    """An SVG rendered into the analysis box as :func:`analyze_image_geometry` reads it. resvg holds the GIL, so the
    render goes to the worker process (killed past its time), never to a thread of this one."""
    from app.core.parse_offload import run_parse
    from app.services.svg_raster import rasterize_svg_async

    clean = await run_parse(_geometry_markup, data)
    return await rasterize_svg_async(clean, box=ANALYSIS_LONG_EDGE, dpi=0) if clean else None


async def image_geometry(data: bytes, content_type: str | None = None) -> dict[str, Any] | None:
    """:func:`analyze_image_geometry` on the parse executor; ``None`` on any failure."""
    from app.core.parse_offload import run_parse

    try:
        if data and len(data) <= MAX_ANALYSIS_BYTES and _is_svg(data, content_type):
            png = await _svg_png(data)
            return await run_parse(_measure, png, ("PNG",)) if png else None
        return await run_parse(analyze_image_geometry, data, content_type)
    except Exception as exc:  # the geometry is a refinement - never fail the caller
        logger.debug("Image geometry skipped: %s", format_exception_message(exc))
        return None


#: Bumped when the tone reading changes (:func:`image_tone`), so a kept one can be told apart.
IMAGE_TONE_VERSION = 1


def _tone(raster: bytes, formats: tuple[str, ...]) -> dict[str, Any] | None:
    """``{"v", "tone", "opaque", "ratio"}`` of a raster decoded as one of *formats* (:func:`image_tone`)."""
    import numpy as np

    decoded = _decoded(raster, formats)
    if decoded is None:
        return None
    pixels = np.asarray(decoded[0], dtype=np.float64) / 255.0
    alpha = pixels[..., 3]
    inked = alpha * 255.0 > TRIM_ALPHA_THRESHOLD
    if not inked.any():
        return None
    rgb = pixels[..., :3]
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    luminance = linear @ np.array([0.2126, 0.7152, 0.0722])
    weight = np.where(inked, alpha, 0.0)
    rows, cols = np.nonzero(inked)
    return {
        "v": IMAGE_TONE_VERSION,
        "tone": round(float((luminance * weight).sum() / weight.sum()), 4),
        "opaque": bool(inked.all()),
        "ratio": round(float(cols.max() - cols.min() + 1) / float(rows.max() - rows.min() + 1), 4),
    }


async def image_tone(data: bytes, content_type: str | None = None) -> dict[str, Any] | None:
    """How light an image draws: ``tone``, the mean relative luminance of its ink - the pixels past
    :data:`TRIM_ALPHA_THRESHOLD`, weighted by their opacity -, ``opaque`` when it has no transparency (its "ink" is
    then its own background), and ``ratio``, the width over the height of its ink's box - what tells two drawings of
    one mark in two tones from two different pictures. Read like :func:`image_geometry`, bounded the same way;
    ``None`` on any failure."""
    from app.core.parse_offload import run_parse

    try:
        if not data or len(data) > MAX_ANALYSIS_BYTES:
            return None
        if _is_svg(data, content_type):
            png = await _svg_png(data)
            return await run_parse(_tone, png, ("PNG",)) if png else None
        pil_format = _raster_format(data)
        return await run_parse(_tone, data, (pil_format,)) if pil_format else None
    except Exception as exc:  # the tone is a choice between twins - never fail the caller
        logger.debug("Image tone skipped: %s", format_exception_message(exc))
        return None


def render_geometry(meta: object, *, storage_key: str | None = None) -> dict[str, Any] | None:
    """What the renderer reads of stored geometry: the ratio source and the trim.

    No box for a file never analysed or analysed as unreadable (``{"v": 1}``
    alone): the renderer draws it from the file's own size. With the file's
    *storage_key*, ``rev`` names the bytes - served with or without a box: a
    file replaced in place keeps its id - and its URL - but not its storage
    key, so a renderer that versions the URL by ``rev`` never keeps an image it
    cached, nor crops it with the trim box of the file that replaced it.
    ``None`` when there is neither a box nor a key.
    """
    out: dict[str, Any] = {}
    if isinstance(meta, dict):
        trim = meta.get("trim")
        width, height = meta.get("w"), meta.get("h")
        if isinstance(trim, list) and len(trim) == 4 and isinstance(width, int) and isinstance(height, int):
            out = {"w": width, "h": height, "trim": trim}
            if isinstance(meta.get("ink"), int | float):
                out["ink"] = meta["ink"]
    if storage_key:
        out["rev"] = hashlib.sha256(storage_key.encode("utf-8")).hexdigest()[:12]
    return out or None


async def _read_within_budget(candidates: list[Any], budget_seconds: float) -> list[tuple[Any, str, dict, bool]]:
    """``(file id, storage key read, geometry, downloaded)`` for the files read in time.

    A few at a time (``BACKFILL_CONCURRENCY``), under one wall-clock budget: a
    file still on its way when it runs out is left unread for a later save.
    Read by content, never by declared type (a logo stored as
    `application/octet-stream` is still a PNG); only a file too big to be read
    at all is marked read without a download.
    """
    from app.services.file_storage import file_storage

    if not candidates:
        return []  # asyncio.wait refuses an empty set
    gate = asyncio.Semaphore(BACKFILL_CONCURRENCY)

    async def read(row: Any) -> tuple[Any, str, dict, bool] | None:
        key = row.s3_object_name
        if (getattr(row, "size", None) or 0) > MAX_ANALYSIS_BYTES:
            return row.id, key, {"v": IMAGE_META_VERSION}, False
        async with gate:
            try:
                data = await file_storage.download(key)
            except Exception as exc:
                logger.debug("Image geometry backfill: %s not downloaded: %s", row.id, format_exception_message(exc))
                return None
            return row.id, key, await image_geometry(data, row.content_type) or {"v": IMAGE_META_VERSION}, True

    tasks = [asyncio.ensure_future(read(row)) for row in candidates]
    done, pending = await asyncio.wait(tasks, timeout=budget_seconds)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        logger.debug("Image geometry backfill: %d file(s) left for a later save (budget spent)", len(pending))
    return [task.result() for task in done if task.exception() is None and task.result() is not None]


async def backfill_image_geometry(
    db,
    file_ids: Iterable[str],
    *,
    limit: int = MAX_BACKFILL_PER_SAVE,
    budget_seconds: float = BACKFILL_BUDGET_SECONDS,
) -> int:
    """Read and store the geometry of the listed files that have none yet.

    For files stored before the geometry existed (or through a path that does
    not read it): a deck save calls this for the logos it references, so the
    next read of the deck - and the save's own layout check - carries their
    trim. A file is judged by its bytes (:func:`analyze_image_geometry`
    sniffs), never by its declared type. Bounded (*limit* files per call, read
    a few at a time within *budget_seconds* - the save holds its deck's row
    lock meanwhile), best effort (a file that is not an image this can read is
    marked read, never retried on every save), and never raises - the caller's
    save does not depend on it. Returns how many files were downloaded and
    read. The caller commits.

    It runs inside the caller's transaction, where a failed statement would
    abort the whole transaction on PostgreSQL and fail the save anyway: the
    lookup and the writes each run in a SAVEPOINT, so a geometry failure costs
    the geometry, never the deck. A reading is written only to a row that is
    still unread AND still names the object that was read: an in-place
    replacement committed meanwhile carries the geometry of its own bytes, and
    a stale reading never lands over it. Rows are written in id order, one
    lock order for every save.
    """
    import uuid

    from sqlalchemy import select, update

    from app.models.file_attachment import FileAttachment

    wanted: list[uuid.UUID] = []
    for raw in file_ids:
        try:
            wanted.append(uuid.UUID(str(raw)))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return 0
    try:
        async with db.begin_nested():
            rows = (
                await db.execute(
                    select(
                        FileAttachment.id,
                        FileAttachment.s3_object_name,
                        FileAttachment.content_type,
                        FileAttachment.size,
                    ).where(
                        FileAttachment.id.in_(sorted(set(wanted))),
                        FileAttachment.image_meta.is_(None),
                    )
                )
            ).all()
    except Exception as exc:
        logger.debug("Image geometry backfill: lookup failed: %s", format_exception_message(exc))
        return 0
    candidates = list(rows)[:limit]
    if not candidates:
        return 0
    try:
        readings = await _read_within_budget(candidates, budget_seconds)
    except Exception as exc:  # the geometry is a refinement - never fail the save
        logger.debug("Image geometry backfill: reads failed: %s", format_exception_message(exc))
        return 0
    if not readings:
        return 0
    try:
        async with db.begin_nested():
            for file_id, storage_key, meta, _downloaded in sorted(readings, key=lambda reading: str(reading[0])):
                await db.execute(
                    update(FileAttachment)
                    .where(
                        FileAttachment.id == file_id,
                        FileAttachment.image_meta.is_(None),
                        FileAttachment.s3_object_name == storage_key,
                    )
                    .values(image_meta=meta)
                )
    except Exception as exc:
        logger.debug("Image geometry backfill: write failed: %s", format_exception_message(exc))
        return 0
    return sum(1 for reading in readings if reading[3])


__all__ = [
    "ANALYSIS_LONG_EDGE",
    "IMAGE_META_VERSION",
    "IMAGE_TONE_VERSION",
    "MAX_ANALYSIS_BYTES",
    "MAX_BACKFILL_PER_SAVE",
    "TRIM_ALPHA_THRESHOLD",
    "analyze_image_geometry",
    "backfill_image_geometry",
    "image_geometry",
    "image_tone",
    "render_geometry",
]
