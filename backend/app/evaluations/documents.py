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

"""Render the documents of a built-in benchmark dataset.

A benchmark document is declared ONCE, as structured blocks in the dataset's
seed file, and everything else is derived from that declaration:

- the file a model reads - a text PDF (``format: "pdf"``) or a scanned page
  (``format: "scan"``, a PNG with the rotation, blur and speckles of a
  photocopy);
- the gold transcription the judge compares against (:func:`document_markdown`).

So the file and its expected answer can never disagree, and no binary is
committed to the repository: the files are rendered at seed time with the
standard library (the PDF) and Pillow (the scan), both already dependencies.

The PDF uses the standard Helvetica fonts with WinAnsi encoding, so it carries
a real text layer (what ``pypdf`` extracts and what a provider that reads PDFs
natively reads) and needs no embedded font. Text must therefore be encodable
in cp1252, which :func:`validate_document` checks up front.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import unicodedata
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

PDF_CONTENT_TYPE = "application/pdf"
PNG_CONTENT_TYPE = "image/png"
DOCUMENT_FORMATS = ("pdf", "scan")
BLOCK_TYPES = ("title", "heading", "paragraph", "list", "table", "page_break")


class DocumentError(ValueError):
    """A benchmark document declaration that cannot be rendered."""


#: Bump on a deliberate change of how documents are drawn (layout, noise, fonts):
#: it is part of :func:`document_fingerprint`, so the seed treats every document
#: as a new test case and old scores stay with the files they were measured on.
RENDERER_VERSION = 1
_FINGERPRINT_CHARS = 16


@dataclass(frozen=True)
class RenderedDocument:
    file_name: str
    content_type: str
    data: bytes
    fingerprint: str = ""


def document_fingerprint(document: dict) -> str:
    """Identity of a document as declared: its blocks, format and name, plus the renderer version.

    Deliberately NOT the rendered bytes, which a Pillow upgrade may shift by a
    few pixels without changing the test case.
    """
    canonical = json.dumps({"renderer": RENDERER_VERSION, "document": document}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:_FINGERPRINT_CHARS]


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def _block_texts(block: dict) -> list[str]:
    kind = block.get("type")
    if kind in ("title", "heading", "paragraph"):
        return [block.get("text") or ""]
    if kind == "list":
        return list(block.get("items") or [])
    if kind == "table":
        return [cell for row in block.get("rows") or [] for cell in row]
    return []


def validate_document(document: dict) -> None:
    """Raise :class:`DocumentError` when *document* cannot be rendered faithfully."""
    file_name = document.get("file_name") or ""
    fmt = document.get("format")
    if fmt not in DOCUMENT_FORMATS:
        raise DocumentError(f"{file_name or '?'}: format must be one of {DOCUMENT_FORMATS}, got {fmt!r}")
    expected_suffix = ".pdf" if fmt == "pdf" else ".png"
    if not file_name.endswith(expected_suffix) or "/" in file_name or "\\" in file_name:
        raise DocumentError(f"{file_name or '?'}: a {fmt} document needs a bare file name ending in {expected_suffix}")
    blocks = document.get("blocks") or []
    if not any(_block_texts(b) for b in blocks):
        raise DocumentError(f"{file_name}: a document needs at least one block with text")
    for index, block in enumerate(blocks):
        kind = block.get("type")
        if kind not in BLOCK_TYPES:
            raise DocumentError(f"{file_name}: blocks.{index} has unknown type {kind!r}")
        if kind == "table":
            rows = block.get("rows") or []
            if len(rows) < 2 or len({len(r) for r in rows}) != 1:
                raise DocumentError(f"{file_name}: blocks.{index} needs a header row and rows of equal length")
        for text in _block_texts(block):
            if not text.strip() or "\n" in text or "|" in text:
                raise DocumentError(f"{file_name}: blocks.{index} holds an empty, multi-line or '|' text")
            try:
                text.encode("cp1252")
            except UnicodeEncodeError as exc:
                raise DocumentError(f"{file_name}: blocks.{index} is not encodable in cp1252: {text!r}") from exc


def document_markdown(document: dict) -> str:
    """The gold transcription of *document*: its text, in reading order, as Markdown."""
    parts: list[str] = []
    for block in document.get("blocks") or []:
        kind = block["type"]
        if kind == "title":
            parts.append(f"# {block['text']}")
        elif kind == "heading":
            parts.append(f"## {block['text']}")
        elif kind == "paragraph":
            parts.append(block["text"])
        elif kind == "list":
            parts.append("\n".join(f"- {item}" for item in block["items"]))
        elif kind == "table":
            header, *rows = block["rows"]
            lines = [
                "| " + " | ".join(header) + " |",
                "|" + "|".join(" --- " for _ in header) + "|",
                *("| " + " | ".join(row) + " |" for row in rows),
            ]
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


def render_document(document: dict) -> RenderedDocument:
    """Render *document* to the bytes a model reads."""
    validate_document(document)
    fingerprint = document_fingerprint(document)
    if document["format"] == "pdf":
        return RenderedDocument(document["file_name"], PDF_CONTENT_TYPE, _render_pdf(document), fingerprint)
    return RenderedDocument(document["file_name"], PNG_CONTENT_TYPE, _render_scan(document), fingerprint)


# ---------------------------------------------------------------------------
# Text PDF (standard library only)
# ---------------------------------------------------------------------------

# Helvetica advance widths (1/1000 em) for printable ASCII 32..126, from the
# standard Adobe font metrics. Accented letters are measured as their base
# letter, close enough for line wrapping.
_HELVETICA_WIDTHS = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]  # fmt: skip
_BOLD_FACTOR = 1.06

_PAGE_WIDTH = 595
_PAGE_HEIGHT = 842
_MARGIN = 56
_CONTENT_WIDTH = _PAGE_WIDTH - 2 * _MARGIN
# (font size, bold, space before, space after) per block type.
_PDF_STYLES = {
    "title": (17, True, 0, 10),
    "heading": (12, True, 8, 4),
    "paragraph": (10, False, 0, 8),
    "list": (10, False, 0, 8),
    "table": (9, False, 2, 10),
}
_TABLE_PADDING = 8


def _char_width(char: str) -> int:
    code = ord(char)
    if 32 <= code <= 126:
        return _HELVETICA_WIDTHS[code - 32]
    base = unicodedata.normalize("NFD", char)[:1]
    if base and 32 <= ord(base) <= 126:
        return _HELVETICA_WIDTHS[ord(base) - 32]
    return 556


def pdf_text_width(text: str, size: float, bold: bool = False) -> float:
    """Width of *text* in points, set in Helvetica at *size*."""
    width = sum(_char_width(c) for c in text) * size / 1000
    return width * _BOLD_FACTOR if bold else width


def _wrap(text: str, measure: Callable[[str], float], limit: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if current and measure(candidate) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _pdf_string(text: str) -> str:
    raw = text.encode("cp1252")
    escaped = "".join(
        f"\\{chr(b)}" if b in (0x28, 0x29, 0x5C) else (chr(b) if 32 <= b < 127 else f"\\{b:03o}") for b in raw
    )
    return f"({escaped})"


def _pdf_text_string(text: str) -> str:
    """*text* as a PDF text string (the Info dictionary): UTF-16BE behind its byte order mark.

    A reader decodes a literal text string as PDFDocEncoding, not as the page
    font's WinAnsi, and the two differ in 0x80-0x9F (the euro sign, curly
    quotes, dashes), so :func:`_pdf_string` would mis-spell those there.
    """
    return f"<FEFF{text.encode('utf-16-be').hex().upper()}>"


def table_column_widths(rows: list[list[str]], size: float) -> list[float]:
    """The column widths the PDF gives a table: its widest cell plus padding."""
    return [
        max(pdf_text_width(row[col], size, bold=(i == 0)) for i, row in enumerate(rows)) + _TABLE_PADDING
        for col in range(len(rows[0]))
    ]


class _PdfPages:
    def __init__(self) -> None:
        self.pages: list[list[str]] = [[]]
        self.y = _PAGE_HEIGHT - _MARGIN

    def need(self, height: float) -> None:
        if self.y - height < _MARGIN and self.pages[-1]:
            self.new_page()

    def new_page(self) -> None:
        self.pages.append([])
        self.y = _PAGE_HEIGHT - _MARGIN

    def text(self, x: float, size: float, bold: bool, text: str) -> None:
        font = "F2" if bold else "F1"
        self.pages[-1].append(f"BT /{font} {size} Tf {x:.2f} {self.y:.2f} Td {_pdf_string(text)} Tj ET")

    def rule(self, x1: float, x2: float, y: float) -> None:
        self.pages[-1].append(f"0.5 w {x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S")


def _render_pdf(document: dict) -> bytes:
    out = _PdfPages()
    for block in document["blocks"]:
        kind = block["type"]
        if kind == "page_break":
            out.new_page()
            continue
        size, bold, before, after = _PDF_STYLES[kind]
        leading = size * 1.35
        out.y -= before
        measure = partial(pdf_text_width, size=size, bold=bold)
        if kind in ("title", "heading", "paragraph"):
            for line in _wrap(block["text"], measure, _CONTENT_WIDTH):
                out.need(leading)
                out.y -= size
                out.text(_MARGIN, size, bold, line)
                out.y -= leading - size
        elif kind == "list":
            indent = pdf_text_width("- ", size)
            for item in block["items"]:
                lines = _wrap(item, measure, _CONTENT_WIDTH - indent)
                for index, line in enumerate(lines):
                    out.need(leading)
                    out.y -= size
                    if index == 0:
                        out.text(_MARGIN, size, False, "-")
                    out.text(_MARGIN + indent, size, False, line)
                    out.y -= leading - size
        elif kind == "table":
            rows = block["rows"]
            widths = table_column_widths(rows, size)
            if sum(widths) > _CONTENT_WIDTH:
                raise DocumentError(f"{document['file_name']}: a table is wider than the page")
            for row_index, row in enumerate(rows):
                out.need(leading)
                out.y -= size
                x = _MARGIN
                for col, cell in enumerate(row):
                    out.text(x, size, row_index == 0, cell)
                    x += widths[col]
                out.y -= leading - size
                if row_index == 0:
                    out.rule(_MARGIN, _MARGIN + sum(widths), out.y - 1)
                    out.y -= 3
        out.y -= after
    return _pdf_bytes(out.pages, title=_first_text(document))


def _first_text(document: dict) -> str:
    for block in document["blocks"]:
        texts = _block_texts(block)
        if texts:
            return texts[0]
    return document["file_name"]


def _pdf_bytes(pages: list[list[str]], *, title: str) -> bytes:
    """Serialise *pages* (content-stream operators) into a deterministic PDF."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_id = add(b"")  # filled once the page tree id is known
    pages_id = add(b"")
    font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    resources = f"<< /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >>"
    page_ids: list[int] = []
    for operators in pages:
        stream = zlib.compress("\n".join(operators).encode("latin-1"), 9)
        content_id = add(
            f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream + b"\nendstream"
        )
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
                    f"/Resources {resources} /Contents {content_id} 0 R >>"
                ).encode()
            )
        )
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    info_id = add(f"<< /Title {_pdf_text_string(title)} /Producer (XTM One benchmark seed) >>".encode("ascii"))

    buffer = io.BytesIO()
    buffer.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(buffer.tell())
        buffer.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_at = buffer.tell()
    buffer.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        buffer.write(f"{offset:010d} 00000 n \n".encode())
    buffer.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R /Info {info_id} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n".encode()
    )
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Scanned page (Pillow)
# ---------------------------------------------------------------------------

