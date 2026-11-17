# SPDX-License-Identifier: Apache-2.0

"""The one bounded SVG rasterizer.

Every SVG the platform renders to pixels server-side goes through here: the
logo geometry reader (``services/image_geometry.py``) and every SVG a
model is shown (``llm/base.py``: a chat upload, a user message image, a tool
result - no vision provider accepts ``image/svg+xml``). The markup is
untrusted, and resvg renders whatever it is given, so the markup is checked
first and the render is bounded:

* **Parsed defused** (``defusedxml``): an internal entity is expanded within a
  bound - an Adobe Illustrator export declares its namespaces this way, and
  resvg expands them itself - while a parameter or external entity (the XXE
  vectors), a billion-laughs expansion and a flood of declarations are refused.
* **Nothing outside the document is reached**: resvg reads the file an
  ``<image>`` / ``<feImage>`` path names, so a non-local ``href`` and a
  non-local ``url(...)`` target in an attribute (case-insensitive) are DROPPED
  and the rest of the drawing renders - resvg drew nothing there on its own. A
  ``#id`` is kept, and so is an ``<image>`` ``data:`` URI: a PNG / JPEG / GIF /
  WebP whose header says it is at most ``MAX_EMBEDDED_IMAGE_PIXELS``, or a
  nested SVG (draw.io icons) checked under these same rules. Under
  ``strict`` (``SVG_STRICT_RENDERING``) such an SVG is refused whole instead.
* **Bounded shapes**: a ``<use>`` draws its target again, and so does a
  pattern, a mask, a clip path or a filter for every element naming it; a
  marker is drawn once per vertex of every shape naming it. Markers and paint
  servers are inherited, so one named on a group is drawn for every shape in
  it. Nested ones multiply (ten uses of a group of ten uses ...: a ~1 KB file
  is seconds of resvg per level). The shapes the document draws once every
  reference is expanded are counted, memoized per target, and past
  ``MAX_SVG_EXPANDED_ELEMENTS`` the SVG is refused - as it is past
  ``MAX_SVG_DEPTH`` levels of nesting, which resvg recurses through.
* **Bounded filters**: in the same pass, every filter application is costed
  by the region it covers and the work of its primitives (a morphology window,
  turbulence octaves, a convolution kernel, a blur...), scaled by the
  transforms above it. A filter is inherited too: an attribute or a stylesheet
  rule naming one on a group or the root filters that whole subtree as one
  layer over its region (the canvas when its bounds are unknown), so it is
  charged there, not only on the leaves - past ``MAX_FILTER_WORK_MS``, or a
  region past ``MAX_FILTER_REGION_PIXELS`` (resvg does not clip a filter region
  to the canvas), the SVG is refused in milliseconds. A stylesheet is read as
  resvg reads it (:func:`_css_text`): a ``}`` or a ``/*`` in a quoted string is
  text, never the end of the rule that names the filter.
* **A pixel box**: the render is fitted INTO ``box`` x ``box`` px, keeping its
  ratio - a ~100-byte SVG with a tall viewBox never allocates a pixmap longer
  than the box. With ``keep_native_size`` an SVG whose own size fits the box
  is rendered at that size, the size resvg always gave it - one resvg
  sizes by its content included, when that content provably fits the box, and
  one sized in em / ex under a font size the document sets, when a bound of
  that font size does; any other size is fitted into the box.
* **Absolute units as a browser reads them**: an SVG is rendered at
  ``RENDER_DPI``, CSS's 96 px per inch, so a pt / mm / in length - the root's
  own size, a ``font-size="12pt"``, a ``stroke-width="1mm"``, a nested
  viewport - is drawn at the size a browser draws it, where resvg-py's default
  0 dpi reads it as 0. A caller that must read an SVG exactly as that default
  did (the logo geometry, whose stored trim boxes were read so) passes
  ``dpi=0``.

What is checked is the markup; what is rendered is the ORIGINAL markup unless
a reference had to be dropped, so an SVG that passes renders exactly as resvg
renders it (the template sanitizer, which drops a ``<style>`` naming any ``url(``, is for
markup a browser displays).

resvg holds the GIL while it renders, so a thread does not free the event
loop: async callers use :func:`rasterize_svg_async`, which renders in a
dedicated worker process under a wall-clock timeout (the worker is killed
past it, and when its caller goes away) and, on POSIX, an address-space
limit. :func:`rasterize_svg` is the same work, inline, for synchronous
callers - bounded by the same checks, but not by the timeout, the memory cap
or the crash isolation only a process gives (a text the checks let through
can still take seconds): markup from async code always goes through the
worker. The async results are cached (``RENDER_CACHE_MAX_ENTRIES``), so a
conversation's history is not rendered again on every turn.

Never raises: an SVG that fails a check, cannot be parsed or cannot be
rendered yields ``None``, which every caller treats as "no image";
:func:`render_svg` / :func:`render_svg_async` also say why (a refusal, a
worker killed or dead), for a caller that must report it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import logging
import math
import multiprocessing
import re
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any, NamedTuple

from app.core.worker_process import exit_with_parent, limit_address_space

logger = logging.getLogger(__name__)

#: Past this the markup is not read at all.
MAX_SVG_BYTES = 8 * 1024 * 1024
#: Shapes an SVG may draw once every reference is expanded: a detailed logo or
#: a large diagram is a few thousand, and resvg draws ~100k shapes a second.
MAX_SVG_EXPANDED_ELEMENTS = 50_000
#: A document with more elements of its own may draw this many times them: a
#: big flat chart is no multiplication, and ``MAX_SVG_BYTES`` bounds its cost.
EXPANSION_ALLOWANCE = 4
#: Deepest nesting an SVG may reach, a ``<use>`` counting as the depth of what
#: it draws: resvg recurses once per level and overflows a thread's stack past
#: a few dozen levels (64 on a 2 MB Windows main thread, under 256 on an 8 MB
#: Linux one) - the process dies. A real SVG nests a handful of levels; the
#: worker renders on a stack sized for this.
MAX_SVG_DEPTH = 128
#: Largest raster an ``<image>`` may embed (resvg decodes it at its own size).
MAX_EMBEDDED_IMAGE_PIXELS = 16_000_000
#: Estimated time the filters of one SVG may take (milliseconds, see
#: ``_FILTER_COST``): a filter bomb is refused in the structural pass instead
#: of holding the worker until ``RASTER_TIMEOUT_SECONDS``.
MAX_FILTER_WORK_MS = 5_000
#: Largest region one filter application may cover (device pixels): resvg does
#: not clip a filter region to the canvas, so a region past it is a pixmap of
#: its own - four times the 4096 px box at most.
MAX_FILTER_REGION_PIXELS = 4 * 4096 * 4096
#: Largest side of the render a content-sized SVG is drawn at to learn the size
#: resvg gives it, before one fitted into a smaller box (the logo geometry's): the
#: chat path's box, so it is never a larger pixmap than a chat render.
MAX_CONTENT_PROBE_SIDE = 4096
#: The resolution an SVG renders at and every bound reads absolute units at:
#: CSS's (1in = 96px, 1pt = 1/72in, 1pc = 12pt, 1cm = 1in / 2.54, 1mm = 1cm / 10),
#: which every browser applies. resvg-py's default is 0, which reads every
#: pt / mm / in length as 0: a root sized in one has an invalid size, and a
#: font size, a stroke or a viewport inside is not drawn.
RENDER_DPI = 96
#: Rendered PNGs kept per process, so a conversation's history does not render
#: every SVG again on every turn: at most this many entries and this many bytes.
RENDER_CACHE_MAX_ENTRIES = 256
RENDER_CACHE_MAX_BYTES = 32 * 1024 * 1024
#: Wall-clock bound of one render in the worker process (seconds).
RASTER_TIMEOUT_SECONDS = 10.0
#: How long a fresh worker may take to start and import resvg (seconds) - kept
#: out of the render's timeout, so a slow start on a loaded host never kills a
#: render that had not begun, and no SVG ever gets this much time.
WORKER_START_TIMEOUT_SECONDS = 60.0
#: How long a render may wait for the ones queued ahead of it (seconds): past
#: it the image is skipped, so a burst of slow SVGs - hostile or not - delays a
#: caller by this and its own render at most, never by the whole queue.
RASTER_QUEUE_TIMEOUT_SECONDS = 30.0
#: What a render may add to the worker's address space past what the worker
#: maps once started (POSIX, ``worker_process.limit_address_space``): a render
#: that tries to take more fails there, never in the web process.
WORKER_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024
#: Stack of the thread the worker renders on: ``MAX_SVG_DEPTH`` levels of
#: resvg recursion with room to spare, whatever the platform's main thread gets.
WORKER_RENDER_STACK_BYTES = 64 * 1024 * 1024
#: Internal entities a DOCTYPE may declare: an Illustrator export declares a
#: handful (namespace URIs, style fragments); past this the document is refused.
MAX_SVG_ENTITIES = 256
#: What every entity reference in the document may add in all once expanded
#: (bytes): the billion-laughs bound, kept by this module whatever the libexpat
#: in use caps, so an expanded document is never much larger than the markup.
MAX_SVG_ENTITY_EXPANSION = MAX_SVG_BYTES
#: How deep an ``<image>`` may nest another SVG as a ``data:`` URI (draw.io
#: embeds its icons this way): each level is checked under the same rules.
MAX_NESTED_SVG_DEPTH = 3

#: A ``url(...)`` function reference (case-insensitive - CSS folds the function
#: name, so ``URL(#id)`` is the same reference), capturing its raw target: a
#: local ``#id``, a ``data:`` URI, or an external path / URL. The local-only
#: gate (:func:`_external_url`) refuses the last kind.
_URL_REFERENCE = re.compile(r"url\(\s*['\"]?\s*([^)'\"\s]+)", re.IGNORECASE)
#: Paint servers drawn as a shader: naming one draws no shape again.
_FLAT_REFERENCES = frozenset({"lineargradient", "radialgradient"})
#: The properties that instance a marker at the vertices of a shape.
_MARKER_PROPERTIES = frozenset({"marker", "marker-start", "marker-mid", "marker-end"})
_MARKER_LONGHANDS = ("marker-start", "marker-mid", "marker-end")
#: The properties that name a paint server (a pattern is drawn for every element it paints).
_PAINT_PROPERTIES = frozenset({"fill", "stroke"})
#: The shapes resvg draws a marker on (every shape, as SVG 2 says) and, for the
#: shapes with a fixed outline, at most how many vertices.
_FIXED_VERTICES = {"line": 2, "rect": 9, "circle": 5, "ellipse": 5}
_MARKABLE = frozenset({"path", "polyline", "polygon", *_FIXED_VERTICES})
#: The elements a paint server is drawn for.
_PAINTED = frozenset({*_MARKABLE, "text", "tspan", "textpath"})
#: The elements a stylesheet rule can make draw a referenced resource again.
_RENDERED = frozenset({*_MARKABLE, "text", "use", "image"})
#: ``xml:id``, which resvg resolves a reference to like an ``id``.
_XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
#: A number as resvg reads one: a sign, ASCII digits (``float()`` would also
#: take other scripts' digits), a fraction, an exponent.
_NUM = r"[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
_NUMBER = re.compile(_NUM)
#: The whitespace resvg skips - XML's four characters, not every Unicode space.
_WS = "[ \t\n\r]"
#: A root ``width`` / ``height`` as resvg reads it: leading whitespace, a number
#: and a lower-case unit, and nothing after it - ``"100 "`` or ``"100PX"`` is no
#: length to resvg, which then takes 100%.
_LENGTH = re.compile(rf"{_WS}*({_NUM})(px|em|ex|in|cm|mm|pt|pc|%)?")
#: Pixels per absolute unit at ``RENDER_DPI``.
_UNIT_PX = {
    "px": 1.0,
    "in": RENDER_DPI,
    "cm": RENDER_DPI / 2.54,
    "mm": RENDER_DPI / 25.4,
    "pt": RENDER_DPI / 72,
    "pc": RENDER_DPI / 6,
}
#: What one filter primitive costs resvg, in milliseconds per megapixel of its
#: region (measured on resvg-py 0.3.2); morphology, turbulence and convolution
#: are costed by their window, octaves and kernel in ``_primitive_cost``.
_FILTER_COST = {
    "feblend": 120,
    "fecolormatrix": 55,
    "fecomponenttransfer": 50,
    "fecomposite": 110,
    "fediffuselighting": 60,
    "fedisplacementmap": 65,
    "fedropshadow": 190,
    "feflood": 10,
    "fegaussianblur": 80,
    "feimage": 60,
    "femerge": 110,
    "feoffset": 50,
    "fespecularlighting": 260,
    "fetile": 55,
}
_FILTER_COST_UNKNOWN = 120
#: What a filter inherits through its ``href`` along with its primitives: its region.
_FILTER_REGION_ATTRIBUTES = ("filterUnits", "x", "y", "width", "height")
#: The ways one filter may inherit (a hop to each filter holding a duplicated id).
_MAX_FILTER_VARIANTS = 16
#: A ``transform`` list, one function at a time.
_TRANSFORM = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^()]*)\)")
#: How much a transform or a viewport this cannot read may enlarge a filter.
_UNKNOWN_SCALE = 64.0
#: A ``viewBox``: four numbers, whitespace and / or one comma between them.
_SEP = rf"(?:{_WS}*,{_WS}*|{_WS}+)"
_VIEW_BOX = re.compile(rf"{_WS}*({_NUM}){_SEP}({_NUM}){_SEP}({_NUM}){_SEP}({_NUM}){_WS}*")
#: What resvg reads an ``em`` as when the document sets no font size (an ``ex`` is half of it).
_DEFAULT_EM_PX = 16.0
_DATA_IMAGE = re.compile(r"^\s*data:image/(png|jpeg|jpg|gif|webp)\s*;\s*base64\s*,", re.IGNORECASE)
_PIL_FORMATS = {"png": "PNG", "jpeg": "JPEG", "jpg": "JPEG", "gif": "GIF", "webp": "WEBP"}
#: A path ``d`` command letter, and per coordinate set it takes: how many
#: numbers, and the most vertices resvg puts a marker on. ``H`` / ``V`` take ONE
#: number each (dividing a path's numbers by two, as a polyline's points
#: divide, undercounts a horizontal / vertical run by half); an
#: arc is drawn as up to four cubic segments, one per quadrant of its sweep,
#: each ending on a vertex. ``Z`` draws the marker back at the subpath start.
_PATH_COMMAND = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])([^MmLlHhVvCcSsQqTtAaZz]*)")
_PATH_SET = {"m": (2, 1), "l": (2, 1), "h": (1, 1), "v": (1, 1), "c": (6, 1), "s": (4, 1), "q": (4, 1), "t": (2, 1), "a": (7, 4)}
#: One entity declaration of a DOCTYPE's internal subset: a parameter mark, its
#: name, then an external id (``SYSTEM`` / ``PUBLIC``) or its quoted replacement text.
_ENTITY_DECL = re.compile(
    rb"<!ENTITY\s+(%\s+)?([^\s%\"'>]+)\s+(?:(SYSTEM|PUBLIC)\b|\"([^\"]*)\"|'([^']*)')",
    re.DOTALL | re.IGNORECASE,
)
_ENTITY_REF = re.compile(rb"&([A-Za-z_:][\w:.\-]*);")
#: A whole ``url(...)`` function (case-insensitive), capturing its target.
_URL_FUNCTION = re.compile(r"url\(\s*['\"]?\s*([^)'\"\s]*)[^)]*\)", re.IGNORECASE)
#: A nested SVG as an ``<image>`` ``data:`` URI, base64 or percent-encoded.
_DATA_SVG = re.compile(r"^\s*data:image/svg\+xml\s*((?:;[^,]*)?),", re.IGNORECASE)
#: Whitespace (after an optional BOM) in front of an XML declaration.
_LEADING_BEFORE_DECLARATION = re.compile(r"^\ufeff?[ \t\r\n]+(?=<\?xml)")
#: A start tag (its name, its attributes, its end) and one quoted attribute.
_START_TAG = re.compile(r"(<[A-Za-z_][^\s/>]*)((?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*+\"|'[^']*+'))*+)(\s*/?>)")
_ATTRIBUTE = re.compile(r"\s+([^\s=/>]+)\s*=\s*(?:\"[^\"]*+\"|'[^']*+')")
#: A CSS property name as written; anything else in front of a ``:`` is charged both ways.
_PROPERTY = re.compile(r"-?[a-z][a-z-]*")


def _local_tag(element: Any) -> str:
    return element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


def _path_vertices(d: str) -> int:
    """How many vertices a marker is drawn at along a path ``d`` (an upper bound).

    Per coordinate SET a command takes (``_PATH_SET``): a run of ``H`` / ``V``
    (one number each) counts one per number and a curve one per curve, not one
    per two numbers - dividing every number by two, as a polyline's points
    divide, undercounts a horizontal / vertical run by half (resvg draws a
    marker per vertex) - and an arc its up to four segments. ``Z``
    draws one at the subpath start; a command whose numbers do not fill its
    last set counts that set too, so a malformed run is never undercounted.
    """
    total = 0
    for match in _PATH_COMMAND.finditer(d):
        shape = _PATH_SET.get(match.group(1).lower())
        if shape is None:  # Z / z: the closepath marker at the subpath start
            total += 1
            continue
        numbers_per_set, vertices_per_set = shape
        numbers = sum(1 for _ in _NUMBER.finditer(match.group(2)))
        total += max(1, -(-numbers // numbers_per_set)) * vertices_per_set
    return total


def _vertices(element: Any) -> int:
    """How many vertices a marker is drawn at on *element* (an upper bound); 0 off a shape."""
    tag = _local_tag(element)
    if tag in _FIXED_VERTICES:
        return _FIXED_VERTICES[tag]
    if tag == "path":
        d = element.get("d")
        return max(1, _path_vertices(d)) if d else 0
    if tag in ("polyline", "polygon"):
        points = element.get("points")
        return max(1, sum(1 for _ in _NUMBER.finditer(points)) // 2) if points else 0
    return 0


def _embedded_image_ok(href: str) -> bool:
    """Whether an ``<image>`` href is a raster ``data:`` URI of a bounded size."""
    match = _DATA_IMAGE.match(href)
    if not match:
        return False
    try:
        raw = base64.b64decode("".join(href[match.end() :].split()), validate=True)
    except (binascii.Error, ValueError):
        return False
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw), formats=(_PIL_FORMATS[match.group(1).lower()],)) as opened:
            width, height = opened.size
    except Exception:
        return False
    return 0 < width * height <= MAX_EMBEDDED_IMAGE_PIXELS


def _url_targets(value: str) -> list[str]:
    """Every ``url(...)`` target in *value* (case-insensitive), quotes stripped."""
    return _URL_REFERENCE.findall(value)


def _local_ref_ids(value: str) -> list[str]:
    """The ids of the LOCAL ``url(#id)`` references in *value*."""
    return [target[1:] for target in _url_targets(value) if target.startswith("#")]


def _external_url(value: str) -> bool:
    """Whether *value* carries a ``url(...)`` reaching outside the document.

    A ``url(#id)`` is local and a ``url(data:...)`` is inline; anything else - a
    file path, an ``http(s)`` / ``file`` URL, a protocol-relative ``//`` - would
    send resvg to the filesystem or the network, so an attribute carrying one is
    refused (the local-only contract). Case-insensitive: ``URL(...)`` counts.
    """
    if "url(" not in value.lower():
        return False
    return any(
        not target.startswith("#") and not target.lower().startswith("data:")
        for target in _url_targets(value)
    )


class _StrictRefusalError(Exception):
    """A non-local reference under *strict* - refuse the whole SVG."""


def _doctype_internal_subset(data: bytes) -> bytes:
    """The bytes between ``[`` and its matching ``]`` in the DOCTYPE, or ``b""``.

    Scanned manually (quotes respected), so a ``]`` inside an entity value does
    not end the subset early and hide a declaration from the bounded checks.
    """
    doctype = re.search(rb"<!DOCTYPE\b", data, re.IGNORECASE)
    if not doctype:
        return b""
    i = data.find(b"[", doctype.end())
    close = data.find(b">", doctype.end())
    if i < 0 or (0 <= close < i):  # no internal subset (external DTD or none)
        return b""
    start = i + 1
    quote = 0
    for j in range(start, len(data)):
        ch = data[j]
        if quote:
            if ch == quote:
                quote = 0
        elif ch in (0x22, 0x27):  # " '
            quote = ch
        elif ch == 0x5D:  # ]
            return data[start:j]
    return data[start:]


def _check_internal_entities(data: bytes) -> None:
    """Refuse an SVG whose DOCTYPE entities are unsafe or would expand too far.

    An internal general entity is expanded by resvg (an Illustrator export
    declares a few for its namespace URIs and style fragments), so it is kept -
    but a parameter entity (``%``) and an external one (``SYSTEM`` / ``PUBLIC``)
    are the XXE vectors and refused; more than ``MAX_SVG_ENTITIES`` declarations
    (a name declared again counts again) is refused; and the text every
    reference in the document expands to is summed (transitively, cycle-safe)
    and refused past ``MAX_SVG_ENTITY_EXPANSION`` - the billion-laughs bound
    this module keeps itself, whatever the libexpat in use caps, before the
    permissive parse. A name declared twice is measured by its FIRST value:
    XML binds the first declaration (XML 1.0 section 4.2), and expat and resvg
    both expand it.
    """
    subset = _doctype_internal_subset(data)
    if not subset:
        raise ValueError("entity declaration outside a readable internal subset")
    values: dict[str, bytes] = {}
    for count, decl in enumerate(_ENTITY_DECL.finditer(subset), start=1):
        parameter, name, external, dq, sq = decl.groups()
        if parameter is not None or external is not None:
            raise ValueError("a parameter or external entity is declared")
        if count > MAX_SVG_ENTITIES:
            raise ValueError("too many entity declarations")
        values.setdefault(name.decode("utf-8", "replace"), dq if dq is not None else sq)

    memo: dict[str, int] = {}

    def expanded_length(name: str, seen: frozenset[str]) -> int:
        if name in memo:
            return memo[name]
        if name in seen:
            raise ValueError("an entity reference cycle")
        value = values.get(name)
        if value is None:
            return 0  # the five built-ins and numeric refs; resvg refuses any other undefined ref
        seen = seen | {name}
        total, pos = 0, 0
        for ref in _ENTITY_REF.finditer(value):
            total += (ref.start() - pos) + expanded_length(ref.group(1).decode("utf-8", "replace"), seen)
            pos = ref.end()
        total += len(value) - pos
        if total > MAX_SVG_ENTITY_EXPANSION:
            raise ValueError("an entity expands too far")
        memo[name] = total
        return total

    for name in values:
        expanded_length(name, frozenset())
    body_total = 0
    for ref in _ENTITY_REF.finditer(data):
        name = ref.group(1).decode("utf-8", "replace")
        if name in memo:
            body_total += memo[name]
            if body_total > MAX_SVG_ENTITY_EXPANSION:
                raise ValueError("the document's entity references expand too far")


def _as_resvg_reads(data: bytes) -> bytes:
    """The markup resvg is handed, as bytes expat parses the same way: resvg
    renders ``data.decode("utf-8", errors="replace")`` (an invalid byte is U+FFFD
    to it) and reads past whitespace in front of the XML declaration, both of
    which expat refuses. (A repeated default ``xmlns``, the third, is handled
    by :func:`_parse_svg_root` on the rare document that fails to parse.)"""
    return _LEADING_BEFORE_DECLARATION.sub("", data.decode("utf-8", errors="replace"), count=1).encode("utf-8")


def _first_default_xmlns(tag: re.Match[str]) -> str:
    """A start tag with every default ``xmlns`` but its first dropped."""
    seen = False

    def keep(attribute: re.Match[str]) -> str:
        nonlocal seen
        if attribute.group(1) != "xmlns":
            return attribute.group(0)
        if seen:
            return ""
        seen = True
        return attribute.group(0)

    return tag.group(1) + _ATTRIBUTE.sub(keep, tag.group(2)) + tag.group(3)


def _defused_parse(source: bytes, **forbid: bool) -> Any:
    """*source* parsed by defusedxml as UTF-8 (its declaration aside, as resvg reads it)."""
    from defusedxml.ElementTree import DefusedXMLParser

    parser = DefusedXMLParser(encoding="utf-8", **forbid)
    parser.feed(source)
    return parser.close()


def _parse_svg_root(data: bytes, *, strict: bool = False) -> Any:
    """Parse *data* for the bounds check, expanding bounded internal entities.

    What is parsed is the text resvg renders (:func:`_as_resvg_reads`). A
    document with no entity declaration parses under the strict defaults (no
    entity, no external reference). One that declares entities is bounded by
    :func:`_check_internal_entities` first, then parsed with internal entities
    expanded and external references still refused - so the walk sees the shapes
    the entities expand to, while the ORIGINAL bytes (which resvg expands the
    same way) are what renders. Under *strict* any entity declaration is refused.
    A document that fails to parse is tried once more with a default ``xmlns``
    repeated on one start tag kept once - resvg keeps the first.
    """
    source = _as_resvg_reads(data)
    try:
        return _parse_source(source, strict=strict)
    except SyntaxError:  # ElementTree's ParseError: not well-formed
        text = source.decode("utf-8")
        once = _START_TAG.sub(_first_default_xmlns, text)
        if once == text:
            raise
        return _parse_source(once.encode("utf-8"), strict=strict)


def _parse_source(source: bytes, *, strict: bool) -> Any:
    from defusedxml.common import EntitiesForbidden

    try:
        return _defused_parse(source)
    except EntitiesForbidden:
        if strict:
            raise
        _check_internal_entities(source)
        return _defused_parse(source, forbid_entities=False)


def _strip_external_urls(value: str) -> str:
    """*value* with every non-local, non-``data:`` ``url(...)`` removed.

    A ``url(#id)`` or a ``url(data:...)`` is kept; a file path or a URL is
    dropped, so a paint / marker / filter reaching outside the document draws
    nothing (its fallback, then the property's default) instead of sending resvg
    to the filesystem or the network - what resvg did with it on its own, minus
    the read.
    """

    def replace(match: re.Match[str]) -> str:
        target = match.group(1)
        if target.startswith("#") or target.lower().startswith("data:"):
            return match.group(0)
        return ""

    return _URL_FUNCTION.sub(replace, value)


def _nested_svg_bytes(href: str) -> bytes | None:
    """The SVG bytes of an ``<image>`` ``data:image/svg+xml`` URI, or ``None``."""
    match = _DATA_SVG.match(href)
    if not match:
        return None
    body = href[match.end() :]
    if "base64" in match.group(1).lower():
        try:
            return base64.b64decode("".join(body.split()), validate=False)
        except (binascii.Error, ValueError):
            return None
    from urllib.parse import unquote_to_bytes

    return unquote_to_bytes(body)


def _neutralize_references(root: Any, *, strict: bool, depth: int = 0) -> bool:
    """Drop every reference that would send resvg outside the document; return
    whether the tree changed (then the caller renders the serialized tree).

    A local ``#id`` and, on an ``<image>``, a bounded raster ``data:`` URI are
    kept. A non-local ``href`` is removed (an ``<a>`` link is left - resvg never
    follows it). A nested ``data:image/svg+xml`` on an ``<image>`` (draw.io
    embeds its icons this way) is decoded and checked under these same rules and
    the shape budget up to ``MAX_NESTED_SVG_DEPTH`` deep, kept when it passes and
    dropped when it does not. A non-local ``url(...)`` in an attribute (a paint,
    a marker, a filter, or one in a ``style`` attribute) is stripped; a
    ``<style>`` element's own CSS is left, as resvg loads nothing from it. Under
    *strict* the first non-local reference refuses the whole SVG instead.
    """
    changed = False
    for element in root.iter():
        tag = _local_tag(element)
        for name in list(element.attrib):
            value = element.attrib[name]
            local = _local_name(name)
            if local == "href":
                target = value.strip()
                if target.startswith("#") or tag == "a":
                    continue
                if tag == "image":
                    if _embedded_image_ok(target):
                        continue
                    replacement = _clean_nested_svg(value, strict=strict, depth=depth)
                    if replacement is not None:
                        if replacement is not value:
                            element.set(name, replacement)
                            changed = True
                        continue
                if strict:
                    raise _StrictRefusalError
                del element.attrib[name]
                changed = True
            elif _external_url(value):
                if strict:
                    raise _StrictRefusalError
                kept = _strip_external_urls(value)
                if kept.strip():
                    element.set(name, kept)
                else:  # nothing left: the property's default, as with no reference at all
                    del element.attrib[name]
                changed = True
    return changed


def _clean_nested_svg(href: str, *, strict: bool, depth: int) -> str | None:
    """A cleaned ``data:image/svg+xml`` href for a nested SVG, or ``None`` to drop it.

    ``None`` (the caller strips the ``<image>``) when the nesting is too deep,
    the data is not a decodable SVG, or it blows the shape budget; otherwise the
    nested SVG is checked and its references neutralized like the parent. An
    unchanged nested SVG returns *href* itself (the very object), so a clean
    one keeps its bytes; a changed one is re-encoded.
    """
    if depth >= MAX_NESTED_SVG_DEPTH:
        return None
    data = _nested_svg_bytes(href.strip())
    if not data or len(data) > MAX_SVG_BYTES:
        return None
    try:
        nested = _parse_svg_root(data, strict=strict)
        if _local_tag(nested) != "svg":
            return None
        changed = _neutralize_references(nested, strict=strict, depth=depth + 1)
        if not svg_is_bounded(nested, _depth=depth + 1):
            return None
    except _StrictRefusalError:
        raise
    except Exception as exc:
        logger.info("Nested SVG not kept: %s", repr(exc)[:200])
        return None
    if not changed:
        return href
    return "data:image/svg+xml;base64," + base64.b64encode(_serialize_svg(nested)).decode("ascii")


def _serialize_svg(root: Any) -> bytes:
    """*root* as SVG bytes, its namespaces kept so resvg renders it as the
    original did (proven byte-for-byte over the corpus). Used only for an SVG
    whose references were neutralized; a clean SVG renders its own bytes."""
    import xml.etree.ElementTree as ET

    ET.register_namespace("", "http://www.w3.org/2000/svg")
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def _kind(prop: str) -> str:
    """How an element naming a reference in the property *prop* draws it again."""
    if prop in _MARKER_PROPERTIES:
        return "marker"
    if prop in _PAINT_PROPERTIES:
        return "paint"
    if prop == "filter":
        return "filter"
    return "once"


def _declaration_refs(css: str) -> list[tuple[str, str]]:
    """``(id, kind)`` for the local ``url(#id)`` references of CSS declarations."""
    found: list[tuple[str, str]] = []
    for declaration in re.split(r"[;{}]", css):
        if "url(" not in declaration.lower():
            continue
        prop, sep, value = declaration.partition(":")
        prop = prop.strip().lower()
        kinds = (_kind(prop),) if sep and _PROPERTY.fullmatch(prop) else ("marker", "paint", "filter")
        for ident in _local_ref_ids(value if sep else declaration):
            found.extend((ident, kind) for kind in kinds)
    return found


#: A CSS comment, a quoted string (a backslash escaping the next character) or
#: a quote left open, in the order a CSS parser meets them: a ``/*`` inside a
#: string is text, and so is a quote inside a comment.
_CSS_TOKEN = re.compile(r"/\*.*?(?:\*/|$)|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|[\"']", re.DOTALL)
_CSS_STRING_STRUCTURE = str.maketrans("{};", "   ")


def _css_text(css: str) -> str | None:
    """*css* as a CSS parser reads its structure - resvg's included: every
    comment blanked, and the ``{`` / ``}`` / ``;`` inside a quoted string too, so
    ``content: "}"`` or ``content: "/*"`` never ends a rule or hides the
    declarations after it; the rest is kept (``url("#id")`` included). ``None``
    when a string is left open, where parsers disagree on what follows."""
    if "/*" not in css and '"' not in css and "'" not in css:
        return css
    left_open = False

    def token(match: re.Match[str]) -> str:
        nonlocal left_open
        text = match.group(0)
        if text.startswith("/*"):
            return " "
        if len(text) == 1:
            left_open = True
            return text
        return text.translate(_CSS_STRING_STRUCTURE)

    structure = _CSS_TOKEN.sub(token, css)
    return None if left_open else structure


#: One CSS rule: its selector list, then its declaration block (no nested block).
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
#: A selector's combinators, which separate its compound selectors.
_SELECTOR_COMBINATOR = re.compile(r"\s*[>+~]\s*|\s+")
#: A leading type selector (an element name or ``*``) of a compound selector.
_SELECTOR_TYPE = re.compile(r"^(\*|[A-Za-z_][\w-]*)")
#: An attribute selector or a pseudo-class / -element, stripped before the type is read.
_SELECTOR_ATTR = re.compile(r"\[[^\]]*\]")
_SELECTOR_PSEUDO = re.compile(r"::?[\w-]+(?:\([^)]*\))?")
#: A class / an id in a compound selector.
_SELECTOR_CLASS = re.compile(r"\.([\w-]+)")
_SELECTOR_ID = re.compile(r"#([\w-]+)")
#: A parsed key (subject) selector: ``(type or None, classes, id or None)``.
_KeySelector = tuple[str | None, frozenset[str], str | None]


def _selector_key(selector: str) -> _KeySelector:
    """The ``(type, classes, id)`` an element must have for *selector* to match it.

    Only the KEY (rightmost) compound selector is read - the element a rule
    actually styles; an ancestor combinator (`.deck g`) is ignored, which
    widens the match (more elements charged), the safe direction. ``type`` is
    ``None`` for ``*`` or a compound with no element name (a class / id /
    attribute / pseudo selector), so it matches any element.
    """
    tokens = [token for token in _SELECTOR_COMBINATOR.split(selector.strip()) if token]
    core = tokens[-1] if tokens else ""
    core = _SELECTOR_PSEUDO.sub("", _SELECTOR_ATTR.sub("", core))
    type_match = _SELECTOR_TYPE.match(core)
    type_ = type_match.group(1).lower() if type_match else None
    if type_ == "*":
        type_ = None
    classes = frozenset(_SELECTOR_CLASS.findall(core))
    ids = _SELECTOR_ID.findall(core)
    return type_, classes, ids[-1] if ids else None


def _stylesheet_filter_rules(css: str) -> list[tuple[str, _KeySelector]]:
    """``(filter id, key selector)`` for every ``filter: url(#id)`` a ``<style>``
    rule declares, so the filter can be charged on the elements it actually
    applies to - containers and the root included, which the per-leaf charge
    misses (a ``g { filter: url(#f) }`` filters the whole group as one layer).
    A sheet whose structure cannot be told (:func:`_css_text`) may apply any
    filter it names to any element."""
    text = _css_text(css)
    if text is None:
        return [(ident, (None, frozenset(), None)) for ident, kind in _declaration_refs(css) if kind == "filter"]
    rules: list[tuple[str, _KeySelector]] = []
    for rule in _CSS_RULE.finditer(text):
        selectors, block = rule.group(1), rule.group(2)
        if "url(" not in block.lower():
            continue
        idents: list[str] = []
        for declaration in _split_declarations(block):
            prop, sep, value = declaration.partition(":")
            prop = prop.strip().lower()
            if sep and _PROPERTY.fullmatch(prop) and _kind(prop) == "filter":
                idents.extend(_local_ref_ids(value))
        if not idents:
            continue
        keys = [_selector_key(selector) for selector in selectors.split(",") if selector.strip()]
        rules.extend((ident, key) for ident in idents for key in keys)
    return rules


def _matching_sheet_filters(node: Any, tag: str, rules: list[tuple[str, _KeySelector]]) -> set[str]:
    """The filter ids a ``<style>`` rule applies to *node* - a leaf or a
    container / the root alike, so a ``g { filter }`` is charged on the group it
    filters and a ``.shadow { filter }`` only on the elements that carry
    ``shadow``, never on a group merely because a leaf below it does.

    A rule's key selector matches when its type is absent (``*``, a class / id
    selector) or equals *tag*, its id (if any) is the node's, and every class it
    names is on the node."""
    node_classes = frozenset((node.get("class") or "").split())
    node_id = node.get("id") or node.get(_XML_ID)
    matched: set[str] = set()
    for ident, (key_type, key_classes, key_id) in rules:
        if key_type is not None and key_type != tag:
            continue
        if key_id is not None and key_id != node_id:
            continue
        if key_classes <= node_classes:
            matched.add(ident)
    return matched


def _references(element: Any) -> list[tuple[str, str]]:
    """``(id, kind)`` for every element *element* draws again.

    A local ``href`` draws its target as if it stood here (``clone``: a
    ``<use>`` copy inherits from the ``<use>``) - except on an ``<a>``, a link;
    a ``url(#id)`` draws it once for this element (``once``: a clip path, a
    mask; ``filter``, which also costs its primitives' work), at every vertex
    of every shape at or below it (``marker``), or for every painted element at
    or below it (``paint``: a pattern) - markers and paint servers are inherited.
    """
    found: list[tuple[str, str]] = []
    for name, value in element.attrib.items():
        local = _local_name(name)
        if local == "href":
            value = value.strip()
            if value.startswith("#") and _local_tag(element) != "a":
                found.append((value[1:], "clone"))
            continue
        if "url(" not in value.lower():
            continue
        if local == "style":
            found.extend(_declaration_refs(value))
        else:
            found.extend((ident, _kind(local)) for ident in _local_ref_ids(value))
    return found


def _transform_scale(value: str | None) -> float:
    """An upper bound of how much the ``transform`` list *value* enlarges a length."""
    if not value or not value.strip():
        return 1.0
    scale, end = 1.0, 0
    for match in _TRANSFORM.finditer(value):
        if value[end : match.start()].strip(" \t\n\r,"):
            return _UNKNOWN_SCALE
        end = match.end()
        name, args = match.group(1), [float(n) for n in _NUMBER.findall(match.group(2))]
        if name == "matrix" and len(args) == 6:
            factor = math.hypot(*args[:4])  # the Frobenius norm bounds the largest stretch
        elif name == "scale" and args:
            factor = max(abs(a) for a in args[:2])
        elif name in ("skewX", "skewY") and args:
            factor = math.sqrt(2 + math.tan(math.radians(args[0])) ** 2)
        elif name in ("translate", "rotate"):
            factor = 1.0
        else:
            return _UNKNOWN_SCALE
        scale *= factor
    if value[end:].strip(" \t\n\r,") or not math.isfinite(scale):
        return _UNKNOWN_SCALE
    return scale


def _plain_length(value: str | None) -> float | None:
    """A length in px or an absolute unit, or ``None`` (absent, relative, or unread)."""
    match = _LENGTH.fullmatch(value) if value else None
    if not match or match.group(2) in ("%", "em", "ex"):
        return None
    number = float(match.group(1)) * _UNIT_PX[match.group(2) or "px"]
    return abs(number) if math.isfinite(number) else None


def _viewport_scale(element: Any, width: str | None, height: str | None, canvas: tuple[float, float]) -> float:
    """How much a viewBox on *element* enlarges its content in a *width* x *height* viewport."""
    view = _view_box_size(element.get("viewBox"))
    if view is None:
        return 1.0
    w = _plain_length(width) if width is not None else None
    h = _plain_length(height) if height is not None else None
    return max((w if w is not None else canvas[0]) / view[0], (h if h is not None else canvas[1]) / view[1])


def _numbers(value: str | None) -> list[float]:
    return [float(n) for n in _NUMBER.findall(value or "")]


def _bbox_area(node: Any) -> float | None:
    """The user-space bounding-box area of a simple shape, or ``None`` when unsure."""
    tag = _local_tag(node)
    if tag in ("rect", "image", "use", "svg", "foreignobject"):
        w, h = _plain_length(node.get("width")), _plain_length(node.get("height"))
        return w * h if w is not None and h is not None else None
    if tag == "circle":
        r = _plain_length(node.get("r"))
        return 4 * r * r if r is not None else None
    if tag == "ellipse":
        rx, ry = _plain_length(node.get("rx")), _plain_length(node.get("ry"))
        return 4 * rx * ry if rx is not None and ry is not None else None
    if tag == "line":
        x1, y1, x2, y2 = (_plain_length(node.get(k) or "0") for k in ("x1", "y1", "x2", "y2"))
        return abs(x2 - x1) * abs(y2 - y1) if None not in (x1, y1, x2, y2) else None
    source = node.get("points") if tag in ("polyline", "polygon") else node.get("d") if tag == "path" else None
    if source is None or (tag == "path" and re.search(r"[a-zHVAhva]", source.replace("e", "").replace("E", ""))):
        return None  # relative or single-axis path commands: the coordinates are not points
    numbers = _numbers(source)
    if len(numbers) < 2:
        return 0.0
    xs, ys = numbers[0::2], numbers[1::2]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def _fraction(value: str | None, default: float) -> float:
    match = _LENGTH.fullmatch(value) if value else None
    if not match or match.group(2) not in (None, "%"):
        return default
    number = abs(float(match.group(1))) / (100 if match.group(2) == "%" else 1)
    return number if math.isfinite(number) else default


def _filter_region(
    filter_el: Any,
    node: Any,
    scale: float,
    canvas: tuple[float, float],
    max_area: float | None = None,
    area_override: float | None = None,
) -> float:
    """The device pixels one application of *filter_el* on *node* covers (an estimate).

    *area_override* is *node*'s own bbox area in device pixels, for a container
    whose box this cannot otherwise read (a ``<g>``): the filter is then charged
    at the group it filters, never the whole canvas. *max_area*
    caps the object-bounding-box fallback (a container / text / use / image with
    no known box) at the document's drawn extent, so a small drawing on a large
    canvas is not charged at the whole canvas. A ``userSpaceOnUse`` region is
    relative to the viewport, so neither applies to it.
    """
    if filter_el.get("filterUnits") == "userSpaceOnUse":
        sides = []
        for attr, extent in (("width", canvas[0]), ("height", canvas[1])):
            raw = filter_el.get(attr)
            length = _plain_length(raw)
            if length is not None:
                sides.append(length * scale)
            else:
                sides.append(extent * _fraction(raw, 1.2) if raw is None or raw.strip().endswith("%") else extent * 1.2)
        return sides[0] * sides[1]
    fw, fh = _fraction(filter_el.get("width"), 1.2), _fraction(filter_el.get("height"), 1.2)
    area = _bbox_area(node)
    if area is None:
        if area_override is not None:
            return area_override * fw * fh
        bound = canvas[0] * canvas[1]
        if max_area is not None:
            bound = min(bound, max_area)
        return bound * fw * fh
    return area * scale * scale * fw * fh


def _primitive_cost(primitive: Any, scale: float, side: float) -> float:
    """Milliseconds per megapixel of region for one filter primitive at *scale*."""
    tag = _local_tag(primitive)
    if tag == "femorphology":
        radii = [abs(r) for r in _numbers(primitive.get("radius"))] or [0.0]
        rx, ry = radii[0], radii[1] if len(radii) > 1 else radii[0]
        return 2.5 * min(2 * rx * scale + 1, 2 * side + 1) * min(2 * ry * scale + 1, 2 * side + 1)
    if tag == "feturbulence":
        octaves = _numbers(primitive.get("numOctaves") or "1")
        return 50 + 50 * max(0.0, octaves[0] if octaves else 1.0)
    if tag == "feconvolvematrix":
        order = [abs(o) for o in _numbers(primitive.get("order") or "3")] or [3.0]
        return 25 * order[0] * (order[1] if len(order) > 1 else order[0])
    return _FILTER_COST.get(tag, _FILTER_COST_UNKNOWN)


def svg_is_bounded(
    root: Any,
    cap: int = MAX_SVG_EXPANDED_ELEMENTS,
    *,
    canvas: tuple[float, float] = (4096.0, 4096.0),
    scale: float = 1.0,
    filters: bool = True,
    _depth: int = 0,
) -> bool:
    """Whether the document *root* is safe to render and draws at most *cap* shapes.

    See :func:`_expanded_total`, which refuses by raising. *filters* ``False``
    leaves the filter work out, for a caller that charges it once the render's
    scale is known.
    """
    try:
        _expanded_total(root, cap, canvas=canvas, scale=scale, nesting=_depth, filters=filters)
    except (ValueError, RecursionError):
        return False
    return True


def _expanded_total(
    root: Any,
    cap: int = MAX_SVG_EXPANDED_ELEMENTS,
    *,
    canvas: tuple[float, float] = (4096.0, 4096.0),
    scale: float = 1.0,
    nesting: int = 0,
    filters: bool = True,
) -> tuple[int, float]:
    """``(shapes drawn, filter ms)`` of the document *root*; raises ``ValueError``
    when it is not safe to render or draws more than *cap* shapes.

    Refused: a non-local ``href`` (outside ``<a>``, a link resvg never
    follows), an ``<image>`` that is not a bounded raster ``data:`` URI or a
    nested SVG, a reference that reaches itself, nesting deeper than
    ``MAX_SVG_DEPTH``, and
    more than *cap* shapes - or ``EXPANSION_ALLOWANCE`` times the document's
    own elements, when that is more - once every reference is expanded
    (memoized per target, see :func:`_references`). A stylesheet that names a resource
    (``<style>`` ``url(#id)``) may apply it to any element, so each rendered
    element is charged for it, once per vertex for a marker. An id held by
    several elements is charged as the costliest of them - resvg draws the
    first for a ``<use>`` and the last for a ``url(#id)`` - and an ``xml:id``
    is an id.

    Filters are costed in the same pass: each application charges the region
    it covers (``_filter_region``: an explicit user-space region, a simple
    shape's bounding box, else the *canvas*, in device pixels) times its
    primitives' cost (``_primitive_cost``), at the scale the transforms and
    viewports above it give it - *scale* is the root's, device pixels per user
    unit. A ``<style>`` ``filter`` on a CONTAINER or the root filters that whole
    subtree as ONE layer over its region (a ``g { filter }`` over the group's
    own box, the drawn extent or the canvas when the box is unknown), which the
    per-leaf charge above misses, so it is charged there too on every container
    the rule's key selector matches (:func:`_matching_sheet_filters`), at the
    costliest matching filter - the cascade applies one per element. Past
    ``MAX_FILTER_WORK_MS`` in all, or one
    region past ``MAX_FILTER_REGION_PIXELS``, the SVG is refused. An estimate,
    not a proof:
    the worker's timeout and memory cap stay the backstop for what it misses.

    A nested SVG an ``<image>`` carries as a ``data:`` URI is drawn every time
    that image is (a ``<use>`` of it included): its own shapes and filter work
    are charged at the image, up to ``MAX_NESTED_SVG_DEPTH`` levels deep.
    """
    requested_cap = cap
    by_id: dict[str, list[Any]] = {}
    refs: dict[int, list[tuple[str, str]]] = {}
    sheet_refs: set[tuple[str, str]] = set()
    sheet_filter_rules: list[tuple[str, _KeySelector]] = []
    nested_images: dict[int, str] = {}
    elements = 0
    for element in root.iter():
        elements += 1
        tag = _local_tag(element)
        for name, value in element.attrib.items():
            # A url(...) paint / marker / filter target that is not local (an
            # external path or URL) would reach the filesystem or the network:
            # refuse it, whatever attribute or case it is written in.
            if _external_url(value):
                raise ValueError("a url() reaching outside the document")
            if _local_name(name) != "href" or value.strip().startswith("#"):
                continue
            if tag == "image" and _embedded_image_ok(value):
                continue
            if tag == "image" and _DATA_SVG.match(value):
                nested_images[id(element)] = value
                continue
            if tag != "a":
                raise ValueError("an href reaching outside the document")
        if tag == "style" and element.text and "url(" in element.text.lower():
            sheet_refs.update(_declaration_refs(element.text))
            sheet_filter_rules.extend(_stylesheet_filter_rules(element.text))
        found = _references(element)
        if found:
            refs[id(element)] = found
        for key in ("id", _XML_ID):
            ident = element.get(key)
            if ident:
                by_id.setdefault(ident, []).append(element)
    cap = max(cap, EXPANSION_ALLOWANCE * elements)
    # Vertices only matter where a marker is named; counting them reads every path.
    marks = any(kind == "marker" for _, kind in sheet_refs) or any(
        kind == "marker" for found in refs.values() for _, kind in found
    )
    vertex_counts: dict[int, int] = {}
    sheet = {"marker": 0, "paint": 0, "once": 0}
    # A <style> filter on a container filters that whole subtree over its box.
    # Charge it at the container's OWN box (device px, from the extent walk) so
    # a small group is not charged at the whole canvas; the doc extent is the
    # fallback cap for a container the walk could not bound.
    sheet_extent_area: float | None = None
    sheet_container_areas: dict[int, float] = {}
    if sheet_filter_rules:
        walked = _extent_walk(root)
        if walked is not None:
            (extent_w, extent_h), boxes_user = walked
            sheet_extent_area = (extent_w * scale) * (extent_h * scale)
            sheet_container_areas = {node_id: area * scale * scale for node_id, area in boxes_user.items()}
    memo: dict[int, tuple[int, int, int, int, float]] = {}
    active: set[int] = set()

    def vertices_of(node: Any) -> int:
        if not marks:
            return 0
        key = id(node)
        if key not in vertex_counts:
            vertex_counts[key] = _vertices(node)
        return vertex_counts[key]

    def own_scale(node: Any, is_root: bool) -> float:
        """How much *node* enlarges its content: its transform, and a nested viewport."""
        factor = _transform_scale(node.get("transform"))
        if not is_root and _local_tag(node) == "svg":
            factor *= _viewport_scale(node, node.get("width"), node.get("height"), canvas)
        return factor

    def site_scale(ident: str, node: Any) -> float:
        """How much a ``<use>`` enlarges a ``<symbol>`` whose viewBox it fills."""
        symbols = [t for t in by_id.get(ident, ()) if _local_tag(t) == "symbol"]
        return max((_viewport_scale(t, node.get("width"), node.get("height"), canvas) for t in symbols), default=1.0)

    chain_memo: dict[int, list[tuple[dict[str, str], list[Any]]]] = {}
    chain_active: set[int] = set()

    def filter_variants(target: Any) -> list[tuple[dict[str, str], list[Any]]]:
        """Every ``(region attributes, primitives)`` the filter *target* draws with,
        inherited through its ``href`` chain as resvg follows it (its own
        attributes first; a hop to any filter holding the id; a cycle draws nothing)."""
        key = id(target)
        if key in chain_memo:
            return chain_memo[key]
        own = {name: value for name in _FILTER_REGION_ATTRIBUTES if (value := target.get(name)) is not None}
        primitives = [p for p in target if _local_tag(p).startswith("fe")]
        variants = [(own, primitives)]
        if not primitives and key not in chain_active:
            chain_active.add(key)
            href = next((v.strip()[1:] for n, v in target.attrib.items() if _local_name(n) == "href" and v.strip().startswith("#")), "")
            inherited = [
                ({**attrs, **own}, parent_primitives)
                for parent in by_id.get(href, ())
                if _local_tag(parent) == "filter"
                for attrs, parent_primitives in filter_variants(parent)
            ]
            chain_active.discard(key)
            if len(inherited) > _MAX_FILTER_VARIANTS:
                raise ValueError("a filter inheriting from too many others")
            variants = inherited or variants
        chain_memo[key] = variants
        return variants

    def filter_work(
        ident: str, node: Any, s: float, max_area: float | None = None, area_override: float | None = None
    ) -> float:
        """Estimated milliseconds of one application of the filter *ident* on *node* at scale *s*."""
        worst = 0.0
        if not filters:
            return worst
        for target in by_id.get(ident, ()):
            if _local_tag(target) != "filter":
                continue
            for attrs, primitives in filter_variants(target):
                region = _filter_region(attrs, node, s, canvas, max_area, area_override)
                if not region <= MAX_FILTER_REGION_PIXELS:
                    return math.inf
                side = math.sqrt(max(region, 0.0))
                worst = max(worst, region / 1e6 * sum(_primitive_cost(p, s, side) for p in primitives))
        return worst

    nested_memo: dict[str, tuple[int, float]] = {}

    def nested_cost(href: str) -> tuple[int, float]:
        """``(shapes, filter ms)`` a nested ``data:`` SVG costs each time its image draws."""
        if href not in nested_memo:
            if nesting + 1 > MAX_NESTED_SVG_DEPTH:
                raise ValueError("nested SVG images too deep")
            data = _nested_svg_bytes(href.strip())
            if not data or len(data) > MAX_SVG_BYTES:
                raise ValueError("a nested SVG image that cannot be read")
            try:
                nested_root = _parse_svg_root(data)
            except Exception as exc:
                raise ValueError("a nested SVG image that cannot be parsed") from exc
            nested_memo[href] = _expanded_total(nested_root, requested_cap, nesting=nesting + 1, filters=filters)
        return nested_memo[href]

    def drawn(ident: str) -> tuple[int, int, int, int, float]:
        best = (0, 0, 0, 0, 0.0)
        for target in by_id.get(ident, ()):
            if _local_tag(target) not in _FLAT_REFERENCES:
                total, painted, vertices, depth, work = expanded(target)
                best = (
                    max(best[0], total),
                    max(best[1], painted),
                    max(best[2], vertices),
                    max(best[3], depth),
                    max(best[4], work),
                )
        return best

    def expanded(element: Any) -> tuple[int, int, int, int, float]:
        """``(shapes drawn, painted elements, marker vertices, depth, filter ms)`` of *element*."""
        key = id(element)
        if key in memo:
            return memo[key]
        if key in active:
            raise ValueError("a reference reaches itself")
        active.add(key)
        # The walk starts at the element's own scale; a referenced element is
        # costed at unit scale and its work grown by the scale of each site.
        base = scale if element is root else 1.0
        order: list[tuple[Any, float]] = []
        stack = [(element, own_scale(element, element is root))]
        while stack:
            node, rel = stack.pop()
            order.append((node, rel))
            stack.extend((child, rel * own_scale(child, False)) for child in node)
        sums: dict[int, tuple[int, int, int, int, float]] = {}
        total, filters = 0, 0.0
        # Children before their parent, so what a group names can be charged
        # for every shape below it; ``total`` is the running sum, checked as it
        # grows so a refusal never costs the whole walk.
        for node, rel in reversed(order):
            tag = _local_tag(node)
            s = base * rel
            own_vertices = vertices_of(node)
            below, painted, vertices, depth, work_below = 0, int(tag in _PAINTED), own_vertices, 0, 0.0
            for child in node:
                child_below, child_painted, child_vertices, child_depth, child_work = sums.pop(id(child))
                below += child_below
                painted += child_painted
                vertices += child_vertices
                depth = max(depth, child_depth)
                work_below += child_work
            here, marker, paint = 1, 0, 0
            work, marker_work, paint_work = 0.0, 0.0, 0.0
            if id(node) in nested_images:
                nested_shapes, nested_work = nested_cost(nested_images[id(node)])
                here += nested_shapes
                work += nested_work
            for ident, kind in refs.get(id(node), ()):
                ref_total, ref_painted, ref_vertices, ref_depth, ref_work = drawn(ident)
                depth = max(depth, ref_depth)
                grown = ref_work * max(1.0, s * site_scale(ident, node)) ** 4 if ref_work else 0.0
                if kind == "marker":
                    marker += ref_total
                    marker_work += grown
                elif kind == "paint":
                    paint += ref_total
                    paint_work += grown
                else:
                    here += ref_total
                    work += grown
                    if kind == "clone":
                        painted += ref_painted
                        vertices += ref_vertices
                    elif kind == "filter":
                        work += filter_work(ident, node, s)
            here += marker * max(1, vertices) + paint * max(1, painted)
            work += marker_work * max(1, vertices) + paint_work * max(1, painted)
            if tag in _MARKABLE:
                here += sheet["marker"] * max(1, own_vertices)
            if tag in _PAINTED:
                here += sheet["paint"]
            if tag in _RENDERED:
                here += sheet["once"]
            if sheet_filter_rules:
                # A <style> filter is charged on the elements its selector
                # matches - a leaf, or a container / the root, which resvg
                # filters as ONE layer over its box (the per-leaf charge missed
                # that). resvg applies one filter per element (the
                # cascade winner), so the costliest match is the honest bound; a
                # container is charged at its OWN box (never re-charged on its
                # children), the drawn extent when its box is unknown.
                matched = _matching_sheet_filters(node, tag, sheet_filter_rules)
                if matched:
                    override = None if tag in _RENDERED else sheet_container_areas.get(id(node))
                    fallback = None if tag in _RENDERED else sheet_extent_area
                    work += max(filter_work(ident, node, s, fallback, override) for ident in matched)
            total += here
            filters += work
            if total > cap:
                raise ValueError("too many shapes")
            if not filters <= MAX_FILTER_WORK_MS:
                raise ValueError("filters too costly")
            if depth >= MAX_SVG_DEPTH:
                raise ValueError("nested too deep")
            sums[id(node)] = (below + here, painted, vertices, depth + 1, work_below + work)
        active.discard(key)
        memo[key] = sums[id(element)]
        return memo[key]

    for ident, kind in sheet_refs:
        sheet["once" if kind == "filter" else kind] += drawn(ident)[0]
    if max(sheet.values()) > cap:
        raise ValueError("too many shapes")
    memo.clear()  # the targets counted above carried no stylesheet charge
    shapes, _painted, _vertices_below, _depth, work = expanded(root)
    return shapes, work


def _view_box_size(value: str | None) -> tuple[float, float] | None:
    """The ``viewBox`` size as resvg reads it, or ``None`` for one it would ignore."""
    match = _VIEW_BOX.fullmatch(value) if value else None
    if not match:
        return None
    numbers = [float(part) for part in match.groups()]
    if not all(math.isfinite(number) for number in numbers) or numbers[2] <= 0 or numbers[3] <= 0:
        return None
    return numbers[2], numbers[3]


#: A rule's declarations: a ``{...}`` holding no other block (an ``@media`` block holds rules).
_CSS_BLOCK = re.compile(r"\{([^{}]*)\}")
_CSS_PROPERTY = re.compile(r"-?[a-z][a-z0-9-]*")
_FONT_SIZE_PROPERTIES = frozenset({"font-size", "font"})


def _split_declarations(block: str) -> list[str]:
    """*block* split at each ``;`` outside parentheses and quotes (``url(data:...;base64,...)``)."""
    parts, depth, quote, start = [], 0, "", 0
    for i, ch in enumerate(block):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            parts.append(block[start:i])
            start = i + 1
    parts.append(block[start:])
    return parts


def _css_declarations(root: Any) -> list[tuple[str, str]] | None:
    """``(property, value)`` of every CSS declaration of *root*, from its ``style``
    attributes and ``<style>`` elements, property names lower-cased - or ``None``
    when a backslash (a CSS escape) could spell a property this does not read,
    or a string left open hides where a rule ends (:func:`_css_text`)."""
    sources: list[tuple[str, bool]] = []
    for element in root.iter():
        if _local_tag(element) == "style" and element.text:
            sources.append((element.text, True))
        style = element.get("style")
        if style:
            sources.append((style, False))
    declarations: list[tuple[str, str]] = []
    for raw, sheet in sources:
        text = None if "\\" in raw else _css_text(raw)
        if text is None:
            return None
        for block in _CSS_BLOCK.findall(text) if sheet else (text,):
            for declaration in _split_declarations(block):
                name, sep, value = declaration.partition(":")
                if sep or name.strip():
                    declarations.append((name.strip().lower(), value.strip()))
    return declarations


def _default_font_size(root: Any) -> bool:
    """Whether nothing in the document can change the font size an ``em`` on the root reads:
    a ``font-size`` / ``font`` on the root, or one anywhere in its CSS (a rule may select it)."""
    if any(_local_name(name) in _FONT_SIZE_PROPERTIES for name in root.attrib):
        return False
    declarations = _css_declarations(root)
    return declarations is not None and not any(name in _FONT_SIZE_PROPERTIES for name, _ in declarations)


def intrinsic_size(root: Any) -> tuple[float, float] | None:
    """The size resvg renders *root* at on its own, or ``None`` when unsure.

    Read as resvg reads it: a ``width`` / ``height`` in px (or unitless) or an
    absolute unit at ``RENDER_DPI``, a percentage of the ``viewBox``, an absent
    one = 100% - and so is one resvg cannot read (``"100 "``, ``"100PX"``) -
    and ``em`` / ``ex`` at the default font size. ``None`` for anything else -
    above all when resvg would take the size from the document's CONTENT (no
    usable ``viewBox`` and a missing or relative dimension), which
    :func:`_content_extent` bounds instead, and an ``em`` / ``ex`` size under a
    font size the document sets, which :func:`_font_size_bound` bounds.
    """
    return _root_size(
        root,
        _view_box_size(root.get("viewBox")),
        lambda: _DEFAULT_EM_PX if _default_font_size(root) else None,
    )


def _root_size(
    root: Any, view: tuple[float, float] | None, em_px: Callable[[], float | None]
) -> tuple[float, float] | None:
    """*root*'s ``width`` / ``height`` as :func:`intrinsic_size` reads them, an
    ``em`` being ``em_px()`` px (an ``ex`` half of it) - ``None`` when that is
    ``None``, or when the size is not one this reads."""
    font: float | None = None
    size: list[float] = []
    for attribute, index in (("width", 0), ("height", 1)):
        raw = root.get(attribute)
        match = _LENGTH.fullmatch(raw) if raw is not None else None
        if match is None:
            number, unit = 100.0, "%"
        else:
            number, unit = float(match.group(1)), match.group(2) or "px"
        if unit == "%":
            if view is None:
                return None
            value = view[index] * number / 100
        elif unit in ("em", "ex"):
            if font is None:
                font = em_px()
                if font is None:
                    return None
            value = number * (font if unit == "em" else font / 2)
        else:
            value = number * _UNIT_PX[unit]
        if not math.isfinite(value) or value <= 0:
            return None
        size.append(value)
    return size[0], size[1]


def _sized_by_content(root: Any, view: tuple[float, float] | None) -> bool:
    """Whether resvg takes *root*'s size from what it draws: no usable
    ``viewBox``, and a ``width`` or ``height`` missing, relative or unread."""
    if view is not None:
        return False
    for attribute in ("width", "height"):
        match = _LENGTH.fullmatch(root.get(attribute) or "")
        if match is None or match.group(2) == "%":
            return True
    return False


#: ``font-size`` keywords as resvg reads them: the default size times 1.2 per
#: step from ``medium`` (``larger`` / ``smaller``: a step from the parent's size,
#: which for the root is the default).
_FONT_SIZE_STEPS = {
    "xx-small": -3, "x-small": -2, "small": -1, "medium": 0, "large": 1, "x-large": 2, "xx-large": 3,
    "larger": 1, "smaller": -1,
}  # fmt: skip
#: The values that leave the root at the default size (its parent's, the initial one).
_FONT_SIZE_DEFAULTS = frozenset({"inherit", "initial", "unset", "revert", "revert-layer"})
#: A relative font size is the parent's size - the default, for the root - over this.
_FONT_SIZE_RELATIVE = {"em": 1, "ex": 2, "%": 100}
_IMPORTANT = re.compile(rf"!{_WS}*important{_WS}*$", re.IGNORECASE)
#: A ``font-size`` length: a number, its unit (none is px), nothing else.
_FONT_SIZE_LENGTH = re.compile(rf"{_WS}*({_NUM})(px|em|ex|in|cm|mm|pt|pc|%)?{_WS}*", re.IGNORECASE)
#: Every size a ``font`` shorthand may set: a length WITH its unit, or a keyword.
_FONT_SHORTHAND_SIZES = re.compile(
    rf"(?<![\w.#-])(?:({_NUM})(px|em|ex|in|cm|mm|pt|pc|%)|({'|'.join(_FONT_SIZE_STEPS)}))(?![\w.%-])",
    re.IGNORECASE,
)


def _font_size_px(prop: str, value: str) -> float | None:
    """The largest font size (px) the ``font-size`` / ``font`` declaration *value*
    may give the root - an absolute unit at ``RENDER_DPI``, never less than resvg
    reads it, a relative one against the default - or ``None`` when it names no
    size resvg reads. resvg reads a number with a unit (none is px) or a size
    keyword, and ignores anything else (a function, a variable, a system font,
    another unit): this reads the same, case-insensitively and wherever a
    shorthand may hold it, so what it cannot read resvg cannot either."""
    word = _IMPORTANT.sub("", value).strip().lower()
    if word in _FONT_SIZE_DEFAULTS:
        return _DEFAULT_EM_PX
    if prop == "font-size":
        match = _FONT_SIZE_LENGTH.fullmatch(word)
        if match:
            found = [(match.group(1), match.group(2) or "px", "")]
        else:
            found = [("", "", word)] if word in _FONT_SIZE_STEPS else []
    else:
        found = _FONT_SHORTHAND_SIZES.findall(word)
    sizes: list[float] = []
    for number, unit, keyword in found:
        if keyword:
            sizes.append(_DEFAULT_EM_PX * 1.2 ** _FONT_SIZE_STEPS[keyword])
        elif unit in _FONT_SIZE_RELATIVE:
            sizes.append(abs(float(number)) * _DEFAULT_EM_PX / _FONT_SIZE_RELATIVE[unit])
        else:
            sizes.append(abs(float(number)) * _UNIT_PX[unit])
    size = max(sizes, default=math.inf)
    return size if math.isfinite(size) else None


def _font_size_bound(root: Any) -> float | None:
    """An upper bound of the font size (px) an ``em`` on the root reads, or
    ``None`` when one cannot be told.

    resvg takes it from the root's ``font-size`` / ``font`` attribute, its
    ``style`` or a stylesheet rule - whichever the cascade picks, and it is
    never larger than the largest of them all and of the default (a value
    naming no size it reads leaves the default). Every ``font-size`` / ``font``
    declaration of every ``<style>`` counts, whatever its selector: a rule this
    reads as not selecting the root must never make the bound too small, or a
    pixmap past the box would be drawn at that size.
    """
    declarations = [(_local_name(name), value) for name, value in root.attrib.items()]
    sources = [root.get("style") or ""]
    sources += [element.text for element in root.iter() if _local_tag(element) == "style" and element.text]
    for raw in sources:
        # A CSS escape could spell a property this does not read; a string left
        # open hides where the declarations end.
        text = None if "\\" in raw else _css_text(raw)
        if text is None:
            return None
        # One split serves a style attribute and a whole sheet: a selector
        # fragment has no ":" or one whose left side (``a`` of ``a:hover``) is
        # no font property, so it is read as nothing - and a declaration of a
        # rule that does not select the root is read too, which only raises it.
        for declaration in re.split(r"[;{}]", text):
            name, sep, value = declaration.partition(":")
            if sep:
                declarations.append((name.strip().lower(), value))
    sizes = (_font_size_px(name, value) for name, value in declarations if name in _FONT_SIZE_PROPERTIES)
    return max([_DEFAULT_EM_PX, *(size for size in sizes if size is not None)])


#: Elements whose subtree is drawn only through a reference, never where it
#: stands (and animation / metadata elements, which draw nothing at all).
_DRAWN_BY_REFERENCE = frozenset(
    {
        "defs", "symbol", "marker", "pattern", "mask", "clippath", "filter", "lineargradient",
        "radialgradient", "style", "title", "desc", "metadata", "script", "animate",
        "animatetransform", "animatemotion", "set", "mpath", "view", "cursor",
    }
)  # fmt: skip
#: Elements drawing their children where they stand.
_CONTAINERS = frozenset({"g", "a", "switch"})
_SVG_TAG_PREFIX = "{http://www.w3.org/2000/svg}"
#: How wide one character may draw, in font sizes: 2 for the scripts whose glyphs
#: stay within about an em (Latin, Greek, Cyrillic, CJK), 8 for any other - an
#: Arabic ligature such as U+FDFD is several ems wide.
_NARROW_GLYPH_EM, _WIDE_GLYPH_EM = 2.0, 8.0
#: How far a line of text may draw above or below its baseline, in font sizes.
_GLYPH_HEIGHT_EM = 1.5
#: CSS properties that cannot move, size or add what resvg draws (paint, what an
#: effect or a hidden element takes away, the font face, text placement inside
#: the glyph allowance, and the HTML-only properties of a label stylesheet, which
#: resvg ignores): CSS made of them leaves a content-sized SVG bounded. Any other
#: property - ``transform``, a geometry property, letter spacing, ``white-space``,
#: ``overflow`` ... - leaves it unbounded. ``font-size`` / ``font``, ``stroke-width``
#: and the marker properties enlarge the bound instead (:func:`_css_bound`).
_CSS_BOUND_SAFE = frozenset(
    {
        "fill", "fill-opacity", "fill-rule", "stroke", "stroke-opacity", "stroke-dasharray",
        "stroke-dashoffset", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "opacity",
        "color", "stop-color", "stop-opacity", "flood-color", "flood-opacity", "lighting-color",
        "solid-color", "solid-opacity", "paint-order", "mix-blend-mode", "isolation", "vector-effect",
        "color-interpolation", "color-interpolation-filters", "color-rendering", "shape-rendering",
        "text-rendering", "image-rendering", "filter", "clip-path", "clip-rule", "mask", "mask-type",
        "display", "visibility", "font-family", "font-weight", "font-style", "font-variant",
        "font-stretch", "font-kerning", "font-feature-settings", "font-variation-settings",
        "font-display", "src", "unicode-range", "text-anchor", "dominant-baseline",
        "alignment-baseline", "text-decoration", "text-decoration-line", "text-decoration-color",
        "text-decoration-style", "direction", "unicode-bidi", "line-height", "text-align",
        "max-width", "cursor", "pointer-events", "user-select", "box-sizing", "box-shadow",
        "word-break", "overflow-wrap", "word-wrap", "will-change",
    }
)  # fmt: skip
#: The families of HTML-only and animation properties resvg ignores (``padding-top``...).
_CSS_BOUND_SAFE_PREFIXES = ("padding", "margin", "border", "background", "outline", "animation", "transition", "font-variant-")
#: A ``font-size`` / ``stroke-width`` value this reads: px, unitless or pt.
_CSS_LENGTH = re.compile(rf"{_WS}*({_NUM})(px|pt)?{_WS}*(?:!{_WS}*important{_WS}*)?", re.IGNORECASE)
#: The size of a ``font`` shorthand: a px / pt length, before an optional ``/line-height``.
_CSS_FONT_SHORTHAND_SIZE = re.compile(rf"(?<![\w.#-])({_NUM})(px|pt)(?:{_WS}*/{_WS}*[^ \t\n\r,]+)?(?={_WS}|$)", re.IGNORECASE)


def _css_px(match: re.Match[str] | None) -> float | None:
    """A matched CSS length in px - a pt read at ``RENDER_DPI``, never less than resvg reads it."""
    if match is None:
        return None
    number = abs(float(match.group(1))) * (_UNIT_PX["pt"] if (match.group(2) or "").lower() == "pt" else 1.0)
    return number if math.isfinite(number) else None


class _CssBound(NamedTuple):
    """What a document's CSS may add to what it draws, taken everywhere a rule may apply."""

    font: float  #: the largest font size it sets (px)
    stroke: float  #: the largest stroke width it sets (px), for markers in stroke-width units
    markers: str  #: every marker it names


def _css_bound(root: Any) -> _CssBound | None:
    """How the document's CSS may enlarge what it draws (:class:`_CssBound`), or
    ``None`` when it may move or size it in a way this cannot bound: a property
    outside ``_CSS_BOUND_SAFE``, a length it cannot read, an escape."""
    declarations = _css_declarations(root)
    if declarations is None:
        return None
    font = stroke = 0.0
    markers: list[str] = []
    for name, value in declarations:
        if not _CSS_PROPERTY.fullmatch(name):
            return None
        if name.startswith("-") or name in _CSS_BOUND_SAFE or name.startswith(_CSS_BOUND_SAFE_PREFIXES):
            continue  # a leading "-": vendor-prefixed or custom, a property resvg does not know
        if name in _MARKER_PROPERTIES:
            markers.append(value)
            continue
        if name == "font":
            length = _css_px(_CSS_FONT_SHORTHAND_SIZE.search(value))
        elif name in ("font-size", "stroke-width"):
            length = _css_px(_CSS_LENGTH.fullmatch(value))
        else:
            return None
        if length is None:
            return None
        if name == "stroke-width":
            stroke = max(stroke, length)
        else:
            font = max(font, length)
    return _CssBound(font, stroke, " ".join(markers))
#: A number list that carries a unit other than px (``%``, ``em``...).
_NOT_PX_LIST = re.compile(r"[^0-9eE.+\-\s,px]")
_Affine = tuple[float, float, float, float, float, float]
_IDENTITY: _Affine = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _multiply(m: _Affine, n: _Affine) -> _Affine:
    """The matrix ``m x n`` (SVG's ``a b c d e f``): *n* applied first, then *m*."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2, a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def _transform_matrix(value: str | None) -> _Affine | None:
    """The matrix a ``transform`` list names, or ``None`` when this cannot read it."""
    if not value or not value.strip():
        return _IDENTITY
    matrix, end = _IDENTITY, 0
    for match in _TRANSFORM.finditer(value):
        if value[end : match.start()].strip(" \t\n\r,"):
            return None
        end = match.end()
        name, args = match.group(1), [float(n) for n in _NUMBER.findall(match.group(2))]
        if name == "matrix" and len(args) == 6:
            step: _Affine = (args[0], args[1], args[2], args[3], args[4], args[5])
        elif name == "translate" and len(args) in (1, 2):
            step = (1.0, 0.0, 0.0, 1.0, args[0], args[1] if len(args) == 2 else 0.0)
        elif name == "scale" and len(args) in (1, 2):
            step = (args[0], 0.0, 0.0, args[1] if len(args) == 2 else args[0], 0.0, 0.0)
        elif name == "rotate" and len(args) in (1, 3):
            cos, sin = math.cos(math.radians(args[0])), math.sin(math.radians(args[0]))
            step = (cos, sin, -sin, cos, 0.0, 0.0)
            if len(args) == 3:
                around = (1.0, 0.0, 0.0, 1.0, args[1], args[2])
                step = _multiply(_multiply(around, step), (1.0, 0.0, 0.0, 1.0, -args[1], -args[2]))
        elif name == "skewX" and len(args) == 1:
            step = (1.0, 0.0, math.tan(math.radians(args[0])), 1.0, 0.0, 0.0)
        elif name == "skewY" and len(args) == 1:
            step = (1.0, math.tan(math.radians(args[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            return None
        matrix = _multiply(matrix, step)
    if value[end:].strip(" \t\n\r,") or not all(math.isfinite(v) for v in matrix):
        return None
    return matrix


def _user_length(value: str | None, default: float | None = 0.0) -> float | None:
    """A coordinate / length in user units - px, unitless, or an absolute unit
    (read at ``RENDER_DPI``, never less than resvg reads it) - *default* when
    absent, ``None`` for a relative or unreadable one."""
    if value is None:
        return default
    match = _LENGTH.fullmatch(value)
    if not match or match.group(2) in ("%", "em", "ex"):
        return None
    number = float(match.group(1)) * _UNIT_PX[match.group(2) or "px"]
    return number if math.isfinite(number) else None


def _path_points(d: str) -> list[tuple[float, float]] | None:
    """Points bounding the path ``d``: its vertices, its curves' control points
    (a Bezier stays within them) and each arc's endpoints widened by its
    diameter or its chord, whichever is larger; ``None`` for a malformed one."""
    points: list[tuple[float, float]] = []
    x = y = start_x = start_y = 0.0
    for match in _PATH_COMMAND.finditer(d):
        command = match.group(1)
        low, relative = command.lower(), command.islower()
        numbers = [float(n) for n in _NUMBER.findall(match.group(2))]
        if low == "z":
            x, y = start_x, start_y
            points.append((x, y))
            continue
        per = _PATH_SET[low][0]
        if not numbers or len(numbers) % per:
            return None
        for i in range(0, len(numbers), per):
            group = numbers[i : i + per]
            if low in "mlt":
                x, y = (x + group[0], y + group[1]) if relative else (group[0], group[1])
                if low == "m" and i == 0:
                    start_x, start_y = x, y
                points.append((x, y))
            elif low == "h":
                x = x + group[0] if relative else group[0]
                points.append((x, y))
            elif low == "v":
                y = y + group[0] if relative else group[0]
                points.append((x, y))
            elif low in "csq":
                pairs = [(group[j], group[j + 1]) for j in range(0, per, 2)]
                pairs = [(x + px, y + py) for px, py in pairs] if relative else pairs
                points.extend(pairs)
                x, y = pairs[-1]
            else:  # an arc
                end_x, end_y = (x + group[5], y + group[6]) if relative else (group[5], group[6])
                reach = max(2 * max(abs(group[0]), abs(group[1])), math.hypot(end_x - x, end_y - y))
                for px, py in ((x, y), (end_x, end_y)):
                    points.extend(((px - reach, py - reach), (px + reach, py + reach)))
                x, y = end_x, end_y
    return points


def _number_list(value: str | None) -> list[float] | None:
    """The numbers of an ``x`` / ``y`` / ``points`` list, ``None`` if one carries a relative unit."""
    if value is None:
        return []
    if _NOT_PX_LIST.search(value):
        return None
    return [float(n) for n in _NUMBER.findall(value)]


def _content_extent(root: Any) -> tuple[float, float] | None:
    """An upper bound of the size resvg gives *root* from its CONTENT, or ``None``
    (see :func:`_extent_walk`)."""
    walked = _extent_walk(root)
    return None if walked is None else walked[0]


def _extent_walk(root: Any) -> tuple[tuple[float, float], dict[int, float]] | None:
    """``(far corner, {id(container): bbox area})`` of *root*, or ``None``.

    resvg sizes an SVG with no usable ``viewBox`` and no definite width AND
    height by the far corner, from the origin, of what it draws (markers
    included, strokes and filter regions not; an empty document is 100 x 100).
    This bounds that corner from the markup: every shape's points through the
    transforms above it, a ``<use>`` through its target, a marker by its
    clipped viewport, a text by a generous glyph width - and whatever its CSS
    adds, taken everywhere a rule may apply (:func:`_css_bound`). Anything it
    cannot bound - CSS that could move or size what is drawn, a relative length,
    a text offset list, an element it does not know - gives ``None``, and the
    caller fits the render into its box. Every container's OWN bbox area (in
    user units) is recorded on the way, so a ``<style>`` filter on a group is
    charged at the group's box, not the whole canvas. Walks every
    ``<use>`` expansion once, so call it only on a document the shape budget
    already bounded.
    """
    css = _css_bound(root)
    if root.get("transform") or css is None:
        return None
    by_id: dict[str, Any] = {}
    for element in root.iter():
        for key in ("id", _XML_ID):
            ident = element.get(key)
            if ident and ident not in by_id:
                by_id[ident] = element
    far = [0.0, 0.0]
    active: set[int] = set()
    # Each open container's running [minx, miny, maxx, maxy]; a shape widens
    # every one it is inside, so each container ends with its own subtree box.
    container_stack: list[list[float]] = []
    container_boxes: dict[int, float] = {}

    def reach_of_markers(markers: tuple[str, ...], stroke: float) -> float | None:
        reach = 0.0
        for value in markers:
            for ident in _local_ref_ids(value):
                marker = by_id.get(ident)
                if marker is None or _local_tag(marker) != "marker":
                    continue
                if (marker.get("overflow") or "hidden").strip() not in ("hidden", "scroll"):
                    return None
                width, height = _user_length(marker.get("markerWidth"), 3.0), _user_length(marker.get("markerHeight"), 3.0)
                ref_x, ref_y = _user_length(marker.get("refX")), _user_length(marker.get("refY"))
                if None in (width, height, ref_x, ref_y):
                    return None
                view = _view_box_size(marker.get("viewBox"))
                factor = max(1.0, width / view[0], height / view[1]) if view else 1.0  # type: ignore[operator]
                units = max(stroke, css.stroke) if (marker.get("markerUnits") or "strokeWidth") != "userSpaceOnUse" else 1.0
                reach = max(
                    reach,
                    (2 * math.hypot(width, height) + math.hypot(ref_x, ref_y) * factor) * units,  # type: ignore[arg-type]
                )
        return reach

    def add(points: list[tuple[float, float]], matrix: _Affine, reach: float) -> None:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        corners = (
            (min(xs) - reach, min(ys) - reach),
            (max(xs) + reach, min(ys) - reach),
            (min(xs) - reach, max(ys) + reach),
            (max(xs) + reach, max(ys) + reach),
        )
        a, b, c, d, e, f = matrix
        for px, py in corners:
            tx, ty = a * px + c * py + e, b * px + d * py + f
            far[0], far[1] = max(far[0], tx), max(far[1], ty)
            for acc in container_stack:
                acc[0], acc[1] = min(acc[0], tx), min(acc[1], ty)
                acc[2], acc[3] = max(acc[2], tx), max(acc[3], ty)

    def shape_points(element: Any, tag: str, font: float) -> list[tuple[float, float]] | None:
        get = element.get
        if tag in ("rect", "image", "foreignobject", "svg"):
            x, y, w, h = _user_length(get("x")), _user_length(get("y")), _user_length(get("width"), None), _user_length(get("height"), None)
            if None in (x, y, w, h):
                return None
            return [(x, y), (x + w, y + h)]  # type: ignore[operator]
        if tag == "circle":
            cx, cy, r = _user_length(get("cx")), _user_length(get("cy")), _user_length(get("r"), None)
            if None in (cx, cy, r):
                return None
            return [(cx - r, cy - r), (cx + r, cy + r)]  # type: ignore[operator]
        if tag == "ellipse":
            cx, cy = _user_length(get("cx")), _user_length(get("cy"))
            rx, ry = _user_length(get("rx"), None), _user_length(get("ry"), None)
            rx, ry = (rx if rx is not None else ry), (ry if ry is not None else rx)
            if None in (cx, cy, rx, ry):
                return None
            return [(cx - rx, cy - ry), (cx + rx, cy + ry)]  # type: ignore[operator]
        if tag == "line":
            coords = [_user_length(get(k)) for k in ("x1", "y1", "x2", "y2")]
            if None in coords:
                return None
            return [(coords[0], coords[1]), (coords[2], coords[3])]  # type: ignore[list-item]
        if tag in ("polyline", "polygon"):
            numbers = _number_list(get("points"))
            if numbers is None:
                return None
            return list(zip(numbers[0::2], numbers[1::2], strict=False)) or [(0.0, 0.0)]
        if tag == "path":
            return _path_points(get("d") or "") if get("d") else [(0.0, 0.0)]
        if tag == "text":
            if any(_local_tag(n) == "textpath" or any(n.get(k) is not None for k in ("dx", "dy", "rotate", "textLength")) for n in element.iter()):
                return None
            chars = "".join(element.itertext())
            if not any(n.get(_XML_SPACE) == "preserve" for n in element.iter()):
                chars = " ".join(chars.split())  # resvg collapses whitespace, as SVG does by default
            sizes = [font] + [_user_length(n.get("font-size"), font) for n in element.iter() if n is not element]
            xs = [v for n in element.iter() for v in (_number_list(n.get("x")) or [])]
            ys = [v for n in element.iter() for v in (_number_list(n.get("y")) or [])]
            if None in sizes or any(_number_list(n.get(k)) is None for n in element.iter() for k in ("x", "y")):
                return None
            em = max(*sizes, css.font)  # type: ignore[type-var]
            narrow = all(ord(c) < 0x0590 or 0x2E80 <= ord(c) <= 0x9FFF or 0xAC00 <= ord(c) <= 0xD7AF or 0xFF00 <= ord(c) <= 0xFFEF for c in chars)
            width = len(chars) * em * (_NARROW_GLYPH_EM if narrow else _WIDE_GLYPH_EM)
            height = em * _GLYPH_HEIGHT_EM
            xs, ys = xs or [0.0], ys or [0.0]
            return [(min(xs) - width, min(ys) - height), (max(xs) + width, max(ys) + height)]
        return None

    def enter_container(element: Any, walk_children: Any) -> bool:
        acc = [math.inf, math.inf, -math.inf, -math.inf]
        container_stack.append(acc)
        try:
            ok = walk_children()
        finally:
            container_stack.pop()
        if ok and acc[2] >= acc[0]:  # drew something: record its box, widen the parent
            container_boxes[id(element)] = (acc[2] - acc[0]) * (acc[3] - acc[1])
            for parent in container_stack:
                parent[0], parent[1] = min(parent[0], acc[0]), min(parent[1], acc[1])
                parent[2], parent[3] = max(parent[2], acc[2]), max(parent[3], acc[3])
        return ok

    def walk(element: Any, matrix: _Affine, font: float, stroke: float, markers: tuple[str, ...], depth: int) -> bool:
        """Grow ``far`` and the open container boxes by what *element* draws; ``False`` when unbounded."""
        if not isinstance(element.tag, str) or (element.tag.startswith("{") and not element.tag.startswith(_SVG_TAG_PREFIX)):
            return True  # another namespace's element: resvg draws nothing of it (an un-namespaced one it draws)
        if depth > MAX_SVG_DEPTH or id(element) in active:
            return False
        tag = _local_tag(element)
        if tag in _DRAWN_BY_REFERENCE:
            return True
        own = _transform_matrix(element.get("transform"))
        font_size = _user_length(element.get("font-size"), font)
        stroke_width = _user_length(element.get("stroke-width"), stroke)
        if own is None or font_size is None or stroke_width is None:
            return False
        # Markers are inherited (a group's marker-end is drawn on every shape in
        # it, a <use>'s on its copy): every candidate is kept, an upper bound.
        shorthand = element.get("marker") or ""
        markers = tuple(f"{inherited} {shorthand} {element.get(prop) or ''}" for prop, inherited in zip(_MARKER_LONGHANDS, markers, strict=True))
        here = _multiply(matrix, own)
        if tag in _CONTAINERS or (tag == "svg" and depth == 0):
            return enter_container(
                element, lambda: all(walk(child, here, font_size, stroke_width, markers, depth + 1) for child in element)
            )
        if tag == "use":
            target = by_id.get(next((v.strip()[1:] for n, v in element.attrib.items() if _local_name(n) == "href" and v.strip().startswith("#")), ""))
            if target is None:
                return True  # a reference to nothing draws nothing
            x, y = _user_length(element.get("x")), _user_length(element.get("y"))
            if x is None or y is None:
                return False
            placed = _multiply(here, (1.0, 0.0, 0.0, 1.0, x, y))
            if _local_tag(target) in ("symbol", "svg"):
                w, h = _user_length(element.get("width"), None), _user_length(element.get("height"), None)
                if w is None or h is None:
                    return False
                add([(0.0, 0.0), (w, h)], placed, 0.0)
                return True
            active.add(id(element))
            try:
                return walk(target, placed, font_size, stroke_width, markers, depth + 1)
            finally:
                active.discard(id(element))
        points = shape_points(element, tag, font_size)
        if points is None:
            return False
        reach = reach_of_markers(markers, stroke_width)
        if reach is None:
            return False
        add(points, here, reach)
        return True

    try:
        if not walk(root, _IDENTITY, _DEFAULT_EM_PX, 1.0, (css.markers,) * len(_MARKER_LONGHANDS), 0):
            return None
    except Exception:  # a bound it cannot take is no bound: the caller fits the render
        return None
    if far == [0.0, 0.0]:  # nothing drawn where it stands: resvg's 100 x 100 default
        return (100.0, 100.0), container_boxes
    return (far[0], far[1]), container_boxes


def _render_canvas(
    root: Any, size: tuple[float, float] | None, box: int, native: bool
) -> tuple[tuple[float, float], float]:
    """``((width, height), scale)`` of the render: the canvas in device pixels
    and the device pixels one user unit of the root covers."""
    view = _view_box_size(root.get("viewBox"))
    if size is None:  # sized by its content: fitted into the box by a factor this cannot know
        return (float(box), float(box)), float(box)
    user = max(size[0] / view[0], size[1] / view[1]) if view else 1.0
    fit = 1.0 if native else box / max(size)
    return (size[0] * fit, size[1] * fit), user * fit


def _png_size(png: bytes) -> tuple[int, int]:
    """A PNG's pixel size, from its header."""
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def _content_size(root: Any, render_text: str, render_dpi: int, extent: tuple[float, float]) -> tuple[float, float] | None:
    """The size resvg gives a content-sized *root* (a lower bound of it, so the
    fitted scale read from it is never too small), from a render at that size -
    inside *extent*, its filters charged at 1:1 first - or ``None``."""
    if not svg_is_bounded(root, canvas=extent, scale=1.0):
        return None
    try:
        import resvg_py

        png = resvg_py.svg_to_bytes(svg_string=render_text, dpi=render_dpi)
    except BaseException as exc:  # a PanicException (a BaseException) is no size either
        if not isinstance(exc, Exception) and type(exc).__name__ != "PanicException":
            raise
        return None
    if not png:
        return None
    width, height = _png_size(bytes(png))  # resvg rounds its size up to whole pixels
    return max(width - 1.0, 1.0), max(height - 1.0, 1.0)


