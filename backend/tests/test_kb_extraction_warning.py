"""A Word document whose text the extractor misses is flagged, never a silent
``ready``.

A knowledge base synced from a drive stored a template whose body sat in an
element the extractor skipped as 41 characters, status ready: a reader could
not tell it from an empty template. ``chunking.extraction_warning`` measures
the extracted text against the body's raw text runs, and the drive syncs keep
what it says on the document's ``extra_metadata``.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from docx import Document

from app.services import chunking
from app.services.chunking import extract_text_from_file, extraction_warning
from app.services.kb_sync import _set_extraction_warning

_BODY = "Confidential information covers every document shared under this agreement. " * 6


def _save(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _plain() -> bytes:
    doc = Document()
    doc.add_paragraph("Mutual NDA")
    doc.add_paragraph(_BODY)
    doc.add_paragraph("Term and termination")
    return _save(doc)


def test_a_fully_extracted_docx_carries_no_warning():
    data = _plain()
    text = extract_text_from_file(data, "nda.docx")

    assert extraction_warning(data, "nda.docx", text) is None


def test_a_docx_extracted_to_its_headings_is_flagged_partial():
    data = _plain()

    warning = extraction_warning(data, "nda.docx", "Mutual NDA\nTerm and termination")

    assert warning is not None
    assert warning.startswith("partial text: ")
    assert "characters of the document body were extracted" in warning


def test_a_short_docx_is_never_flagged():
    doc = Document()
    doc.add_paragraph("Short note")
    data = _save(doc)

    assert extraction_warning(data, "short.docx", "") is None


def test_a_large_docx_with_little_text_is_flagged(monkeypatch):
    monkeypatch.setattr(chunking, "_DOCX_SPARSE_FILE_BYTES", 1)
    doc = Document()
    doc.add_paragraph("Scan")
    data = _save(doc)

    warning = extraction_warning(data, "scan.docx", "Scan")

    assert warning is not None and warning.startswith("little text: ")


def test_other_formats_and_unreadable_files_are_not_measured():
    assert extraction_warning(b"%PDF-1.4", "report.pdf", "") is None
    assert extraction_warning(b"not a zip", "broken.docx", "x" * 50) is None


def test_the_sync_records_and_clears_the_warning_on_the_document():
    doc = SimpleNamespace(extra_metadata={"keep": 1})

    _set_extraction_warning(doc, "partial text: 41 of the 9000 characters")
    assert doc.extra_metadata == {
        "keep": 1,
        "extraction_warning": "partial text: 41 of the 9000 characters",
    }

    _set_extraction_warning(doc, None)
    assert doc.extra_metadata == {"keep": 1}

    empty = SimpleNamespace(extra_metadata={"extraction_warning": "stale"})
    _set_extraction_warning(empty, None)
    assert empty.extra_metadata is None
