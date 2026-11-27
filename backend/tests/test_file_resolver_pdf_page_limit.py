"""PDF page-limit guard in the file resolver.

A PDF with more pages than the provider accepts natively must degrade to
text extraction instead of producing a request-content 400 that poisons
the whole conversation (the attachment is re-sent on every turn).
"""

import sys
from types import SimpleNamespace

from app.llm.base import FileRef
from app.llm.file_resolver import (
    PROVIDER_PDF_PAGE_LIMITS,
    FallbackText,
    NativeFile,
    resolve_file,
)
from app.llm.router import FailoverLLMProvider

_PDF_BYTES = b"%PDF-1.4 fake"


def _install_fake_pypdf(monkeypatch, page_count: int) -> None:
    """Install a fake ``pypdf`` module whose reader reports *page_count* pages."""

    class _FakePdfReader:
        def __init__(self, _stream):
            self.pages = [
                SimpleNamespace(extract_text=lambda: "some extracted page text")
                for _ in range(page_count)
            ]

    monkeypatch.setitem(
        sys.modules, "pypdf", SimpleNamespace(PdfReader=_FakePdfReader)
    )


def _pdf_ref() -> FileRef:
    return FileRef.from_bytes(_PDF_BYTES, "big.pdf", "application/pdf")


async def test_pdf_over_anthropic_limit_falls_back_to_text(monkeypatch):
    _install_fake_pypdf(monkeypatch, PROVIDER_PDF_PAGE_LIMITS["anthropic"] + 1)

    resolved = await resolve_file(_pdf_ref(), "anthropic")

    assert isinstance(resolved, FallbackText)
    assert "big.pdf" in resolved.text
    assert "some extracted page text" in resolved.text


async def test_pdf_at_anthropic_limit_stays_native(monkeypatch):
    _install_fake_pypdf(monkeypatch, PROVIDER_PDF_PAGE_LIMITS["anthropic"])

    resolved = await resolve_file(_pdf_ref(), "anthropic")

    assert isinstance(resolved, NativeFile)
    assert resolved.data == _PDF_BYTES
    assert resolved.content_type == "application/pdf"


async def test_pdf_over_gemini_limit_falls_back_to_text(monkeypatch):
    _install_fake_pypdf(monkeypatch, PROVIDER_PDF_PAGE_LIMITS["gemini"] + 1)

    resolved = await resolve_file(_pdf_ref(), "gemini")

    assert isinstance(resolved, FallbackText)


async def test_unparseable_pdf_stays_native(monkeypatch):
    """Fail-open: when the page count cannot be determined, keep the
    current native behaviour instead of degrading a valid document."""

    class _BrokenPdfReader:
        def __init__(self, _stream):
            raise ValueError("corrupt xref table")

    monkeypatch.setitem(
        sys.modules, "pypdf", SimpleNamespace(PdfReader=_BrokenPdfReader)
    )

    resolved = await resolve_file(_pdf_ref(), "anthropic")

    assert isinstance(resolved, NativeFile)


async def test_pdf_for_openai_compat_still_uses_text_fallback(monkeypatch):
    """An OpenAI-compatible backend has no native PDF support — the
    extraction path is unaffected by the page-limit guard."""
    _install_fake_pypdf(monkeypatch, 3)

    resolved = await resolve_file(_pdf_ref(), "openai_compat")

    assert isinstance(resolved, FallbackText)


def test_pdf_page_limit_error_is_non_failoverable():
    exc = Exception(
        "Error code: 400 - {'type': 'error', 'error': {'type': "
        "'invalid_request_error', 'message': 'messages.16.content.0.pdf."
        "source.base64.data: A maximum of 600 PDF pages may be provided.'}}"
    )
    assert FailoverLLMProvider._is_non_failoverable(exc)
