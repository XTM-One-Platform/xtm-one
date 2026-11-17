"""``.docx`` text extraction keeps tables, headers and footers.

``Document.paragraphs`` is the body's top-level paragraphs only: the cells of
a table - where meeting minutes keep their action items - and the page
headers / footers were silently dropped on every surface that reads a Word
file (knowledge-base ingestion, the chat upload message, the inline fallback,
``read_file_content``). The reported symptom was minutes whose headings
arrived and whose items did not.
"""

from __future__ import annotations

import io

import pytest
from docx import Document

from app.services.attachment_extract import extract_attachment_text
from app.services.chunking import extract_text_from_file

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _save(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _minutes() -> bytes:
    doc = Document()
    doc.add_paragraph("PWC #14 Minutes")
    doc.add_paragraph("Action Item(s):")
    table = doc.add_table(rows=3, cols=3)
    for r, row in enumerate(
        [
            ("#", "Action", "Owner"),
            ("1", "Deploy the connector to production", "PWC"),
            ("2", "Share the IOC feed mapping", "UOB"),
        ]
    ):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    doc.add_paragraph("Closed Item(s):")
    closed = doc.add_table(rows=1, cols=1)
    closed.cell(0, 0).text = "SSO integration validated"
    return _save(doc)


def test_docx_table_rows_are_extracted_in_reading_order():
    text = extract_text_from_file(_minutes(), "minutes.docx")

    assert "| 1 | Deploy the connector to production | PWC |" in text
    assert "| 2 | Share the IOC feed mapping | UOB |" in text
    assert "| SSO integration validated |" in text
    order = [
        text.index("Action Item(s):"),
        text.index("Deploy the connector"),
        text.index("Closed Item(s):"),
        text.index("SSO integration validated"),
    ]
    assert order == sorted(order)


def test_the_chat_inline_path_reads_the_same_tables():
    """The inline fallback and the upload message go through
    ``extract_attachment_text``, which falls through to the same extraction
    for a Word file."""
    data = _minutes()

    assert extract_attachment_text(data, "minutes.docx", DOCX_MIME) == extract_text_from_file(
        data, "minutes.docx"
    )
    assert "Deploy the connector" in extract_attachment_text(data, "minutes.docx", DOCX_MIME)


def test_docx_merged_cells_are_not_repeated():
    doc = Document()
    table = doc.add_table(rows=2, cols=3)
    merged = table.cell(0, 0).merge(table.cell(0, 2))
    merged.text = "Quarterly review"
    table.cell(1, 0).text = "a"
    table.cell(1, 1).text = "b"
    table.cell(1, 2).text = "c"

    text = extract_text_from_file(_save(doc), "t.docx")

    assert text.count("Quarterly review") == 1
    assert "| Quarterly review |" in text
    assert "| a | b | c |" in text


def test_docx_vertically_merged_cell_is_written_once_where_it_starts():
    doc = Document()
    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 0).merge(table.cell(2, 0)).text = "Region EMEA"
    for r, value in enumerate(("alpha", "beta", "gamma")):
        table.cell(r, 1).text = value

    text = extract_text_from_file(_save(doc), "t.docx")

    assert text.count("Region EMEA") == 1
    assert "| Region EMEA | alpha |" in text
    assert "|  | beta |" in text
    assert "|  | gamma |" in text


def test_docx_nested_table_is_extracted():
    doc = Document()
    outer = doc.add_table(rows=1, cols=2)
    outer.cell(0, 0).text = "Outer"
    inner = outer.cell(0, 1).add_table(rows=1, cols=2)
    inner.cell(0, 0).text = "inner-left"
    inner.cell(0, 1).text = "inner-right"

    text = extract_text_from_file(_save(doc), "t.docx")

    assert "Outer" in text
    assert "| inner-left | inner-right |" in text


def test_docx_pipe_in_a_cell_is_escaped():
    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "a|b"
    table.cell(0, 1).text = "c"

    assert "| a\\|b | c |" in extract_text_from_file(_save(doc), "t.docx")


def test_docx_header_and_footer_are_extracted_once():
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "CONFIDENTIAL - Project X"
    doc.sections[0].footer.paragraphs[0].text = "Page footer text"
    doc.add_paragraph("Body paragraph")
    doc.add_section()  # inherits (links to) the first section's header / footer
    doc.add_paragraph("Second section body")

    text = extract_text_from_file(_save(doc), "t.docx")

    assert text.count("CONFIDENTIAL - Project X") == 1
    assert text.count("Page footer text") == 1
    assert text.index("CONFIDENTIAL") < text.index("Body paragraph") < text.index("Page footer text")
    assert "Second section body" in text
    assert text.startswith("[Header] CONFIDENTIAL - Project X")
    assert text.endswith("[Footer] Page footer text")


