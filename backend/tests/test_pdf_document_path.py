"""The PDF document path.

An Incident Response agent lost its way to a readable PDF: the read-back of a
file it had downloaded earlier in the run answered "no file attached", the
trace of its own previous run answered "not visible to you", and it drew the
document in the sandbox with matplotlib's ``PdfPages`` - 7pt monospaced text,
no headings, no tables - and remembered that recipe. These tests pin the four
platform answers:

* ``generate_file(source_file_id=...)`` renders an existing Markdown / HTML /
  text file of the conversation exactly as ``content`` would, without the
  model re-sending the text;
* a sandbox run that writes a PDF with a PDF-drawing library is told, in its
  result, that the file is not a document;
* a file of the current run stays readable by ``read_file_content`` for the
  rest of the run, and a refusal says what it is (a spent window budget, an
  ambiguous name), never "no file attached" for a file that exists;
* an agent reads the traces of its own earlier runs, and still no other
  agent's.
"""

from __future__ import annotations

import base64
import io
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.services.builtin_tools.traces as traces_module
from app.llm.base import FileRef
from app.services.builtin_tools import _shared as shared
from app.services.builtin_tools import (
    build_code_interpreter_tool,
    build_generate_file_tool,
    media,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolCall, ToolDefinition, ToolResult

REPORT_MD = (
    "# Incident Response Report\n\n"
    "## Affected Assets\n\n"
    "| Host | Technique | Status |\n"
    "| --- | --- | --- |\n"
    "| srv-web-01 | T1059 | Contained |\n"
    "| srv-db-02 | T1021 | Monitoring |\n"
    "| wks-fin-07 | T1566 | Eradicated |\n\n"
    "Containment completed \U0001f512 at 14:05 UTC.\n"
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _ref(data: bytes, filename: str, content_type: str, source: str = "agent_download") -> FileRef:
    ref = FileRef.from_bytes(data, filename, content_type)
    ref.source = source
    return ref


def _build(monkeypatch, persisted: list[dict], **kwargs):
    """The file_generation suite with the persistence seam captured in memory."""

    async def _fake_persist(**persist_kwargs):
        persisted.append(persist_kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=persist_kwargs["filename"],
            content_type=persist_kwargs["content_type"],
            size=len(persist_kwargs["raw_bytes"]),
            s3_object_name=f"chat/conv/{uuid.uuid4().hex}-{persist_kwargs['filename']}",
            source=persist_kwargs["source"],
        )

    monkeypatch.setattr("app.services.builtin_tools._persist_generated_file", _fake_persist)
    kwargs.setdefault("conversation_id", str(uuid.uuid4()))
    return build_generate_file_tool(db=None, user_id=str(uuid.uuid4()), **kwargs)


async def _call(executor, name: str, **arguments) -> ToolResult:
    return await executor.execute_tool(ToolCall(id=f"call-{name}", name=name, arguments=arguments))


def _capture_render(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        calls.append({"content": content, "title": title})
        return b"%PDF-1.4\n%fake\n%%EOF"

    monkeypatch.setattr("app.services.builtin_tools._render_text_to_pdf", _fake_render)
    return calls


# ── Fix 1: generate_file(source_file_id=...) ─────────────────────────────────


def _chromium_or_skip(exc: Exception) -> None:
    text = str(exc)
    if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
        raise exc
    if os.environ.get("REQUIRE_CHROMIUM") == "1":
        pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
    pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


@pytest.mark.chromium
@pytest.mark.asyncio
async def test_generate_file_source_file_id_pdf(monkeypatch):
    """The real renderer: headings and table cells come out as a document,
    never as the literal ``#`` and ``|`` of the Markdown source."""
    from pypdf import PdfReader

    from app.services import pdf_render

    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    source = _ref(REPORT_MD.encode("utf-8"), "ir-report.md", "text/markdown", source="agent_generated")
    ctx.add(source)
    executor.file_context = ctx

    try:
        await pdf_render.render_text_to_pdf("probe")
    except Exception as exc:
        _chromium_or_skip(exc)

    result = await _call(executor, "generate_file", source_file_id=source.file_id, filename="doc.pdf")

    assert result.is_error is False, result.content
    assert result.content.startswith("[[FILE:") and result.content.endswith("]]")
    raw = persisted[0]["raw_bytes"]
    assert raw.startswith(b"%PDF-")
    assert persisted[0]["content_type"] == "application/pdf"
    reader = PdfReader(io.BytesIO(raw))
    assert len(reader.pages) >= 1
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    for expected in ("Incident Response Report", "Affected Assets", "srv-web-01", "T1059", "Monitoring", "wks-fin-07"):
        assert expected in text, f"{expected!r} missing from the rendered PDF text"
    assert "#" not in text, "a Markdown heading marker survived the render"
    assert "|" not in text, "a Markdown table pipe survived the render"


@pytest.mark.asyncio
async def test_generate_file_source_file_id_renders_exactly_what_content_would(monkeypatch):
    """Same renderer input, same title, same stored type and the same marker
    shape: the source file is only a way not to re-send the text."""
    render_calls = _capture_render(monkeypatch)
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    source = _ref(REPORT_MD.encode("utf-8"), "ir-report.md", "text/markdown")

    def _turn() -> FileContext:
        ctx = FileContext()
        ctx.add(source)
        executor.file_context = ctx
        return ctx

    content_ctx = _turn()
    by_content = await _call(executor, "generate_file", content=REPORT_MD, filename="doc.pdf", file_tag="download_file")
    source_ctx = _turn()
    by_source = await _call(
        executor, "generate_file", source_file_id=source.file_id, filename="doc.pdf", file_tag="download_file"
    )

    assert by_content.is_error is False and by_source.is_error is False
    assert render_calls[0] == render_calls[1] == {"content": REPORT_MD, "title": "doc"}
    for key in ("filename", "content_type", "source", "raw_bytes"):
        assert persisted[0][key] == persisted[1][key], key
    for result, ctx in ((by_content, content_ctx), (by_source, source_ctx)):
        assert result.content == f"[[FILE:{result.files[0].file_id}]]"
        assert ctx.get(result.files[0].file_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("handle", ["marker", "filename", "prefixed"])
async def test_generate_file_source_file_id_takes_every_handle_the_read_tools_take(monkeypatch, handle):
    render_calls = _capture_render(monkeypatch)
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    source = _ref(b"# Minutes\n\n- item", "minutes.md", "application/octet-stream")
    ctx.add(source)
    executor.file_context = ctx
    spelled = {
        "marker": f"[[FILE:{source.file_id}]]",
        "filename": "minutes.md",
        "prefixed": f"file-{source.file_id}",
    }[handle]

    result = await _call(executor, "generate_file", source_file_id=spelled, filename="minutes.pdf")

    assert result.is_error is False, result.content
    assert render_calls[0]["content"] == "# Minutes\n\n- item"


@pytest.mark.asyncio
async def test_generate_file_source_file_id_feeds_the_docx_and_text_paths_too(monkeypatch):
    docx_calls: list[str] = []

    async def _fake_docx(content: str, *, title: str | None = None) -> bytes:
        docx_calls.append(content)
        return b"PK\x03\x04docx"

    monkeypatch.setattr("app.services.builtin_tools._render_text_to_docx", _fake_docx)
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    source = _ref("\ufeff<h1>Inventaire</h1>".encode(), "inventory.html", "text/html")
    ctx.add(source)
    executor.file_context = ctx

    docx = await _call(executor, "generate_file", source_file_id=source.file_id, filename="inventory.docx")
    copy = await _call(executor, "generate_file", source_file_id=source.file_id, filename="inventory-copy.html")

    assert docx.is_error is False and copy.is_error is False
    assert docx_calls == ["<h1>Inventaire</h1>"], "the BOM is not part of the document"
    assert persisted[1]["raw_bytes"] == b"<h1>Inventaire</h1>"


@pytest.mark.asyncio
async def test_generate_file_source_file_id_and_content_exclusive(monkeypatch):
    render_calls = _capture_render(monkeypatch)
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    source = _ref(REPORT_MD.encode(), "ir-report.md", "text/markdown")
    ctx.add(source)
    executor.file_context = ctx

    both = await _call(executor, "generate_file", filename="doc.pdf", content="# Other", source_file_id=source.file_id)
    neither = await _call(executor, "generate_file", filename="doc.pdf")
    base64_source = await _call(
        executor, "generate_file", filename="doc.pdf", source_file_id=source.file_id, encoding="base64"
    )

    for result in (both, neither, base64_source):
        assert result.is_error is True
        assert result.content.startswith("Error:")
    assert "exactly one of content" in both.content
    assert "content or source_file_id is required" in neither.content
    assert "encoding='base64' cannot be combined with source_file_id" in base64_source.content
    assert render_calls == [] and persisted == []

    # An empty content next to a source is an unused optional field, not a second input.
    blank = await _call(executor, "generate_file", filename="doc.pdf", content="", source_file_id=source.file_id)
    assert blank.is_error is False
    # And an empty content alone is still the empty file it always was.
    empty = await _call(executor, "generate_file", filename="empty.txt", content="")
    assert empty.is_error is False and persisted[-1]["raw_bytes"] == b""


@pytest.mark.asyncio
async def test_generate_file_source_file_id_rejects_binary(monkeypatch):
    render_calls = _capture_render(monkeypatch)
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    png = _ref(_PNG, "diagram.png", "image/png")
    pdf = _ref(b"%PDF-1.4\n%%EOF", "previous.pdf", "application/pdf")
    disguised = _ref(b"\x00\x01binary", "notes.md", "text/markdown")
    for ref in (png, pdf, disguised):
        ctx.add(ref)
    executor.file_context = ctx
    monkeypatch.setattr(shared, "_own_session", lambda: _FakeRowSession([], []))

    image = await _call(executor, "generate_file", source_file_id=png.file_id, filename="doc.pdf")
    rendered = await _call(executor, "generate_file", source_file_id=pdf.file_id, filename="doc.pdf")
    binary = await _call(executor, "generate_file", source_file_id=disguised.file_id, filename="doc.pdf")
    unknown = await _call(executor, "generate_file", source_file_id=str(uuid.uuid4()), filename="doc.pdf")

    assert image.is_error and "'diagram.png' (image/png) is not a Markdown, HTML or plain text file" in image.content
    assert rendered.is_error and "write its Markdown to a file first" in rendered.content
    assert binary.is_error and "holds binary data, not text" in binary.content
    assert unknown.is_error and "is attached to this conversation" in unknown.content
    assert render_calls == [] and persisted == []


def test_generate_file_schema_and_description_name_source_file_id(monkeypatch):
    defs, _executor = _build(monkeypatch, [])
    generate = next(d for d in defs if d.name == "generate_file")
    assert generate.input_schema["required"] == ["filename"]
    assert "source_file_id" in generate.input_schema["properties"]
    first_paragraph = generate.description.split("\n", 1)[0]
    assert (
        "To change a text file that already exists, use edit_file_content; to render an existing "
        ".md/.html file to PDF or DOCX, pass source_file_id."
    ) in first_paragraph


# ── Fix 2 and 3: the sandbox guardrail and the code_interpreter description ──

_PDFPAGES_CODE = """
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

with PdfPages("report.pdf") as pdf:
    fig = plt.figure(figsize=(8.27, 11.69))
    fig.text(0.05, 0.95, "Incident Response Report", family="DejaVu Sans", size=7)
    pdf.savefig(fig)
"""

_CHART_CODE = """
import matplotlib.pyplot as plt

plt.bar(["a", "b"], [1, 2])
plt.savefig("chart.png")
"""


def _sandbox_returning(monkeypatch, filename: str, content_type: str, data: bytes) -> None:
    async def _fake_execute(_code, timeout=300, user_id=None, quota_user_id=None, db=None):
        return {
            "stdout": "",
            "stderr": "",
            "error": None,
            "images": [],
            "output_files": [
                {
                    "filename": filename,
                    "data_base64": base64.b64encode(data).decode("ascii"),
                    "content_type": content_type,
                    "size": len(data),
                }
            ],
        }

    async def _fake_persist(**kwargs):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name=f"chat/conv/{kwargs['filename']}",
            source=kwargs["source"],
        )

    monkeypatch.setattr("app.services.code_interpreter.execute_python", _fake_execute)
    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _fake_persist)


@pytest.mark.asyncio
async def test_code_interpreter_sandbox_pdf_warning(monkeypatch):
    _sandbox_returning(monkeypatch, "report.pdf", "application/pdf", b"%PDF-1.4\n%%EOF")
    _defs, executor = build_code_interpreter_tool(db=object(), user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    executor.file_context = FileContext()

    result = await _call(executor, "code_interpreter", code=_PDFPAGES_CODE)

    assert result.is_error is False
    assert media.SANDBOX_PDF_WARNING in result.content
    assert result.content.count("WARNING:") == 1
    # The run and its file stand: the warning rides next to the marker.
    assert result.files and result.files[0].filename == "report.pdf"
    assert f"[[FILE:{result.files[0].file_id}]]" in result.content

    _sandbox_returning(monkeypatch, "chart.png", "image/png", _PNG)
    chart = await _call(executor, "code_interpreter", code=_CHART_CODE)
    assert chart.is_error is False
    assert "WARNING:" not in chart.content


@pytest.mark.asyncio
async def test_the_warning_rides_a_run_that_persists_nothing_too(monkeypatch):
    _sandbox_returning(monkeypatch, "report.pdf", "application/pdf", b"%PDF-1.4\n%%EOF")
    _defs, executor = build_code_interpreter_tool()

    result = await _call(executor, "code_interpreter", code=_PDFPAGES_CODE)

    content = result.content if isinstance(result, ToolResult) else str(result)
    assert media.SANDBOX_PDF_WARNING in content


@pytest.mark.parametrize(
    ("code", "filename", "drawn"),
    [
        (_PDFPAGES_CODE, "report.pdf", True),
        ("from reportlab.pdfgen import canvas\ncanvas.Canvas('r.pdf').save()", "r.pdf", True),
        ("from fpdf import FPDF\nFPDF().output('r.pdf')", "r.pdf", True),
        ("import weasyprint\nweasyprint.HTML(string='x').write_pdf('r.pdf')", "r.pdf", True),
        ("from pypdf import PdfWriter\nw = PdfWriter()\nw.write('r.pdf')", "r.pdf", True),
        ("import pypdf; pypdf.PdfWriter().write('r.pdf')", "r.pdf", True),
        # A chart saved as a PDF figure is what the sandbox PDF output is for.
        ("import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.savefig('chart.pdf')", "chart.pdf", False),
        # Reading a PDF with pypdf and writing a CSV writes no PDF.
        ("from pypdf import PdfReader\nopen('t.csv', 'w').write('a')", "t.csv", False),
        (_PDFPAGES_CODE, "report.png", False),
    ],
)
def test_sandbox_drew_pdf_document(code, filename, drawn):
    output = [{"filename": filename, "content_type": "application/octet-stream"}]
    assert media.sandbox_drew_pdf_document(code, output) is drawn


def test_the_pdf_writer_patterns_are_one_extendable_list():
    assert r"\bPdfPages\b" in media.SANDBOX_PDF_WRITER_PATTERNS
    assert media.sandbox_drew_pdf_document("from matplotlib.backends.backend_pdf import x", [{"filename": "a.PDF"}])
    assert media.sandbox_drew_pdf_document("import REPORTLAB", [{"filename": "x", "content_type": "application/pdf"}])


def test_code_interpreter_description_states_the_two_pdf_paths():
    (definition,), _executor = build_code_interpreter_tool()
    assert (
        "matplotlib PDF output is for chart figures only; documents (reports, inventories, minutes) are "
        "rendered by generate_file from Markdown or HTML, never drawn in the sandbox."
    ) in definition.description


# ── Fix 4: prior-artifact read-back ──────────────────────────────────────────


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeRowSession:
    """The short session ``load_scoped_file_ref`` opens, recording the statement."""

    def __init__(self, rows, statements: list):
        self._rows = rows
        self._statements = statements

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement):
        self._statements.append(statement)
        return _FakeResult(self._rows)


def _row(filename: str, content_type: str, **scope) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        filename=filename,
        content_type=content_type,
        size=1234,
        s3_object_name=f"assignments/{uuid.uuid4().hex}/{filename}",
        source="agent_download",
        description=None,
        **scope,
    )


@pytest.mark.asyncio
async def test_read_file_content_resolves_a_file_of_this_run_the_context_never_held(monkeypatch):
    """The context is what the loop knows it produced; the run's rows are what
    it produced. A file bound to this run that the context missed is found,
    registered, and read - never answered with "no file attached"."""
    run_id = str(uuid.uuid4())
    row = _row("IR-Report-2026-09-28.md", "text/markdown", assignment_run_id=uuid.UUID(run_id))
    statements: list = []
    monkeypatch.setattr(shared, "_own_session", lambda: _FakeRowSession([row], statements))

    async def _fake_download(_object_name):
        return REPORT_MD.encode("utf-8")

    monkeypatch.setattr("app.services.file_storage.file_storage.download", _fake_download)
    _defs, executor = _build(monkeypatch, [], conversation_id=None, assignment_run_id=run_id)
    ctx = FileContext()
    executor.file_context = ctx

    result = await _call(executor, "read_file_content", file_id=f"[[FILE:{row.id}]]")

    assert result.is_error is False, result.content
    assert "Incident Response Report" in result.content
    assert ctx.get(str(row.id)) is not None, "registered for the rest of the run"
    where = str(statements[0].whereclause)
    assert "file_attachments.id = " in where
    assert "file_attachments.assignment_run_id = " in where
    assert "file_attachments.template_container_id IS NULL" in where
    assert "conversation_id" not in where

    # Registered once: the next read is the context's, no second lookup.
    again = await _call(executor, "read_file_content", file_id=str(row.id))
    assert again.is_error is False and len(statements) == 1


@pytest.mark.asyncio
async def test_the_scoped_lookup_reads_nothing_outside_this_conversation_or_run(monkeypatch):
    statements: list = []
    monkeypatch.setattr(shared, "_own_session", lambda: _FakeRowSession([], statements))

    # No scope at all, or a filename: nothing is looked up.
    assert await shared.load_scoped_file_ref(str(uuid.uuid4()), conversation_id=None, assignment_run_id=None) is None
    assert await shared.load_scoped_file_ref("report.pdf", conversation_id=str(uuid.uuid4()), assignment_run_id=None) is None
    assert statements == []

    conversation_id = str(uuid.uuid4())
    missing = await shared.load_scoped_file_ref(str(uuid.uuid4()), conversation_id=conversation_id, assignment_run_id=None)
    assert missing is None
    where = str(statements[0].whereclause)
    assert "file_attachments.conversation_id = " in where
    assert "assignment_run_id" not in where

    _defs, executor = _build(monkeypatch, [], conversation_id=conversation_id)
    executor.file_context = FileContext()
    refused = await _call(executor, "read_file_content", file_id=str(uuid.uuid4()))
    assert refused.is_error is True
    assert "is attached to this conversation" in refused.content


@pytest.mark.asyncio
async def test_a_failing_lookup_is_a_refusal_never_a_crash(monkeypatch):
    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("pool exhausted")

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(shared, "_own_session", lambda: _Broken())
    _defs, executor = _build(monkeypatch, [], assignment_run_id=str(uuid.uuid4()))
    executor.file_context = FileContext()

    result = await _call(executor, "read_file_content", file_id=str(uuid.uuid4()))

    assert result.is_error is True and "is attached to this conversation" in result.content


def _as_provider(script):
    from tests.test_tool_call_dedup import _ScriptedLLM as _Provider

    return _Provider(script)


@pytest.mark.asyncio
async def test_read_file_content_survives_later_steps(monkeypatch):
    """A file a download tool attached at step 1 is read back at step 6, after
    other steps and with the working-context pruning at its most aggressive:
    pruning takes tool bodies and attachments out of the transcript, never the
    file out of the run."""
    from app.llm.base import ChatMessage, ChatResponse
    from app.tools import agent_loop as al
    from app.tools.base import CompositeToolExecutor

    for name in ("_lf_start_llm_gen", "_lf_end_generation", "_lf_end_span", "_lf_end_tool_span"):
        monkeypatch.setattr(al, name, lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_loop_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_compute_live_tool_result_budget", lambda _window: 1)
    monkeypatch.setattr(al, "_compute_tool_result_files_budget", lambda _window: 1)

    previous = _ref(REPORT_MD.encode("utf-8"), "IR-Report-previous.md", "text/markdown")

    async def _download_previous_report() -> ToolResult:
        # Like an MCP binary result: the file rides on the result, the tool
        # itself registers nothing - the loop does.
        return ToolResult(
            tool_call_id="",
            content=f"Downloaded 'IR-Report-previous.md'. [[FILE:{previous.file_id}]]",
            files=[previous],
        )

    async def _capture_console(page: int = 1) -> ToolResult:
        # Newer file-bearing results and large bodies: what makes the
        # retention passes take the download's attachment and older bodies.
        return ToolResult(
            tool_call_id="",
            content=f"console page {page}: " + ("alert " * 1_500),
            files=[_ref(_PNG + bytes([page]), f"console-{page}.png", "image/png")],
        )

    other = CallableToolExecutor()
    other.register("download_previous_report", _download_previous_report)
    other.register("capture_console", _capture_console)
    other_defs = [
        ToolDefinition(name="download_previous_report", description="d", input_schema={"type": "object", "properties": {}}),
        ToolDefinition(
            name="capture_console",
            description="c",
            input_schema={"type": "object", "properties": {"page": {"type": "integer"}}},
        ),
    ]
    file_defs, file_executor = _build(monkeypatch, [])
    composite = CompositeToolExecutor()
    composite.add_tools(other_defs, other)
    composite.add_tools(file_defs, file_executor)

    def _step(i: int, name: str, arguments: dict) -> ChatResponse:
        # Substantive prose between batches releases the earlier results to
        # the ordinary budget (the current-turn protection holds them until then).
        return ChatResponse(
            content=f"Step {i}: " + "reviewing the incident timeline before writing the report. " * 20,
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"s{i}", name=name, arguments=arguments)],
        )

    script = [
        _step(1, "download_previous_report", {}),
        *(_step(i, "capture_console", {"page": i}) for i in range(2, 6)),
        _step(6, "read_file_content", {"file_id": previous.file_id}),
        ChatResponse(content="Report updated.", model="stub"),
    ]
    ctx = FileContext()
    messages: list[ChatMessage] = [ChatMessage(role="user", content="update the incident report")]
    response = await al.run_agent_loop(
        llm=_as_provider(script),
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=12,
        file_context=ctx,
    )

    assert response.content == "Report updated."
    tool_results = {m.tool_call_id: m for m in messages if m.role == "tool"}
    # The retention passes did run: the download lost its attachment, the
    # older bodies were stubbed.
    assert tool_results["s1"].files is None
    assert "removed from the working context" in tool_results["s1"].content
    assert tool_results["s2"].content.startswith("[Earlier capture_console result")
    read = tool_results["s6"]
    assert "no file" not in read.content
    assert "Incident Response Report" in read.content
    assert "srv-web-01" in read.content
    assert ctx.get(previous.file_id) is not None