class SvgRender(NamedTuple):
    """What a render of an SVG gave: its PNG, or ``None`` and why (:attr:`reason`)."""

    png: bytes | None
    reason: str | None = None


def _refused(reason: str) -> SvgRender:
    logger.info("SVG not rendered: %s", reason)
    return SvgRender(None, reason)


def _unread(data: bytes) -> SvgRender | None:
    """The refusal of markup never read at all - empty, or past :data:`MAX_SVG_BYTES` - else ``None``."""
    if not data:
        return SvgRender(None, "the SVG is empty")
    if len(data) > MAX_SVG_BYTES:
        return _refused(f"the SVG is {len(data)} bytes, past the {MAX_SVG_BYTES} read")
    return None


def rasterize_svg(
    data: bytes, *, box: int, keep_native_size: bool = False, strict: bool = False, dpi: int | None = None
) -> bytes | None:
    """PNG bytes of the SVG *data*, bounded as the module says, or ``None`` (:func:`render_svg`, its PNG alone)."""
    return render_svg(data, box=box, keep_native_size=keep_native_size, strict=strict, dpi=dpi).png


def render_svg(
    data: bytes, *, box: int, keep_native_size: bool = False, strict: bool = False, dpi: int | None = None
) -> SvgRender:
    """The PNG of the SVG *data*, bounded as the module says, or why there is none.

    An internal entity is expanded (bounded) and a non-local reference is
    dropped, the rest of the drawing rendering with nothing fetched - or, under
    *strict*, an SVG declaring an entity or reaching outside itself is refused
    whole. What renders is the ORIGINAL bytes when nothing was neutralized
    (byte-for-byte as resvg renders them); only an SVG whose references were
    dropped renders the cleaned tree. *dpi* ``None`` reads absolute units at
    ``RENDER_DPI``, as a browser does; a caller that must read an SVG exactly
    as resvg-py's defaults did passes ``0``.

    Blocking (and holding the GIL): from async code use
    :func:`render_svg_async`.
    """
    unread = _unread(data)
    if unread is not None:
        return unread
    render_text = data.decode("utf-8", errors="replace")
    try:
        root = _parse_svg_root(data, strict=strict)
        if _local_tag(root) != "svg":
            return _refused("its root is not <svg>")
        try:
            if _neutralize_references(root, strict=strict):
                render_text = _serialize_svg(root).decode("utf-8", errors="replace")
                logger.info("SVG rendered with its references outside the document dropped")
        except _StrictRefusalError:
            return _refused("a non-local reference, refused under strict rendering")
        size = intrinsic_size(root)
        view = _view_box_size(root.get("viewBox"))
        extent = None
        if size is None and not _sized_by_content(root, view):
            # A definite size in em / ex under a font size the document sets:
            # resvg renders it no larger than this bound, so it is rendered at
            # resvg's size when the bound fits the box - as it always was - and
            # fitted into the box otherwise, never drawn at a size nothing bounds.
            extent = _root_size(root, view, lambda: _font_size_bound(root))
        elif size is None:
            # Sized by its content: rendered at that size, as resvg sized it on
            # its own, when the content provably fits the box. The walk bounding
            # it follows every <use>, so the shape budget comes first; the
            # filters are charged below, at the scale the render is drawn at.
            if not svg_is_bounded(root, filters=False):
                return _refused("its references, shapes or filters are unbounded")
            extent = _content_extent(root)
        by_content = keep_native_size and extent is not None and max(extent) <= box
        native = by_content or (keep_native_size and size is not None and max(size) <= box)
        render_dpi = RENDER_DPI if dpi is None else dpi
        canvas, scale = _render_canvas(root, extent if by_content else size, box, native)
        if not native and extent is not None and view is None and max(extent) <= MAX_CONTENT_PROBE_SIDE:
            # Fitted by a factor only resvg's own content size gives (the logo
            # geometry fits every SVG into its box): read from a render at that
            # size, which the bound keeps within MAX_CONTENT_PROBE_SIDE, so the
            # fitted render's filters are charged at the scale it is drawn at -
            # never more than the conservative charge kept when it cannot be read.
            content = _content_size(root, render_text, render_dpi, extent)
            if content is not None:
                scale = box / max(content)
                canvas = (content[0] * scale, content[1] * scale)
        if not svg_is_bounded(root, canvas=canvas, scale=scale):
            return _refused("its references, shapes or filters are unbounded")
    except Exception as exc:  # not well-formed, declares an unsafe entity, or no SVG at all
        return _refused(f"it could not be read: {repr(exc)[:200]}")
    fit: dict[str, int] = {} if native else {"width": box, "height": box}
    try:
        import resvg_py

        png = resvg_py.svg_to_bytes(svg_string=render_text, dpi=render_dpi, **fit)
        if by_content and png and max(_png_size(bytes(png))) > box:
            # The content outgrew its bound after all (a font drawing wider
            # than the glyph allowance): fitted into the box as it would be.
            png = resvg_py.svg_to_bytes(svg_string=render_text, dpi=render_dpi, width=box, height=box)
    except Exception as exc:
        return _refused(f"resvg could not draw it: {repr(exc)[:200]}")
    except BaseException as exc:
        # resvg is Rust: a panic reaches Python as pyo3's PanicException, a
        # BaseException - still no image, never an error for the caller.
        if type(exc).__name__ != "PanicException":
            raise
        return _refused(f"resvg could not draw it: {repr(exc)[:200]}")
    return SvgRender(bytes(png)) if png else _refused("resvg drew nothing")