def _with_first_and_even_headers(*, first_page: bool, even_pages: bool) -> bytes:
    doc = Document()
    section = doc.sections[0]
    section.header.paragraphs[0].text = "Default header"
    section.first_page_header.paragraphs[0].text = "First page header"
    section.even_page_footer.paragraphs[0].text = "Even page footer"
    section.different_first_page_header_footer = first_page
    doc.settings.odd_and_even_pages_header_footer = even_pages
    doc.add_paragraph("Body")
    return _save(doc)


def test_docx_first_and_even_page_headers_word_does_not_draw_are_skipped():
    """A first-page / even-page header whose switch is off is never shown by
    Word - its stale text is not part of the document."""
    text = extract_text_from_file(_with_first_and_even_headers(first_page=False, even_pages=False), "t.docx")

    assert "Default header" in text
    assert "First page header" not in text
    assert "Even page footer" not in text


def test_docx_first_and_even_page_headers_are_read_when_shown():
    text = extract_text_from_file(_with_first_and_even_headers(first_page=True, even_pages=True), "t.docx")

    assert "[Header] Default header" in text
    assert "[Header] First page header" in text
    assert "[Footer] Even page footer" in text


def test_a_table_merged_down_thousands_of_rows_extracts():
    """python-docx resolves a vertically merged cell by recursing up the rows
    it spans: a column merged down more than ~1,000 rows raised
    ``RecursionError`` and the whole document extracted to nothing."""
    rows = 3000
    doc = Document()
    doc.add_paragraph("Inventory")
    table = doc.add_table(rows=rows, cols=2)
    trs = [row._tr for row in table.rows]
    trs[0].tc_lst[0].vMerge = "restart"
    trs[0].tc_lst[0].clear_content()
    trs[0].tc_lst[0].add_p().append(_run("Site A"))
    for tr in trs[1:]:
        tr.tc_lst[0].vMerge = "continue"
    for i, tr in enumerate(trs):
        tr.tc_lst[1].clear_content()
        tr.tc_lst[1].add_p().append(_run(f"asset-{i}"))

    text = extract_text_from_file(_save(doc), "big.docx")

    assert text.startswith("Inventory")
    assert text.count("Site A") == 1
    assert "| Site A | asset-0 |" in text
    assert f"|  | asset-{rows - 1} |" in text


def _run(value: str):
    from docx.oxml import OxmlElement

    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = value
    r.append(t)
    return r


def test_a_docx_the_table_reader_trips_on_still_yields_its_paragraphs(monkeypatch):
    """Whatever the richer table / header reader fails on, the document keeps
    the body paragraphs it always extracted."""
    from app.services import chunking

    def _boom(_content: bytes) -> str:
        raise ValueError("unexpected table structure")

    monkeypatch.setattr(chunking, "_extract_docx_text", _boom)

    assert extract_text_from_file(_minutes(), "minutes.docx") == (
        "PWC #14 Minutes\n\nAction Item(s):\n\nClosed Item(s):"
    )


def test_a_paragraph_only_docx_reads_as_before():
    doc = Document()
    doc.add_paragraph("First")
    doc.add_paragraph("   ")
    doc.add_paragraph("Second")

    assert extract_text_from_file(_save(doc), "t.docx") == "First\n\nSecond"


def test_a_corrupt_docx_extracts_to_empty_not_zip_bytes():
    corrupt = b"PK\x03\x04" + b"\x00garbage" * 50

    assert extract_text_from_file(corrupt, "broken.docx") == ""


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("broken.pdf", b"%PDF-1.7\n" + b"\x00\xff garbage" * 50),
        ("broken.pdf", b"\xef\xbb\xbf  %PDF-1.4\n" + b"\x00\xff garbage" * 50),
        ("broken.pptx", b"PK\x03\x04" + b"\x00garbage" * 50),
        ("broken.xlsx", b"PK\x03\x04" + b"\x00garbage" * 50),
    ],
)
def test_a_corrupt_binary_document_extracts_to_empty_not_its_bytes(filename, data):
    """A failed parse of a real binary document used to hand its bytes,
    decoded as UTF-8, to the reader as if they were the document's text."""
    assert extract_text_from_file(data, filename) == ""


