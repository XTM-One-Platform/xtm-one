# SPDX-License-Identifier: Apache-2.0

"""Server-side visual summary of a raster image.

Why this exists
---------------
An agent that renders an image and publishes it (a diagram screenshotted in
the headless browser, then ``upload_notion_file``) has exactly one way to know
what the image looks like: the pixels the tool result attaches as a vision
block. Those pixels are the first thing the context budget prunes, a
DOM probe only measures geometry, and on 2026-09-24 that combination twice put
a diagram on a Notion page with the wrong logo colour - the agent published on
the strength of "no overflow, no overlap" and its memory of an earlier
version.

This module computes, from the bytes, a SHORT machine-readable summary that
rides on the tool result as text and therefore outlives the pixels: the image
dimensions, the dominant colour, and the distinct SATURATED colours covering at
least :data:`SATURATED_COLOR_MIN_SHARE` of the opaque pixels, each with its
hex value and share. A cyan mark that should have been blue is an unexpected
hex in that list, readable without vision. The same summary feeds the
``expect_colors`` / ``forbid_colors`` check ``upload_notion_file`` runs BEFORE
anything reaches the page.

What it deliberately is not
---------------------------
Not a colour histogram of the whole image (greys, blacks and whites are the
paper and the ink of every diagram and say nothing), not a per-region logo
detector (the issue asked to keep it simple), and never a reason for the
screenshot or the upload to fail: an image that cannot be decoded yields
``None`` and the caller says the summary is unavailable. Only an explicit
colour check the agent asked for may refuse.

How the colours are counted
---------------------------
1. EVERY pixel is counted, at full resolution, when the image is at most
   :data:`_MAX_EXACT_PIXELS` and has at most :data:`_MAX_EXACT_COLORS`
   distinct colours (Pillow's ``getcolors`` does the counting in C; a
   diagram or a screenshot is well inside both). Only then is "absent" a
   fact about the image. A larger or more colourful image - a photograph -
   is subsampled to :data:`SUMMARY_SAMPLE_LONG_EDGE` with NEAREST (pixel
   VALUES stay exact, their number is bounded), the summary says ``sampled``,
   and a ``forbid_colors`` check refuses on it: a one-pixel line of a
   forbidden colour covers the threshold share and still falls between the
   samples.
2. Transparent pixels (alpha below :data:`_OPAQUE_ALPHA`) are excluded; every
   share is a fraction of the OPAQUE pixels.
3. A pixel is saturated when its chroma (``max - min`` channel) reaches
   :data:`SATURATED_MIN_CHROMA` and it is not near-black. Chroma, not HSV
   saturation: a dark navy text colour has S = 0.5 and reads as black.
4. The exact saturated colours are kept whole on the summary
   (:attr:`ImageSummary.saturated`, with their Lab computed once) and are
   what the GATE reads: a colour is present when the exact colours within
   :data:`COLOR_MATCH_DELTA_E` of it add up to the threshold, whatever the
   bucketing and the merge below did with them - two drifted halves of one
   mark each under the threshold are one presence, and a small mark is not
   lost in the mean of a bucket a larger colour dominates. A count that
   held more saturated colours than the palette keeps drops the rarest and
   says so (``palette_cut``): the gate then refuses a forbid check like on
   a sampled image. An expected and a forbidden colour whose match regions
   overlap (:data:`GATE_SEPARATION_DELTA_E`) are refused before any read.
5. For the LINE, exact colours are grouped into :data:`_BUCKET_STEP`-wide RGB
   buckets (each reported at its mean, so a flat fill is reported at its
   exact value), the buckets are folded by perceptual distance
   (:data:`MERGE_DELTA_E`) so a gradient or an anti-aliased edge is one
   colour, EVERY colour covering at least the threshold is kept, largest
   first, the line prints the first :data:`MAX_SUMMARY_COLORS` and counts the
   rest. The distinct-colour cap can drop a colour from that list; when the
   dropped buckets could add up to one over the threshold the line says so
   (``truncated``). The gate never reads that list, so the cap cannot blind it.
6. An animated container (GIF, WebP, APNG, multi-page TIFF) is read at its
   first frame only, the summary says how many frames it holds, and a
   ``forbid_colors`` check refuses on it for the same reason as on a sampled
   one: a colour absent from frame one may be in frame two.

Everything CPU-bound runs on the parse executor (:func:`summarize_image` ->
``run_parse``), never inline on the event loop - the gate included
(:func:`run_color_check`: :func:`check_color_expectations` walks up to
:data:`_MAX_EXACT_COLORS` exact colours per gate colour).
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from app.core.color import RGB, chroma, delta_e, format_hex, parse_hex_color, srgb_to_lab
from app.core.utils import format_exception_message

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger(__name__)

#: Long edge, in pixels, an image the exact count cannot take is subsampled to
#: (NEAREST) before counting. 640 keeps a 40 px logo mark on a 1920-wide
#: diagram at ~170 samples while bounding the count at ~410k pixels for a
#: square image. A summary counted this way is ``sampled``.
SUMMARY_SAMPLE_LONG_EDGE = 640

#: Images up to this many pixels are counted EXACTLY, every pixel at full
#: resolution, so the gate's "absent" is a fact about the whole image and not
#: about a sample (a one-pixel line of a forbidden colour covers the threshold
#: share and can still fall between NEAREST samples). 16
#: megapixels is a 1920-wide full-page screenshot 8300 px tall; the RGBA
#: buffer is 64 MB, on the parse executor.
_MAX_EXACT_PIXELS = 16_000_000
#: Most distinct colours the exact count enumerates before the image is
#: handed to the subsample instead (Pillow stops counting there). A flat
#: diagram has a few thousand, anti-aliasing included, a UI screenshot tens of
#: thousands; only a photograph runs past this, and a photograph is not what
#: the gate is for. Also bounds the Python-side bucketing loop and the exact
#: palette the gate reads: a fallback count that holds more saturated colours
#: than this keeps the most frequent and says ``palette_cut``.
_MAX_EXACT_COLORS = 1 << 17

#: Pixel count above which the image is not decoded at all: decoding is the one
#: unbounded allocation here (4 bytes per pixel before the subsample), and a
#: 40-megapixel raster is not a diagram. Such an image gets no summary.
MAX_SUMMARY_PIXELS = 40_000_000

#: Share of the opaque pixels a colour must cover to be listed - and to count
#: as "present" for the gate. 0.05 % is a 30x30 logo mark on a 1600x900
#: diagram (0.06 %); the tints of an anti-aliased edge and the steps of a
#: gradient are folded into their fill or stay under it.
SATURATED_COLOR_MIN_SHARE = 0.0005

#: Most colours the summary LINE prints (largest share first); the rest are
#: counted as ``(+N more)``. A display cap only: every colour over the
#: threshold stays on :attr:`ImageSummary.colors`, and the gate does not even
#: read that list - it sums the exact colours (:attr:`ImageSummary.saturated`).
MAX_SUMMARY_COLORS = 12

#: Chroma floor (``max - min`` channel, 0-255) for a "saturated" pixel. A light
#: card tint (``#E6F0FF``, chroma 25) and every grey stay out; a dark brand
#: navy (``#0C1E3B``, chroma 47) is in.
SATURATED_MIN_CHROMA = 40

#: Brightest channel (0-255) under which a pixel is near-black whatever its
#: chroma.
SATURATED_MIN_VALUE = 50

#: ΔE (CIE76) under which two colour buckets are folded into one listed colour:
#: the steps of a gradient and the shades of an anti-aliased edge, never two
#: brand colours (Filigran blue vs the cyan that must not replace it is > 40).
MERGE_DELTA_E = 10.0

#: ΔE within which a saturated bucket counts towards an expected / forbidden
#: colour. Wide enough for JPEG artefacts and the mean of an anti-aliased
#: bucket, far narrower than the distance between two colours a brand
#: guideline lists. Wider than :data:`MERGE_DELTA_E` on purpose - which is why
#: the gate sums exact colours rather than reading the merged list: two halves
#: of a drifted mark 11 apart stay two list entries, and are one presence.
COLOR_MATCH_DELTA_E = 12.0

#: Least ΔE between an EXPECTED and a FORBIDDEN colour for the gate to tell
#: them apart: twice the match radius. CIE76 is Euclidean in Lab, so a pixel
#: within the radius of both puts the two within 2 x radius of each other
#: (triangle inequality) - and any pair closer than that has a colour between
#: them that counts for both, so one pixel could satisfy the expected colour
#: AND convict the forbidden one. A pair under this bound is refused up front.
GATE_SEPARATION_DELTA_E = 2 * COLOR_MATCH_DELTA_E

#: Most colours one ``expect_colors`` / ``forbid_colors`` list may carry.
MAX_CHECK_COLORS = 16

#: Alpha below which a pixel is transparent and ignored.
_OPAQUE_ALPHA = 128
#: Edge of the RGB buckets exact colours are first grouped into.
_BUCKET_STEP = 16
#: Cap on the distinct colours the perceptual merge keeps for the LINE, so a
#: colourful photograph costs a fraction of a second: the buckets are at most
#: 16^3 = 4096 by construction and EVERY one is tested against the kept
#: colours, so the work is bounded at 4096 x 64 distances. A bucket that
#: matches no kept colour once the cap is full is discarded, and the discarded
#: mass is summed: when it could add up to a colour over the threshold the
#: line says so (``truncated``). The gate reads the exact colours, not the list.
_MAX_DISTINCT_COLORS = 64

_COLOR_LIST_SPLIT_RE = re.compile(r"[\s,;]+")
_TOKEN_DISPLAY_CHARS = 40

#: Magic bytes of the raster formats Pillow decodes here. SVG is deliberately
#: absent: it is source text, and rasterising it is the caller's job.
_RASTER_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II\x2a\x00", "image/tiff"),
    (b"MM\x00\x2a", "image/tiff"),
)


@dataclass(frozen=True)
class ColorShare:
    """One colour: its sRGB value and its share of the opaque pixels.

    ``lab`` is the colour's CIE L*a*b*, computed once on the executor for the
    exact colours the gate walks (up to :data:`_MAX_EXACT_COLORS` of them,
    for up to :data:`MAX_CHECK_COLORS` gate colours - recomputing it per
    comparison would be the slow part); ``None`` on a colour built by hand.
    """

    rgb: RGB
    share: float
    lab: tuple[float, float, float] | None = field(default=None, repr=False, compare=False)

    @property
    def hex(self) -> str:
        return format_hex(self.rgb)


@dataclass(frozen=True)
class ImageSummary:
    """What :func:`summarize_image_bytes` reads out of an image."""

    width: int
    height: int
    #: The largest colour bucket of ALL opaque pixels, greys included - the
    #: paper of a diagram, the background of a screenshot.
    dominant: ColorShare | None
    #: EVERY distinct saturated colour over the threshold, merged, largest
    #: share first (never capped here - :func:`format_visual_summary` prints
    #: the first :data:`MAX_SUMMARY_COLORS`). What the LINE lists.
    colors: tuple[ColorShare, ...]
    #: Fraction of the pixels that were transparent (ignored by every share).
    transparent_share: float = 0.0
    #: True when the pixels the distinct-colour cap discarded could add up to a
    #: colour over the threshold: ``colors`` is then incomplete and the line
    #: says so. The gate reads ``saturated`` and is unaffected.
    truncated: bool = False
    #: Frames in the container; only the FIRST is read. Above 1 the summary
    #: describes one frame of an animation, and a ``forbid_colors`` check must
    #: refuse on it - the colour may be in a frame that was not read.
    frames: int = 1
    #: EVERY exact saturated colour (value, share, Lab), largest first - not
    #: bucketed, not merged; at most :data:`_MAX_EXACT_COLORS`. What the GATE
    #: reads: a colour is present when the exact colours within
    #: :data:`COLOR_MATCH_DELTA_E` of it add up to the threshold, so neither
    #: the bucketing (a 16-cube holds colours up to 26 dE apart, and a mean is
    #: pulled towards the larger one) nor the merge nor the display cap can
    #: hide it.
    saturated: tuple[ColorShare, ...] = ()
    #: True when the counts come from the NEAREST subsample rather than from
    #: every pixel (the image was over :data:`_MAX_EXACT_PIXELS` or had more
    #: than :data:`_MAX_EXACT_COLORS` distinct colours). A feature thinner
    #: than the sample step can then be missed, so absence is not provable and
    #: a ``forbid_colors`` check must refuse.
    sampled: bool = False
    #: True when the image held more distinct saturated colours than
    #: ``saturated`` can carry (:data:`_MAX_EXACT_COLORS`) and the rarest were
    #: dropped - a whole-image count of a small, very colourful image. Every
    #: pixel was counted for the line, but the gate no longer sees every
    #: colour value: a mark made of many one-pixel shades sits in the dropped
    #: tail, so absence is not provable and a ``forbid_colors`` check must
    #: refuse.
    palette_cut: bool = False
    #: Share of the opaque pixels whose colour stands at least
    #: :data:`INK_MIN_DELTA_E` from the dominant one, greys included: the text
    #: and lines of a painted page in black or grey, which ``colors`` (saturated
    #: only) never lists. What tells a painted light page from a blank one.
    ink_share: float = 0.0

    @property
    def complete(self) -> bool:
        """True when every pixel of the whole image was counted AND every
        exact colour of it is on ``saturated`` - the only state in which the
        absence of a colour is provable. The one predicate the gate relies
        on; every flag that can make a negative check wrong is folded in."""
        return not self.sampled and not self.palette_cut and self.frames <= 1


def looks_like_raster_image(data: bytes, content_type: str = "") -> bool:
    """True when *data* is worth handing to the decoder.

    The declared ``content_type`` is a hint (an SVG is routinely uploaded as
    ``image/png``); the magic bytes decide. ``image/svg+xml`` is never a
    raster whatever the bytes say.
    """
    if not data:
        return False
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared == "image/svg+xml":
        return False
    if any(data[: len(magic)] == magic for magic, _ in _RASTER_MAGIC):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return declared.startswith("image/")


def _accumulate(
    buckets: dict[tuple[int, int, int], list[int]],
    key: tuple[int, int, int],
    count: int,
    rgb: RGB,
) -> None:
    entry = buckets.get(key)
    if entry is None:
        buckets[key] = [count, rgb[0] * count, rgb[1] * count, rgb[2] * count]
        return
    entry[0] += count
    entry[1] += rgb[0] * count
    entry[2] += rgb[1] * count
    entry[3] += rgb[2] * count


def _bucket_means(buckets: dict[tuple[int, int, int], list[int]]) -> list[tuple[RGB, int]]:
    """``(mean colour, pixel count)`` per bucket, largest first."""
    means = [
        ((round(sr / count), round(sg / count), round(sb / count)), count)
        for count, sr, sg, sb in buckets.values()
        if count > 0
    ]
    means.sort(key=lambda item: -item[1])
    return means


def _merge_close_colors(
    means: list[tuple[RGB, int]], threshold_count: float
) -> tuple[list[tuple[RGB, int]], bool]:
    """Fold buckets within :data:`MERGE_DELTA_E` of a larger one into it.

    Largest first, so the colour that names a group is the one that covers the
    most pixels (a flat fill, not its edge). EVERY bucket is tested against
    the kept colours - there are at most 4096 of them by construction - so a
    kept group's count includes every bucket within :data:`MERGE_DELTA_E` of
    it, whatever its rank; Lab is computed once per bucket and only the
    number of DISTINCT colours is capped (:data:`_MAX_DISTINCT_COLORS`), so a
    photograph costs a fraction of a second, a diagram milliseconds.

    Returns ``(merged, truncated)``: ``truncated`` is True when the buckets
    DISCARDED - those that matched no kept colour once the cap was full -
    together reach *threshold_count*. Fail-closed on the aggregate, not on any
    one bucket: several nearby buckets each under the threshold, none of them
    kept, can add up to one colour over it, and with no representative
    retained no later bucket can fold into them. A discarded bucket is by
    definition not within reach of any kept colour, so it can never complete
    a kept group; and when the discarded mass cannot reach the threshold, no
    cluster among it can either - the list is then complete.
    """
    kept: list[list] = []  # [rgb, count, lab]
    discarded = 0
    for rgb, count in means:
        lab = srgb_to_lab(rgb)
        for entry in kept:
            other = entry[2]
            distance = (
                (lab[0] - other[0]) ** 2 + (lab[1] - other[1]) ** 2 + (lab[2] - other[2]) ** 2
            ) ** 0.5
            if distance < MERGE_DELTA_E:
                entry[1] += count
                break
        else:
            if len(kept) >= _MAX_DISTINCT_COLORS:
                discarded += count
                continue
            kept.append([rgb, count, lab])
    kept.sort(key=lambda entry: -entry[1])
    return [(entry[0], entry[1]) for entry in kept], discarded >= threshold_count


def _frame_count(opened: Image.Image) -> int:
    """How many frames the container holds; 1 for a still image.

    Read through the plugin (``n_frames`` - for a GIF that walks the frame
    headers without decoding them). A container whose frames cannot be
    counted raises, so the caller returns ``None`` and the gate refuses as
    "could not be decoded" - it never passes on frame 0 of a file it could
    not read to the end.
    """
    try:
        return max(1, int(getattr(opened, "n_frames", 1)))
    except Exception as exc:
        raise ValueError(f"frame count unreadable: {format_exception_message(exc)}") from exc


def summarize_image_bytes(data: bytes) -> ImageSummary | None:
    """Read the summary out of *data*. CPU-bound - call through :func:`summarize_image`.

    ``None`` when the bytes are not an image Pillow can decode (or are over
    :data:`MAX_SUMMARY_PIXELS`); never raises for bad input.
    """
    if not data:
        return None
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        logger.warning("Pillow is not installed; image summaries are unavailable")
        return None

    try:
        with Image.open(io.BytesIO(data)) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_SUMMARY_PIXELS:
                logger.info(
                    "Image summary skipped: %dx%d is over the %d-pixel cap",
                    width,
                    height,
                    MAX_SUMMARY_PIXELS,
                )
                return None
            frames = _frame_count(opened)
            colors = None
            if width * height <= _MAX_EXACT_PIXELS:
                # Every pixel, at full resolution, counted in C: the one way
                # "absent" is a fact about the image and not about a sample.
                # ``None`` means more distinct colours than the cap - a
                # photograph - and hands the image to the subsample below.
                counted = opened.convert("RGBA")
                colors = counted.getcolors(maxcolors=_MAX_EXACT_COLORS)
                del counted
            sampled = False
            if colors is None:
                long_edge = max(width, height)
                if long_edge > SUMMARY_SAMPLE_LONG_EDGE:
                    # Only here is a pixel left uncounted; a small image that
                    # merely had too many colours for the exact pass is still
                    # counted whole below.
                    sampled = True
                    scale = SUMMARY_SAMPLE_LONG_EDGE / long_edge
                    size = (max(1, round(width * scale)), max(1, round(height * scale)))
                    try:
                        # NEAREST on the source mode: exact pixel values, and
                        # the full-size RGBA conversion is never allocated.
                        sample = opened.resize(size, Image.NEAREST).convert("RGBA")
                    except (ValueError, OSError):
                        sample = opened.convert("RGBA").resize(size, Image.NEAREST)
                else:
                    sample = opened.convert("RGBA")
                colors = sample.getcolors(maxcolors=max(1, sample.width * sample.height))
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None

    if not colors:  # pragma: no cover - maxcolors is the pixel count, so never None
        return None

    total_opaque = 0
    transparent = 0
    all_buckets: dict[tuple[int, int, int], list[int]] = {}
    saturated_buckets: dict[tuple[int, int, int], list[int]] = {}
    saturated_exact: list[tuple[RGB, int]] = []
    for count, pixel in colors:
        red, green, blue, alpha = pixel
        if alpha < _OPAQUE_ALPHA:
            transparent += count
            continue
        total_opaque += count
        rgb = (red, green, blue)
        key = (red // _BUCKET_STEP, green // _BUCKET_STEP, blue // _BUCKET_STEP)
        _accumulate(all_buckets, key, count, rgb)
        if chroma(rgb) >= SATURATED_MIN_CHROMA and max(rgb) >= SATURATED_MIN_VALUE:
            _accumulate(saturated_buckets, key, count, rgb)
            saturated_exact.append((rgb, count))

    total = total_opaque + transparent
    transparent_share = transparent / total if total else 0.0
    if total_opaque == 0:
        return ImageSummary(
            width,
            height,
            None,
            (),
            transparent_share=transparent_share,
            frames=frames,
            sampled=sampled,
        )

    bucket_means = _bucket_means(all_buckets)
    dominant_rgb, dominant_count = bucket_means[0]
    dominant = ColorShare(dominant_rgb, dominant_count / total_opaque)
    # Per bucket (at most 4096), never per pixel: the grey or black text of a
    # painted page is ink; a skeleton a few shades off the background is not.
    ink_count = sum(count for rgb, count in bucket_means[1:] if delta_e(rgb, dominant_rgb) >= INK_MIN_DELTA_E)

    threshold_count = SATURATED_COLOR_MIN_SHARE * total_opaque
    # The exact colours, whole, for the gate - with their Lab computed here,
    # once, on the executor: a colour split by JPEG drift over many of them is
    # one presence when they are summed within the match tolerance, and a
    # small mark sharing a 16-cube with a large different colour is not lost
    # in that bucket's mean. Largest first. The exact pass cannot exceed the
    # cap (getcolors stops there), but the fallback can - its sample is every
    # pixel of a small noisy image, or 410k samples of a large one - so the
    # rarest colours are cut and the summary SAYS so (``palette_cut``): a
    # whole-image count that could not keep every colour must not read as
    # complete, or a mark made of one-pixel shades in the dropped tail would
    # pass a forbid check.
    saturated_exact.sort(key=lambda item: -item[1])
    palette_cut = len(saturated_exact) > _MAX_EXACT_COLORS
    saturated = tuple(
        ColorShare(rgb, count / total_opaque, lab=srgb_to_lab(rgb))
        for rgb, count in saturated_exact[:_MAX_EXACT_COLORS]
    )
    merged, truncated = _merge_close_colors(_bucket_means(saturated_buckets), threshold_count)
    # Every colour over the threshold, uncapped, for the LINE (which applies
    # MAX_SUMMARY_COLORS when it prints).
    listed = tuple(
        ColorShare(rgb, count / total_opaque)
        for rgb, count in merged
        if count >= threshold_count
    )
    return ImageSummary(
        width,
        height,
        dominant,
        listed,
        transparent_share=transparent_share,
        truncated=truncated,
        frames=frames,
        saturated=saturated,
        sampled=sampled,
        palette_cut=palette_cut,
        ink_share=ink_count / total_opaque,
    )


async def summarize_image(data: bytes) -> ImageSummary | None:
    """:func:`summarize_image_bytes` on the parse executor; ``None`` on any failure.

    A summary is a courtesy to the reader of the tool result, never a reason
    for the tool to fail: every error is logged and swallowed here.
    """
    from app.core.parse_offload import run_parse

    try:
        return await run_parse(summarize_image_bytes, data)
    except Exception as exc:
        logger.warning("Image summary failed: %s", format_exception_message(exc), exc_info=True)
        return None


#: The share of the dominant colour above which a capture with no saturated colour is reported as possibly blank.
BLANK_DOMINANT_SHARE = 0.95

#: CIE76 distance from the dominant colour from which a pixel counts as ink (text, a line, an icon): far above the
#: few units between a background and its loading skeleton.
INK_MIN_DELTA_E = 25.0

#: Ink share from which a capture is painted whatever its colours: the grey text of a light 404 page covers a few
#: percent of it.
BLANK_MAX_INK_SHARE = 0.005


def looks_blank(summary: ImageSummary | None) -> bool:
    """True when *summary* is one colour over :data:`BLANK_DOMINANT_SHARE` with no saturated colour and almost no
    ink (under :data:`BLANK_MAX_INK_SHARE`): a screenshot of a page that did not paint (background only), not a
    capture to rely on. A light page of grey or black text is painted, saturated colour or not."""
    return (
        summary is not None
        and summary.dominant is not None
        and summary.dominant.share > BLANK_DOMINANT_SHARE
        and not summary.colors
        and summary.ink_share < BLANK_MAX_INK_SHARE
    )


def _percent(share: float) -> str:
    """``5.6%`` / ``0.06%`` - two decimals under one percent, so a small logo
    mark near the listing threshold never prints as ``0.1%`` or ``0.0%``."""
    value = share * 100
    return f"{value:.2f}%" if value < 1 else f"{value:.1f}%"


def coverage_threshold_label() -> str:
    """``"0.05%"`` - the listing threshold as the summary line prints it."""
    return f"{SATURATED_COLOR_MIN_SHARE * 100:g}%"


def format_visual_summary(summary: ImageSummary | None) -> str:
    """The one line every image-bearing tool result carries.

    ``Visual summary: 1600x900 px; dominant #FFFFFF 81.2%; saturated colours
    (>= 0.05% coverage): #7B2CBF 2.3%, #0015A8 1.1%, #0FBCFF 0.40%.`` - hex
    values uppercase, shares of the opaque pixels, largest first. One format
    for the screenshot and the upload results, so a reader (or a test)
    compares them directly.
    """
    if summary is None:
        return "Visual summary: unavailable (the image could not be decoded, or is over the size cap)."
    parts = [f"{summary.width}x{summary.height} px"]
    if summary.frames > 1:
        parts.append(f"animated, only the first of {summary.frames} frames read")
    if summary.sampled:
        parts.append("sampled, not every pixel counted")
    if summary.palette_cut:
        parts.append(f"colour check limited to the {_MAX_EXACT_COLORS} most frequent colours")
    if summary.dominant is not None:
        parts.append(f"dominant {summary.dominant.hex} {_percent(summary.dominant.share)}")
    if summary.transparent_share >= 0.005:
        parts.append(f"transparent {_percent(summary.transparent_share)}")
    shown = summary.colors[:MAX_SUMMARY_COLORS]
    listed = ", ".join(f"{color.hex} {_percent(color.share)}" for color in shown) if shown else "none"
    if len(summary.colors) > len(shown):
        listed += f" (+{len(summary.colors) - len(shown)} more)"
    if summary.truncated:
        listed += " (incomplete: more distinct colours than the summary enumerates)"
    parts.append(f"saturated colours (>= {coverage_threshold_label()} coverage): {listed}")
    return "Visual summary: " + "; ".join(parts) + "."


def _is_checkable_color(rgb: RGB) -> bool:
    """The saturation predicate the pixel count applies, on a gate colour.

    A colour that would never be counted as saturated - a grey, white, a
    near-black, a pale tint - can never be found in ``colors``: forbidding it
    would always pass and expecting it would always fail, both silently. Such
    an entry is refused up front instead.
    """
    return chroma(rgb) >= SATURATED_MIN_CHROMA and max(rgb) >= SATURATED_MIN_VALUE


def parse_color_list(value: object, *, what: str) -> tuple[list[RGB], str | None]:
    """Coerce an ``expect_colors`` / ``forbid_colors`` argument into colours.

    Accepts the list of hex strings the schema declares AND the one string a
    model routinely sends instead (``"#0015A8, #7B2CBF"``), with or without
    the ``#``. Returns ``(colours, None)`` - duplicates dropped, order kept -
    or ``([], error)`` naming the offending entry, so the agent fixes the
    argument instead of guessing what was read. An entry the check could
    never measure (see :func:`_is_checkable_color`) is an error too: the gate
    is refused, never silently passed, on a colour it cannot see. Only an
    OMITTED gate - ``None``, ``""``, ``[]`` - means "no check"; a supplied
    entry that holds no colour (``[""]``, ``"   "``) is a malformed gate and
    refused, never read as an empty one that lets the upload through.
    """
    if value is None or value == "" or value == []:
        return [], None
    blank = (
        f"Error: {what} has a blank entry; drop it, or write a hex colour like #0015A8 "
        "(omit the argument entirely for no check)."
    )
    tokens: list[str] = []
    if isinstance(value, str):
        tokens = [token for token in _COLOR_LIST_SPLIT_RE.split(value.strip()) if token]
        if not tokens:
            return [], blank
    elif isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, str):
                return [], (
                    f"Error: {what} must be a list of hex colours such as "
                    f'["#0015A8", "#7B2CBF"] (got a {type(item).__name__} in the list).'
                )
            item_tokens = [token for token in _COLOR_LIST_SPLIT_RE.split(item.strip()) if token]
            if not item_tokens:
                return [], blank
            tokens.extend(item_tokens)
    else:
        return [], (
            f"Error: {what} must be a list of hex colours such as "
            f'["#0015A8", "#7B2CBF"] (got {type(value).__name__}).'
        )
    colors: list[RGB] = []
    for token in tokens:
        rgb = parse_hex_color(token)
        if rgb is None:
            shown = token if len(token) <= _TOKEN_DISPLAY_CHARS else token[:_TOKEN_DISPLAY_CHARS] + "..."
            return [], (
                f"Error: {what} entry '{shown}' is not a 6-digit hex colour "
                "(write it like #0015A8)."
            )
        if not _is_checkable_color(rgb):
            return [], (
                f"Error: {what} entry '{format_hex(rgb)}' is a grey, white, near-black "
                "or pale colour; the colour check measures SATURATED colours only, so "
                "it cannot be checked (the dominant colour is reported in the visual "
                "summary instead). Drop it, or pass a saturated brand colour."
            )
        if rgb not in colors:
            colors.append(rgb)
    if len(colors) > MAX_CHECK_COLORS:
        return [], f"Error: {what} lists {len(colors)} colours; {MAX_CHECK_COLORS} is the cap."
    return colors, None


def conflicting_gate_colors(expect: list[RGB], forbid: list[RGB]) -> str | None:
    """The refusal for an expected and a forbidden colour the gate cannot tell apart.

    Presence is decided within :data:`COLOR_MATCH_DELTA_E` of EACH gate
    colour, so the two match regions overlap as soon as the colours are
    within :data:`GATE_SEPARATION_DELTA_E` (twice the radius) of each other:
    one observed colour then counts for both, and the check would convict the
    forbidden colour on the very pixels that satisfied the expected one (a
    refusal with the wrong reason), or pass the expected colour on the
    forbidden one's pixels. A single radius here was not enough - ``#0015A8``
    and ``#0037C8`` are 12.6 apart and ``#0018AB`` is within 12 of both.
    Such a pair is refused before any byte is read, the
    same colour in both lists being the degenerate case. ``None`` when every
    expected colour is checkably far from every forbidden one.
    """
    for wanted in expect:
        for banned in forbid:
            if wanted == banned:
                return (
                    f"Error: {format_hex(wanted)} is listed in BOTH expect_colors and "
                    "forbid_colors; decide which one it is and call again."
                )
            distance = delta_e(wanted, banned)
            if distance <= GATE_SEPARATION_DELTA_E:
                return (
                    f"Error: expected {format_hex(wanted)} and forbidden {format_hex(banned)} "
                    f"are too close for the check to tell apart (dE {distance:.1f}; a pixel "
                    f"counts for a colour within {COLOR_MATCH_DELTA_E:g}, so the two must be "
                    f"at least {GATE_SEPARATION_DELTA_E:g} apart); drop one of them and call "
                    "again."
                )
    return None


def _matching_share(summary: ImageSummary, rgb: RGB) -> tuple[float, ColorShare | None]:
    """``(share, closest)``: the share of the opaque pixels within
    :data:`COLOR_MATCH_DELTA_E` of *rgb*, summed over every exact saturated
    colour, and the colour nearest to it (``None`` when none is within
    tolerance).

    Summed over the EXACT colours (:attr:`ImageSummary.saturated`), never read
    off the merged list or the buckets: the match tolerance is wider than the
    merge tolerance, so two drifted halves of one mark can be two list entries
    each under the threshold - or one of them folded into a larger neighbour
    that is itself out of tolerance - while together they are the mark; and a
    16-cube bucket holds colours up to 26 dE apart, so a small mark sharing
    one with a large different colour is farther from the bucket's mean than
    the tolerance. A summary built without exact
    colours falls back to its list.
    """
    target = srgb_to_lab(rgb)
    matched = 0.0
    closest: ColorShare | None = None
    closest_distance = 0.0
    for color in summary.saturated or summary.colors:
        lab = color.lab if color.lab is not None else srgb_to_lab(color.rgb)
        distance = (
            (lab[0] - target[0]) ** 2 + (lab[1] - target[1]) ** 2 + (lab[2] - target[2]) ** 2
        ) ** 0.5
        if distance > COLOR_MATCH_DELTA_E:
            continue
        matched += color.share
        if closest is None or distance < closest_distance:
            closest, closest_distance = color, distance
    return matched, closest


def check_color_expectations(
    summary: ImageSummary, *, expect: list[RGB], forbid: list[RGB]
) -> list[str]:
    """The problems an ``expect_colors`` / ``forbid_colors`` check finds, if any.

    A colour is "present" when the exact saturated colours within
    :data:`COLOR_MATCH_DELTA_E` of it add up to :data:`SATURATED_COLOR_MIN_SHARE`
    of the opaque pixels (:func:`_matching_share`). A forbidden colour present
    or an expected colour absent is one problem each, worded with the colour
    and the share the caller asked about - an empty list is a pass. A summary
    that is not :attr:`ImageSummary.complete` (``sampled``, or the first frame
    of an animation) can prove presence but never absence, so on it a
    forbidden colour that was NOT found is a problem too: the gate refuses
    rather than passes on an incomplete count.

    CPU-bound in proportion to the exact colours (up to :data:`_MAX_EXACT_COLORS`
    per gate colour, Lab precomputed): an async tool calls it through
    :func:`run_color_check`, like :func:`summarize_image`.
    """
    problems: list[str] = []
    reasons: list[str] = []
    if summary.sampled:
        reasons.append(
            "the image was too large or too colourful to count every pixel and was sampled"
        )
    if summary.palette_cut:
        reasons.append(
            "the image has more distinct saturated colours than the check can compare, so "
            f"only the {_MAX_EXACT_COLORS} most frequent were kept"
        )
    if summary.frames > 1:
        reasons.append(f"only the first of its {summary.frames} frames was read")
    incomplete = " and ".join(reasons)
    for rgb in forbid:
        share, closest = _matching_share(summary, rgb)
        if closest is not None and share >= SATURATED_COLOR_MIN_SHARE:
            spelled = closest.hex if closest.hex != format_hex(rgb) else "exactly"
            problems.append(
                f"forbidden colour {format_hex(rgb)} is present "
                f"({spelled}, {_percent(share)} of the image)"
            )
        elif incomplete:
            problems.append(
                f"forbidden colour {format_hex(rgb)} cannot be ruled out ({incomplete}, "
                "so its absence is not verifiable)"
            )
    for rgb in expect:
        share, _ = _matching_share(summary, rgb)
        if share < SATURATED_COLOR_MIN_SHARE:
            present = (
                ", ".join(
                    f"{color.hex} {_percent(color.share)}"
                    for color in summary.colors[:MAX_SUMMARY_COLORS]
                )
                or "none"
            )
            if len(summary.colors) > MAX_SUMMARY_COLORS:
                present += f" (+{len(summary.colors) - MAX_SUMMARY_COLORS} more)"
            note = f"; {incomplete}" if incomplete else ""
            problems.append(
                f"expected colour {format_hex(rgb)} is absent (no saturated colour "
                f"within tolerance covers >= {coverage_threshold_label()} of the image; "
                f"present: {present}{note})"
            )
    return problems


async def run_color_check(
    summary: ImageSummary, *, expect: list[RGB], forbid: list[RGB]
) -> list[str]:
    """:func:`check_color_expectations` on the parse executor.

    Unlike :func:`summarize_image` this does NOT swallow a failure: a gate the
    agent asked for that could not run must refuse, never pass, so an
    executor error propagates to the tool's own error handling.
    """
    from app.core.parse_offload import run_parse

    return await run_parse(partial(check_color_expectations, summary, expect=expect, forbid=forbid))