# ---------------------------------------------------------------------------
# The worker process
# ---------------------------------------------------------------------------

_executor: ProcessPoolExecutor | None = None
_executor_lock = threading.Lock()
#: The workers that have started (:func:`_start_worker` ran in them).
_started: weakref.WeakSet[ProcessPoolExecutor] = weakref.WeakSet()
#: The render cache (see ``RENDER_CACHE_MAX_ENTRIES``): the PNG - or the reason
#: of a refusal the worker returned - by digest of the input and the render
#: parameters, least recently used first. A timeout, a crash or a skip is never
#: stored: it says something about the moment, not about the bytes.
#: ``(sha256 of the input, box, keep_native_size, dpi rendered at, strict)``.
_CacheKey = tuple[bytes, int, bool, int, bool]
_render_cache: OrderedDict[_CacheKey, bytes | str | None] = OrderedDict()
_render_cache_bytes = 0
_render_cache_lock = threading.Lock()
_CACHE_MISS = object()
#: What an entry costs beyond its PNG (the key, the dict slot).
_CACHE_ENTRY_OVERHEAD = 128


def _cache_cost(value: bytes | str | None) -> int:
    return _CACHE_ENTRY_OVERHEAD + (len(value) if value else 0)


def _cache_get(key: _CacheKey) -> Any:
    with _render_cache_lock:
        if key not in _render_cache:
            return _CACHE_MISS
        _render_cache.move_to_end(key)
        return _render_cache[key]