@pytest.mark.asyncio
async def test_read_file_content_window_budget_message(monkeypatch):
    from app.core.config import settings
    from app.services.code_interpreter import mount_path_hint
    from app.tools.base import offering_tools

    monkeypatch.setattr(settings, "agent_loop_read_windows_per_file", 2)
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    text = "".join(f"line {i:05d}\n" for i in range(3_000))
    ref = _ref(text.encode(), "timeline.txt", "text/plain")
    ctx.add(ref)
    executor.file_context = ctx

    with offering_tools(build_code_interpreter_tool()[0]):
        for offset in (0, 1_000):
            served = await _call(executor, "read_file_content", file_id=ref.file_id, offset=offset, max_chars=1_000)
            assert served.is_error is False
        refused = await _call(executor, "read_file_content", file_id=ref.file_id, offset=2_000, max_chars=1_000)

    assert refused.is_error is True
    assert refused.content.startswith("Error: window budget for this file exhausted this turn, 2 of 2 served")
    assert "use code_interpreter to process the whole file" in refused.content
    assert mount_path_hint(ref.filename, ref.file_id) in refused.content
    assert f"The file is still attached (file_id={ref.file_id})" in refused.content
    assert "no file" not in refused.content


@pytest.mark.asyncio
async def test_an_ambiguous_filename_names_the_candidates_instead_of_no_file(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    older = _ref(b"# v1", "IR-Report.md", "text/markdown")
    newer = _ref(b"# v2", "IR-Report.md", "text/markdown")
    ctx.add(older)
    ctx.add(newer)
    executor.file_context = ctx

    for tool in ("read_file_content", "edit_file_content", "annotate_file"):
        arguments = {"file_id": "IR-Report.md"}
        if tool == "edit_file_content":
            arguments["edits"] = [{"find": "v1", "replace": "v3"}]
        if tool == "annotate_file":
            arguments["notes"] = "the report"
        result = await _call(executor, tool, **arguments)
        assert result.is_error is True, tool
        assert "2 files of this conversation are named 'IR-Report.md'" in result.content
        assert result.content.index(older.file_id) < result.content.index(newer.file_id)
        assert "no file" not in result.content


def test_the_available_files_listing_keeps_the_newest_files():
    ctx = FileContext()
    refs = [_ref(b"x", f"file-{i:02d}.txt", "text/plain") for i in range(25)]
    for ref in refs:
        ctx.add(ref)

    listing = shared.list_available_files(ctx)

    assert "5 earlier file(s) not listed" in listing
    assert "file-24.txt" in listing and "file-05.txt" in listing
    assert "file-04.txt" not in listing


# ── Fix 4: own-trace visibility ──────────────────────────────────────────────


class _SessionCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, _entry):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None


