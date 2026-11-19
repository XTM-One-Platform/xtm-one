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

"""Document chunking service – splits text into token-counted chunks.

Uses tiktoken for accurate token counting (matches OpenAI embedding models).
Supports configurable chunk size and overlap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from app.core.config import settings
from app.core.utils import format_exception_message

if TYPE_CHECKING:
    import tiktoken

logger = logging.getLogger(__name__)

# Lazy-load tiktoken encoder
_encoder = None


def _sanitize_extracted_text(text: str) -> str:
    """Remove null bytes that PostgreSQL text columns reject."""
    return text.replace("\x00", "")


#: Leading bytes of the binary containers the extractor parses: zip (the
#: OpenXML formats) and OLE2 (legacy ``.xls``). PDF is matched separately, its
#: header may follow a few bytes of junk.
_ZIP_SIGNATURES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
)
_OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _parse_failure_fallback(content: bytes, filename: str) -> str:
    """What a failed document parse yields.

    A zip or PDF document that failed to parse extracts to nothing - its
    compressed bytes decoded as text would be noise handed to a reader or an
    index as content. An OLE2 container follows :func:`_is_binary_container`,
    even when it embeds a PDF. Anything else (a text file carrying a document
    extension) is read as text.
    """
    if _is_binary_container(content, filename):
        return ""
    if b"%PDF-" in content[:1024] and not content.startswith(_OLE2_SIGNATURE):
        return ""
    return _sanitize_extracted_text(content.decode("utf-8", errors="replace"))


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        import tiktoken

        _encoder = tiktoken.get_encoding(
            "cl100k_base"
        )  # Used by text-embedding-3-large / OpenAI models
    return _encoder


def _split_into_sections(text: str) -> list[str]:
    """Split text into logical sections at markdown headings and double newlines.

    Preserves paragraph/heading boundaries so chunks are semantically coherent.
    """
    import re

    # Split on markdown headings (# ...) or double newlines
    parts = re.split(r"(?=\n#{1,4} )|(?:\n\n+)", text)
    # Filter empty parts and strip whitespace
    return [p.strip() for p in parts if p and p.strip()]


# The chunking parameters a knowledge base may carry. Every write path
# refuses values outside them through `chunk_params_problem`, and `chunk_text`
# clamps into them through `normalize_chunk_params`, so a legacy row holding
# anything else still terminates. The split below advances by
# `chunk_size - chunk_overlap` tokens: an overlap reaching the size used to
# never advance, which froze the worker's event loop on the first oversized
# section. Keeping the overlap at most half the size bounds the work to twice
# the no-overlap chunk count.
#
# The merge below keeps up to `chunk_overlap` tokens and then appends a section
# of up to `chunk_size`, so one chunk can reach `chunk_size + chunk_overlap`
# tokens. That sum is capped at CHUNK_TOKENS_MAX, under the 8,191-token input
# limit of text-embedding-3 with headroom for the separators the merge joins in.
CHUNK_TOKENS_MAX = 8000
CHUNK_SIZE_MIN = 64
CHUNK_SIZE_MAX = CHUNK_TOKENS_MAX


def chunk_overlap_max(chunk_size: int) -> int:
    """The largest overlap allowed for *chunk_size*: half of it, and no more
    than keeps `chunk_size + chunk_overlap` within CHUNK_TOKENS_MAX."""
    return max(0, min(chunk_size // 2, CHUNK_TOKENS_MAX - chunk_size))


def chunk_param_type_problem(name: str, value: object) -> str | None:
    """Why *value* is not an integer chunking parameter, or None when it is.

    Booleans are refused although Python counts them as ints. Schemas run this
    on the RAW input (a `before` validator), since lax coercion would already
    have turned `true` or `"128"` into an int.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{name} must be an integer."
    return None