def _cache_put(key: _CacheKey, value: bytes | str | None) -> None:
    """Keep *value*, evicting the least recently used entries past either bound."""
    global _render_cache_bytes
    cost = _cache_cost(value)
    if cost > RENDER_CACHE_MAX_BYTES // 4:  # one entry never takes the cache over
        return
    with _render_cache_lock:
        if key in _render_cache:
            _render_cache_bytes -= _cache_cost(_render_cache.pop(key))
        _render_cache[key] = value
        _render_cache_bytes += cost
        while len(_render_cache) > RENDER_CACHE_MAX_ENTRIES or _render_cache_bytes > RENDER_CACHE_MAX_BYTES:
            _render_cache_bytes -= _cache_cost(_render_cache.popitem(last=False)[1])


def clear_svg_raster_cache() -> None:
    """Drop every cached render."""
    global _render_cache_bytes
    with _render_cache_lock:
        _render_cache.clear()
        _render_cache_bytes = 0


#: One render at a time per event loop, so the timeout measures the render
#: and never the queue in front of it.
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _init_worker() -> None:
    """Worker initializer: the address-space cap, the watch on the parent, and
    the stack every render thread gets (see ``WORKER_RENDER_STACK_BYTES``)."""
    limit_address_space(WORKER_ADDRESS_SPACE_BYTES, "SVG raster worker")
    exit_with_parent("svg-raster-parent-watch")
    try:
        threading.stack_size(WORKER_RENDER_STACK_BYTES)
    except (ValueError, RuntimeError) as exc:  # a platform that fixes the size
        logger.debug("SVG raster worker renders on the default stack: %s", repr(exc)[:200])