class _VisibilityStore:
    """A trace store that applies the per-user visibility the way the real one
    does: a non-admin reader sees only the traces listed as visible."""

    def __init__(self, traces: dict, visible: set):
        self._traces = traces
        self._visible = visible
        self.readers: list[bool] = []

    async def get_trace_detail(self, _db, *, user, user_group_ids, trace_id):
        self.readers.append(bool(user.is_admin))
        trace = self._traces.get(trace_id)
        if trace is None or (not user.is_admin and trace_id not in self._visible):
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace, None, None, None

    async def get_trace_span(self, _db, *, user, user_group_ids, trace_id, span_id):
        if not user.is_admin and trace_id not in self._visible:
            raise HTTPException(status_code=404, detail="Trace not found")
        return SimpleNamespace(id=span_id, output={"text": "span output"}, input=None, metadata_json=None, tool_arguments=None)


def _install_trace_store(monkeypatch, store) -> list[str]:
    audits: list[str] = []
    monkeypatch.setattr(traces_module, "async_session", lambda: _SessionCtx())

    async def _user(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), email="run@example.com", is_admin=False), []

    async def _audit(_db, **kwargs):
        audits.append(kwargs.get("action", ""))

    import app.services.audit as audit_module

    monkeypatch.setattr(audit_module, "log_audit", _audit)

    async def _no_run(_db, _value, *, own_agent_id):
        return None, None

    monkeypatch.setattr(traces_module, "_load_user_context", _user)
    monkeypatch.setattr(traces_module, "_own_trace_of_run", _no_run)
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: store)
    return audits


