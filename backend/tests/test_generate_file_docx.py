"""Tests for DOCX rendering in the ``generate_file`` tool.

The ``generate_file`` tool used to fall through to a UTF-8 byte-encode for
every non-PDF binary format — so an agent asked for a Word document got raw
Markdown / plain text bytes saved under a ``.docx`` filename, which Word
refuses to open (no ``PK`` ZIP magic). The new
``application/vnd.openxmlformats-officedocument.wordprocessingml.document``
short-circuit renders the agent's Markdown / HTML to a real OOXML document
via python-docx, mirroring the existing PDF path.
"""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import pytest

from app.tools.base import FileContext, ToolCall

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class _FakeDB:
    def __init__(self):
        self.added = []

    async def execute(self, _query):  # pragma: no cover - not used here
        raise AssertionError("no DB queries expected")

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def _build_tool(monkeypatch, persist):
    from app.services import builtin_tools

    monkeypatch.setattr(builtin_tools, "_persist_generated_file", persist)
    _defs, executor = builtin_tools.build_generate_file_tool(
        db=_FakeDB(),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    executor.file_context = FileContext()
    return executor


# ──────────────────────────────────────────────────────────────────────────
# Routing: .docx must go through the renderer, never the UTF-8 fallback
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_file_renders_docx_via_renderer(monkeypatch):
    """When the effective content type is the Word OOXML MIME and encoding
    is not base64, the tool MUST route the text content through
    ``_render_text_to_docx`` instead of byte-encoding it. A regression to
    the UTF-8 fallback re-introduces the corrupt-DOCX bug."""
    from app.services import builtin_tools

    docx_calls: list[dict[str, object]] = []

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        docx_calls.append({"content": content, "title": title})
        return b"PK\x03\x04fake-docx-zip-bytes"

    monkeypatch.setattr(builtin_tools, "_render_text_to_docx", _fake_render)

    created = SimpleNamespace(
        id=uuid.uuid4(),
        filename="report.docx",
        content_type=DOCX_MIME,
        size=24,
        s3_object_name="chat/conv/report.docx",
    )

    async def _fake_persist(**_kwargs):
        assert _kwargs["raw_bytes"].startswith(b"PK"), (
            "raw_bytes must be the rendered DOCX (ZIP magic), not the "
            f"source Markdown — got {_kwargs['raw_bytes'][:16]!r}"
        )
        assert _kwargs["content_type"] == DOCX_MIME
        return created

    executor = _build_tool(monkeypatch, _fake_persist)
    result = await executor.execute_tool(
        ToolCall(
            id="call-docx-1",
            name="generate_file",
            arguments={
                "filename": "report.docx",
                "content": "# FIMI Trend Report\n\nExecutive summary…",
                "file_tag": "download_file",
            },
        )
    )

    assert result.is_error is False
    assert docx_calls, "DOCX renderer must be invoked for .docx output"
    assert "FIMI Trend Report" in str(docx_calls[0]["content"])
    assert docx_calls[0]["title"] == "report"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_content_type",
    [
        DOCX_MIME,
        DOCX_MIME.upper(),
        f"{DOCX_MIME}; charset=utf-8",
        f"  {DOCX_MIME}  ",
    ],
)
async def test_generate_file_normalizes_docx_content_type(
    monkeypatch, raw_content_type
):
    """Mixed-case / parameterized DOCX content types must still route
    through the renderer and persist the normalized base MIME — mirroring
    the PDF normalization contract."""
    from app.services import builtin_tools

    docx_calls: list[str] = []

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        docx_calls.append(content)
        return b"PK\x03\x04fake"

    monkeypatch.setattr(builtin_tools, "_render_text_to_docx", _fake_render)

    persisted: dict[str, object] = {}

    async def _fake_persist(**_kwargs):
        persisted.update(_kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=_kwargs["filename"],
            content_type=_kwargs["content_type"],
            size=len(_kwargs["raw_bytes"]),
            s3_object_name="chat/conv/report.docx",
        )

    executor = _build_tool(monkeypatch, _fake_persist)
    result = await executor.execute_tool(
        ToolCall(
            id="call-docx-norm",
            name="generate_file",
            arguments={
                "filename": "report.docx",
                "content": "# Report",
                "content_type": raw_content_type,
            },
        )
    )

    assert result.is_error is False
    assert docx_calls, (
        f"content_type={raw_content_type!r} must route to the DOCX renderer"
    )
    assert persisted["raw_bytes"].startswith(b"PK")
    assert persisted["content_type"] == DOCX_MIME