def _start_worker() -> None:
    """A fresh worker's first task: the imports every render needs."""
    import defusedxml.ElementTree  # noqa: F401
    import resvg_py  # noqa: F401


def _render_in_worker(
    data: bytes, box: int, keep_native_size: bool, strict: bool = False, dpi: int | None = None
) -> SvgRender:
    """:func:`render_svg` on a thread of its own, whose stack the initializer sized: the worker logs nowhere a
    caller reads, so the reason of a refusal comes back with it."""
    result: list[SvgRender] = [SvgRender(None, "the render thread ended without a result")]

    def render() -> None:
        result[0] = render_svg(data, box=box, keep_native_size=keep_native_size, strict=strict, dpi=dpi)

    thread = threading.Thread(target=render, name="svg-raster-render")
    thread.start()
    thread.join()
    return result[0]


def _get_executor() -> ProcessPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ProcessPoolExecutor(
                max_workers=1,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_worker,
            )
        return _executor


def _discard_executor(executor: ProcessPoolExecutor) -> None:
    """Kill the worker (a render past its time) and let the next call start a new one."""
    global _executor
    with _executor_lock:
        if _executor is executor:
            _executor = None
    try:
        executor.kill_workers()
    except Exception:
        logger.debug("SVG raster worker could not be killed", exc_info=True)
    executor.shutdown(wait=False, cancel_futures=True)


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = asyncio.Semaphore(1)
    return gate


