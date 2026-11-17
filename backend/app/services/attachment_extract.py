# SPDX-License-Identifier: Apache-2.0

"""Server-side reading of data-bearing attachments: HTML, JSON, JSON Lines, CSV.

An HTML attachment used to reach the model as its raw SOURCE: the chat inline
path (``file_resolver._extract_fallback``) and ``read_file_content`` both read
an ``.html`` file as plain text. A 559 KB self-contained HTML plan is mostly
styles, ``<svg>`` geometry and the data its scripts embed, so the inlined text
hit the fallback cap on markup and the model paged through the source with
``read_file_content`` until the turn looped (25 Sep 2026, "[Sam] General
Assistant", 97 iterations with no answer).

This module is the ONE reader of such an attachment, shared by every surface
that shows one to a model - the LLM inline fallback, the chat upload message,
``read_file_content(mode="extract_structured")`` and ``query_file_json`` - so
a rendering always reads the same way and an offset one surface names means
the same characters on the other (:func:`extract_attachment_text`, one
decoding, one rendering). The two inline surfaces render only a file whose
plain extraction their cap would cut; one that fits is inlined as that
extraction, whole (:func:`extract_inline_attachment_text`). The
renderings:

* HTML - :func:`render_html_attachment`: the page as a reader sees it
  (headings, paragraphs, list items, tables as ``| a | b |`` rows, in reading
  order) with styles, code, SVG / canvas geometry and inert or hidden content
  left out and accounted for, preceded by a bounded ``## Embedded data``
  section describing every data block the page's scripts carry - JSON
  ``<script>`` elements AND JavaScript literals (``const ROWS = [...]``,
  ``window.DATA = {...}``, ``JSON.parse('...')``). The data section comes
  FIRST so a caller's length cap never cuts it.
* JSON - verbatim when small, a structured summary past
  :data:`JSON_VERBATIM_MAX_CHARS` (:func:`summarize_json`: bounded,
  shape-preserving, numeric / geo payloads folded, field statistics for an
  array of records).
* JSON Lines / NDJSON - the same summary over the lines, parsed one by one:
  ``$[i]`` is line ``i + 1`` of the file, a line holding no value keeping its
  slot (:class:`JsonlPlaceholder`).
* CSV / TSV - verbatim when small, past :data:`CSV_VERBATIM_MAX_CHARS` a digest
  (row count, per-column statistics over a sample, sample rows - saying how
  much of the file it read) ahead of the text.

Every result says what it did NOT read: a sampled statistic, a capped row or
record count, a data block past the cap, a page that ends inside an element
never closed, a rendering that failed and fell back - so neither the model
nor the loop reads a partial answer as a complete one.
* :func:`query_json` - a small JSONPath subset, so a large payload is read by
  the value a question needs; :func:`run_json_query` is the whole
  ``query_file_json`` computation, bounded, for the parse executor.

Everything here is pure CPU on in-memory data and bounded in time and memory
on hostile input: the HTML reader is one streaming pass of the standard
library tokenizer (no tree, no recursion), the script scanner is one linear
pass with a total budget, every summary is rendered against a character
budget instead of rendered whole and cut. Callers run it off the event loop
(``core.parse_offload.run_parse``); :func:`extract_attachment_text`
memoises its result per content hash, because the inline fallback re-reads
every attachment of a conversation's history on every turn.

Knowledge-base ingestion keeps indexing the source
(``chunking.extract_text_from_file``) unless a knowledge base opts in
(``html_ingestion = "rendered"``): ``kb_embedding_text`` then embeds the
reading of :func:`extract_html_document`, so a change to that reading changes
what such a knowledge base embeds for the documents it processes next.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import itertools
import json
import logging
import math
import re
import sys
import threading
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from html import unescape as _html_unescape
from html.parser import HTMLParser
from typing import Any

from app.llm.attachment_cache import BoundedLRU

logger = logging.getLogger(__name__)

# -- Bounds -------------------------------------------------------------------

#: A ``.json`` / ``.jsonl`` attachment at most this long is inlined VERBATIM;
#: past it the structured summary is inlined instead.
JSON_VERBATIM_MAX_CHARS = 40_000
#: Same for CSV / TSV: past it the whole-file digest leads the text.
CSV_VERBATIM_MAX_CHARS = 40_000
#: Bound on the summary of ONE embedded data block.
JSON_BLOCK_SUMMARY_MAX_CHARS = 6_000
#: Bound on the summary of a whole JSON / JSON Lines attachment.
JSON_DOCUMENT_SUMMARY_MAX_CHARS = 20_000
#: Bound on the ``## Embedded data`` section of a rendered HTML attachment.
EMBEDDED_DATA_SECTION_MAX_CHARS = 24_000
#: Bound on the digest of a CSV attachment.
CSV_DIGEST_MAX_CHARS = 12_000
#: Most matches one ``query_json`` returns.
QUERY_MAX_RESULTS = 200
#: Bound on the rendered result of one ``query_file_json`` call.
QUERY_RESULT_MAX_CHARS = 30_000

#: Characters of an HTML page the reader tokenizes. The stdlib tokenizer runs
#: about 0.5-1 s per MB of hostile markup and a rendering past the cache's
#: entry bound is redone on every turn, so a 40 MB upload would cost tens of
#: seconds of the parse pool per turn; past this the page is read in part and
#: says so.
HTML_RENDER_MAX_CHARS = 8_000_000
#: Characters of page text a rendering keeps: more than any model window
#: holds, and small enough for the rendering to stay cacheable.
HTML_TEXT_MAX_CHARS = 2_000_000
#: A JavaScript literal shorter than this is a configuration object or an
#: analytics snippet, not the page's data.
MIN_JS_PAYLOAD_CHARS = 300
#: Data blocks recorded per document (JSON scripts and JavaScript literals).
MAX_EMBEDDED_BLOCKS = 64
#: Script characters captured per document; the rest is counted, not kept.
_SCRIPT_CAPTURE_MAX_CHARS = 16_000_000
#: Script characters the JavaScript scanner walks per document.
_JS_SCAN_MAX_CHARS = 8_000_000
#: Literal characters handed to the JavaScript-to-JSON normaliser per
#: document (a JSON-shaped literal never gets there: ``raw_decode`` reads it).
_JS_NORMALISE_MAX_CHARS = 4_000_000
#: Anchored literals the scanner tracks at once (``x = [`` still open).
_JS_MAX_OPEN_ANCHORS = 256
#: JSON Lines lines held for the summary / the query - a record and the
#: placeholder of a line holding none alike, so ``$[i]`` stays line ``i + 1``.
JSONL_MAX_RECORDS = 50_000
#: The largest JSON text parsed in one piece - a document, a data block, the
#: records of a JSON Lines file together. ``json.loads`` holds up to ~25
#: times the text in objects (an empty dict is 64 bytes), so an uncapped
#: 40 MB upload would cost a gigabyte on every read of it; past the cap the
#: text is reported, and code_interpreter computes over any size.
JSON_PARSE_MAX_CHARS = 8_000_000
#: CSV rows read for the digest (the count is then reported as "at least").
CSV_MAX_ROWS = 50_000
#: Rows the field statistics are computed over.
STATS_SAMPLE_ROWS = 5_000
#: Distinct values at or under which a string field is tallied.
CATEGORICAL_MAX_DISTINCT = 20
#: Rows shown under a digest's statistics, and the cut of one row.
SAMPLE_ROWS = 20
SAMPLE_ROW_CHARS = 400

#: Bumped whenever a rendering changes, so a memoised result never outlives it.
RENDER_VERSION = 5
_RENDER_CACHE_MAX_ENTRIES = 32
_RENDER_CACHE_MAX_CHARS = 16_000_000

# -- Kind detection -----------------------------------------------------------

_HTML_EXTENSIONS = frozenset({"html", "htm", "xhtml"})
_HTML_MIME_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_JSON_EXTENSIONS = frozenset({"json", "geojson", "jsonld"})
_JSONL_EXTENSIONS = frozenset({"jsonl", "ndjson"})
_JSONL_MIME_TYPES = frozenset(
    {
        "application/x-ndjson",
        "application/ndjson",
        "application/jsonl",
        "application/x-jsonlines",
        "application/jsonlines",
    }
)
_CSV_EXTENSIONS = frozenset({"csv", "tsv"})
_CSV_MIME_TYPES = frozenset({"text/csv", "application/csv", "text/tab-separated-values"})


def _extension(filename: str | None) -> str:
    name = (filename or "").strip().lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _mime(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def is_html_attachment(filename: str | None, content_type: str | None = None) -> bool:
    """``.html`` / ``.htm`` / ``.xhtml`` or an HTML MIME type."""
    return _extension(filename) in _HTML_EXTENSIONS or _mime(content_type) in _HTML_MIME_TYPES


def is_jsonl_attachment(filename: str | None, content_type: str | None = None) -> bool:
    """``.jsonl`` / ``.ndjson`` or a JSON Lines MIME type: one document per line."""
    return _extension(filename) in _JSONL_EXTENSIONS or _mime(content_type) in _JSONL_MIME_TYPES


def is_json_attachment(filename: str | None, content_type: str | None = None) -> bool:
    """``.json`` / ``.geojson`` / ``.jsonld`` or a JSON MIME type (``+json`` included)."""
    if is_jsonl_attachment(filename, content_type):
        return False
    if _extension(filename) in _JSON_EXTENSIONS:
        return True
    mime = _mime(content_type)
    return mime in {"application/json", "text/json"} or mime.endswith("+json")


def is_csv_attachment(filename: str | None, content_type: str | None = None) -> bool:
    """``.csv`` / ``.tsv`` or a CSV / TSV MIME type."""
    return _extension(filename) in _CSV_EXTENSIONS or _mime(content_type) in _CSV_MIME_TYPES


def attachment_kind(filename: str | None, content_type: str | None = None) -> str | None:
    """``html`` / ``jsonl`` / ``json`` / ``csv`` - the kinds this module renders - or ``None``."""
    if is_html_attachment(filename, content_type):
        return "html"
    if is_jsonl_attachment(filename, content_type):
        return "jsonl"
    if is_json_attachment(filename, content_type):
        return "json"
    if is_csv_attachment(filename, content_type):
        return "csv"
    return None


# -- Decoding -----------------------------------------------------------------

_META_CHARSET_RE = re.compile(rb"""<meta[^>]{0,200}?charset\s*=\s*["']?\s*([A-Za-z0-9._:-]{1,40})""", re.IGNORECASE)
_MIME_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9._:-]{1,40})", re.IGNORECASE)
#: Bytes of an HTML page searched for its ``<meta charset>`` / ``http-equiv``
#: declaration (the WHATWG prescan reads 1,024; a generated page can open
#: with a longer ``<head>``).
_META_PRESCAN_BYTES = 4096
#: The byte-order marks, longest first (a UTF-32 LE mark starts like UTF-16 LE's).
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
#: Text codecs Python knows that no browser honours as a document charset:
#: a declaration naming one is ignored, like an unknown name.
_IGNORED_DECLARED_CODECS = frozenset(
    {"utf-7", "unicode-escape", "raw-unicode-escape", "idna", "punycode", "undefined", "mbcs", "oem"}
)
_UTF16_32_CODECS = frozenset({"utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"})


def _text_codec(name: str) -> str | None:
    """Python's name for a declared charset that is a TEXT encoding, else ``None``.

    ``base64`` / ``zlib`` / ``rot13`` / ``hex`` are codecs too - bytes to
    bytes - and a page declaring one must not pick the decoder.
    """
    try:
        info = codecs.lookup(name)
    except LookupError:
        return None
    if not getattr(info, "_is_text_encoding", True) or info.name in _IGNORED_DECLARED_CODECS:
        return None
    return info.name


def _declared_charset(data: bytes, content_type: str | None, *, html: bool) -> str | None:
    """The charset the attachment DECLARES: its MIME type's ``charset``, then,
    for an HTML page, a ``<meta charset>`` / ``http-equiv`` in its first bytes.

    The transport's word outranks the page's own (the WHATWG order), and a
    page's meta is only read for HTML - a JSON or CSV file that merely
    CONTAINS such a string (scraped markup in a field) declares nothing. A
    meta naming UTF-16 / UTF-32 is read as UTF-8: the prescan could only find
    it in an ASCII-compatible document.
    """
    match = _MIME_CHARSET_RE.search(content_type or "")
    if match:
        name = _text_codec(match.group(1))
        if name:
            return name
    if html:
        meta = _META_CHARSET_RE.search(data[:_META_PRESCAN_BYTES])
        if meta:
            name = _text_codec(meta.group(1).decode("ascii", errors="ignore"))
            if name:
                return "utf-8" if name in _UTF16_32_CODECS else name
    return None


def decode_attachment_text(
    data: bytes, content_type: str | None = None, *, html: bool = False, utf8_first: bool = False
) -> str:
    """The ONE decoding of an attachment's bytes for READING (never for editing).

    A byte-order mark wins, then the DECLARED charset (the MIME type's
    ``charset``, then - *html* - the page's ``<meta>``), then strict UTF-8,
    then Windows-1252 (the encoding of most non-UTF-8 exports) with
    replacement characters. The declaration comes before the UTF-8 guess
    because bytes can be valid UTF-8 and still not BE UTF-8 - a BOM-less
    UTF-16 file (its ASCII half is NUL bytes, which UTF-8 accepts), a legacy
    page whose accented bytes happen to form UTF-8 sequences - and the
    browser the user opened the file in follows the declaration. JSON is the
    exception (*utf8_first*): RFC 8259 makes it UTF-8 and gives its media
    type no ``charset`` a compliant reader honours, so a declaration is only
    consulted for bytes that are not UTF-8, and UTF-16 / UTF-32 without a
    BOM is recognised from its first bytes. Lenient on purpose, so a legacy
    export is READ instead of falling back to its source, and deterministic,
    so the inline rendering and ``read_file_content(mode="extract_structured")``
    serve the same characters for the same bytes.
    """
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return data[len(bom) :].decode(codec, errors="replace")
    if utf8_first:
        # A JSON text starts with an ASCII character and never holds a NUL
        # byte in UTF-8 (a control character must be escaped): UTF-16 /
        # UTF-32 without a BOM - valid UTF-8 as well when its text is ASCII -
        # shows one in its first two bytes, and ``json.detect_encoding`` reads
        # which it is (RFC 4627 s3, what ``json.loads`` does with bytes),
        # declared or not (``charset=utf-16`` alone decodes little-endian).
        codec = json.detect_encoding(data)
        if codec != "utf-8":
            return data.decode(codec, errors="replace")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    charset = _declared_charset(data, content_type, html=html)
    if charset is not None and charset != "utf-8":
        try:
            return data.decode(charset, errors="replace")
        except (LookupError, UnicodeError):
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    return data.decode("cp1252", errors="replace")


#: A lone surrogate: what ``json.loads`` (and a JavaScript ``\\u`` escape)
#: makes of half a UTF-16 pair. A str carrying one cannot be encoded as
#: UTF-8 - the HTTP client, the LLM SDK and the database all raise on it - so
#: one bad escape in a file would fail the upload and every later request of
#: the conversation. A Python str holds no valid pairs, so every match is one.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _encodable(text: str) -> str:
    """*text* with every lone surrogate replaced by U+FFFD (a no-op scan for clean text)."""
    return _SURROGATE_RE.sub("\ufffd", text) if _SURROGATE_RE.search(text) else text