_SCAN_WIDTH = 1240
_SCAN_MARGIN = 96
_SCAN_PAPER = 243
_SCAN_INK = 38
# (font size, fake-bold stroke, space before, space after) per block type.
_SCAN_STYLES = {
    "title": (40, 1, 0, 22),
    "heading": (29, 1, 14, 8),
    "paragraph": (24, 0, 0, 16),
    "list": (24, 0, 0, 16),
    "table": (22, 0, 4, 18),
}
_SCAN_ROTATION_DEGREES = 0.7


def _render_scan(document: dict) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    fonts: dict[int, ImageFont.FreeTypeFont] = {}

    def font(size: int):
        if size not in fonts:
            fonts[size] = ImageFont.load_default(size=size)
        return fonts[size]

    content_width = _SCAN_WIDTH - 2 * _SCAN_MARGIN
    # (y offset, x, size, stroke, text) and (y, x1, x2) rules, laid out first
    # so the page is exactly as tall as its content.
    draws: list[tuple[float, float, int, int, str]] = []
    rules: list[tuple[float, float, float]] = []
    y = float(_SCAN_MARGIN)
    for block in document["blocks"]:
        kind = block["type"]
        if kind == "page_break":
            y += 48
            continue
        size, stroke, before, after = _SCAN_STYLES[kind]
        leading = size * 1.4
        face = font(size)
        y += before
        if kind in ("title", "heading", "paragraph"):
            for line in _wrap(block["text"], face.getlength, content_width):
                draws.append((y, _SCAN_MARGIN, size, stroke, line))
                y += leading
        elif kind == "list":
            indent = face.getlength("- ")
            for item in block["items"]:
                for index, line in enumerate(_wrap(item, face.getlength, content_width - indent)):
                    if index == 0:
                        draws.append((y, _SCAN_MARGIN, size, 0, "-"))
                    draws.append((y, _SCAN_MARGIN + indent, size, 0, line))
                    y += leading
        elif kind == "table":
            rows = block["rows"]
            widths = [max(face.getlength(row[c]) for row in rows) + 28 for c in range(len(rows[0]))]
            if sum(widths) > content_width:
                raise DocumentError(f"{document['file_name']}: a table is wider than the page")
            for row_index, row in enumerate(rows):
                x = float(_SCAN_MARGIN)
                for col, cell in enumerate(row):
                    draws.append((y, x, size, 1 if row_index == 0 else 0, cell))
                    x += widths[col]
                y += leading
                if row_index == 0:
                    rules.append((y, _SCAN_MARGIN, _SCAN_MARGIN + sum(widths)))
                    y += 8
        y += after
    height = int(y + _SCAN_MARGIN)

    image = Image.new("L", (_SCAN_WIDTH, height), _SCAN_PAPER)
    pen = ImageDraw.Draw(image)
    for top, x, size, stroke, text in draws:
        pen.text((x, top), text, font=font(size), fill=_SCAN_INK, stroke_width=stroke, stroke_fill=_SCAN_INK)
    for top, x1, x2 in rules:
        pen.line([(x1, top), (x2, top)], fill=_SCAN_INK, width=2)

    # The photocopy: speckles, a slight skew and a soft focus, deterministic
    # per file so the same declaration always renders the same bytes.
    rng = random.Random(document["file_name"])  # noqa: S311 - reproducible noise, not a secret
    for _ in range(_SCAN_WIDTH * height // 1400):
        px, py = rng.randrange(_SCAN_WIDTH), rng.randrange(height)
        radius = rng.choice((0, 0, 1))
        pen.ellipse([px, py, px + radius, py + radius], fill=rng.randrange(140, 215))
    image = image.rotate(_SCAN_ROTATION_DEGREES, resample=Image.Resampling.BICUBIC, fillcolor=_SCAN_PAPER)
    image = image.filter(ImageFilter.GaussianBlur(0.6))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