class _Failed(NamedTuple):
    """What :func:`_in_worker` returns when the worker could not run the work - and was discarded."""

    reason: str


async def _in_worker(executor: ProcessPoolExecutor, timeout: float, what: str, fn: Any, *args: Any) -> Any:
    """``fn(*args)`` in the worker, or :class:`_Failed` when it could not run,
    overran *timeout* or died - the worker is then discarded, and the next call
    starts a fresh one. A caller cancelled meanwhile takes the worker with it."""
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(executor, fn, *args)
    except Exception as exc:  # a pool that broke while idle (a killed worker) or was shut down
        logger.warning("SVG raster worker unavailable (%s); image skipped", repr(exc)[:200])
        _discard_executor(executor)
        return _Failed(f"the SVG raster worker was unavailable ({repr(exc)[:200]})")
    try:
        return await asyncio.wait_for(future, timeout)
    except TimeoutError:
        logger.warning("SVG %s killed after %.1fs", what, timeout)
        _discard_executor(executor)
        return _Failed(f"the SVG raster worker's {what} was killed after {timeout:.1f} s")
    except asyncio.CancelledError:
        # Nothing else would ever stop what the worker is busy with, and the
        # next call would queue behind it: kill it with its caller.
        _discard_executor(executor)
        raise
    except (BrokenProcessPool, OSError, RuntimeError) as exc:
        # The worker died on this payload (a crash or the memory cap). Do
        # NOT re-run it inline: that would drop the timeout and RLIMIT_AS
        # this isolation exists for, on the very payload that broke it.
        # Skip the image; the next call starts a fresh worker.
        logger.warning("SVG raster worker failed (%s); image skipped, not rendered inline", repr(exc)[:200])
        _discard_executor(executor)
        return _Failed(
            f"the SVG raster worker died during its {what} - a crash, or an allocation past its address-space cap "
            f"({repr(exc)[:200]})"
        )
    except Exception as exc:  # the render itself never raises; anything else is no image
        logger.warning("SVG %s failed in the worker: %s", what, repr(exc)[:200])
        return _Failed(f"the SVG raster worker's {what} failed ({repr(exc)[:200]})")