#: A decimal character reference ``int()`` may refuse. The stdlib tokenizer
#: decodes the references of the text, of RCDATA (``<title>``,
#: ``<textarea>``) and of attribute values with ``int()``, which refuses more
#: digits than ``sys.get_int_max_str_digits()`` - 4,300 by default, never set
#: below ``str_digits_check_threshold`` (640) - so one ``&#`` and a run of
#: digits raised out of the whole page; with the limit lifted it converts
#: any run, in seconds for a few million digits. Such a reference never
#: reaches the tokenizer (:meth:`_HtmlReader.feed`); a hex one is left to
#: it, since ``int(..., 16)`` has no limit and stays linear.
_CHARREF_MAX_DIGITS = sys.int_info.str_digits_check_threshold
_LONG_CHARREF_RE = re.compile(rf"&#([0-9]{{{_CHARREF_MAX_DIGITS + 1},}})(;?)")
#: Digits of the largest code point (0x10FFFF is 1,114,111).
_CODE_POINT_MAX_DIGITS = 7
#: The candidate delimiters of a held reference's index: the 66 noncharacters
#: (U+FDD0..U+FDEF and the last two code points of every plane), which Unicode
#: reserves for a program's internal use and no decoding produces
#: (``html.unescape`` drops them).
_NONCHARACTERS = (
    *map(chr, range(0xFDD0, 0xFDF0)),
    *(chr(plane << 16 | low) for plane in range(17) for low in (0xFFFE, 0xFFFF)),
)


def _absent_pair(text: str) -> tuple[str, str] | None:
    """Two :data:`_NONCHARACTERS` *text* does not carry, ``None`` when it carries 65 of the 66.

    Two membership scans unless the page carries noncharacters of its own
    (at most 66: 0.12 s on an 8M-character page carrying 65 of them).
    """
    absent = (char for char in _NONCHARACTERS if char not in text)
    first, second = next(absent, None), next(absent, None)
    return None if first is None or second is None else (first, second)


def _shorten_charref(match: re.Match[str]) -> str:
    """A :data:`_LONG_CHARREF_RE` reference as the short one the tokenizer
    decodes to the same character: its leading zeros dropped, U+FFFD past the
    last code point."""
    digits = match.group(1).lstrip("0") or "0"
    if len(digits) > _CODE_POINT_MAX_DIGITS:
        return "\ufffd"
    return f"&#{digits}{match.group(2)}"


# -- Safe number handling (no OverflowError, no NaN) --------------------------


def _is_number(value: Any) -> bool:
    """A JSON number usable in statistics: an int (never a bool) or a finite float."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt_num(value: Any) -> str:
    """A number for a human: grouped digits, 2 decimals, scientific past 1e15 - never raises."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        if abs(value) < 10**15:
            return f"{value:,}"
        digits = str(abs(value))
        return f"{'-' if value < 0 else ''}{digits[0]}.{digits[1:4]}e+{len(digits) - 1}"
    number = _as_float(value)
    if number is None:
        return str(value)
    if number.is_integer() and abs(number) < 1e15:
        return f"{int(number):,}"
    if abs(number) >= 1e15:
        return f"{number:.3e}"
    return f"{number:,.2f}"


def _safe_mean_sum(numbers: list[Any]) -> tuple[str, str]:
    """``(sum, mean)`` of *numbers* as text; ``"overflow"`` when a float cannot hold them."""
    floats = [_as_float(n) for n in numbers]
    if any(f is None for f in floats):
        return "overflow", "overflow"
    try:
        total = math.fsum(floats)  # type: ignore[arg-type]
    except OverflowError:
        return "overflow", "overflow"
    if not math.isfinite(total):
        return "overflow", "overflow"
    return _fmt_num(total), _fmt_num(total / len(floats))


# -- Bounded JSON output ------------------------------------------------------


class _Out:
    """A character budget over emitted text: ``add`` raises :class:`_BudgetFullError` past it."""

    __slots__ = ("limit", "parts", "used")

    def __init__(self, limit: int) -> None:
        self.limit = max(0, limit)
        self.parts: list[str] = []
        self.used = 0

    def add(self, text: str) -> None:
        room = self.limit - self.used
        if len(text) > room:
            if room > 0:
                self.parts.append(text[:room])
                self.used = self.limit
            raise _BudgetFullError
        self.parts.append(text)
        self.used += len(text)

    def text(self) -> str:
        return "".join(self.parts)


class _BudgetFullError(Exception):
    """The output budget is spent."""


def dump_json_bounded(value: Any, max_chars: int, *, indent: int | None = 2) -> tuple[str, bool]:
    """``json.dumps(value, indent=indent)`` cut at *max_chars* (one line with
    ``indent=None``); ``(text, complete)``.

    The encoder is consumed chunk by chunk and stopped at the bound, so a
    match of a hundred thousand rows costs the bound, not the whole
    serialisation. A value too deep for the encoder is reported incomplete.
    """
    out = _Out(max_chars)
    encoder = json.JSONEncoder(indent=indent, ensure_ascii=False, default=str)
    try:
        for chunk in encoder.iterencode(value):
            out.add(chunk)
    except _BudgetFullError:
        return out.text(), False
    except (RecursionError, ValueError, TypeError):
        return out.text(), False
    return out.text(), True


# -- JSON summary (budgeted, shape-preserving) --------------------------------

_SUMMARY_MAX_STRING = 240
_SUMMARY_MAX_LIST_ITEMS = 12
_SUMMARY_MAX_DICT_KEYS = 80
_SUMMARY_MAX_DEPTH = 8
_SUMMARY_NUMERIC_FOLD = 12
#: Keys whose long values are geometry, never prose.
_GEO_KEYS = frozenset(
    {
        "coordinates",
        "geometry",
        "geometries",
        "d",
        "path",
        "paths",
        "points",
        "polyline",
        "polygon",
        "polygons",
        "vertices",
        "bbox",
        "bounds",
        "arcs",
        "transform",
        "matrix",
        "svg",
        "viewbox",
    }
)
_GEO_STRING_FOLD = 80


def describe_shape(value: Any) -> str:
    """``object with 8 keys`` / ``array of 12 items`` / ``string (40 chars)`` ..."""
    if isinstance(value, dict):
        return f"object with {len(value):,} key{'s' if len(value) != 1 else ''}"
    if isinstance(value, JsonlLines):
        return value.describe()
    if isinstance(value, list):
        return f"array of {len(value):,} item{'s' if len(value) != 1 else ''}"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return f"string ({len(value):,} chars)"
    return type(value).__name__


def _numeric_shape(value: list) -> str | None:
    """``<N numbers, min .. max>`` / ``<N x M numeric rows>`` for a long numeric
    array, else ``None``. Looks two levels down at most - never recursive."""
    if len(value) < _SUMMARY_NUMERIC_FOLD:
        return None
    if all(_is_number(item) for item in value):
        return f"[<{len(value):,} numbers, {_fmt_num(min(value))} .. {_fmt_num(max(value))}>]"
    if all(isinstance(item, list) and item and all(_is_number(x) for x in item) for item in value):
        widths = {len(item) for item in value}
        width = f"{min(widths)}-{max(widths)}" if len(widths) > 1 else str(next(iter(widths)))
        return f"[<{len(value):,} x {width} numeric rows>]"
    if all(
        isinstance(item, list)
        and all(_is_number(x) or (isinstance(x, list) and all(_is_number(y) for y in x)) for x in item)
        for item in value
    ):
        return f"[<{len(value):,} nested numeric arrays>]"
    return None


def _render_scalar(value: Any, *, key: str | None) -> str:
    if isinstance(value, str):
        if key and key.lower() in _GEO_KEYS and len(value) > _GEO_STRING_FOLD:
            return json.dumps(f"<string, {len(value):,} chars: {value[:24]}...>", ensure_ascii=False)
        if len(value) > _SUMMARY_MAX_STRING:
            return json.dumps(
                value[:_SUMMARY_MAX_STRING] + f"... <{len(value) - _SUMMARY_MAX_STRING:,} more chars>",
                ensure_ascii=False,
            )
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= 10**30:
        return _fmt_num(value)
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=True)
    except (TypeError, ValueError, OverflowError):
        return json.dumps(str(value), ensure_ascii=False)


def _field_lines(
    rows: list[dict],
    *,
    indent: str,
    max_fields: int = 40,
    total: int | None = None,
    field_count: int | None = None,
) -> Iterator[str]:
    """Per-field statistics over (a sample of) an array of records, one line each.

    Numbers: count, min, max, sum, mean; booleans: true / false; strings: a
    tally when few distinct values, else the cardinality and examples; nulls
    counted. Every computation is overflow-safe and bounded by
    :data:`STATS_SAMPLE_ROWS`, and a sample says so. *total* / *field_count*
    are the record and field counts when *rows* holds only a sample of them
    (the CSV digest keeps its statistics sample, never every row).
    """
    invalid, blank = _placeholder_counts(rows)
    # A JSON Lines line holding no value is no row: every statistic - the
    # counts, the distinct values, min / max / sum, the nulls - is the
    # records' alone.
    sample = _stats_sample(rows)
    record_count = len(rows) - invalid - blank if total is None else total
    fields: dict[str, None] = {}
    for row in sample:
        for key in row:
            fields.setdefault(str(key), None)
            if len(fields) > 400:
                break
    if not fields:
        return
    if len(sample) < record_count:
        # First, where no cut reaches it: a statistic repeated without it
        # reads as covering every row.
        yield f"{indent}(statistics over the first {len(sample):,} of {record_count:,} rows)"
    names = list(fields)
    names_total = max(len(names), field_count or 0)
    listed = ", ".join(names[:max_fields]) + (f", ... ({names_total} fields)" if names_total > max_fields else "")
    yield f"{indent}fields: {listed}"
    for name in names[:max_fields]:
        values = [row.get(name) for row in sample if name in row]
        present = [v for v in values if v is not None]
        nulls = len(values) - len(present)
        if not present:
            yield f"{indent}  {name}: always null"
            continue
        numbers = [v for v in present if _is_number(v)]
        if numbers and len(numbers) >= 0.8 * len(present):
            total, mean = _safe_mean_sum(numbers)
            line = (
                f"{indent}  {name}: number x{len(numbers)}: min {_fmt_num(min(numbers))}, "
                f"max {_fmt_num(max(numbers))}, sum {total}, mean {mean}"
            )
        elif all(isinstance(v, bool) for v in present):
            trues = sum(1 for v in present if v)
            line = f"{indent}  {name}: boolean: true {trues}, false {len(present) - trues}"
        elif all(isinstance(v, str) for v in present):
            counts = Counter(present)
            if len(counts) <= CATEGORICAL_MAX_DISTINCT and len(present) >= 3:
                top = ", ".join(f"{_short(v, 60)} ({c})" for v, c in counts.most_common(8))
                more = f", +{len(counts) - 8} more" if len(counts) > 8 else ""
                line = f"{indent}  {name}: {len(counts)} distinct: {top}{more}"
            else:
                examples = ", ".join(_short(v, 60) for v, _ in counts.most_common(3))
                line = f"{indent}  {name}: string, {len(counts):,} distinct, e.g. {examples}"
        elif all(isinstance(v, list) for v in present):
            avg = sum(len(v) for v in present) / len(present)
            line = f"{indent}  {name}: array (avg {avg:.1f} items)"
        elif all(isinstance(v, dict) for v in present):
            keys = Counter(k for v in present for k in itertools.islice(v, 50))
            line = f"{indent}  {name}: object, keys {', '.join(str(k) for k in list(keys)[:12])}"
        else:
            kinds = Counter(_type_name(v) for v in present)
            line = f"{indent}  {name}: mixed ({', '.join(f'{k} {c}' for k, c in kinds.most_common())})"
        if nulls:
            line += f"; null {nulls}"
        yield line[:600]


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _short(value: Any, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (ValueError, RecursionError, TypeError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _compact_record(item: Any) -> str | None:
    """A flat record (an object of short scalars) as one compact JSON line, else ``None``."""
    if not isinstance(item, dict) or not item or len(item) > 40:
        return None
    for key, value in item.items():
        if isinstance(value, (dict, list)) or (isinstance(value, str) and len(value) > 120):
            return None
        if str(key).lower() in _GEO_KEYS:
            return None
    try:
        line = json.dumps(item, ensure_ascii=False, separators=(", ", ": "))
    except (TypeError, ValueError):
        return None
    return line if len(line) <= SAMPLE_ROW_CHARS else None


def _placeholder_counts(rows: list) -> tuple[int, int]:
    """How many placeholders *rows* holds, ``(invalid, blank)``: kept on a
    file's :class:`JsonlLines`, counted on any other list - a placeholder
    only sits at the top level of those lines or in a query's matches (at
    most :data:`QUERY_MAX_RESULTS`), so the first rows of a list hold them all."""
    if isinstance(rows, JsonlLines):
        return rows.invalid, rows.blank
    invalid = blank = 0
    for row in itertools.islice(rows, STATS_SAMPLE_ROWS):
        if isinstance(row, JsonlPlaceholder):
            if row.reason is None:
                blank += 1
            else:
                invalid += 1
    return invalid, blank


def _stats_sample(rows: list) -> list:
    """The rows a statistic of *rows* reads: its first :data:`STATS_SAMPLE_ROWS`
    values, a JSON Lines placeholder never one of them. The summary's
    records-or-values decision and :func:`_field_lines` read this ONE sample,
    so a file opening on lines that hold no value is still an array of
    records, and the statistics never meet a row the decision did not check."""
    if not any(_placeholder_counts(rows)):
        return rows[:STATS_SAMPLE_ROWS]
    return list(itertools.islice((row for row in rows if not isinstance(row, JsonlPlaceholder)), STATS_SAMPLE_ROWS))


def _is_row(item: Any) -> bool:
    """A record of an array of records - or a JSON Lines placeholder in its slot."""
    return isinstance(item, (dict, JsonlPlaceholder))


def _rows_line(rows: list) -> str:
    """``2,000 records``; with placeholders ``2,003 items: 2,000 records, 2
    invalid lines, 1 blank line`` - a placeholder is never counted a record."""
    invalid, blank = _placeholder_counts(rows)
    if not invalid and not blank:
        return f"{len(rows):,} records"
    records = len(rows) - invalid - blank
    parts = [f"{records:,} record{'s' if records != 1 else ''}"]
    if invalid:
        parts.append(f"{invalid:,} invalid line{'s' if invalid != 1 else ''}")
    if blank:
        parts.append(f"{blank:,} blank line{'s' if blank != 1 else ''}")
    return f"{len(rows):,} items: {', '.join(parts)}"


def _emit_summary(value: Any, out: _Out, *, key: str | None, depth: int, pad: str) -> None:
    """Emit the summary of *value* into *out* (recursion bounded by ``_SUMMARY_MAX_DEPTH``)."""
    if isinstance(value, JsonlPlaceholder):
        # A JSON Lines line holding no value, in its slot: a marker, never a value.
        out.add(str(value))
        return
    if isinstance(value, dict):
        if depth >= _SUMMARY_MAX_DEPTH:
            out.add(f"{{<object with {len(value):,} keys, nested too deep>}}")
            return
        if not value:
            out.add("{}")
            return
        out.add("{")
        shown = min(len(value), _SUMMARY_MAX_DICT_KEYS)
        for position, (child_key, child) in enumerate(itertools.islice(value.items(), shown)):
            out.add(f"\n{pad}  {json.dumps(str(child_key), ensure_ascii=False)}: ")
            _emit_summary(child, out, key=str(child_key), depth=depth + 1, pad=pad + "  ")
            if position < shown - 1 or len(value) > shown:
                out.add(",")
        if len(value) > shown:
            out.add(f"\n{pad}  ... <{len(value) - shown:,} more keys>")
        out.add(f"\n{pad}}}")
        return
    if isinstance(value, list):
        if depth >= _SUMMARY_MAX_DEPTH:
            out.add(f"[<array of {len(value):,} items, nested too deep>]")
            return
        if not value:
            out.add("[]")
            return
        folded = _numeric_shape(value)
        if folded is not None:
            out.add(folded)
            return
        head = value[:_SUMMARY_MAX_LIST_ITEMS]
        # A file's lines holding placeholders always say how many: never the
        # one-line scalar form, which cannot, and a count line even when no
        # record is a dict (a file of scalars, or of invalid lines only).
        held = isinstance(value, JsonlLines) and bool(value.invalid or value.blank)
        if (
            not held
            and all(isinstance(item, (str, int, float, bool)) or item is None for item in head)
            and all(
                isinstance(item, (str, int, float, bool)) or item is None
                for item in value[: _SUMMARY_MAX_LIST_ITEMS * 8]
            )
        ):
            rendered = ", ".join(_render_scalar(item, key=None) for item in head)
            if len(value) > len(head):
                rendered += f", ... <{len(value) - len(head):,} more items>"
            out.add(f"[{rendered}]")
            return
        out.add("[")
        records = len(value) > len(head) and all(_is_row(item) for item in head)
        if records:
            # Decided on the rows the statistics read - the first values, never
            # the first slots, which a run of placeholders can fill.
            sample = _stats_sample(value)
            records = bool(sample) and all(isinstance(item, dict) for item in sample)
        if records or held:
            out.add(f"\n{pad}  // {_rows_line(value)}")
        if records:
            for line in _field_lines(value, indent=f"{pad}  // "):
                out.add("\n" + line)
        if held and len(value) > len(head) and all(isinstance(item, JsonlPlaceholder) for item in head):
            # A head of placeholders shows no value of the file: name the
            # first line holding one, with the path that reads it.
            first = next((i for i in range(len(head), len(value)) if not isinstance(value[i], JsonlPlaceholder)), None)
            if first is not None:
                example, complete = dump_json_bounded(value[first], SAMPLE_ROW_CHARS, indent=None)
                out.add(
                    f"\n{pad}  // the first line holding a value is line {first + 1}, $[{first}]: "
                    f"{example}{'' if complete else '...'}"
                )
        for position, item in enumerate(head):
            out.add(f"\n{pad}  ")
            compact = _compact_record(item)
            if compact is not None:
                out.add(compact)  # a flat record reads best as one line
            else:
                _emit_summary(item, out, key=None, depth=depth + 1, pad=pad + "  ")
            if position < len(head) - 1 or len(value) > len(head):
                out.add(",")
        if len(value) > len(head):
            out.add(f"\n{pad}  ... <{len(value) - len(head):,} more items>")
        out.add(f"\n{pad}]")
        return
    out.add(_render_scalar(value, key=key))


def summarize_json(data: Any, *, max_chars: int = JSON_DOCUMENT_SUMMARY_MAX_CHARS, query_hint: bool = True) -> str:
    """A bounded, shape-preserving rendering of *data*.

    Every key is kept (up to a bound per object), every string is cut at a
    readable length, long arrays show their head and their count (an array
    of records its per-field statistics first), and a numeric or geometry
    payload is reduced to its shape. Rendered AGAINST the budget: past
    *max_chars* the emission stops and says so, so a document of half a
    million leaves costs the budget, not its whole rendering.
    """
    out = _Out(max_chars)
    try:
        _emit_summary(data, out, key=None, depth=0, pad="")
    except _BudgetFullError:
        hint = " - use query_file_json(file_id, path) to read a part of it" if query_hint else ""
        return out.text() + f"\n... [summary cut at {max_chars:,} chars{hint}]"
    except RecursionError:  # pragma: no cover - depth is bounded above
        return out.text() + "\n... [summary cut: nested too deep]"
    return out.text()


# -- HTML: one streaming pass -------------------------------------------------

#: ``<script type="...">`` values whose body is a JSON document.
_JSON_SCRIPT_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/vnd.api+json",
        "application/hal+json",
        "application/geo+json",
        "application/x-json",
        "importmap",
        "speculationrules",
        "text/json",
    }
)
#: ``<script type="...">`` values that are JavaScript (scanned for literals).
_JS_SCRIPT_TYPES = frozenset(
    {"", "text/javascript", "application/javascript", "module", "text/ecmascript", "application/ecmascript"}
)
#: Elements whose whole subtree is never text a reader sees. ``script`` is
#: handled apart (its body is captured as data), ``title`` too (the page title).
_SKIP_TAGS = frozenset(
    {
        "style",
        "svg",
        "math",
        "canvas",
        "noscript",
        "template",
        "iframe",
        "object",
        "audio",
        "video",
        "xmp",
        "noembed",
        "noframes",
    }
)
#: Elements a ``hidden`` attribute removes from the text. Only elements whose
#: end tag is required: on an element with an optional end tag (``<p>``,
#: ``<li>``) an implied close would never come and the rest of the page would
#: be skipped with it.
_HIDEABLE_TAGS = frozenset(
    {
        "div",
        "section",
        "article",
        "aside",
        "nav",
        "header",
        "footer",
        "main",
        "span",
        "table",
        "ul",
        "ol",
        "dl",
        "form",
        "fieldset",
        "details",
        "dialog",
        "figure",
        "blockquote",
        "pre",
    }
)
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "header",
        "hgroup",
        "html",
        "legend",
        "main",
        "nav",
        "option",
        "p",
        "section",
        "summary",
        "caption",
        "center",
    }
)
_HEADING_LEVEL = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_WS_RE = re.compile(r"\s+")
#: The longest title the rendering heads with: it comes first, so an
#: unbounded one would push the page's text and data past the inline cap.
_TITLE_MAX_CHARS = 300
#: Where markup opens for the tokenizer: ``<`` and a letter (a tag), ``</``
#: and a letter (an end tag), ``<!`` or ``<?``. Any other ``<`` is text.
_MARKUP_OPEN_RE = re.compile(r"<(?:/?[a-zA-Z]|[!?])")