def _trace(agent_id: str, output: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.UUID(agent_id), input=None, output=output, metadata_json=None)


@pytest.mark.asyncio
async def test_trace_payload_visible_to_same_agent(monkeypatch):
    """The run's user cannot see the trace of the agent's previous run (a
    company-managed agent run on someone's behalf): the agent still reads it,
    because it is its own. Another agent's trace the user cannot see stays
    "not found", and one the user can see stays refused without a grant."""
    me, them = str(uuid.uuid4()), str(uuid.uuid4())
    mine = _trace(me, "previous IR report markdown")
    hidden_other = _trace(them, "someone else's hidden run")
    visible_other = _trace(them, "someone else's visible run")
    store = _VisibilityStore(
        {t.id: t for t in (mine, hidden_other, visible_other)},
        visible={visible_other.id},
    )
    audits = _install_trace_store(monkeypatch, store)
    _defs, executor = traces_module.build_trace_tools(db=object(), user_id=str(uuid.uuid4()), agent_id=me)

    own = await _call(executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=str(mine.id), field="output")
    own_span = await _call(
        executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=str(mine.id), span_id=str(uuid.uuid4()), field="output"
    )
    hidden = await _call(executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=str(hidden_other.id), field="output")
    visible = await _call(executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=str(visible_other.id), field="output")

    assert own.is_error is False, own.content
    assert json.loads(own.content)["content"] == "previous IR report markdown"
    assert own_span.is_error is False and "span output" in json.loads(own_span.content)["content"]
    assert hidden.is_error is True and "trace not found, or not visible to you" in hidden.content
    assert "someone else" not in hidden.content
    assert visible.is_error is True and "not allowed to read payloads for this trace" in visible.content
    assert audits == [], "reading its own trace is no cross-agent read"