def chunk_params_problem(chunk_size: object, chunk_overlap: object) -> str | None:
    """Why these chunking parameters cannot be stored, or None when they can."""
    problem = chunk_param_type_problem("chunk_size", chunk_size)
    problem = problem or chunk_param_type_problem("chunk_overlap", chunk_overlap)
    if problem:
        return problem
    chunk_size, chunk_overlap = cast(int, chunk_size), cast(int, chunk_overlap)
    if not CHUNK_SIZE_MIN <= chunk_size <= CHUNK_SIZE_MAX:
        return (
            f"chunk_size must be between {CHUNK_SIZE_MIN} and {CHUNK_SIZE_MAX} "
            f"tokens (got {chunk_size})."
        )
    limit = chunk_overlap_max(chunk_size)
    if not 0 <= chunk_overlap <= limit:
        return (
            f"chunk_overlap must be between 0 and half of chunk_size, with "
            f"chunk_size + chunk_overlap at most {CHUNK_TOKENS_MAX} tokens "
            f"({limit} for chunk_size {chunk_size}; got {chunk_overlap})."
        )
    return None


def normalize_chunk_params(chunk_size: object, chunk_overlap: object) -> tuple[int, int]:
    """Clamp *chunk_size* and *chunk_overlap* into the allowed ranges.

    The identity for every pair `chunk_params_problem` accepts, so a valid
    collection is chunked exactly as configured; a row written before the
    bounds existed is chunked with the nearest allowed values instead of
    hanging.
    """
    try:
        size = int(chunk_size)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        size = 512
    try:
        overlap = int(chunk_overlap)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        overlap = 0
    size = min(max(size, CHUNK_SIZE_MIN), CHUNK_SIZE_MAX)
    overlap = min(max(overlap, 0), chunk_overlap_max(size))
    return size, overlap


# The input limit of text-embedding-3. `chunk_text` counts sections, not the
# "\n\n" separators it joins them with, so a run of very short sections can
# merge into a chunk past it; only such a chunk, which could never have been
# embedded, is re-split. Every other chunk keeps its boundaries.
EMBEDDING_INPUT_TOKENS_MAX = 8191


def _split_tokens(
    tokens: list[int], enc: tiktoken.Encoding, chunk_size: int, chunk_overlap: int
) -> list[str]:
    """Cut *tokens* into windows of *chunk_size* overlapping by *chunk_overlap*."""
    out: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        out.append(enc.decode(tokens[start:end]))
        # Never zero or negative, whatever the caller passed.
        start += max(1, chunk_size - chunk_overlap)
    return out


