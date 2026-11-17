"""Tests for embedding a forwardable file image into a generated PDF/DOCX.

The PDF/DOCX renderers block every non-data: URL (SSRF guard) and never see the
platform file store, so an agent's ``![diagram](file:<id>)`` / ``[[FILE:id]]``
rendered as a broken image. ``generate_file`` now resolves those IMAGE handles
via the conversation FileContext and inlines the bytes as a ``data:`` URI before
rendering, and the DOCX renderer embeds ``data:`` images as real pictures.
"""

from __future__ import annotations

import io
import struct
import uuid
import zlib
from base64 import b64encode
from types import SimpleNamespace

import pytest

from app.llm.base import FileRef
from app.services.builtin_tools import media
from app.tools.base import FileContext, ToolCall


def _make_png_1x1() -> bytes:
    """Build a guaranteed-valid 1x1 truecolor PNG (no PIL dependency)."""

    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit RGB
    idat = zlib.compress(b"\x00\xff\x00\x00")  # filter byte + one red pixel
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


_PNG_1X1 = _make_png_1x1()
_PNG_1X1_B64 = b64encode(_PNG_1X1).decode("ascii")


def _persist_ok(**kwargs):
    async def _inner(**kw):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kw["filename"],
            content_type=kw["content_type"],
            size=len(kw["raw_bytes"]),
            s3_object_name=f"chat/conv/{kw['filename']}",
        )

    return _inner


def _build(monkeypatch, file_context, *, pdf_sink=None, docx_sink=None):
    monkeypatch.setattr(media, "_persist_generated_file", _persist_ok())

    if pdf_sink is not None:

        async def _fake_pdf(content, *, title=None):
            pdf_sink["content"] = content
            return b"%PDF-1.4 fake"

        monkeypatch.setattr(media, "_render_text_to_pdf", _fake_pdf)
    if docx_sink is not None:

        async def _fake_docx(content, *, title=None):
            docx_sink["content"] = content
            return b"PK\x03\x04fake"

        monkeypatch.setattr(media, "_render_text_to_docx", _fake_docx)

    _defs, executor = media.build_generate_file_tool(
        db=None, user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4())
    )
    executor.file_context = file_context
    return executor


def _fc_with_image(file_id: str, *, content_type: str = "image/png") -> FileContext:
    fc = FileContext()
    fc.add(FileRef.from_bytes(_PNG_1X1, "diagram.png", content_type, file_id=file_id))
    return fc


# ── _inline_embedded_file_refs (unit) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_inline_resolves_file_uuid_and_marker_to_data_uri():
    fid = str(uuid.uuid4())
    fc = _fc_with_image(fid)
    content = f"# Recap\n\n![diagram](file:{fid})\n\nand <img src=\"[[FILE:{fid}]]\">"
    out = await media._inline_embedded_file_refs(content, fc)
    assert f"file:{fid}" not in out
    assert f"[[FILE:{fid}]]" not in out
    assert out.count("data:image/png;base64,") == 2


@pytest.mark.asyncio
async def test_inline_skips_non_image_files():
    fid = str(uuid.uuid4())
    fc = FileContext()
    fc.add(FileRef.from_bytes(b"%PDF-1.4", "doc.pdf", "application/pdf", file_id=fid))
    content = f"![x](file:{fid})"
    out = await media._inline_embedded_file_refs(content, fc)
    # non-image left untouched (won't render as an <img> anyway)
    assert out == content


@pytest.mark.asyncio
async def test_inline_leaves_unresolvable_handle_untouched():
    fc = _fc_with_image(str(uuid.uuid4()))
    other = str(uuid.uuid4())
    content = f"![x](file:{other})"
    out = await media._inline_embedded_file_refs(content, fc)
    assert out == content


@pytest.mark.asyncio
async def test_inline_noop_without_context():
    out = await media._inline_embedded_file_refs("![x](file:abc)", None)
    assert out == "![x](file:abc)"


# ── generate_file end-to-end (renderer mocked, content captured) ──────────


@pytest.mark.asyncio
async def test_generate_pdf_inlines_image_handle(monkeypatch):
    fid = str(uuid.uuid4())
    sink: dict[str, str] = {}
    executor = _build(monkeypatch, _fc_with_image(fid), pdf_sink=sink)
    result = await executor.execute_tool(
        ToolCall(
            id="p1",
            name="generate_file",
            arguments={
                "filename": "recap.pdf",
                "content": f"# Architecture\n\n![FedRAMP](file:{fid})",
            },
        )
    )
    assert result.is_error is False, result.content
    assert "data:image/png;base64," in sink["content"]
    assert f"file:{fid}" not in sink["content"]


@pytest.mark.asyncio
async def test_generate_docx_inlines_image_handle(monkeypatch):
    fid = str(uuid.uuid4())
    sink: dict[str, str] = {}
    executor = _build(monkeypatch, _fc_with_image(fid), docx_sink=sink)
    result = await executor.execute_tool(
        ToolCall(
            id="d1",
            name="generate_file",
            arguments={
                "filename": "recap.docx",
                "content": f"# Architecture\n\n![FedRAMP]([[FILE:{fid}]])",
            },
        )
    )
    assert result.is_error is False, result.content
    assert "data:image/png;base64," in sink["content"]


# ── DOCX renderer actually embeds a data: image ───────────────────────────


@pytest.mark.asyncio
async def test_docx_renderer_embeds_data_uri_image():
    from docx import Document

    from app.services.docx_render import render_text_to_docx

    md = f"# Diagram\n\n![arch](data:image/png;base64,{_PNG_1X1_B64})\n"
    raw = await render_text_to_docx(md, title="t")
    doc = Document(io.BytesIO(raw))
    assert len(doc.inline_shapes) == 1, "the data: PNG must embed as a real picture"


@pytest.mark.asyncio
async def test_docx_renderer_falls_back_to_alt_for_unembeddable():
    from docx import Document

    from app.services.docx_render import render_text_to_docx

    # Not valid base64 image data -> embedding fails -> alt text survives.
    md = "![the schema](data:image/png;base64,not-real-bytes)"
    raw = await render_text_to_docx(md, title="t")
    doc = Document(io.BytesIO(raw))
    assert len(doc.inline_shapes) == 0
    assert any("the schema" in p.text for p in doc.paragraphs)