@pytest.mark.asyncio
async def test_trace_payload_of_another_agent_is_unchanged_for_a_non_admin_agent(monkeypatch):
    other_agent = str(uuid.uuid4())
    hidden = _trace(other_agent, "hidden")
    store = _VisibilityStore({hidden.id: hidden}, visible=set())
    _install_trace_store(monkeypatch, store)
    # An agent with NO id (the /mcp/xtm server) has no own traces to widen to.
    _defs, executor = traces_module.build_trace_tools(db=object(), user_id=str(uuid.uuid4()), agent_id=None)

    result = await _call(executor, traces_module.TRACE_PAYLOAD_TOOL_NAME, trace_id=str(hidden.id))

    assert result.is_error is True and "not found, or not visible" in result.content
    assert store.readers == [False], "no second, visibility-free read without a calling agent"


@pytest.mark.asyncio
async def test_an_agent_lists_its_own_traces_whatever_the_user_sees(monkeypatch):
    me = str(uuid.uuid4())
    captured: list[dict] = []
    _install_trace_store(monkeypatch, _VisibilityStore({}, set()))

    async def _fake_fetch(_db, **kwargs):
        captured.append(kwargs)
        return '{"count": 0, "traces": []}'

    monkeypatch.setattr(traces_module, "_fetch_traces", _fake_fetch)
    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=me, supervision_target_agent_ids=[]
    )

    mine = await _call(executor, "inspect_my_traces")
    self_target = await _call(executor, "inspect_agent_traces", target_agent_id=me.upper())
    other = await _call(executor, "inspect_agent_traces", target_agent_id=str(uuid.uuid4()))

    assert mine.is_error is False and self_target.is_error is False
    assert other.is_error is True and "not allowed to inspect traces" in other.content
    assert [c["agent_ids"] for c in captured] == [[me], [me]]
    assert all(c["user"].is_admin for c in captured), "own traces are read past the per-user visibility"