def _merged_chunks(
    parts: list[str],
    counted_tokens: int,
    enc: tiktoken.Encoding,
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    """Join *parts* into one chunk, re-split only past the embedding input limit.

    *counted_tokens* sums the parts alone; a separator encodes to about one
    token, so the joined text is re-encoded only when its separators could
    carry it past EMBEDDING_INPUT_TOKENS_MAX.
    """
    text = "\n\n".join(parts)
    if counted_tokens + 2 * len(parts) > EMBEDDING_INPUT_TOKENS_MAX:
        tokens = enc.encode(text)
        if len(tokens) > EMBEDDING_INPUT_TOKENS_MAX:
            return _split_tokens(tokens, enc, chunk_size, chunk_overlap)
    return [text]


def chunk_text(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 128,
) -> list[str]:
    """Split text into chunks of approximately `chunk_size` tokens with `chunk_overlap` overlap.

    Defaults are tuned for RAG quality with text-embedding-3-large:
    - 512 tokens per chunk for precise semantic matching
    - 128 tokens overlap (~25%) for continuity across chunk boundaries

    Strategy:
      1. First split into logical sections (paragraphs, headings)
      2. Merge small sections into chunks up to the token limit
      3. Split oversized sections at token boundaries as a fallback

    Uses tiktoken cl100k_base encoding for accurate token counting.

    The parameters are clamped through `normalize_chunk_params` first, so the
    split always advances and the call always terminates.
    """
    chunk_size, chunk_overlap = normalize_chunk_params(chunk_size, chunk_overlap)
    if not text or len(text.strip()) < 10:
        return [text] if text else []

    try:
        enc = _get_encoder()
    except Exception:
        return _chunk_by_words(text, chunk_size, chunk_overlap)

    total_tokens = len(enc.encode(text))
    if total_tokens <= chunk_size:
        return [text]

    # Step 1: Split into logical sections
    sections = _split_into_sections(text)

    # Step 2: Merge sections into chunks, respecting token limits
    chunks: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0

    for section in sections:
        section_tokens = len(enc.encode(section))

        # If a single section exceeds chunk_size, split it at token boundaries
        if section_tokens > chunk_size:
            # Flush current buffer first
            if current_parts:
                chunks.extend(
                    _merged_chunks(
                        current_parts, current_tokens, enc, chunk_size, chunk_overlap
                    )
                )

            # Split oversized section at token boundaries
            chunks.extend(
                _split_tokens(enc.encode(section), enc, chunk_size, chunk_overlap)
            )
            current_parts = []
            current_tokens = 0
            continue

        # If adding this section would exceed the limit, flush
        if current_tokens + section_tokens > chunk_size and current_parts:
            chunks.extend(
                _merged_chunks(
                    current_parts, current_tokens, enc, chunk_size, chunk_overlap
                )
            )
            # Keep overlap
            overlap_parts = []
            overlap_count = 0
            for p in reversed(current_parts):
                p_tokens = len(enc.encode(p))
                if overlap_count + p_tokens <= chunk_overlap:
                    overlap_parts.insert(0, p)
                    overlap_count += p_tokens
                else:
                    break
            current_parts = overlap_parts
            current_tokens = overlap_count

        current_parts.append(section)
        current_tokens += section_tokens

    # Flush remaining
    if current_parts:
        chunks.extend(
            _merged_chunks(current_parts, current_tokens, enc, chunk_size, chunk_overlap)
        )

    return chunks


def _chunk_by_words(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Fallback: chunk by words (approximate token count)."""
    chunk_size, chunk_overlap = normalize_chunk_params(chunk_size, chunk_overlap)
    words = text.split()
    if len(words) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        start += max(1, chunk_size - chunk_overlap)
        if start >= len(words):
            break

    return chunks


def extraction_extension(filename: str) -> str:
    """The extension :func:`extract_text_from_file` picks its extractor by."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


#: Nesting depth up to which a table inside a table cell is read.
_DOCX_MAX_TABLE_DEPTH = 3

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"

#: Run children read as text, as ``python-docx`` reads them (``Run.text``):
#: a column or page break reads as nothing, a line break as a new line.
_DOCX_RUN_TEXT = {
    f"{_W_NS}tab": "\t",
    f"{_W_NS}ptab": "\t",
    f"{_W_NS}cr": "\n",
    f"{_W_NS}noBreakHyphen": "-",
}

#: Never read: deleted or moved-away revisions, field codes, properties (the
#: tab stops of a ``w:pPr`` are ``w:tab`` too), and the VML fallback of a
#: drawing whose ``mc:Choice`` already holds the same text box.
_DOCX_SKIPPED = {
    f"{_W_NS}del",
    f"{_W_NS}moveFrom",
    f"{_W_NS}instrText",
    f"{_W_NS}delInstrText",
    f"{_W_NS}pPr",
    f"{_W_NS}rPr",
    f"{_W_NS}sdtPr",
    f"{_W_NS}sdtEndPr",
    _MC_FALLBACK,
}

#: Wrappers whose children are read in their place: content controls, custom
#: XML and smart tags, tracked insertions.
_DOCX_WRAPPERS = {
    f"{_W_NS}sdt",
    f"{_W_NS}sdtContent",
    f"{_W_NS}customXml",
    f"{_W_NS}smartTag",
    f"{_W_NS}ins",
    f"{_W_NS}moveTo",
}


def _docx_paragraph_text(p: Any, boxes: list[Any]) -> str:
    """The text of a ``w:p``, every run it holds included.

    ``Paragraph.text`` reads the paragraph's own runs and hyperlinks only: a
    run inside an inline content control, a simple field, a smart tag or a
    tracked insertion was dropped. Text boxes anchored in the paragraph are
    appended to *boxes*, read after it.
    """
    out: list[str] = []

    def walk(el: Any) -> None:
        for child in el:
            tag = child.tag
            if not isinstance(tag, str) or tag in _DOCX_SKIPPED:
                continue
            if tag == f"{_W_NS}t":
                out.append(child.text or "")
            elif tag == f"{_W_NS}br":
                if child.get(f"{_W_NS}type") in (None, "textWrapping"):
                    out.append("\n")
            elif tag in _DOCX_RUN_TEXT:
                out.append(_DOCX_RUN_TEXT[tag])
            elif tag == f"{_W_NS}txbxContent":
                boxes.append(child)
            else:
                walk(child)

    walk(p)
    return "".join(out)


def _docx_children(el: Any, wanted: str) -> list[Any]:
    """The *wanted* children of *el*, through content controls and wrappers
    (a repeating-section control wraps whole rows, a cell control a cell)."""
    found: list[Any] = []
    for child in el:
        tag = child.tag
        if tag == wanted:
            found.append(child)
        elif tag in _DOCX_WRAPPERS:
            found.extend(_docx_children(child, wanted))
    return found


def _docx_cell_text(tc: Any, nested: list[str], depth: int) -> str:
    parts = [line.strip() for line in _docx_block_items(tc, depth, nested) if line.strip()]
    return " ".join(parts).replace("|", "\\|")


def _docx_vmerge(tc: Any) -> str | None:
    tc_pr = tc.find(f"{_W_NS}tcPr")
    merge = tc_pr.find(f"{_W_NS}vMerge") if tc_pr is not None else None
    if merge is None:
        return None
    return merge.get(f"{_W_NS}val") or "continue"


def _docx_table_lines(tbl: Any, depth: int) -> list[str]:
    """One ``| a | b |`` line per row, nested tables after their row.

    The row's own ``w:tc`` elements are read, not ``row.cells``: python-docx
    repeats a horizontally merged cell once per grid column, and resolves a
    vertically merged one by recursing up the rows it spans - quadratic, and a
    ``RecursionError`` past ~1,000 rows. A merged cell is written once, where
    it starts; the rows it continues into carry an empty cell in its place.
    Rows and cells wrapped in a content control are read too.
    """
    lines: list[str] = []
    for tr in _docx_children(tbl, f"{_W_NS}tr"):
        cells: list[str] = []
        nested: list[str] = []
        for tc in _docx_children(tr, f"{_W_NS}tc"):
            if _docx_vmerge(tc) == "continue":
                cells.append("")
                continue
            cells.append(_docx_cell_text(tc, nested, depth))
        if any(cells):
            lines.append("| " + " | ".join(cells) + " |")
        lines.extend(nested)
    return lines


def _docx_block_items(el: Any, depth: int, nested: list[str] | None = None) -> list[str]:
    """The paragraphs of a block container (body, header, footer, cell, text
    box) in reading order, through content controls.

    A table yields its rows as one block; inside a cell (*nested* given) it
    goes to *nested*, written after the cell's row, down to
    :data:`_DOCX_MAX_TABLE_DEPTH`. A text box is read after the paragraph that
    anchors it.
    """
    blocks: list[str] = []
    for child in el:
        tag = child.tag
        if tag == f"{_W_NS}p":
            boxes: list[Any] = []
            text = _docx_paragraph_text(child, boxes)
            if text.strip():
                blocks.append(text)
            for box in boxes:
                blocks.extend(_docx_block_items(box, depth, nested))
        elif tag == f"{_W_NS}tbl":
            if nested is None:
                lines = _docx_table_lines(child, depth)
                if lines:
                    blocks.append("\n".join(lines))
            elif depth < _DOCX_MAX_TABLE_DEPTH:
                nested.extend(_docx_table_lines(child, depth + 1))
        elif tag in _DOCX_WRAPPERS:
            blocks.extend(_docx_block_items(child, depth, nested))
    return blocks


def _docx_blocks(container: Any) -> list[str]:
    """Paragraphs and tables of a body, header or footer, in reading order:
    content controls, text boxes, fields and table cells included."""
    return _docx_block_items(container._element, 1)


def _extract_docx_text(content: bytes) -> str:
    """The text of a Word document: headers, body, footers, tables included.

    ``Document.paragraphs`` is the body's top-level paragraphs only, so a
    table's cells - where meeting minutes keep their action items - the page
    headers / footers, content controls (a template's whole body) and text
    boxes never reached a reader or an index.
    """
    import io

    from docx import Document

    doc = Document(io.BytesIO(content))
    # A first-page / even-page header is only drawn when the section (resp.
    # the document) turns it on; otherwise its stale text is not in the
    # document the reader sees.
    even_pages = bool(doc.settings.odd_and_even_pages_header_footer)

    def _parts(attrs: tuple[str, str, str], label: str) -> list[str]:
        default, first, even = attrs
        out: list[str] = []
        seen: set[str] = set()
        for section in doc.sections:
            shown = [default]
            if section.different_first_page_header_footer:
                shown.append(first)
            if even_pages:
                shown.append(even)
            for attr in shown:
                part = getattr(section, attr)
                if part.is_linked_to_previous:
                    continue
                text = "\n".join(_docx_blocks(part)).strip()
                if text and text not in seen:
                    seen.add(text)
                    out.append(f"[{label}] {text}")
        return out

    headers = _parts(("header", "first_page_header", "even_page_header"), "Header")
    footers = _parts(("footer", "first_page_footer", "even_page_footer"), "Footer")
    return "\n\n".join([*headers, *_docx_block_items(doc.element.body, 1), *footers])


def _extract_docx_paragraphs(content: bytes) -> str:
    """The body's top-level paragraphs - what a Word document yielded before
    tables and headers were read, kept as the fallback so a document the
    richer reader trips on still extracts what it always did."""
    import io

    from docx import Document

    doc = Document(io.BytesIO(content))
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


_ole2_text_warning_logged = False


def _is_binary_container(content: bytes, filename: str) -> bool:
    """Whether *content* is a container whose bytes must not be read as text.

    A zip always is: its entries are compressed. An OLE2 container (a legacy
    ``.doc``, an ``.msg``) keeps its body text as plain 8-bit / UTF-16 runs
    that stay searchable when read as text, so it is read as text unless
    ``STRICT_BINARY_TEXT_EXTRACTION`` is on.
    """
    global _ole2_text_warning_logged
    if content.startswith(_ZIP_SIGNATURES):
        return True
    if not content.startswith(_OLE2_SIGNATURE):
        return False
    if settings.strict_binary_text_extraction:
        return True
    if _ole2_text_warning_logged:
        logger.debug("Reading OLE2 container %r as text", filename)
    else:
        _ole2_text_warning_logged = True
        logger.warning(
            "Reading OLE2 container %r as text (no dedicated extractor); "
            "STRICT_BINARY_TEXT_EXTRACTION=true extracts such files to nothing. "
            "Logged once per process.",
            filename,
        )
    return False


#: What separates two pages in a PDF's extracted text.
PDF_PAGE_SEPARATOR = "\n\n"


def pdf_page_texts(content: bytes) -> list[str]:
    """The text layer of each page of a PDF, in page order. CPU-bound.

    A PDF's extracted text is these joined by :data:`PDF_PAGE_SEPARATOR`, so
    a reader that needs to know where a page ends in a stored document
    (``kb_figures``) reads the same pages. Raises what ``pypdf`` raises.
    """
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return [_sanitize_extracted_text(page.extract_text() or "") for page in reader.pages]


def extract_text_from_file(content: bytes, filename: str) -> str:
    """Extract text from various file formats."""
    ext = extraction_extension(filename)

    if ext == "pdf":
        try:
            return PDF_PAGE_SEPARATOR.join(pdf_page_texts(content))
        except Exception as e:
            logger.warning("PDF extraction failed: %s", format_exception_message(e))
            return _parse_failure_fallback(content, filename)

    if ext == "docx":
        try:
            return _sanitize_extracted_text(_extract_docx_text(content))
        except Exception as e:
            logger.warning(
                "DOCX table / header extraction failed, reading paragraphs only: %s",
                format_exception_message(e),
            )
        try:
            return _sanitize_extracted_text(_extract_docx_paragraphs(content))
        except Exception as e:
            logger.warning("DOCX extraction failed: %s", format_exception_message(e))
            return _parse_failure_fallback(content, filename)

    if ext == "xlsx":
        try:
            import io

            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            parts: list[str] = []
            for sheet in wb.worksheets:
                parts.append(f"## {sheet.title}")
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    if any(cells):
                        parts.append(" | ".join(cells))
            wb.close()
            return _sanitize_extracted_text("\n".join(parts))
        except Exception as e:
            logger.warning("XLSX extraction failed: %s", format_exception_message(e))
            return _parse_failure_fallback(content, filename)

    if ext == "xls":
        try:
            import xlrd

            wb = xlrd.open_workbook(file_contents=content)
            parts_xls: list[str] = []
            for sheet in wb.sheets():
                parts_xls.append(f"## {sheet.name}")
                for rx in range(sheet.nrows):
                    cells = [str(sheet.cell_value(rx, cx)) for cx in range(sheet.ncols)]
                    if any(cells):
                        parts_xls.append(" | ".join(cells))
            return _sanitize_extracted_text("\n".join(parts_xls))
        except Exception as e:
            logger.warning("XLS extraction failed: %s", format_exception_message(e))
            return _parse_failure_fallback(content, filename)

    if ext == "pptx":
        try:
            import io

            from pptx import Presentation

            prs = Presentation(io.BytesIO(content))
            parts_pptx: list[str] = []
            for index, slide in enumerate(prs.slides, start=1):
                slide_lines: list[str] = []
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        txt = shape.text_frame.text.strip()
                        if txt:
                            slide_lines.append(txt)
                    elif shape.has_table:
                        for row in shape.table.rows:
                            cells = [cell.text.strip() for cell in row.cells]
                            if any(cells):
                                slide_lines.append(" | ".join(cells))
                if slide.has_notes_slide:
                    notes = (slide.notes_slide.notes_text_frame.text or "").strip()
                    if notes:
                        slide_lines.append(f"[Notes] {notes}")
                if slide_lines:
                    parts_pptx.append(f"## Slide {index}\n" + "\n".join(slide_lines))
            return _sanitize_extracted_text("\n\n".join(parts_pptx))
        except Exception as e:
            logger.warning("PPTX extraction failed: %s", format_exception_message(e))
            return _parse_failure_fallback(content, filename)

    # Default: treat as text - unless it is a zip container (an archive, a
    # misnamed Office file), whose compressed bytes decoded as UTF-8 would
    # reach a reader or an index as noise.
    if _is_binary_container(content, filename):
        return ""
    return _sanitize_extracted_text(content.decode("utf-8", errors="replace"))


async def extract_text_from_file_async(content: bytes, filename: str) -> str:
    """Async wrapper for :func:`extract_text_from_file`.

    Document parsing (pypdf, python-docx, openpyxl, xlrd, python-pptx) is
    pure-CPU work that can take minutes on large or pathological files.
    Running it inline in an ``async def`` blocks the whole event loop for
    that long — in production this froze ``/api/health`` until the kubelet
    killed the pod. This wrapper offloads the parse to the
    dedicated bounded executor in ``core/parse_offload.py``.

    ALWAYS use this from async code paths; the sync function is only for
    genuinely synchronous callers (scripts, tests).
    """
    from app.core.parse_offload import run_parse

    return await run_parse(extract_text_from_file, content, filename)