@pytest.mark.asyncio
async def test_generate_file_docx_render_failure_returns_friendly_error(monkeypatch):
    """A renderer failure must surface as a clean ``ToolResult`` error so
    the agent can fall back — and must NOT persist a corrupt/empty file."""
    from app.services import builtin_tools

    async def _fake_render_raises(_content: str, **_kwargs) -> bytes:
        raise RuntimeError("python-docx exploded")

    monkeypatch.setattr(builtin_tools, "_render_text_to_docx", _fake_render_raises)

    persist_called = {"v": False}

    async def _fake_persist(**_kwargs):
        persist_called["v"] = True
        return SimpleNamespace(id=uuid.uuid4(), filename="x", content_type="x", size=0)

    executor = _build_tool(monkeypatch, _fake_persist)
    result = await executor.execute_tool(
        ToolCall(
            id="call-docx-err",
            name="generate_file",
            arguments={"filename": "report.docx", "content": "# Report"},
        )
    )

    assert result.is_error is True
    assert "DOCX rendering failed" in result.content
    assert persist_called["v"] is False, (
        "Failed DOCX render must NOT persist a FileAttachment row"
    )


@pytest.mark.asyncio
async def test_generate_file_docx_base64_path_bypasses_renderer(monkeypatch):
    """Pre-built DOCX bytes supplied via ``encoding='base64'`` are the
    source of truth — the renderer must NOT run."""
    import base64 as _b64

    from app.services import builtin_tools

    render_called = {"v": False}

    async def _fake_render(_content: str, **_kwargs) -> bytes:
        render_called["v"] = True
        return b"should-not-be-called"

    monkeypatch.setattr(builtin_tools, "_render_text_to_docx", _fake_render)

    docx_bytes = b"PK\x03\x04real-docx-from-caller"

    async def _fake_persist(**_kwargs):
        assert _kwargs["raw_bytes"] == docx_bytes
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=_kwargs["filename"],
            content_type=_kwargs["content_type"],
            size=len(docx_bytes),
            s3_object_name="chat/conv/report.docx",
        )

    executor = _build_tool(monkeypatch, _fake_persist)
    result = await executor.execute_tool(
        ToolCall(
            id="call-docx-b64",
            name="generate_file",
            arguments={
                "filename": "report.docx",
                "content": _b64.b64encode(docx_bytes).decode("ascii"),
                "encoding": "base64",
            },
        )
    )

    assert result.is_error is False
    assert render_called["v"] is False, (
        "base64-encoded DOCX input MUST bypass the renderer"
    )


# ──────────────────────────────────────────────────────────────────────────
# Real renderer: Markdown → valid OOXML document
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_text_to_docx_produces_valid_word_document():
    """The rendered bytes must be a real OOXML ZIP (``PK`` magic) that
    python-docx can re-open, with the Markdown structure mapped to Word
    elements — headings, paragraphs, lists, and tables. This is the
    end-to-end guarantee that the downloaded file opens in Word."""
    from docx import Document

    from app.services.docx_render import render_text_to_docx

    md = (
        "# FIMI Trend Report\n\n"
        "**Reporting period:** 28 May 2026 - 11 June 2026\n\n"
        "## Executive Summary\n\n"
        "- Trend 1 - Storm-1516 amplification\n"
        "- Trend 2 - Spamouflage targeting\n\n"
        "| Metric | Count |\n"
        "|--------|-------|\n"
        "| Incidents | 44 |\n"
        "| Channels | 28 |\n\n"
        "See [the platform](https://example.com/incidents) for details.\n"
    )

    raw = await render_text_to_docx(md, title="FIMI Trend Report")

    assert raw.startswith(b"PK"), (
        f"DOCX must be an OOXML ZIP container; got leading bytes {raw[:8]!r}"
    )

    doc = Document(io.BytesIO(raw))
    texts = [p.text for p in doc.paragraphs]

    heading_styles = {
        p.style.name for p in doc.paragraphs if p.text == "FIMI Trend Report"
    }
    assert "Heading 1" in heading_styles, (
        f"H1 must map to the Heading 1 style; paragraphs: {texts[:5]}"
    )
    assert any("Executive Summary" in t for t in texts)
    assert any("Storm-1516" in t for t in texts)

    bullet_styles = {
        p.style.name for p in doc.paragraphs if "Trend 1" in p.text
    }
    assert any(s.startswith("List Bullet") for s in bullet_styles), bullet_styles

    assert len(doc.tables) == 1, "pipe table must become a real Word table"
    table = doc.tables[0]
    assert table.cell(0, 0).text == "Metric"
    assert table.cell(1, 1).text == "44"

    assert doc.core_properties.title == "FIMI Trend Report"