def _clip_title(raw: str) -> str | None:
    """A ``<title>``'s text, whitespace folded and clipped to :data:`_TITLE_MAX_CHARS` (``None`` when empty)."""
    title = _WS_RE.sub(" ", raw).strip()
    if len(title) > _TITLE_MAX_CHARS:
        title = title[: _TITLE_MAX_CHARS - 3].rstrip() + "..."
    return title or None


_MAX_LIST_INDENT = 6


@dataclass
class _Script:
    kind: str
    element_id: str | None
    parts: list[str] = field(default_factory=list)
    chars: int = 0


class _HtmlReader(HTMLParser):
    """One streaming pass over an HTML document: reading-order text + script bodies.

    Built on the standard library tokenizer, which is linear, keeps no tree
    and never recurses; every structure the reader needs (the skipped
    subtree, the list depth, the table row) is a counter or a flat stack, so
    a page nested ten thousand levels deep costs what a flat one of the same
    size costs.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self._title_parts: list[str] | None = None
        self.lines: list[str] = []
        self._inline: list[str] = []
        self._prefix = ""
        self._skip: list[list[Any]] = []  # [tag, depth]
        self._list_depth = 0
        self._pre_depth = 0
        self._pre_parts: list[str] = []
        # Table state, for the OUTERMOST table only (a nested table's text
        # flows into the outer cell it sits in).
        self._table_depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.scripts: list[_Script] = []
        self._script: _Script | None = None
        self.script_chars = 0
        self._captured_chars = 0
        self.uncaptured_script_chars = 0
        self.stripped: Counter[str] = Counter()
        #: Characters of text seen inside the skipped element now open.
        self._skip_data_chars = 0
        #: The element the page ENDS inside (never closed) and the characters
        #: from it to the end of the page, which the reader does not show.
        self.unclosed: tuple[str, int] | None = None
        #: The ``<title>`` was never closed: the tokenizer reads the rest of
        #: the page as its RCDATA, so the page text would be lost with it.
        self.title_unclosed = False
        #: Page text kept, and the characters past :data:`HTML_TEXT_MAX_CHARS`.
        self._text_chars = 0
        self.text_cut_chars = 0
        #: The references :meth:`feed` held out of the tokenizer, by index:
        #: ``(the reference as the page writes it, what it decodes to)``, and
        #: the two noncharacters around an index (:func:`_absent_pair`).
        self._held: list[tuple[str, str]] = []
        self._held_open = self._held_close = ""
        self._held_re: re.Pattern[str] | None = None

    # -- long character references ---------------------------------------

    def feed(self, data: str) -> None:
        """Feed *data* with every :data:`_LONG_CHARREF_RE` reference held out of the tokenizer.

        Each becomes its index between two noncharacters the page does not
        carry (:func:`_absent_pair`) - so no text of the page can pass for
        one - which neither the tokenizer nor a decoding touches, and
        :meth:`_put_back` restores what the page holds there: the reference's
        value where the tokenizer decodes - text, RCDATA, an attribute value -
        as a browser reads it, whatever digit limit the process set; the
        reference itself in raw text (a script body, ``<plaintext>``), where it
        is data. Only a page carrying 65 of the 66 noncharacters leaves no
        pair: it gets each reference's short form in place instead
        (:func:`_shorten_charref`), the same values, its raw text shortened.
        """
        if "&#" in data and _LONG_CHARREF_RE.search(data):
            data = _LONG_CHARREF_RE.sub(self._hold if self._can_hold(data) else _shorten_charref, data)
        super().feed(data)

    def _can_hold(self, data: str) -> bool:
        """Whether *data* leaves two noncharacters to delimit a held index with, chosen on the first hold."""
        if self._held_re is None:
            pair = _absent_pair(data)
            if pair is None:
                return False
            self._held_open, self._held_close = pair
            self._held_re = re.compile(f"{self._held_open}([0-9]+){self._held_close}")
            return True
        return self._held_open not in data and self._held_close not in data

    def _hold(self, match: re.Match[str]) -> str:
        self._held.append((match.group(0), _html_unescape(_shorten_charref(match))))
        return f"{self._held_open}{len(self._held) - 1}{self._held_close}"

    def _put_back(self, text: str, *, raw: bool) -> str:
        """*text* with each held reference restored: itself in *raw* text, its value elsewhere."""
        if self._held_re is None:
            return text
        slot = 0 if raw else 1
        return self._held_re.sub(lambda match: self._held[int(match.group(1))][slot], text)

    def _decoded_attrs(self, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
        """*attrs* with the held references of their values restored as values (an attribute value is decoded)."""
        if not self._held:
            return attrs
        return [
            (name, self._put_back(value, raw=False) if value and self._held_open in value else value)
            for name, value in attrs
        ]

    # -- text plumbing ---------------------------------------------------

    def _text_target(self) -> list[str]:
        if self._cell is not None:
            return self._cell
        if self._pre_depth:
            return self._pre_parts
        return self._inline

    def _flush(self) -> None:
        if self._cell is not None:
            self._cell.append(" ")
            return
        if self._inline:
            text = _WS_RE.sub(" ", "".join(self._inline)).strip()
            self._inline = []
            if text:
                self.lines.append(self._prefix + text)
        self._prefix = ""

    def _blank(self) -> None:
        self._flush()
        if self._cell is None and self.lines and self.lines[-1] != "":
            self.lines.append("")

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            text = _WS_RE.sub(" ", "".join(self._cell)).strip().replace("|", "\\|")
            self._row.append(text)
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row is not None:
            cells = self._row
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                self.lines.append("| " + " | ".join(cells) + " |")
        self._row = None

    # -- tokenizer callbacks ---------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "script":
            self._open_script(self._decoded_attrs(attrs))
            return
        if self._skip:
            top = self._skip[-1]
            if tag == top[0] and tag not in _VOID_TAGS:
                top[1] += 1
            return
        if tag in _SKIP_TAGS:
            self.stripped[tag] += 1
            self._skip.append([tag, 1])
            self._skip_data_chars = 0
            return
        if tag in _HIDEABLE_TAGS and any(
            name == "hidden" and (value or "").strip().lower() != "until-found"
            for name, value in self._decoded_attrs(attrs)
        ):
            self.stripped["hidden element"] += 1
            self._skip.append([tag, 1])
            self._skip_data_chars = 0
            return
        if tag == "title":
            if self.title is None and self._title_parts is None:
                self._title_parts = []
            return
        self._structure_start(tag)

    def set_cdata_mode(self, elem: str, *args: Any, **kwargs: Any) -> None:
        """The tokenizer's raw-text switch, minus two ``<title>`` cases.

        Only the page's FIRST ``<title>`` is RCDATA (read up to ``</title>``):
        a later one - or one inside ``<svg>``, where a browser parses it as
        markup - must not swallow the rest of the page if it is never closed.
        """
        if elem == "title" and (self._skip or self.title is not None or self.title_unclosed):
            return
        super().set_cdata_mode(elem, *args, **kwargs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "script" or tag == "title" or self._skip:
            return
        if tag in _SKIP_TAGS:
            self.stripped[tag] += 1
            return
        self._structure_start(tag)
        if tag not in _VOID_TAGS:
            self._structure_end(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script":
            self._close_script()
            return
        if self._skip:
            top = self._skip[-1]
            if tag == top[0]:
                top[1] -= 1
                if top[1] <= 0:
                    self._skip.pop()
            return
        if tag == "title":
            if self._title_parts is not None:
                self.title = _clip_title("".join(self._title_parts))
                self._title_parts = None
            return
        self._structure_end(tag)

    def handle_data(self, data: str) -> None:
        if self._held and self._held_open in data:
            # Raw text is data the tokenizer never decodes (a script body):
            # a reference there stays as the page writes it.
            data = self._put_back(data, raw=self.cdata_elem not in (None, *self.RCDATA_CONTENT_ELEMENTS))
        if self._script is not None:
            self._capture_script(data)
            return
        if self._skip:
            self._skip_data_chars += len(data)
            return
        if self._title_parts is not None:
            self._title_parts.append(data)
            return
        if self._text_chars >= HTML_TEXT_MAX_CHARS:
            self.text_cut_chars += len(data)
            return
        self._text_chars += len(data)
        self._text_target().append(data)

    # -- structure ---------------------------------------------------------

    def _structure_start(self, tag: str) -> None:
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._blank()
            else:
                self._text_target().append(" ")
            return
        if self._table_depth == 1:
            if tag == "tr":
                self._close_row()
                self._flush()
                self._row = []
                return
            if tag in ("td", "th"):
                if self._row is None:
                    self._flush()
                    self._row = []
                self._close_cell()
                self._cell = []
                return
        elif self._table_depth > 1 and tag in ("tr", "td", "th"):
            self._text_target().append(" ")
            return
        if tag in _HEADING_LEVEL:
            self._blank()
            if self._cell is None:  # inside a cell a heading is just text
                self._prefix = "#" * _HEADING_LEVEL[tag] + " "
            return
        if tag in ("ul", "ol"):
            self._flush()
            self._list_depth += 1
            return
        if tag == "li":
            self._flush()
            if self._cell is None:
                indent = "  " * (min(max(self._list_depth, 1), _MAX_LIST_INDENT) - 1)
                self._prefix = indent + "- "
            return
        if tag == "br":
            if self._pre_depth and self._cell is None:
                self._pre_parts.append("\n")
            else:
                self._flush()
            return
        if tag == "hr":
            self._blank()
            return
        if tag == "pre":
            self._blank()
            self._pre_depth += 1
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def _structure_end(self, tag: str) -> None:
        if tag == "table":
            if self._table_depth == 1:
                self._close_row()
                self._blank()
            elif self._table_depth > 1:
                self._text_target().append(" ")
            self._table_depth = max(0, self._table_depth - 1)
            return
        if self._table_depth == 1:
            if tag == "tr":
                self._close_row()
                return
            if tag in ("td", "th"):
                self._close_cell()
                return
        if tag in _HEADING_LEVEL:
            self._flush()
            self._blank()
            return
        if tag in ("ul", "ol"):
            self._flush()
            self._list_depth = max(0, self._list_depth - 1)
            if self._list_depth == 0:
                self._blank()
            return
        if tag == "li":
            self._flush()
            return
        if tag == "pre" and self._pre_depth:
            self._pre_depth -= 1
            if not self._pre_depth:
                block = "".join(self._pre_parts)
                self._pre_parts = []
                for line in block.splitlines():
                    if line.strip():
                        self.lines.append(line.rstrip())
                self._blank()
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            if tag == "p":
                self._blank()

    # -- scripts -----------------------------------------------------------

    def _open_script(self, attrs: list[tuple[str, str | None]]) -> None:
        kind = ""
        element_id = None
        for name, value in attrs:
            if name == "type":
                kind = (value or "").split(";", 1)[0].strip().lower()
            elif name == "id":
                element_id = (value or "").strip() or None
        self._script = _Script(kind=kind, element_id=element_id)

    def _capture_script(self, data: str) -> None:
        script = self._script
        if script is None:  # pragma: no cover - handle_data only calls it inside a script
            return
        self.script_chars += len(data)
        room = _SCRIPT_CAPTURE_MAX_CHARS - self._captured_chars
        if room <= 0:
            self.uncaptured_script_chars += len(data)
            return
        kept = data[:room]
        script.parts.append(kept)
        script.chars += len(kept)
        self._captured_chars += len(kept)
        self.uncaptured_script_chars += len(data) - len(kept)

    def _close_script(self) -> None:
        script = self._script
        self._script = None
        if script is None or not script.chars:
            return
        self.scripts.append(script)

    def close(self) -> None:
        if self._title_parts is not None:
            self._recover_unclosed_title()
        super().close()
        if self._script is not None:
            self.unclosed = ("script", self._script.chars)
            self._close_script()
        elif self._skip:
            self.unclosed = (self._skip[0][0], self._skip_data_chars)
        self._close_row()
        if self._pre_parts:
            for line in "".join(self._pre_parts).splitlines():
                if line.strip():
                    self.lines.append(line.rstrip())
            self._pre_parts = []
        self._flush()

    def _recover_unclosed_title(self) -> None:
        """Read the page an unclosed ``<title>`` swallowed (once, never recursively).

        The tokenizer reads a ``<title>`` as RCDATA up to ``</title>``; without
        one, the whole rest of the page is the title's text and the reader
        would render an empty page. That text is read from what the tokenizer
        still holds, before its ``close()`` hands it over decoded - in RCDATA
        mode it consumes nothing past the start tag, or past an end tag it
        cannot finish (``</title`` with no ``>``), so it holds the page as the
        source writes it; decoded twice, escaped markup would become markup.
        Up to the first tag it is the title, after the text read before such
        an end tag, each reference decoded once; the rest is fed again as
        markup, with the title settled so a later ``<title>`` cannot swallow
        it a second time.
        """
        source = self.rawdata
        decoded = "".join(self._title_parts or [])
        self._title_parts = None
        self.title_unclosed = True
        cut = _MARKUP_OPEN_RE.search(source)
        end = len(source) if cut is None else cut.start()
        self.title = _clip_title(decoded + self._put_back(_html_unescape(source[:end]), raw=False)) or ""
        HTMLParser.reset(self)
        if end < len(source):
            # Its long references are held already: fed as it is.
            HTMLParser.feed(self, source[end:])


# -- Embedded data ------------------------------------------------------------


#: The longest element id or literal name a block label quotes.
_LABEL_NAME_MAX_CHARS = 80
#: What would end a label's line, reorder it or hide in it (Unicode general
#: categories): control characters, format characters (bidi marks, zero-width
#: and invisible characters, soft hyphen, tags), surrogates, line and
#: paragraph separators.
_LABEL_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _label_text(value: str) -> str:
    """Page text a block label quotes, as inert text: one line, bounded (an id is the page's, not the platform's)."""
    head = value[: _LABEL_NAME_MAX_CHARS * 4]
    text = "".join(" " if unicodedata.category(char) in _LABEL_UNSAFE_CATEGORIES else char for char in head)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > _LABEL_NAME_MAX_CHARS or len(head) < len(value):
        text = text[: _LABEL_NAME_MAX_CHARS - 3].rstrip() + "..."
    return text


@dataclass
class EmbeddedJson:
    """One data block found in an attachment."""

    #: 1-based position among the attachment's blocks (the ``block`` argument).
    index: int
    #: ``application/json`` / ``application/ld+json`` / ``javascript`` / the
    #: file's own type for a JSON attachment.
    kind: str
    #: The ``id`` of the script element, or the variable a literal is
    #: assigned to (``ROWS``, ``window.DATA``), when there is one.
    element_id: str | None
    #: Parsed value; ``None`` when the block did not parse (see ``error``).
    data: Any
    #: Length of the raw block text.
    raw_chars: int
    error: str | None = None
    #: What of the block was NOT read (``None`` when it was read whole): a
    #: query over a partial block says so, never a definitive "no match".
    partial: str | None = None

    @property
    def label(self) -> str:
        name = _label_text(self.element_id) if self.element_id else ""
        if self.kind == "javascript":
            parts = [f"Block {self.index}: JavaScript literal{' ' + name if name else ''}"]
        else:
            parts = [f"Block {self.index}: {self.kind}"]
            if name:
                parts.append(f"id={json.dumps(name, ensure_ascii=False)}")
        parts.append(f"({self.raw_chars:,} chars, {describe_shape(self.data)})")
        return " ".join(parts)


#: How :func:`_loads` starts the error of a text past :data:`JSON_PARSE_MAX_CHARS`.
_TOO_LARGE_TO_PARSE = "too large to parse here"


def _loads(text: str) -> tuple[Any, str | None]:
    """``json.loads`` that never raises: ``(value, None)`` or ``(None, error)``.

    A text past :data:`JSON_PARSE_MAX_CHARS` is refused, not parsed: the
    parsed objects weigh up to ~25 times the text.
    """
    if len(text) > JSON_PARSE_MAX_CHARS:
        return None, f"{_TOO_LARGE_TO_PARSE} ({len(text):,} chars; the limit is {JSON_PARSE_MAX_CHARS:,})"
    try:
        return json.loads(text), None
    except RecursionError:
        return None, "nested too deep to parse"
    except ValueError as exc:
        return None, f"not valid JSON ({str(exc)[:120]})"


def _parse_error_text(error: str, *, code_interpreter: bool) -> str:
    """A :func:`_loads` error as the reader gets it: a text past the parse cap
    is one the code interpreter reads whole - said only where it is offered."""
    if code_interpreter and error.startswith(_TOO_LARGE_TO_PARSE):
        return f"{error} - code_interpreter reads it whole"
    return error


class _JsBudget:
    """The per-document work budget of the JavaScript scanner."""

    __slots__ = ("normalise_left", "scan_left", "unscanned")

    def __init__(self) -> None:
        self.scan_left = _JS_SCAN_MAX_CHARS
        self.normalise_left = _JS_NORMALISE_MAX_CHARS
        #: Script characters past the scan budget, reported, never read.
        self.unscanned = 0


#: One token of the scanner, found by the C regex engine: a string (skipped
#: whole), a comment opener, an identifier chain (``window.DATA``), one of the
#: characters the scanner reacts to, a whitespace run (keeps the state), or a
#: run of anything else (operators, digits, punctuation - resets it). Every
#: alternative consumes at least one character and the string forms are
#: possessive, so a match never backtracks and the walk is linear.
_JS_TOKEN_RE = re.compile(
    r"""
    (?P<string>'(?:[^'\\\n]|\\[\s\S])*+'?|"(?:[^"\\\n]|\\[\s\S])*+"?|`(?:[^`\\]|\\[\s\S])*+`?)
    | (?P<comment>//|/\*)
    | (?P<ident>[A-Za-z_$][\w$]*+(?:\.[A-Za-z_$][\w$]*+)*+)
    | (?P<punct>[\[\]{}=(])
    | (?P<space>\s++)
    | (?P<other>[^'"`/\[\]{}=(A-Za-z_$\s]++|/)
    """,
    re.VERBOSE,
)
_JS_KEYWORDS_BEFORE_VALUE = frozenset({"const", "let", "var", "return", "typeof", "new", "export", "default", "await"})


@dataclass
class _Literal:
    name: str
    start: int
    end: int
    #: The quote of a ``JSON.parse('...')`` string; ``None`` for a bare literal.
    quote: str | None = None


def _scan_js_literals(script: str, budget: _JsBudget) -> list[_Literal]:
    """Every data literal a script assigns, in one linear pass.

    Recognised: ``<ident or a.b.c> = [...]`` / ``= {...}`` (``const`` /
    ``let`` / ``var`` or none; a literal passed straight to a call on the
    right-hand side - ``= Object.freeze([...])``, ``= new Map([...])`` - keeps
    the name), ``JSON.parse('...')`` / ``("...")``, and a script that IS one
    bare literal. The walk is token by token through :data:`_JS_TOKEN_RE`, so
    strings (a whole JSON payload's worth of them) and comments are skipped as
    units by the C regex engine and an anchor inside them is never taken.
    Brackets are counted, never stacked: only the anchored ones are
    remembered (bounded), so nesting depth costs nothing. A literal is
    bracket-matched here and parsed later from its exact slice, so the cost
    of a literal that fails to parse is bounded by that literal - never by
    its position in the script. The walk stops at the document's scan budget.
    """
    n = min(len(script), budget.scan_left)
    budget.scan_left -= n
    budget.unscanned += len(script) - n
    found: list[_Literal] = []
    open_anchors: list[tuple[int, int, str]] = []  # (depth at open, position, name)
    depth = 0
    i = 0
    last_ident: str | None = None  # the identifier chain that is the last token
    pending: str | None = None  # "<ident> =" just seen: the name the next literal gets
    callee_of: str | None = None  # "<ident> = f" just seen: a literal inside "(" keeps the name
    json_parse_open = False  # "JSON.parse(" just seen: the next string is a candidate
    first_token = True
    match_token = _JS_TOKEN_RE.match
    while i < n:
        token = match_token(script, i, n)
        if token is None:  # pragma: no cover - the ``other`` branch matches any character
            break
        kind = token.lastgroup
        end = token.end()
        if kind == "space":
            i = end
            continue
        if kind == "comment":
            if token.group() == "//":
                nl = script.find("\n", end, n)
                i = n if nl < 0 else nl + 1
            else:
                close = script.find("*/", end, n)
                i = n if close < 0 else close + 2
            continue
        was_first, first_token = first_token, False
        if kind == "string":
            quote = script[i]
            if json_parse_open and end - i - 2 >= MIN_JS_PAYLOAD_CHARS and end - i >= 2 and script[end - 1] == quote:
                label = f"{pending} (JSON.parse)" if pending else "JSON.parse string"
                found.append(_Literal(label, i + 1, end - 1, quote=quote))
            json_parse_open = False
            last_ident = pending = callee_of = None
            i = end
            continue
        if kind == "ident":
            word = token.group()
            json_parse_open = False
            if pending is not None or callee_of is not None:
                callee_of = pending or callee_of  # ``= Object.freeze(`` / ``= new Map(``
            pending = None
            last_ident = None if word in _JS_KEYWORDS_BEFORE_VALUE else word
            i = end
            continue
        if kind == "punct":
            c = script[i]
            i = end
            if c == "=":
                nxt = script[i] if i < n else ""
                if nxt in "=>":  # ``==``, ``===``, ``=>``: not an assignment
                    while i < n and script[i] in "=>":
                        i += 1
                    last_ident = pending = callee_of = None
                    json_parse_open = False
                    continue
                pending = last_ident
                last_ident = callee_of = None
                json_parse_open = False
                continue
            if c == "(":
                json_parse_open = last_ident == "JSON.parse"
                pending = callee_of if last_ident is not None else None
                last_ident = callee_of = None
                continue
            if c in "[{":
                name = pending or ("<script> literal" if was_first else None)
                if name is not None and len(open_anchors) < _JS_MAX_OPEN_ANCHORS:
                    open_anchors.append((depth, i - 1, name))
                depth += 1
            elif depth:  # a closing bracket
                depth -= 1
                if open_anchors and open_anchors[-1][0] == depth:
                    _depth, start, name = open_anchors.pop()
                    if i - start >= MIN_JS_PAYLOAD_CHARS:
                        found.append(_Literal(name, start, i))
            last_ident = pending = callee_of = None
            json_parse_open = False
            continue
        # ``other``: an operator, a number, punctuation - nothing is pending.
        last_ident = pending = callee_of = None
        json_parse_open = False
        i = end
    # Outermost literals only: a literal inside one already kept is part of it.
    found.sort(key=lambda lit: (lit.start, -lit.end))
    kept: list[_Literal] = []
    covered_until = -1
    for literal in found:
        if literal.start < covered_until:
            continue
        kept.append(literal)
        covered_until = literal.end
    return kept


_JS_IDENT_KEY_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _js_literal_to_json(literal: str) -> str:
    """Rewrite a JavaScript object / array literal into JSON, best effort, in one pass.

    Quotes bare keys, turns single- and back-quoted strings into
    double-quoted ones, drops comments and trailing commas, maps
    ``undefined`` / ``NaN`` / ``Infinity`` to ``null``. Anything it cannot
    handle (a template expression, a function, a hex number) is left in
    place and fails the ``json.loads`` that follows - the signal the caller
    wants. Every character is visited a bounded number of times.
    """
    out: list[str] = []
    i = 0
    n = len(literal)
    last_sig = ""
    while i < n:
        ch = literal[i]
        if ch in "'\"`":
            j = i + 1
            chunk: list[str] = []
            while j < n and literal[j] != ch:
                c = literal[j]
                if c == "\\" and j + 1 < n:
                    nxt = literal[j + 1]
                    if ch != '"' and nxt == ch:
                        chunk.append(nxt)
                    else:
                        chunk.append(c + nxt)
                    j += 2
                    continue
                if ch != '"' and c == '"':
                    chunk.append('\\"')
                elif c == "\n":
                    chunk.append("\\n")
                else:
                    chunk.append(c)
                j += 1
            out.append('"' + "".join(chunk) + '"')
            last_sig = '"'
            i = j + 1
            continue
        if ch == "/" and i + 1 < n and literal[i + 1] == "/":
            nl = literal.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if ch == "/" and i + 1 < n and literal[i + 1] == "*":
            close = literal.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        if ch == ",":
            k = i + 1
            while k < n and literal[k] in " \t\r\n":
                k += 1
            if k < n and literal[k] in "]}":
                i = k
                continue
            out.append(ch)
            last_sig = ch
            i += 1
            continue
        if ch.isalpha() or ch in "_$":
            match = _JS_IDENT_KEY_RE.match(literal, i)
            word = match.group(0) if match else ch
            k = i + len(word)
            while k < n and literal[k] in " \t\r\n":
                k += 1
            if k < n and literal[k] == ":" and last_sig in "{,":
                out.append(f'"{word}"')
            elif word in ("undefined", "NaN", "Infinity"):
                out.append("null")
            else:
                out.append(word)
            last_sig = word[-1]
            i += len(word)
            continue
        out.append(ch)
        if not ch.isspace():
            last_sig = ch
        i += 1
    return "".join(out)


_JS_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _js_string_value(body: str) -> str | None:
    """The value of a JavaScript string literal's *body* (between its quotes), or ``None``."""
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            return None
        e = body[i + 1]
        if e in _JS_ESCAPES:
            out.append(_JS_ESCAPES[e])
            i += 2
        elif e == "x":
            if i + 4 > n:
                return None
            try:
                out.append(chr(int(body[i + 2 : i + 4], 16)))
            except ValueError:
                return None
            i += 4
        elif e == "u":
            if body[i + 2 : i + 3] == "{":
                close = body.find("}", i + 3, i + 12)
                if close < 0:
                    return None
                try:
                    out.append(chr(int(body[i + 3 : close], 16)))
                except (ValueError, OverflowError):
                    return None
                i = close + 1
            else:
                try:
                    out.append(chr(int(body[i + 2 : i + 6], 16)))
                except ValueError:
                    return None
                i += 6
        elif e in "\r\n":
            i += 2  # a line continuation
        else:
            out.append(e)
            i += 2
    value = "".join(out)
    if _SURROGATE_RE.search(value):
        # A JavaScript string is UTF-16: ``\ud83d\ude00`` (what an ASCII-only
        # minifier writes for an emoji) is ONE character, which the escapes
        # decoded one by one leave as two lone surrogates. A lone half stays
        # one, for ``_encodable`` to read as U+FFFD.
        value = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    return value


def _parse_literal(script: str, literal: _Literal, budget: _JsBudget) -> tuple[Any, str | None]:
    """Parse one literal from its exact slice: JSON first (C speed), else normalised (budgeted)."""
    raw = script[literal.start : literal.end]
    if literal.quote is not None:
        text = _js_string_value(raw)
        if text is None:
            return None, "a string that is not a valid JavaScript literal"
        return _loads(text)
    value, error = _loads(raw)
    if error is None:
        return value, None
    if len(raw) > budget.normalise_left:
        return None, "too large to normalise"
    budget.normalise_left -= len(raw)
    try:
        return _loads(_js_literal_to_json(raw))
    except RecursionError:  # pragma: no cover - the normaliser is iterative
        return None, "nested too deep to parse"


@dataclass
class HtmlExtraction:
    """What :func:`extract_html_document` read out of one HTML document."""

    title: str | None
    text: str
    json_blocks: list[EmbeddedJson] = field(default_factory=list)
    #: Sizes of the JavaScript literals that looked like data but did not parse.
    unparsed_literal_chars: list[int] = field(default_factory=list)
    #: What was left out: ``style`` / ``svg`` / ``script`` (code) / ``hidden element`` ... -> count.
    stripped: dict[str, int] = field(default_factory=dict)
    source_chars: int = 0
    script_chars: int = 0
    #: Script characters past the capture or scan budget (never scanned).
    unscanned_script_chars: int = 0
    #: Data blocks found past :data:`MAX_EMBEDDED_BLOCKS` (counted, not read).
    dropped_blocks: int = 0
    #: The element the page ENDS inside (never closed) and the characters
    #: from it to the end of the page, which the rendering does not show.
    unclosed: tuple[str, int] | None = None
    #: The ``<title>`` was never closed (a browser shows such a page blank):
    #: the text after it was recovered and read as the page.
    title_unclosed: bool = False
    #: Source characters past :data:`HTML_RENDER_MAX_CHARS` (never tokenized).
    unread_source_chars: int = 0
    #: Page-text characters past :data:`HTML_TEXT_MAX_CHARS` (read, not kept).
    text_cut_chars: int = 0

    def partial_notes(self) -> list[str]:
        """What of the page's data was NOT read, one clause each (empty when all of it was)."""
        notes: list[str] = []
        if self.unread_source_chars:
            notes.append(f"the last {self.unread_source_chars:,} chars of the page were not read")
        if self.dropped_blocks:
            notes.append(
                f"{self.dropped_blocks:,} more data block{'s' if self.dropped_blocks != 1 else ''} past "
                f"the first {MAX_EMBEDDED_BLOCKS} {'were' if self.dropped_blocks != 1 else 'was'} not read"
            )
        if self.unscanned_script_chars:
            notes.append(f"{self.unscanned_script_chars:,} chars of script past the scan budget were not read")
        return notes


def _script_blocks(reader: _HtmlReader) -> tuple[list[EmbeddedJson], list[int], int, int, int]:
    """The data blocks of the page's scripts, in document order, bounded:
    ``(blocks, unparsed literal sizes, code scripts, unscanned chars, dropped blocks)``."""
    blocks: list[EmbeddedJson] = []
    unparsed: list[int] = []
    code_scripts = 0
    dropped = 0
    budget = _JsBudget()
    for script in reader.scripts:
        body = "".join(script.parts)
        if not body.strip():
            continue
        if script.kind in _JSON_SCRIPT_TYPES or script.kind.endswith("+json"):
            if len(blocks) >= MAX_EMBEDDED_BLOCKS:
                dropped += 1
                continue
            value, error = _loads(body.strip())
            blocks.append(
                EmbeddedJson(
                    index=len(blocks) + 1,
                    kind=script.kind,
                    element_id=script.element_id,
                    data=value,
                    raw_chars=len(body.strip()),
                    error=error,
                )
            )
            continue
        if script.kind not in _JS_SCRIPT_TYPES:
            continue  # a template, a shader, text/plain: neither data nor code we read
        code_scripts += 1
        if len(body) < MIN_JS_PAYLOAD_CHARS:
            continue
        if budget.scan_left <= 0:
            budget.unscanned += len(body)
            continue
        for literal in _scan_js_literals(body, budget):
            if len(blocks) >= MAX_EMBEDDED_BLOCKS:
                dropped += 1
                continue
            value, error = _parse_literal(body, literal, budget)
            if error is not None or not isinstance(value, (dict, list)):
                unparsed.append(literal.end - literal.start)
                continue
            blocks.append(
                EmbeddedJson(
                    index=len(blocks) + 1,
                    kind="javascript",
                    element_id=None if literal.name in ("<script> literal", "JSON.parse string") else literal.name,
                    data=value,
                    raw_chars=literal.end - literal.start,
                )
            )
    return blocks, unparsed, code_scripts, reader.uncaptured_script_chars + budget.unscanned, dropped


def _normalise_lines(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _read_html(source: str) -> _HtmlReader:
    reader = _HtmlReader()
    reader.feed(source)
    reader.close()
    return reader


def extract_html_document(html: str) -> HtmlExtraction:
    """Read an HTML document: reading-order text + the data its scripts carry.

    Headings become ``#`` lines, list items ``- `` lines (indented by
    nesting), table rows ``| a | b |`` lines (a ``|`` inside a cell escaped
    as ``\\|``, trailing empty cells and empty rows dropped), ``<br>`` a line
    break and every other block element a line boundary. Styles, scripts,
    SVG / MathML / canvas, inert content (``<template>``, ``<noscript>``) and
    an element carrying the ``hidden`` attribute (``hidden="until-found"``
    is content a reader can reveal, and stays) are left out and counted in
    ``stripped``. The first ``<title>`` outside those subtrees is the title.
    """
    source = html[:HTML_RENDER_MAX_CHARS]
    reader = _read_html(source)
    blocks, unparsed, code_scripts, unscanned, dropped = _script_blocks(reader)
    stripped = dict(reader.stripped)
    if code_scripts:
        stripped["script"] = code_scripts
    return HtmlExtraction(
        title=reader.title,
        text=_normalise_lines(reader.lines),
        json_blocks=blocks,
        unparsed_literal_chars=unparsed,
        stripped=stripped,
        source_chars=len(html),
        script_chars=reader.script_chars,
        unscanned_script_chars=unscanned,
        dropped_blocks=dropped,
        unclosed=reader.unclosed,
        title_unclosed=reader.title_unclosed,
        unread_source_chars=len(html) - len(source),
        text_cut_chars=reader.text_cut_chars,
    )


def format_embedded_data_section(
    blocks: list[EmbeddedJson],
    *,
    unparsed_literal_chars: list[int] | None = None,
    unscanned_script_chars: int = 0,
    dropped_blocks: int = 0,
    max_chars: int = EMBEDDED_DATA_SECTION_MAX_CHARS,
    block_max_chars: int = JSON_BLOCK_SUMMARY_MAX_CHARS,
    code_interpreter: bool = False,
) -> str:
    """The ``## Embedded data`` section: every block, largest first, bounded.

    *code_interpreter* says whether the reader has the code interpreter:
    only then does the section send a whole-file computation there.
    """
    unparsed = unparsed_literal_chars or []
    if not blocks and not unparsed:
        if not unscanned_script_chars:
            return ""
        # No block found in the part scanned is not "no data": a literal past
        # the scan budget would otherwise vanish without a word.
        return (
            "## Embedded data\n"
            f"No data block found in the part of the page's scripts that was scanned; the "
            f"{unscanned_script_chars:,} chars past the scan budget were not read"
            + (" - code_interpreter reads the whole file." if code_interpreter else ".")
        )
    count = len(blocks)
    lines = [
        "## Embedded data",
        f"{count} data block{'s' if count != 1 else ''} found in the page's scripts (JSON <script> "
        "elements and JavaScript literals) - read one value with "
        "query_file_json(file_id, path, block=N) instead of the source"
        + ("; for a computation over every row use code_interpreter." if code_interpreter else "."),
    ]
    if dropped_blocks:
        lines.append(
            f"{dropped_blocks:,} more data block{'s' if dropped_blocks != 1 else ''} past the first "
            f"{MAX_EMBEDDED_BLOCKS} {'were' if dropped_blocks != 1 else 'was'} not read - they are in "
            f"the file{' (code_interpreter reads it whole)' if code_interpreter else ''}, not in this "
            "section or in query_file_json."
        )
    if unparsed:
        sizes = ", ".join(f"{n:,}" for n in unparsed[:5])
        lines.append(
            f"{len(unparsed)} more literal{'s' if len(unparsed) != 1 else ''} ({sizes} chars) "
            "did not parse as data and are omitted."
        )
    if unscanned_script_chars:
        lines.append(f"{unscanned_script_chars:,} chars of script past the scan budget were not read.")
    budget = max_chars - sum(len(line) + 1 for line in lines)
    order = sorted(blocks, key=lambda b: b.raw_chars, reverse=True)
    for position, block in enumerate(order):
        header = f"\n### {block.label}"
        body = (
            f"(skipped: {_parse_error_text(block.error, code_interpreter=code_interpreter)})"
            if block.error
            else summarize_json(block.data, max_chars=min(block_max_chars, max(400, budget - len(header) - 1)))
        )
        chunk = header + "\n" + body
        if len(chunk) > budget:
            remaining = len(order) - position
            lines.append(
                f"\n... [{remaining} block{'s' if remaining != 1 else ''} not summarised here - "
                "the section is bounded; query_file_json(file_id, block=N) shows any of them]"
            )
            break
        lines.append(chunk)
        budget -= len(chunk) + 1
    return "\n".join(lines)


def _stripped_line(doc: HtmlExtraction) -> str:
    """The one line accounting for what the rendering leaves out - and for a
    page it could only read in part (an element never closed)."""
    notes: list[str] = []
    if doc.stripped:
        parts = ", ".join(
            f"{count} <{tag}>" if tag != "hidden element" else f"{count} hidden element{'s' if count != 1 else ''}"
            for tag, count in sorted(doc.stripped.items())
        )
        notes.append(f"not shown: {parts}")
    if doc.unclosed is not None:
        tag, chars = doc.unclosed
        fate = "read as script, not shown" if tag == "script" else "not shown"
        notes.append(
            f"the page ends inside an unclosed <{tag}>, so the {chars:,} characters of text from it to the end are {fate}"
        )
    if doc.title_unclosed:
        notes.append("its <title> is never closed (a browser shows such a page blank) - the text after it is read as the page")
    if doc.unread_source_chars:
        notes.append(
            f"only the first {HTML_RENDER_MAX_CHARS:,} chars were read - the last {doc.unread_source_chars:,} "
            "(text and data alike) are in the file, not in this rendering"
        )
    if doc.text_cut_chars:
        notes.append(f"the page text stops after {HTML_TEXT_MAX_CHARS:,} chars ({doc.text_cut_chars:,} more are not shown)")
    if not notes:
        return ""
    return (
        f"[Rendered from {doc.source_chars:,} chars of HTML; {'; '.join(notes)}. "
        'The verbatim source is read_file_content(mode="source").]'
    )


def render_html_attachment(html: str, *, filename: str | None = None, code_interpreter: bool = False) -> str:
    """The text a reader gets for an HTML attachment.

    ``# <title>``, one line accounting for what was left out, the bounded
    ``## Embedded data`` section, then the page text under ``## Page text``.
    The data comes BEFORE the text on purpose: a caller that caps the
    rendering (the chat message, the inline fallback) cuts the tail of the
    text, never the data or the pointer to ``query_file_json``, and the
    capped text stays a prefix of this rendering, so the offset a cap names
    is the offset ``read_file_content(mode="extract_structured")`` serves.
    *code_interpreter*: see :func:`format_embedded_data_section`.
    """
    doc = extract_html_document(html)
    parts: list[str] = []
    if doc.title:
        parts.append(f"# {doc.title}")
    stripped = _stripped_line(doc)
    if stripped:
        parts.append(stripped)
    embedded = format_embedded_data_section(
        doc.json_blocks,
        unparsed_literal_chars=doc.unparsed_literal_chars,
        unscanned_script_chars=doc.unscanned_script_chars,
        dropped_blocks=doc.dropped_blocks,
        code_interpreter=code_interpreter,
    )
    text = doc.text or "(the page has no visible text)"
    if embedded:
        parts.append(embedded)
        parts.append("## Page text\n\n" + text)
    else:
        parts.append(text)
    return "\n\n".join(parts)


# -- JSON / JSON Lines / CSV attachments --------------------------------------


def _jsonl_lines(text: str) -> Iterator[str]:
    """The lines of a JSON Lines text, one at a time, split on ``\\n`` ONLY.

    ``str.splitlines()`` also splits on U+2028 / U+2029 / U+0085, which JSON
    allows raw inside a string: a record carrying one would become two
    invalid lines. Lazy, so a 40 MB file is never held twice.
    """
    start = 0
    size = len(text)
    while start < size:
        end = text.find("\n", start)
        if end < 0:
            end = size
        yield text[start:end]
        start = end + 1


class JsonlPlaceholder:
    """The slot of a JSON Lines line that holds no value - not valid JSON, or
    blank - so that every later line keeps its index: ``$[i]`` is line
    ``i + 1`` of the file on every surface.

    Never data: a query renders it as its marker (``<invalid line 13>``) and
    counts it apart from the values, the summary shows it in its slot, and
    the field statistics - counts, distinct values, min / max / sum, nulls -
    skip it (a JSON ``null`` in its place would skew them).
    """

    __slots__ = ("column", "line", "reason")

    def __init__(self, line: int, reason: str | None = None, column: int = 0) -> None:
        #: The 1-based line of the file.
        self.line = line
        #: Why the line is not valid JSON; ``None`` for a blank line.
        self.reason = reason
        #: The 1-based column of the line where parsing stopped, 0 when unknown.
        self.column = column

    def __str__(self) -> str:
        return f"<{'blank' if self.reason is None else 'invalid'} line {self.line}>"

    __repr__ = __str__

    def sentence(self, path: str) -> str:
        """The answer to a query whose one match is this slot - an index, or a
        slice or wildcard that selected nothing else: which line, and why it
        holds no value."""
        if self.reason is None:
            what = "blank"
        else:
            what = f"not valid JSON ({self.reason}{f' at column {self.column}' if self.column else ''})"
        return f"1 item matches {path!r}: line {self.line} of the file, which is {what} - it holds no value."


class JsonlLines(list):
    """The lines of a JSON Lines file as ONE array, line N at index N - 1: its
    value, or the :class:`JsonlPlaceholder` holding its slot. Carries its
    counts, so a summary of 50,000 lines never recounts them."""

    def __init__(self) -> None:
        super().__init__()
        #: Every line of the file, the ones past the caps (not in the array) included.
        self.lines = 0
        self.invalid = 0
        self.blank = 0
        #: Lines past :data:`JSONL_MAX_RECORDS` / :data:`JSON_PARSE_MAX_CHARS`.
        self.unread = 0
        #: The first :data:`_JSONL_LISTED_LINES` invalid / blank line numbers, for the reports.
        self.invalid_lines: list[int] = []
        self.blank_lines: list[int] = []

    def describe(self) -> str:
        """``30 lines, $[i] = line i+1 of the file; 1 not valid JSON, 2 blank``."""
        text = f"{len(self):,} line{'s' if len(self) != 1 else ''}, $[i] = line i+1 of the file"
        counts = ((self.invalid, "not valid JSON"), (self.blank, "blank"))
        held = [f"{count:,} {what}" for count, what in counts if count]
        return text + (f"; {', '.join(held)}" if held else "")


#: Line numbers a JSON Lines report names (the invalid lines, the blank ones); the rest are counted.
_JSONL_LISTED_LINES = 10


def _line_numbers(numbers: list[int], total: int) -> str:
    """``line 13`` / ``lines 13, 40, 41`` / ``lines 13, 40, ... (the first 10 of 25)``."""
    listed = ", ".join(str(number) for number in numbers)
    if total == 1:
        return f"line {listed}"
    more = f", ... (the first {len(numbers)} of {total:,})" if total > len(numbers) else ""
    return f"lines {listed}{more}"


def _markers(invalid: int, blank: int, *, quoted: bool) -> str:
    """The placeholder markers a text names: ``<invalid line N>`` and / or ``<blank line N>``."""
    names = [name for count, name in ((invalid, "<invalid line N>"), (blank, "<blank line N>")) if count]
    return " or ".join(f'"{name}"' for name in names) if quoted else " / ".join(names)


def _parse_jsonl(text: str) -> JsonlLines:
    """The lines of a JSON Lines text, line N at index N - 1, bounded by
    :data:`JSONL_MAX_RECORDS` lines and :data:`JSON_PARSE_MAX_CHARS` of
    parsed text together: a placeholder takes its slot like a record, and
    every line past either bound is counted as unread."""
    lines = JsonlLines()
    if not text:
        return lines
    # Every line counts (the newline ending the text is none), so the total
    # is known without walking the lines past a cap.
    lines.lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    parsed_chars = 0
    for number, raw in enumerate(_jsonl_lines(text), start=1):
        stripped = raw.strip()
        if len(lines) >= JSONL_MAX_RECORDS or parsed_chars + len(stripped) > JSON_PARSE_MAX_CHARS:
            lines.unread = lines.lines - number + 1
            break
        if not stripped:
            lines.blank += 1
            if len(lines.blank_lines) < _JSONL_LISTED_LINES:
                lines.blank_lines.append(number)
            lines.append(JsonlPlaceholder(number))
            continue
        parsed_chars += len(stripped)
        try:
            lines.append(json.loads(stripped))
            continue
        except json.JSONDecodeError as exc:
            # The column in the file's line, its indentation included.
            placeholder = JsonlPlaceholder(number, sys.intern(exc.msg), exc.colno + len(raw) - len(raw.lstrip()))
        except RecursionError:
            placeholder = JsonlPlaceholder(number, "nested too deep to parse")
        except ValueError as exc:
            placeholder = JsonlPlaceholder(number, str(exc)[:120])
        lines.invalid += 1
        if len(lines.invalid_lines) < _JSONL_LISTED_LINES:
            lines.invalid_lines.append(number)
        lines.append(placeholder)
    return lines


def render_json_attachment(text: str, *, filename: str | None = None, code_interpreter: bool = False) -> str:
    """A JSON attachment as the reader gets it: verbatim when small, a
    structured summary past :data:`JSON_VERBATIM_MAX_CHARS`."""
    stripped = text.strip()
    if len(stripped) <= JSON_VERBATIM_MAX_CHARS:
        return stripped
    parsed, error = _loads(stripped)
    if error is not None:
        verdict = "not summarised" if len(stripped) > JSON_PARSE_MAX_CHARS else "that could not be read as JSON"
        error = _parse_error_text(error, code_interpreter=code_interpreter)
        return f"[JSON document, {len(stripped):,} chars {verdict} - {error}; shown as its text]\n" + stripped
    return (
        f"[JSON document, {len(stripped):,} chars, {describe_shape(parsed)} - shown as a structured "
        'summary; read a part with query_file_json(file_id, path), the verbatim text with read_file_content(mode="source")]\n'
        + summarize_json(parsed, max_chars=JSON_DOCUMENT_SUMMARY_MAX_CHARS)
    )


def render_jsonl_attachment(text: str, *, filename: str | None = None) -> str:
    """A JSON Lines attachment: verbatim when small, the summary of its lines
    past the bound. Line N is ``$[N-1]`` either way, so only the END of the
    text is trimmed: a leading blank line is line 1."""
    body = text.rstrip()
    if len(body) <= JSON_VERBATIM_MAX_CHARS:
        return body
    lines = _parse_jsonl(body)
    notes = [f"{lines.lines:,} line{'s' if lines.lines != 1 else ''}"]
    if lines.invalid:
        notes.append(f"{lines.invalid:,} not valid JSON ({_line_numbers(lines.invalid_lines, lines.invalid)})")
    if lines.blank:
        notes.append(f"{lines.blank:,} blank ({_line_numbers(lines.blank_lines, lines.blank)})")
    if lines.unread:
        notes.append(f"the last {lines.unread:,} not read")
    kept = (
        f" (a line holding no value keeps its slot as {_markers(lines.invalid, lines.blank, quoted=False)})"
        if lines.invalid or lines.blank
        else ""
    )
    return (
        f"[JSON Lines document, {len(body):,} chars, {', '.join(notes)} - the lines shown as a structured "
        f"summary, $[i] being line i+1 of the file{kept}; read a part with query_file_json(file_id, path) "
        '($[0], $[*].field), the verbatim text with read_file_content(mode="source")]\n'
        + summarize_json(lines, max_chars=JSON_DOCUMENT_SUMMARY_MAX_CHARS)
    )


_CSV_GROUPED_RE = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_CSV_INT_RE = re.compile(r"[-+]?\d+")
_CSV_FLOAT_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?|[-+]?\d+[eE][-+]?\d+")


def _coerce_cell(cell: str) -> Any:
    raw = cell.strip()
    if not raw:
        return None
    if len(raw) > 64:
        return cell
    number = raw.replace(",", "") if _CSV_GROUPED_RE.fullmatch(raw) else raw
    try:
        if _CSV_INT_RE.fullmatch(number):
            return int(number)
        if _CSV_FLOAT_RE.fullmatch(number):
            value = float(number)
            return value if math.isfinite(value) else cell
    except (ValueError, OverflowError):
        pass
    return cell


def _csv_delimiter(text: str, filename: str | None, content_type: str | None) -> str:
    if _extension(filename) == "tsv" or _mime(content_type) == "text/tab-separated-values":
        return "\t"
    first = text[: text.find("\n") if "\n" in text[:65_536] else 65_536]
    counts = {d: first.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


#: Columns of a row the CSV statistics sample keeps: the digest shows 40
#: fields, and a wide export (thousands of columns) must not multiply the
#: sample's memory by its width.
_CSV_STATS_MAX_COLUMNS = 64


def _unique_columns(header: list[str], *, width: int = 0) -> list[str]:
    """The header's column names, each unique, the file's own names kept.

    A repeated name (two ``amount`` columns) would key both into one field
    of every row, the second silently overwriting the first. A repeat
    becomes ``amount_2`` - or the next suffix no column of the file uses, so
    a real ``amount_2`` column keeps its name and its values. A blank name,
    and a position past the header (*width* pads it, for rows wider than
    their header), is ``column_<position>`` on the same terms. The next
    suffix is remembered per name, so a header of one name repeated stays
    linear.
    """
    names = [cell.strip() for cell in header]
    names += [""] * (width - len(names))
    taken = {name for name in names if name}
    seen: set[str] = set()
    next_suffix: dict[str, int] = {}
    columns: list[str] = []
    for position, name in enumerate(names):
        if name and name not in seen:
            seen.add(name)
            columns.append(name)
            continue
        base = name or f"column_{position + 1}"
        if base not in taken:
            taken.add(base)
            columns.append(base)
            continue
        suffix = next_suffix.get(base, 2)
        while f"{base}_{suffix}" in taken:
            suffix += 1
        next_suffix[base] = suffix + 1
        taken.add(f"{base}_{suffix}")
        columns.append(f"{base}_{suffix}")
    return columns


@dataclass
class _CsvDigest:
    text: str
    #: Data rows read (blank rows skipped).
    rows: int
    #: Why the reading stopped before the end of the file; ``None`` = it read every row.
    stop: str | None


def _digest_csv(text: str, *, filename: str | None, content_type: str | None, max_chars: int) -> _CsvDigest | None:
    """The CSV digest and how much of the file it covers.

    Only the statistics sample (:data:`STATS_SAMPLE_ROWS` rows,
    :data:`_CSV_STATS_MAX_COLUMNS` columns each) is held; the other rows are
    counted, up to :data:`CSV_MAX_ROWS`. A read that stops early - that cap,
    or a row the parser refuses (a field over the ``csv`` module's 128 KiB
    limit, broken quoting) - says where, never a silently shorter count.
    """
    delimiter = _csv_delimiter(text, filename, content_type)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        return None
    # Keyed for every cell the sample keeps, a row wider than its header
    # included, so no cell lands on a real column's name.
    keys = _unique_columns(header, width=_CSV_STATS_MAX_COLUMNS)
    columns = keys[: len(header)]
    sample: list[dict] = []
    total = 0
    stop: str | None = None
    try:
        for record in reader:
            if not any(cell.strip() for cell in record):
                continue
            if total >= CSV_MAX_ROWS:
                stop = f"the digest stops at {CSV_MAX_ROWS:,} rows"
                break
            total += 1
            if len(sample) < STATS_SAMPLE_ROWS:
                sample.append(
                    {
                        keys[i]: _coerce_cell(cell)
                        for i, cell in enumerate(itertools.islice(record, _CSV_STATS_MAX_COLUMNS))
                    }
                )
    except csv.Error as exc:
        stop = f"parsing stopped after data row {total:,}: {str(exc)[:120]}"
    if not sample:
        return None
    out = _Out(max_chars)
    rows_text = f"{total:,} data rows" if stop is None else f"at least {total:,} data rows ({stop})"
    try:
        out.add(f"CSV: {rows_text}, {len(columns)} columns, delimiter {delimiter!r}")
        if len(columns) > _CSV_STATS_MAX_COLUMNS:
            out.add(f"\n(statistics for the first {_CSV_STATS_MAX_COLUMNS} of {len(columns)} columns)")
        for line in _field_lines(sample, indent="", total=total, field_count=len(columns)):
            out.add("\n" + line)
        out.add(f"\nfirst {min(SAMPLE_ROWS, len(sample))} rows:")
        for row in sample[:SAMPLE_ROWS]:
            out.add("\n  " + _short(row, SAMPLE_ROW_CHARS))
    except _BudgetFullError:
        return _CsvDigest(out.text() + "\n... (digest truncated)", total, stop)
    return _CsvDigest(out.text(), total, stop)


def digest_csv_text(
    text: str, *, filename: str | None = None, content_type: str | None = None, max_chars: int = CSV_DIGEST_MAX_CHARS
) -> str | None:
    """A digest of a CSV / TSV text (header + rows), or ``None`` when there is nothing tabular."""
    digest = _digest_csv(text, filename=filename, content_type=content_type, max_chars=max_chars)
    return digest.text if digest is not None else None


def render_csv_attachment(
    text: str, *, filename: str | None = None, content_type: str | None = None, code_interpreter: bool = False
) -> str:
    """A CSV attachment: verbatim when small; past the bound, the digest first
    and the text after it (so a cap never hides the shape). The header says
    what the digest covers - the whole file, or its first rows and why - and
    that its column statistics come from a sample past
    :data:`STATS_SAMPLE_ROWS` rows: a count the model repeats must be one the
    digest actually read. It sends a computation over every row to the code
    interpreter only when the reader has it (*code_interpreter*)."""
    if len(text) <= CSV_VERBATIM_MAX_CHARS:
        return text
    digest = _digest_csv(text, filename=filename, content_type=content_type, max_chars=CSV_DIGEST_MAX_CHARS)
    if digest is None:
        return text
    if digest.stop is None:
        scope = f"a digest of the whole file ({digest.rows:,} rows"
        scope += f"; column statistics over the first {STATS_SAMPLE_ROWS:,})" if digest.rows > STATS_SAMPLE_ROWS else ")"
    else:
        scope = f"a digest of its first {digest.rows:,} rows only ({digest.stop})"
    return (
        f"[CSV document, {len(text):,} chars - {scope}, then the file itself"
        + ("; compute over every row with code_interpreter]\n" if code_interpreter else "]\n")
        + f"{digest.text}\n\n## File text\n\n{text}"
    )


# -- Entry points -------------------------------------------------------------

_render_cache: OrderedDict[tuple, str] = OrderedDict()
_render_cache_chars = 0
_render_cache_lock = threading.Lock()
#: How long the plain extraction of a rendered kind is (:func:`_plain_key` ->
#: characters), for :func:`extract_inline_attachment_text`: a file too long to
#: inline is decided from its length on every later request, so its text - up
#: to an upload's 40 MB, and never inlined - is neither extracted again nor
#: kept in the render memo, where it would push out the rendering that is.
_plain_lengths = BoundedLRU(max_entries=1_024)


@dataclass(frozen=True)
class InlineText:
    """What an inline surface reads of an attachment: its text, or the first
    *limit* characters of it, the length of the whole text (the truncation
    footer names it) and the length of the whole text stripped (a text of a
    few blank characters is not inlined)."""

    text: str
    total: int
    stripped_total: int


#: The :class:`InlineText` of a text too long for the render memo, by
#: ``(key, limit)``: a large CSV rendering (its digest, then the whole file)
#: or a large text file would otherwise be rendered or extracted again on
#: every provider request, only for its first *limit* characters.
#: Weighed in characters of those heads - nothing past a cap is kept.
_inline_heads = BoundedLRU(
    max_entries=256, max_bytes=_RENDER_CACHE_MAX_CHARS, weigh=lambda held: len(held.text)
)


def clear_render_cache() -> None:
    """Forget every memoised rendering and plain extraction (tests; a renderer change ships a new version)."""
    global _render_cache_chars
    with _render_cache_lock:
        _render_cache.clear()
        _render_cache_chars = 0
    _plain_lengths.clear()
    _inline_heads.clear()


def _cache_get(key: tuple) -> str | None:
    with _render_cache_lock:
        text = _render_cache.get(key)
        if text is not None:
            _render_cache.move_to_end(key)
        return text


def _cache_put(key: tuple, text: str) -> None:
    global _render_cache_chars
    if len(text) > _RENDER_CACHE_MAX_CHARS // 4:
        return
    with _render_cache_lock:
        if key in _render_cache:
            return
        _render_cache[key] = text
        _render_cache_chars += len(text)
        while _render_cache and (
            len(_render_cache) > _RENDER_CACHE_MAX_ENTRIES or _render_cache_chars > _RENDER_CACHE_MAX_CHARS
        ):
            _old_key, old = _render_cache.popitem(last=False)
            _render_cache_chars -= len(old)


def _render_key(
    kind: str,
    data: bytes,
    filename: str | None,
    content_type: str | None,
    code_interpreter: bool,
    digest: bytes | None = None,
) -> tuple:
    """Everything a rendering depends on besides :data:`RENDER_VERSION`: the
    kind, the extension (``.tsv`` picks the delimiter), the whole MIME type
    (its ``charset`` picks the decoding), whether the reader has the code
    interpreter (the rendering names it only then) and the bytes - by their
    BLAKE2b digest, *digest* when the caller already took it."""
    return (
        RENDER_VERSION,
        kind,
        _extension(filename),
        " ".join((content_type or "").lower().split()),
        code_interpreter,
        len(data),
        digest if digest is not None else hashlib.blake2b(data, digest_size=16).digest(),
    )


def _plain_key(data: bytes, filename: str) -> tuple:
    """What a plain extraction depends on: the extension the extractor is
    picked by, read as ``chunking.extract_text_from_file`` reads it, and the
    bytes."""
    from app.services.chunking import extraction_extension

    return (
        "plain",
        extraction_extension(filename),
        len(data),
        hashlib.blake2b(data, digest_size=16).digest(),
    )


def _plain_extraction(data: bytes, filename: str, key: tuple | None = None) -> str:
    """``chunking.extract_text_from_file``, memoised per :func:`_plain_key`
    (*key* when the caller already took it)."""
    from app.services.chunking import extract_text_from_file

    key = key if key is not None else _plain_key(data, filename)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    text = extract_text_from_file(data, filename)
    _cache_put(key, text)
    return text


def _render(kind: str, data: bytes, filename: str | None, content_type: str | None, code_interpreter: bool) -> str:
    return _encodable(_render_text(kind, data, filename, content_type, code_interpreter))


def _render_text(
    kind: str, data: bytes, filename: str | None, content_type: str | None, code_interpreter: bool
) -> str:
    text = decode_attachment_text(data, content_type, html=kind == "html", utf8_first=kind in ("json", "jsonl"))
    if kind == "html":
        return render_html_attachment(text, filename=filename, code_interpreter=code_interpreter)
    if kind == "json":
        return render_json_attachment(text, filename=filename, code_interpreter=code_interpreter)
    if kind == "jsonl":
        return render_jsonl_attachment(text, filename=filename)
    return render_csv_attachment(
        text, filename=filename, content_type=content_type, code_interpreter=code_interpreter
    )


def extract_attachment_text(
    data: bytes, filename: str, content_type: str | None = None, code_interpreter: bool = False
) -> str:
    """The text to put in front of a reader for one attachment.

    HTML is rendered, JSON / JSON Lines summarised past their verbatim bound,
    CSV digested past its bound (:func:`attachment_kind`); everything else is
    the plain document extraction (``chunking.extract_text_from_file``).
    Never raises for a rendered kind: a renderer failure falls back to the
    plain extraction, under a first line that says so (the reader is told it
    is reading the raw file, not the rendering the tools describe). A
    rendering is memoised per content hash and everything else it depends on
    (:func:`_render_key`), so a conversation whose history carries a large
    page does not re-render it on every turn - and so is a plain extraction
    (:func:`_plain_key`): every provider request of a run resolves the
    history's attachments again, and a 200-page PDF took ~1.3 s to extract
    each time. *code_interpreter* is whether the reader has
    the code interpreter (``code_interpreter_offered`` of the turn's
    surface) - the rendering sends a whole-file computation there only then,
    and a caller that cannot know leaves it off.
    """
    kind = attachment_kind(filename, content_type)
    if kind is None:
        return _plain_extraction(data, filename)
    return _rendered_attachment_text(kind, data, filename, content_type, code_interpreter)


def _rendered_attachment_text(
    kind: str,
    data: bytes,
    filename: str,
    content_type: str | None,
    code_interpreter: bool,
    digest: bytes | None = None,
) -> str:
    """The memoised rendering of an attachment of a rendered *kind* (see
    :func:`extract_attachment_text`); *digest*: see :func:`_render_key`."""
    from app.services.chunking import extract_text_from_file

    key = _render_key(kind, data, filename, content_type, code_interpreter, digest)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        text = _render(kind, data, filename, content_type, code_interpreter)
    except Exception:
        logger.warning(
            "Rendering the %s attachment %s failed - falling back to plain text", kind, filename, exc_info=True
        )
        plain = extract_text_from_file(data, filename)
        if not plain:
            return plain
        return (
            f"[The {kind.upper()} rendering of this file failed: what follows is its plain text "
            "extraction, not the rendered document.]\n\n" + plain
        )
    _cache_put(key, text)
    return text


async def extract_attachment_text_async(
    data: bytes, filename: str, content_type: str | None = None, code_interpreter: bool = False
) -> str:
    """:func:`extract_attachment_text` on the parse executor."""
    from app.core.parse_offload import run_parse

    return await run_parse(extract_attachment_text, data, filename, content_type, code_interpreter)


def _inline_plain_text(data: bytes, filename: str, key: tuple, limit: int, plain_text: str | None = None) -> str | None:
    """The plain extraction of *data* when it is inlined - at most *limit*
    characters - else ``None``.

    An empty extraction is inlined too: ``chunking.extract_text_from_file``
    reads a zip container as nothing on purpose, its compressed bytes decoded
    being noise, so an inline surface shows such a file as its size stub, as
    the released version did - never a rendering of those bytes.

    Only a text that is inlined is kept in the render memo. The length of every
    one is remembered (:data:`_plain_lengths`), so a file too long to inline is
    extracted once, not on every provider request, and its text never takes
    the room of the rendering inlined in its place; an empty one is its
    length. *key* is :func:`_plain_key`; *plain_text*, the extraction when the
    caller already holds it, is decided from instead of extracting again.
    """
    from app.services.chunking import extract_text_from_file

    text = plain_text
    if text is None:
        cached = _cache_get(key)
        if cached is not None:
            return cached if len(cached) <= limit else None
        known = _plain_lengths.get(key)
        if isinstance(known, int) and known > limit:
            return None
        if known == 0:
            return ""
        text = extract_text_from_file(data, filename)
    _plain_lengths.put(key, len(text))
    if len(text) > limit:
        return None
    if text:
        _cache_put(key, text)
    return text


def extract_inline_attachment_text(
    data: bytes,
    filename: str,
    content_type: str | None,
    code_interpreter: bool,
    limit: int,
    plain_text: str | None = None,
) -> str:
    """The text an attachment is INLINED as, for a reader shown at most *limit* characters.

    The chat upload message and the LLM inline fallback. A file of a kind this
    module renders whose plain extraction fits *limit* is inlined as that
    extraction, whole - its source, which a task about the markup, a config
    value or a row needs verbatim. Only a file the limit would cut is rendered
    (:func:`extract_attachment_text`): there the raw source spends the budget
    on markup, and the rendering puts the data first. Every other kind is the
    plain extraction either way. The inline fallback resolves the history's
    attachments on every provider request, so nothing here is redone per
    request: an inlined text and a rendering are memoised, a text too long to
    inline is remembered by its length alone (:func:`_inline_plain_text`), and
    the bytes are hashed once. *plain_text*: see :func:`inline_attachment_text`.
    """
    kind, key, plain = _inline_source(data, filename, content_type, code_interpreter, limit, plain_text)
    if plain is not None:
        return plain
    return _inline_text_for(kind, key, data, filename, content_type, code_interpreter, plain_text)


def _inline_source(
    data: bytes,
    filename: str,
    content_type: str | None,
    code_interpreter: bool,
    limit: int,
    plain_text: str | None = None,
) -> tuple[str | None, tuple, str | None]:
    """The inline rule's decision: ``(kind, key, plain)``, *plain* being the
    plain extraction when it is inlined whole, else ``None`` and *key* the
    memo key of the text inlined instead (the rendering's for a rendered kind).
    The bytes are hashed once: a plain key ends with their digest, and the
    rendering's key reuses it."""
    kind = attachment_kind(filename, content_type)
    key = _plain_key(data, filename)
    if kind is None:
        return kind, key, None
    plain = _inline_plain_text(data, filename, key, limit, plain_text)
    if plain is not None:
        return kind, key, plain
    return kind, _render_key(kind, data, filename, content_type, code_interpreter, key[-1]), None


def _inline_text_for(
    kind: str | None,
    key: tuple,
    data: bytes,
    filename: str,
    content_type: str | None,
    code_interpreter: bool,
    plain_text: str | None = None,
) -> str:
    """The whole text inlined when the plain extraction is not (see :func:`_inline_source`)."""
    if kind is None:
        if plain_text is None:
            return _plain_extraction(data, filename, key)
        _cache_put(key, plain_text)
        return plain_text
    return _rendered_attachment_text(kind, data, filename, content_type, code_interpreter, digest=key[-1])


def _inline_view(text: str, limit: int) -> InlineText:
    return InlineText(text if len(text) <= limit else text[:limit], len(text), len(text.strip()))


def inline_attachment_text(
    data: bytes,
    filename: str,
    content_type: str | None,
    code_interpreter: bool,
    limit: int,
    plain_text: str | None = None,
) -> InlineText:
    """What the chat upload message and the LLM inline fallback read of an
    attachment shown at most *limit* characters: :func:`extract_inline_attachment_text`,
    cut at *limit*, with the whole text's lengths.

    A text too long for the render memo (a large CSV rendering, which carries
    the whole file after its digest, or a large text file) is remembered by
    that cut and its lengths (:data:`_inline_heads`), so a later provider
    request neither renders nor extracts it again. The bytes are
    hashed once per request. *plain_text* is ``chunking.extract_text_from_file``
    of the same bytes and filename when the caller already holds it (the
    upload message extracts every file for its knowledge-base copy): the rule
    decides from it, and the memo learns it, instead of extracting the file a
    second time.
    """
    kind, key, plain = _inline_source(data, filename, content_type, code_interpreter, limit, plain_text)
    if plain is not None:
        return _inline_view(plain, limit)
    held = _inline_heads.get((key, limit))
    if isinstance(held, InlineText):
        return held
    text = _inline_text_for(kind, key, data, filename, content_type, code_interpreter, plain_text)
    view = _inline_view(text, limit)
    if len(text) > _RENDER_CACHE_MAX_CHARS // 4:  # the render memo keeps no such entry
        _inline_heads.put((key, limit), view)
    return view


async def inline_attachment_text_async(
    data: bytes,
    filename: str,
    content_type: str | None,
    code_interpreter: bool,
    limit: int,
    plain_text: str | None = None,
) -> InlineText:
    """:func:`inline_attachment_text` on the parse executor."""
    from app.core.parse_offload import run_parse

    return await run_parse(inline_attachment_text, data, filename, content_type, code_interpreter, limit, plain_text)


def inline_truncation_footer(
    *, file_id: str, filename: str | None, content_type: str | None, shown: int, total: int, code_interpreter: bool
) -> str:
    """What the model reads where an inlined attachment was cut.

    The old footer (``[... truncated, 558,718 total characters]``) named the
    size and nothing else, and the model's way to the rest was paging the
    source in 50k windows. This one names the cut, says not to page to
    answer a question about the document, and gives the ways forward: a
    later part of the SAME text (``mode="extract_structured"`` for a kind
    this module renders, so the offset means the same characters - the
    inline text is a prefix of that rendering) and, when the reader has the
    code interpreter (*code_interpreter*, required: a caller must decide),
    a computation over the whole file there, at the exact path it is
    mounted at.

    That path is the one text here derived from the stored name, which is
    often a third party's text: it must be exact to be usable, so it
    is not fenced, and :func:`~app.services.code_interpreter.safe_mount_name`
    bounds it instead - one line (no control, line or paragraph separator),
    none of ``\\ / : * ? " < > |`` (so no quote ends the hint and no boundary
    marker forms), at most 255 bytes, and the file id's tag before the
    extension.
    """
    mode = ', mode="extract_structured"' if attachment_kind(filename, content_type) else ""
    footer = (
        f"[... truncated: the first {shown:,} of {total:,} characters are shown. Answer from what is "
        "shown and say what you could not read; do not page through the rest to answer a question "
        f'about this document. A specific later part: read_file_content(file_id="{file_id}"{mode}, '
        f"offset={shown})."
    )
    if code_interpreter:
        from app.services.code_interpreter import mount_path_hint

        footer += (
            " A computation over the whole file (every row, counts, sums): "
            f"code_interpreter, where it is mounted at {mount_path_hint(filename, file_id)}."
        )
    return footer + "]"


# -- JSONPath subset ----------------------------------------------------------

_JSONPATH_TOKEN_RE = re.compile(
    r"""
    (?P<recursive>\.\.(?:(?P<rname>[A-Za-z_][\w\-]*)|(?P<rstar>\*)))
    | (?P<dot>\.(?:(?P<dname>[A-Za-z_][\w\-]*)|(?P<dstar>\*)))
    | (?P<bracket>\[\s*(?:
          (?P<bstar>\*)
        | '(?P<bsq>(?:[^'\\]|\\.)*)'
        | "(?P<bdq>(?:[^"\\]|\\.)*)"
        | (?P<slice>-?[0-9]*\s*:\s*-?[0-9]*)
        | (?P<index>-?[0-9]+)
      )\s*\])
    """,
    re.VERBOSE,
)
_JSONPATH_MAX_CHARS = 500


class JsonPathError(ValueError):
    """A ``query_json`` path the selector does not understand."""


def _parse_path(path: str) -> list[tuple[str, Any]]:
    """Tokenise a JSONPath-style *path* into ``(op, arg)`` steps.

    Supported: ``$``, ``.key``, ``['key']``, ``["key"]``, ``[n]`` (negative
    allowed), ``[a:b]``, ``[*]`` / ``.*``, ``..key`` / ``..*``; an index is
    ASCII digits (``[0-9]``, as RFC 9535 spells it) in every form, a quoted
    one included. Filters, unions and slice steps are refused with a message
    naming the supported forms.
    """
    text = (path or "").strip()
    if len(text) > _JSONPATH_MAX_CHARS:
        raise JsonPathError(f"path longer than {_JSONPATH_MAX_CHARS} characters")
    if text in {"", "$", "$."}:
        return []
    if text.startswith("$"):
        text = text[1:]
    elif not text.startswith((".", "[")):
        text = "." + text  # a bare ``key.sub[0]`` means ``$.key.sub[0]``
    steps: list[tuple[str, Any]] = []
    position = 0
    while position < len(text):
        match = _JSONPATH_TOKEN_RE.match(text, position)
        if match is None:
            raise JsonPathError(
                f"unsupported path syntax at position {position}: {text[position : position + 20]!r}. "
                "Supported: $.key, ['key'], [0], [-1], [1:3], [*], .*, ..key, ..* (no filters or unions)."
            )
        if match.group("recursive"):
            steps.append(("recursive", None if match.group("rstar") else match.group("rname")))
        elif match.group("dot"):
            steps.append(("wild", None) if match.group("dstar") else ("key", match.group("dname")))
        elif match.group("bstar"):
            steps.append(("wild", None))
        elif match.group("bsq") is not None:
            steps.append(("key", match.group("bsq").replace("\\'", "'")))
        elif match.group("bdq") is not None:
            steps.append(("key", match.group("bdq").replace('\\"', '"')))
        elif match.group("slice") is not None:
            start_s, stop_s = (part.strip() for part in match.group("slice").split(":", 1))
            steps.append(("slice", (int(start_s) if start_s else None, int(stop_s) if stop_s else None)))
        else:
            steps.append(("index", int(match.group("index"))))
        position = match.end()
    return steps


def _descendants(value: Any) -> Iterator[Any]:
    """Every value under *value*, depth first (the value itself excluded), lazily."""
    stack = [iter(value.values()) if isinstance(value, dict) else iter(value) if isinstance(value, list) else iter(())]
    while stack:
        child = next(stack[-1], _END)
        if child is _END:
            stack.pop()
            continue
        yield child
        if isinstance(child, dict):
            stack.append(iter(child.values()))
        elif isinstance(child, list):
            stack.append(iter(child))


_END = object()


#: How many values one query may visit, across all its steps: the ceiling of
#: its cost whatever the path's shape. A step keeps no working set of its
#: own - capping one would turn ``$[*].b`` into a false "no match" whenever
#: the only ``b`` sits past the cap - so this is what bounds a query, and
#: ``$..a..b`` (the subtree under every ``a``, quadratic on a nested
#: document) stops here and says so.
QUERY_VISIT_BUDGET = 2_000_000


class _Visits:
    """The visit budget a query (or a whole ``query_file_json`` call) spends;
    ``exhausted`` once a visit was refused."""

    __slots__ = ("budget", "exhausted", "left")

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.left = budget
        self.exhausted = False

    def take(self) -> bool:
        if self.left <= 0:
            self.exhausted = True
            return False
        self.left -= 1
        return True


def _step_values(step: tuple[str, Any], values: Iterator[Any], visits: _Visits) -> Iterator[Any]:
    """One path step over *values*, lazily: a value is produced only when the
    next step (or the result) asks for it, so a query stops walking the
    document the moment it holds its answer. Every value a fan-out step
    (slice, wildcard, descent) visits is paid from *visits*; a key or an
    index step is a filter and never multiplies what flows through it."""
    op, arg = step
    for value in values:
        if op == "key":
            if isinstance(value, dict) and arg in value:
                yield value[arg]
            elif isinstance(value, list) and arg.isascii() and arg.isdigit() and int(arg) < len(value):
                # ASCII only: ``str.isdigit`` accepts ``\u00b2``, which ``int`` refuses.
                yield value[int(arg)]
        elif op == "index":
            if isinstance(value, list) and -len(value) <= arg < len(value):
                yield value[arg]
        elif op == "slice":
            if isinstance(value, list):
                # Indices, never a copy: ``[0:1000000]`` over a long array
                # costs the values the query reads, not the array.
                for position in range(len(value))[arg[0] : arg[1]]:
                    if not visits.take():
                        return
                    yield value[position]
        elif op == "wild":
            children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
            for child in children:
                if not visits.take():
                    return
                yield child
        elif op == "recursive":
            nodes = _descendants(value) if arg is None else itertools.chain((value,), _descendants(value))
            for node in nodes:
                if not visits.take():
                    return
                if arg is None:
                    yield node
                elif isinstance(node, dict) and arg in node:
                    yield node[arg]


@dataclass(frozen=True)
class QueryOutcome:
    """What a query found, and whether that is everything."""

    matches: list[Any]
    #: At least one more value matches past the returned ones.
    more: bool = False
    #: The visit budget ran out: a value past it was never searched.
    exhausted: bool = False
    #: The visit budget the query ran under (a whole call's, when shared).
    visit_budget: int = 0


def run_query(
    data: Any,
    path: str,
    *,
    max_results: int = QUERY_MAX_RESULTS,
    visit_budget: int | None = None,
    visits: _Visits | None = None,
) -> QueryOutcome:
    """Evaluate a JSONPath-style *path* against *data*.

    Lazy and bounded: the steps are chained generators, the result stops
    pulling one value past *max_results* (to know whether there are more),
    and every value any step visits is paid from the budget - *visits* when
    the caller shares one across several queries, else a fresh
    *visit_budget* (default :data:`QUERY_VISIT_BUDGET`). The cost is the
    answer's, or the budget's, never a quadratic walk. Raises
    :class:`JsonPathError` on a path the subset does not cover.
    """
    limit = max(1, max_results)
    if visits is None:
        visits = _Visits(QUERY_VISIT_BUDGET if visit_budget is None else visit_budget)
    # A shared budget carries its remaining visits over, never a refusal: a
    # key-only path after it ran out still answers, completely.
    visits.exhausted = False
    values: Iterator[Any] = iter((data,))
    for step in _parse_path(path):
        values = _step_values(step, values, visits)
    matches = list(itertools.islice(values, limit + 1))
    return QueryOutcome(
        matches=matches[:limit], more=len(matches) > limit, exhausted=visits.exhausted, visit_budget=visits.budget
    )


def query_json(data: Any, path: str, *, max_results: int = QUERY_MAX_RESULTS) -> list[Any]:
    """The values *path* matches in *data* (at most *max_results*); see :func:`run_query`."""
    return run_query(data, path, max_results=max_results).matches


def format_query_result(
    matches: list[Any],
    *,
    path: str,
    max_chars: int = QUERY_RESULT_MAX_CHARS,
    more: bool = False,
    exhausted: bool = False,
    visit_budget: int | None = None,
    code_interpreter: bool = False,
) -> str:
    """The values a query matched, bounded: verbatim JSON when it fits, a summary when not.

    *more* and *exhausted* make the count honest: "more than N" when the
    result stopped at its cap, and a partial search named as one - with the
    budget it actually ran under, *visit_budget* - so neither a short list
    nor "No value matches" reads as covering the whole block. A partial
    search points at the code interpreter only when the reader has it
    (*code_interpreter*). A JSON Lines
    :class:`JsonlPlaceholder` is never counted a value: asked for alone it
    answers which line holds nothing and why, in a listing it keeps its slot.
    """
    budget = QUERY_VISIT_BUDGET if visit_budget is None else visit_budget
    searched = (
        f"[Partial search: the visit budget ({budget:,} values) ran out - a match past that point is not "
        "in this result; narrow the path"
        + (", or compute over the file with code_interpreter.]\n" if code_interpreter else ".]\n")
        if exhausted
        else ""
    )
    if not matches:
        return f"{searched}No value matches {path!r}{' in the part searched' if exhausted else ''}."
    placeholders = [item for item in matches if isinstance(item, JsonlPlaceholder)]
    if placeholders and len(matches) == 1 and not more:
        # The one slot asked for holds no value: which line, and why - never a
        # marker that reads as the line's content.
        return f"{searched}{placeholders[0].sentence(path)}"
    payload: Any = matches[0] if len(matches) == 1 else matches
    count = len(matches)
    if placeholders:
        # A JSON Lines line holding no value keeps its slot in a listing of
        # the slots, counted apart from the values.
        blank = sum(1 for item in placeholders if item.reason is None)
        values = count - len(placeholders)
        one = len(placeholders) == 1
        held = (
            f"{values} value{'s' if values != 1 else ''} and {len(placeholders)} line{'' if one else 's'} "
            f"holding none, shown in {'its slot' if one else 'their slots'} as "
            + _markers(len(placeholders) - blank, blank, quoted=True)
        )
        items = f"{count} item{'s' if count != 1 else ''}"
        header = (
            f"More than {items} match {path!r} (the first {count}: {held}):"
            if more
            else f"{items} match {path!r} ({held}):"
        )
    elif more:
        header = f"More than {count} values match {path!r} (showing the first {count}):"
    else:
        header = f"{count} value{'s' if count != 1 else ''} match{'es' if count == 1 else ''} {path!r}:"
    rendered, complete = dump_json_bounded(payload, max_chars)
    if complete:
        return f"{searched}{header}\n{rendered}"
    summary = summarize_json(payload, max_chars=max_chars, query_hint=False)
    return (
        f"{searched}{header}\n[the full value is longer than {max_chars:,} chars - shown as a structured summary; "
        f"narrow the path to read a part verbatim]\n{summary}"
    )


# -- The JSON a file carries, and the whole query_file_json computation -------


def load_attachment_json(data: bytes, filename: str | None, content_type: str | None = None) -> list[EmbeddedJson]:
    """The data blocks an attachment carries, numbered as its rendering numbers them.

    An HTML attachment yields its JSON ``<script>`` elements and JavaScript
    literals (the same discovery, the same order as the ``## Embedded data``
    section); a JSON attachment yields itself; a JSON Lines attachment its
    lines as one array (:class:`JsonlLines`, line N at index N - 1); any
    other text that parses as JSON yields itself. An empty list means there
    is nothing to query.
    """
    return _attachment_blocks(data, filename, content_type)[0]


def _attachment_blocks(
    data: bytes, filename: str | None, content_type: str | None
) -> tuple[list[EmbeddedJson], list[str]]:
    """:func:`load_attachment_json` plus what of the FILE's data was not read
    (blocks past the cap, script past the scan budget) - one clause each."""
    html = is_html_attachment(filename, content_type)
    utf8_first = not html and not is_csv_attachment(filename, content_type)
    text = decode_attachment_text(data, content_type, html=html, utf8_first=utf8_first)
    if html:
        doc = extract_html_document(text)
        return doc.json_blocks, doc.partial_notes()
    if is_jsonl_attachment(filename, content_type):
        # Only the end is trimmed: a leading blank line is line 1 of the file.
        body = text.rstrip()
        if not body:
            return [], []
        lines = _parse_jsonl(body)
        missing: list[str] = []
        if lines.unread:
            missing.append(
                f"only the first {len(lines):,} of its {lines.lines:,} lines were parsed - the last "
                f"{lines.unread:,} are not in these records"
            )
        if lines.invalid == 1:
            number = lines.invalid_lines[0]
            missing.append(f"line {number} is not valid JSON (its slot holds <invalid line {number}>)")
        elif lines.invalid:
            missing.append(
                f"{lines.invalid:,} lines are not valid JSON ({_line_numbers(lines.invalid_lines, lines.invalid)} "
                "- each slot holds <invalid line N>)"
            )
        block = EmbeddedJson(
            index=1,
            kind="application/x-ndjson",
            element_id=None,
            data=lines,
            raw_chars=len(body),
            partial="; ".join(missing) or None,
        )
        return [block], []
    stripped = text.strip()
    if not stripped:
        return [], []
    kind = _mime(content_type) or ("application/json" if is_json_attachment(filename) else "text")
    value, error = _loads(stripped)
    if error is not None:
        if is_json_attachment(filename, content_type):
            return [EmbeddedJson(index=1, kind=kind, element_id=None, data=None, raw_chars=len(stripped), error=error)], []
        return [], []
    return [EmbeddedJson(index=1, kind=kind, element_id=None, data=value, raw_chars=len(stripped))], []


def run_json_query(
    data: bytes,
    filename: str,
    content_type: str | None,
    path: str | None,
    block: Any,
    code_interpreter: bool = False,
) -> tuple[str, bool]:
    """The whole ``query_file_json`` computation, for the parse executor: ``(text, is_error)``.

    Loads the blocks, validates *block* (a number, never a boolean), runs
    the query and formats the result - every block's output against ONE
    budget, so a file with many blocks costs the bound, not a formatting of
    all of them. What the query could not see points at the code
    interpreter only when the reader has it (*code_interpreter*).
    """
    if isinstance(block, bool):
        return "Error: block must be a block number (1, 2, ...), not true / false.", True
    selected: int | None = None
    if block is not None:
        try:
            selected = int(block)
        except (TypeError, ValueError):
            return f"Error: block must be a block number (1, 2, ...), got {block!r}.", True
    blocks, file_notes = _attachment_blocks(data, filename, content_type)
    if not blocks:
        return (
            f"Error: '{filename}' carries no JSON to query - no JSON <script> element or "
            "JavaScript data literal in an HTML file, and the file itself is not a JSON "
            "document. Read it with read_file_content instead.",
            True,
        )
    if selected is not None:
        if selected < 1 or selected > len(blocks):
            listing = "\n".join(f"- {b.label}" for b in blocks[:20])
            more = f"\n- ... {len(blocks) - 20} more" if len(blocks) > 20 else ""
            return (
                f"Error: block {selected} does not exist; the file carries "
                f"{len(blocks)} block{'s' if len(blocks) != 1 else ''}:\n{listing}{more}",
                True,
            )
        targets = [blocks[selected - 1]]
    else:
        targets = blocks
    query = (path or "").strip()
    steps_error = None
    if query:
        try:
            _parse_path(query)
        except JsonPathError as exc:
            steps_error = str(exc)
    if steps_error is not None:
        return f"Error: {steps_error}", True
    sections: list[str] = []
    budget = QUERY_RESULT_MAX_CHARS
    whole_file = "; code_interpreter reads the whole file.]" if code_interpreter else ".]"
    if file_notes:
        # What the query could not see comes FIRST, where no cut reaches it: a
        # value in a block past the cap is in the file, not in these results.
        sections.append(f"[Partial: {'; '.join(file_notes)} - a value there is not in these results{whole_file}")
        budget -= len(sections[-1]) + 2
    if len(blocks) > 1 and selected is None:
        sections.append(f"{len(blocks)} data blocks - pass block=N to query one of them.")
        budget -= len(sections[-1])
    # ONE visit budget for the whole call: a file of 64 blocks must not buy
    # 64 budgets.
    visits = _Visits(QUERY_VISIT_BUDGET)
    for position, target in enumerate(targets):
        if budget <= 200:
            left = len(targets) - position
            sections.append(f"... [{left} more block{'s' if left != 1 else ''} not shown - pass block=N]")
            break
        label = target.label if len(blocks) > 1 else target.label.split(": ", 1)[-1]
        # A block read in part says so right after its label, so neither a
        # match list nor "No value matches" reads as covering the whole file.
        partial = (
            f"[Partial block: {target.partial} - a value there is not in this result{whole_file}\n"
            if target.partial
            else ""
        )
        if target.error:
            section = f"[{label}] skipped: {_parse_error_text(target.error, code_interpreter=code_interpreter)}"
        elif not query:
            section = (
                f"[{label}] {partial}structure (pass path to read a value, e.g. $.items[*].name, $..title, "
                "$.items[0:5]):\n"
                + summarize_json(target.data, max_chars=min(JSON_BLOCK_SUMMARY_MAX_CHARS, max(200, budget - len(partial))))
            )
        else:
            spent_before = visits.left <= 0
            outcome = run_query(target.data, query, visits=visits)
            if outcome.exhausted and spent_before:
                section = (
                    f"[{label}] {partial}not searched: this call's visit budget ({visits.budget:,} values) "
                    "was spent on the blocks above - pass block=N to query this one."
                )
            else:
                room = max(200, budget - len(label) - len(partial) - 4)
                formatted = format_query_result(
                    outcome.matches,
                    path=query,
                    max_chars=room,
                    more=outcome.more,
                    exhausted=outcome.exhausted,
                    visit_budget=outcome.visit_budget,
                    code_interpreter=code_interpreter,
                )
                section = f"[{label}] {partial}{formatted}"
        if len(section) > budget:
            cut_note = "\n... [cut here: this call's output budget is spent - narrow the path or pass block=N]"
            section = section[: max(0, budget - len(cut_note))] + cut_note
        sections.append(section)
        budget -= len(sections[-1]) + 2
    return _encodable("\n\n".join(sections)), False