async def rasterize_svg_async(
    data: bytes,
    *,
    box: int,
    keep_native_size: bool = False,
    strict: bool = False,
    dpi: int | None = None,
    timeout: float | None = None,
) -> bytes | None:
    """:func:`render_svg_async`'s PNG alone: ``None`` for no image."""
    rendered = await render_svg_async(
        data, box=box, keep_native_size=keep_native_size, strict=strict, dpi=dpi, timeout=timeout
    )
    return rendered.png


async def render_svg_async(
    data: bytes,
    *,
    box: int,
    keep_native_size: bool = False,
    strict: bool = False,
    dpi: int | None = None,
    timeout: float | None = None,
) -> SvgRender:
    """:func:`render_svg` in the worker process, off the event loop: the PNG, or why there is none.

    Past *timeout* (default ``RASTER_TIMEOUT_SECONDS``, the render alone - a
    fresh worker starts first, under ``WORKER_START_TIMEOUT_SECONDS``) the
    worker is killed and ``None`` returned; a render that cannot start within
    ``RASTER_QUEUE_TIMEOUT_SECONDS`` is skipped. A worker that cannot be
    started, dies, or is killed fails CLOSED - no PNG, the reason said - and is
    never re-run inline: an inline render would drop the timeout and the
    address-space cap this isolation exists for, on the very payload that just
    broke the worker. A caller cancelled mid-render takes the worker with it.

    A result the worker returned - the PNG, or the reason it refused the SVG -
    is kept in a bounded LRU cache keyed by the SHA-256 of *data* and the
    render parameters, so the same bytes are never rendered twice while the
    entry lives: a hit returns the very same PNG bytes, off the queue, and a
    caller that queued behind a render of the same bytes gets that render.
    """
    unread = _unread(data)
    if unread is not None:
        return unread
    key = (hashlib.sha256(data).digest(), box, keep_native_size, RENDER_DPI if dpi is None else dpi, strict)
    cached = _cache_get(key)
    if cached is not _CACHE_MISS:
        return _cached_render(cached)
    timeout = RASTER_TIMEOUT_SECONDS if timeout is None else timeout
    gate = _gate()
    try:
        async with asyncio.timeout(RASTER_QUEUE_TIMEOUT_SECONDS):
            await gate.acquire()
    except TimeoutError:
        logger.warning("SVG render skipped: still queued after %.0fs", RASTER_QUEUE_TIMEOUT_SECONDS)
        return SvgRender(None, f"it was still queued behind other SVG renders after {RASTER_QUEUE_TIMEOUT_SECONDS:.0f} s")
    try:
        cached = _cache_get(key)
        if cached is not _CACHE_MISS:
            return _cached_render(cached)
        try:
            executor = _get_executor()
        except Exception as exc:  # no worker can be started - fail closed
            logger.warning("SVG raster worker unavailable (%s); image skipped", repr(exc)[:200])
            return SvgRender(None, f"no SVG raster worker could be started ({repr(exc)[:200]})")
        if executor not in _started:
            started = await _in_worker(executor, WORKER_START_TIMEOUT_SECONDS, "worker start", _start_worker)
            if isinstance(started, _Failed):
                return SvgRender(None, started.reason)
            _started.add(executor)
        rendered = await _in_worker(
            executor, timeout, "render", _render_in_worker, data, box, keep_native_size, strict, dpi
        )
        if isinstance(rendered, _Failed):
            return SvgRender(None, rendered.reason)
        _cache_put(key, rendered.png if rendered.png is not None else rendered.reason)
        return rendered
    finally:
        gate.release()


