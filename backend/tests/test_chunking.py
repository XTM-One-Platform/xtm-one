import sys
from types import SimpleNamespace

from app.services.chunking import extract_text_from_file


def test_extract_text_from_file_strips_null_bytes_from_plain_text():
    assert extract_text_from_file(b"hello\x00 world\x00", "note.txt") == "hello world"


def test_extract_text_from_file_strips_null_bytes_from_pdf_pages(monkeypatch):
    class _FakePdfReader:
        def __init__(self, _stream):
            self.pages = [
                SimpleNamespace(extract_text=lambda: "page\x001"),
                SimpleNamespace(extract_text=lambda: "page\x002"),
            ]

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=_FakePdfReader))

    assert extract_text_from_file(b"%PDF-1.4", "report.pdf") == "page1\n\npage2"