def test_a_self_target_needs_no_supervision_grant():
    me = str(uuid.uuid4())
    assert traces_module._supervision_scope(
        admin_bypass=False, target_uuid=me, supervision_allowlist=set(), own_agent_id=me
    ) == [me]
    with pytest.raises(traces_module.SupervisionDeniedError):
        traces_module._supervision_scope(
            admin_bypass=False, target_uuid=str(uuid.uuid4()), supervision_allowlist=set(), own_agent_id=me
        )


# ── pdf_render regression: Markdown is still converted ──────────────────────


@pytest.mark.asyncio
async def test_render_text_to_pdf_hands_chromium_a_converted_markdown_table(monkeypatch):
    """The renderer is not what broke: a Markdown table reaches Chromium as a
    real ``<table>``, headings as ``<h1>`` / ``<h2>``, never as ``#`` / ``|``."""
    import playwright.async_api as pw_api

    from app.services import pdf_render

    seen: dict = {}

    class _Page:
        async def set_content(self, html, wait_until=None):
            seen["html"] = html

        async def pdf(self, **kwargs):
            seen["pdf_kwargs"] = kwargs
            return b"%PDF-1.4\n%%EOF"

    class _Context:
        async def route(self, _pattern, _handler):
            return None

        async def new_page(self):
            return _Page()

        async def close(self):
            return None

    class _Browser:
        async def new_context(self, **kwargs):
            seen["context_kwargs"] = kwargs
            return _Context()

        async def close(self):
            return None

    class _Chromium:
        async def launch(self, **kwargs):
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(pw_api, "async_playwright", lambda: _Playwright())

    pdf = await pdf_render.render_text_to_pdf(REPORT_MD, title="doc")

    assert pdf.startswith(b"%PDF-")
    html = seen["html"]
    assert "<table>" in html and "<td>srv-web-01</td>" in html
    assert "<h1>Incident Response Report</h1>" in html and "<h2>Affected Assets</h2>" in html
    assert "| srv-web-01 |" not in html and "# Incident" not in html
    assert seen["context_kwargs"]["java_script_enabled"] is False
    assert seen["pdf_kwargs"]["format"] == pdf_render.PDF_RENDER_FORMAT