@pytest.mark.asyncio
async def test_render_text_to_docx_renders_html_input():
    """Already-HTML content must be parsed and mapped, not dumped as
    literal markup text."""
    from docx import Document

    from app.services.docx_render import render_text_to_docx

    html = (
        "<html><body><h2>Observations</h2>"
        "<p>Actor <strong>Storm-1516</strong> used <code>armreporter[.]am</code>.</p>"
        "<ol><li>first</li><li>second</li></ol>"
        "</body></html>"
    )
    raw = await render_text_to_docx(html, title="t")
    doc = Document(io.BytesIO(raw))
    texts = [p.text for p in doc.paragraphs]

    assert any("Observations" in t for t in texts)
    para = next(p for p in doc.paragraphs if "Storm-1516" in p.text)
    bold_runs = [r.text for r in para.runs if r.bold]
    assert "Storm-1516" in "".join(bold_runs), (
        f"<strong> must map to a bold run; runs: {[r.text for r in para.runs]}"
    )
    number_styles = {p.style.name for p in doc.paragraphs if p.text == "first"}
    assert any(s.startswith("List Number") for s in number_styles), number_styles
    # No literal markup may survive in the document text.
    assert not any("<" in t and ">" in t for t in texts), texts


@pytest.mark.asyncio
async def test_render_text_to_docx_nested_table_rows_stay_scoped():
    """Rows of a table nested inside a cell must NOT be spliced into the
    outer table (a recursive ``find_all("tr")`` would duplicate them and
    inflate the outer column count); the nested text still surfaces."""
    from docx import Document

    from app.services.docx_render import render_text_to_docx

    html = (
        "<html><body><table>"
        "<tr><th>Outer A</th><th>Outer B</th></tr>"
        "<tr><td>plain</td><td><table><tr><td>inner</td></tr></table></td></tr>"
        "</table></body></html>"
    )
    raw = await render_text_to_docx(html, title="t")
    doc = Document(io.BytesIO(raw))

    outer = doc.tables[0]
    assert len(outer.rows) == 2, "nested rows must not inflate the outer table"
    assert len(outer.columns) == 2
    assert outer.cell(0, 0).text == "Outer A"
    assert "inner" in outer.cell(1, 1).text


@pytest.mark.asyncio
async def test_render_text_to_docx_plain_text_never_empty():
    """Plain text (no Markdown structure) must still produce a readable,
    non-empty document — never an empty shell."""
    from docx import Document

    from app.services.docx_render import render_text_to_docx

    raw = await render_text_to_docx("Just a plain sentence.", title=None)
    doc = Document(io.BytesIO(raw))
    assert any("Just a plain sentence." in p.text for p in doc.paragraphs)


@pytest.mark.asyncio
async def test_render_text_to_docx_embeds_live_hyperlink():
    """Markdown links must become real ``w:hyperlink`` elements with an
    external relationship — not just colored text — so links stay
    clickable in Word."""
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE

    from app.services.docx_render import render_text_to_docx

    raw = await render_text_to_docx(
        "See [incident filter](https://platform.example/incidents?window=14d).",
    )
    doc = Document(io.BytesIO(raw))
    rels = [
        rel
        for rel in doc.part.rels.values()
        if rel.reltype == RELATIONSHIP_TYPE.HYPERLINK
    ]
    assert rels, "document must carry an external hyperlink relationship"
    assert rels[0].target_ref == "https://platform.example/incidents?window=14d"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ms-msdt:/id PCWDiagnostic",
        "/relative/path",
    ],
)
async def test_render_text_to_docx_unsafe_link_schemes_degrade_to_text(url):
    """Non-allow-listed link targets (``file:``, ``javascript:``, custom
    protocol handlers, scheme-less paths) must NOT become live
    ``w:hyperlink`` relationships — the document is opened locally in Word,
    so a clickable ``file:`` / custom-handler link is an attack vector. The
    target is still spelled out as plain text so it is never silently
    lost."""
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE

    from app.services.docx_render import render_text_to_docx

    raw = await render_text_to_docx(f"Open [the target]({url}) now.")
    doc = Document(io.BytesIO(raw))

    rels = [
        rel
        for rel in doc.part.rels.values()
        if rel.reltype == RELATIONSHIP_TYPE.HYPERLINK
    ]
    assert not rels, f"unsafe target {url!r} must not produce a hyperlink rel"
    para = next(p for p in doc.paragraphs if "the target" in p.text)
    assert url in para.text, "the raw target must survive as visible plain text"