@pytest.mark.parametrize(
    "data",
    [b"just some notes", b"PKI rollout notes: rotate the intermediate CA"],
)
def test_a_plain_text_file_named_docx_still_reads_as_text(data):
    """Only a real container signature means binary - a text starting with
    ``PK`` is still text."""
    assert extract_text_from_file(data, "notes.docx") == data.decode()


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("archive.zip", b"PK\x03\x04" + b"\x14\x00\x00\x00 payload" * 50),
        ("empty.zip", b"PK\x05\x06" + b"\x00" * 18),
        ("notes.txt", b"PK\x03\x04" + b"\x00misnamed" * 50),
    ],
)
def test_a_zip_file_read_as_text_extracts_to_nothing(filename, data):
    """The plain-text branch (an archive, a misnamed file) decoded these
    compressed containers as UTF-8 and handed the noise to readers and
    knowledge bases."""
    assert extract_text_from_file(data, filename) == ""


_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE2_FILES = [
    ("legacy.doc", _OLE2 + b"\x00Word 97 body\x00" * 50, "Word 97 body"),
    ("mail.msg", _OLE2 + "Subject: renewal".encode("utf-16-le") * 20, "Subject: renewal"),
    ("broken.xls", _OLE2 + b"\x00Q3 revenue\x00" * 50, "Q3 revenue"),
    ("renamed.docx", _OLE2 + b"\x00Word 97 body\x00" * 50, "Word 97 body"),
    ("renamed.xlsx", _OLE2 + b"\x00Q3 revenue\x00" * 50, "Q3 revenue"),
    ("renamed.pptx", _OLE2 + b"\x00Roadmap slide\x00" * 50, "Roadmap slide"),
    ("renamed.pdf", _OLE2 + b"\x00Word 97 body\x00" * 50, "Word 97 body"),
    ("embeds-a-pdf.xls", _OLE2 + b"\x00" * 504 + b"%PDF-1.4\x00Q3 revenue\x00" * 20, "Q3 revenue"),
    ("embeds-a-pdf.docx", _OLE2 + b"\x00" * 504 + b"%PDF-1.4\x00Word 97 body\x00" * 20, "Word 97 body"),
]


@pytest.mark.parametrize(("filename", "data", "words"), _OLE2_FILES)
def test_an_ole2_file_still_reads_as_text_by_default(filename, data, words):
    """A legacy ``.doc`` / ``.msg`` keeps its body as plain 8-bit / UTF-16
    runs, and so does an OLE2 file whose parse failed (an ``.xls`` xlrd cannot
    read, one carrying a ``.docx`` / ``.xlsx`` / ``.pptx`` / ``.pdf`` name, one
    embedding a PDF): it is decoded as text exactly as on the baseline, so it
    stays searchable with no configuration change."""
    text = extract_text_from_file(data, filename)
    assert text == data.decode("utf-8", errors="replace").replace("\x00", "")
    assert words in text


@pytest.mark.parametrize(("filename", "data", "words"), _OLE2_FILES)
def test_strict_binary_text_extraction_extracts_ole2_to_nothing(monkeypatch, filename, data, words):
    from app.core.config import settings

    monkeypatch.setattr(settings, "strict_binary_text_extraction", True)
    assert extract_text_from_file(data, filename) == ""


def test_strict_binary_text_extraction_leaves_text_files_alone(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "strict_binary_text_extraction", True)
    assert extract_text_from_file(b"plain text saved with a .doc extension", "legacy.doc") == (
        "plain text saved with a .doc extension"
    )


def test_reading_ole2_files_as_text_warns_once_per_process(monkeypatch, caplog):
    import logging

    from app.services import chunking

    monkeypatch.setattr(chunking, "_ole2_text_warning_logged", False)
    with caplog.at_level(logging.WARNING, logger="app.services.chunking"):
        for filename, data, _words in _OLE2_FILES * 2:
            extract_text_from_file(data, filename)
    warnings = [r for r in caplog.records if "OLE2 container" in r.getMessage()]
    assert len(warnings) == 1
    assert "STRICT_BINARY_TEXT_EXTRACTION" in warnings[0].getMessage()


@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("notes.txt", b"PKI rollout notes: rotate the intermediate CA"),
        ("notes.md", "# Title\n\ncaf\u00e9".encode()),
        ("data.json", b'{"a": 1}'),
        ("legacy.doc", b"plain text saved with a .doc extension"),
    ],
)
def test_text_files_still_read_as_text(filename, data):
    assert extract_text_from_file(data, filename) == data.decode()