def _cached_render(cached: bytes | str | None) -> SvgRender:
    """A cache entry as a render: its PNG, or the reason the same bytes were refused."""
    if isinstance(cached, bytes):
        return SvgRender(cached)
    return SvgRender(None, cached or "the same SVG was refused before")


def shutdown_svg_raster_worker() -> None:
    """Best-effort teardown (lifespan / worker shutdown): never waits on a render."""
    global _executor
    with _executor_lock:
        executor = _executor
        _executor = None
    if executor is not None:
        _discard_executor(executor)


__all__ = [
    "EXPANSION_ALLOWANCE",
    "MAX_CONTENT_PROBE_SIDE",
    "MAX_EMBEDDED_IMAGE_PIXELS",
    "MAX_FILTER_REGION_PIXELS",
    "MAX_FILTER_WORK_MS",
    "MAX_SVG_BYTES",
    "MAX_SVG_DEPTH",
    "MAX_SVG_EXPANDED_ELEMENTS",
    "RASTER_QUEUE_TIMEOUT_SECONDS",
    "RASTER_TIMEOUT_SECONDS",
    "RENDER_CACHE_MAX_BYTES",
    "RENDER_CACHE_MAX_ENTRIES",
    "RENDER_DPI",
    "WORKER_START_TIMEOUT_SECONDS",
    "SvgRender",
    "clear_svg_raster_cache",
    "intrinsic_size",
    "rasterize_svg",
    "rasterize_svg_async",
    "render_svg",
    "render_svg_async",
    "shutdown_svg_raster_worker",
    "svg_is_bounded",
]
