"""Tests for OpenCTI ``upload_opencti_file`` server-side PDF rendering.

The OpenCTI Assistant attaches summaries / reports to OpenCTI entities via
``upload_opencti_file``. Previously the tool only UTF-8-encoded whatever text
the agent passed, so asking for a PDF produced a Markdown file with a ``.pdf``
name (or, more often, the agent fell back to ``.md`` and told the user PDF was
unsupported). The tool now renders Markdown / HTML to a real PDF via the shared
Playwright renderer when the target is a PDF.

These tests pin:
  * ``.pdf`` file name (text content) → content routed through
    ``pdf_render.render_text_to_pdf`` and the *rendered PDF bytes* are uploaded
    with ``mime_type='application/pdf'``.
  * ``application/pdf`` mime type behaves the same even with a non-``.pdf`` name.
  * Non-PDF targets (e.g. ``.md``) are uploaded verbatim as UTF-8 — the PDF
    renderer is never invoked.
  * ``encoding='base64'`` for a ``.pdf`` is treated as pre-rendered binary and
    NOT re-rendered.
  * A renderer failure surfaces a friendly error string (no crash) and skips
    the upload.
"""

from __future__ import annotations

import base64

import pytest

from app.integrations.providers import opencti
from app.integrations.providers.opencti import knowledge as opencti_knowledge
from app.llm.base import FileRef
from app.services import pdf_render
from app.tools.base import CallableToolExecutor, FileContext, ToolCall
from tests.integration_file_fakes import HOSTILE_FILE_NAMES, assert_name_only_inside, fenced_and_open

_TOKEN = "https://demo.opencti.io||test-token"
_MARKDOWN = "# Intelligence Summary\n\n| IOC | Type |\n|---|---|\n| evil.com | domain |"


def _build_upload_executor():
    executor = CallableToolExecutor()
    opencti.build_tools(_TOKEN, executor)
    return executor


def _patch_upload_capture(monkeypatch) -> dict:
    """Capture the bytes / mime the tool hands to the GraphQL multipart upload."""
    captured: dict = {}

    async def _fake_upload(
        client,
        base_url,
        auth_headers,
        query,
        variables,
        file_var_path,
        file_name,
        file_bytes,
        mime_type,
    ):
        captured["file_name"] = file_name
        captured["file_bytes"] = file_bytes
        captured["mime_type"] = mime_type
        return (
            {
                "stixCoreObjectEdit": {
                    "importPush": {
                        "id": "file-1",
                        "name": file_name,
                        "uploadStatus": "complete",
                        "metaData": {"mimetype": mime_type},
                    }
                }
            },
            None,
        )

    # ``upload_opencti_file`` lives in the ``opencti.knowledge`` submodule after
    # the package refactor and calls the module-global ``_graphql_upload`` (it is
    # imported there from ``_common``), so patch it on the knowledge module.
    monkeypatch.setattr(opencti_knowledge, "_graphql_upload", _fake_upload)
    return captured


@pytest.mark.asyncio
async def test_upload_pdf_renders_markdown_to_pdf(monkeypatch):
    captured = _patch_upload_capture(monkeypatch)

    render_calls: list[dict] = []

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        render_calls.append({"content": content, "title": title})
        return b"%PDF-1.4\n%fake-rendered\n%%EOF"

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fake_render)

    executor = _build_upload_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "file_name": "Intelligence_Summary.pdf",
                "file_content": _MARKDOWN,
            },
        )
    )

    assert result.is_error is False
    # The agent's Markdown must have been routed through the renderer.
    assert render_calls, "PDF renderer must be invoked for a .pdf upload"
    assert "Intelligence Summary" in render_calls[0]["content"]
    assert render_calls[0]["title"] == "Intelligence_Summary"
    # The *rendered PDF bytes* — not the raw Markdown — must be uploaded.
    assert captured["file_bytes"].startswith(b"%PDF-")
    assert captured["mime_type"] == "application/pdf"
    assert "uploaded successfully" in result.content.lower()


@pytest.mark.asyncio
async def test_upload_pdf_via_mime_type_with_non_pdf_name(monkeypatch):
    captured = _patch_upload_capture(monkeypatch)

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        return b"%PDF-1.4\n%via-mime\n%%EOF"

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fake_render)

    executor = _build_upload_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "file_name": "summary",
                "file_content": _MARKDOWN,
                "mime_type": "application/pdf",
            },
        )
    )

    assert result.is_error is False
    assert captured["file_bytes"].startswith(b"%PDF-")
    assert captured["mime_type"] == "application/pdf"
    # The bare name is normalised to a ``.pdf`` extension so clients/UIs open
    # the attachment correctly.
    assert captured["file_name"] == "summary.pdf"


@pytest.mark.asyncio
async def test_upload_markdown_not_rendered(monkeypatch):
    captured = _patch_upload_capture(monkeypatch)

    async def _fail_render(*_args, **_kwargs):
        raise AssertionError("renderer must NOT run for a non-PDF upload")

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fail_render)

    executor = _build_upload_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "file_name": "summary.md",
                "file_content": _MARKDOWN,
                "mime_type": "text/markdown",
            },
        )
    )

    assert result.is_error is False
    # Markdown uploaded verbatim as UTF-8 bytes.
    assert captured["file_bytes"] == _MARKDOWN.encode("utf-8")
    assert captured["mime_type"] == "text/markdown"


@pytest.mark.asyncio
async def test_upload_pdf_base64_not_rerendered(monkeypatch):
    captured = _patch_upload_capture(monkeypatch)

    async def _fail_render(*_args, **_kwargs):
        raise AssertionError("pre-rendered base64 PDF must NOT be re-rendered")

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fail_render)

    raw_pdf = b"%PDF-1.7\n%already-binary\n%%EOF"
    encoded = base64.b64encode(raw_pdf).decode("ascii")

    executor = _build_upload_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "file_name": "prebuilt.pdf",
                "file_content": encoded,
                "encoding": "base64",
                "mime_type": "application/pdf",
            },
        )
    )

    assert result.is_error is False
    assert captured["file_bytes"] == raw_pdf


@pytest.mark.asyncio
async def test_upload_pdf_base64_sets_pdf_mime(monkeypatch):
    """A pre-rendered base64 PDF whose ``.pdf`` name implies a PDF — but whose
    ``mime_type`` was left at the default — must be uploaded as
    ``application/pdf`` (not ``application/octet-stream``), matching the
    text-render branch so the attachment metadata is correct."""
    captured = _patch_upload_capture(monkeypatch)

    async def _fail_render(*_args, **_kwargs):
        raise AssertionError("pre-rendered base64 PDF must NOT be re-rendered")

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fail_render)

    raw_pdf = b"%PDF-1.7\n%already-binary\n%%EOF"
    encoded = base64.b64encode(raw_pdf).decode("ascii")

    executor = _build_upload_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "file_name": "prebuilt.pdf",
                "file_content": encoded,
                "encoding": "base64",
                # mime_type intentionally omitted — defaults to octet-stream.
            },
        )
    )

    assert result.is_error is False
    assert captured["file_bytes"] == raw_pdf
    assert captured["mime_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_upload_base64_invalid_is_rejected(monkeypatch):
    """Malformed base64 must be rejected (validate=True) rather than silently
    decoded with non-alphabet characters dropped and then uploaded."""
    captured = _patch_upload_capture(monkeypatch)

    executor = _build_upload_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "file_name": "data.bin",
                # '@' / '%' / ' ' are not valid base64 alphabet characters.
                "file_content": "not @ valid base64 %%%",
                "encoding": "base64",
            },
        )
    )

    assert result.is_error is False
    assert "Invalid base64 content" in result.content
    # Nothing was uploaded for invalid input.
    assert "file_bytes" not in captured


@pytest.mark.asyncio
async def test_upload_pdf_render_failure_is_graceful(monkeypatch):
    uploaded: dict = {"called": False}

    async def _fake_upload(*_args, **_kwargs):
        uploaded["called"] = True
        return ({}, None)

    monkeypatch.setattr(opencti_knowledge, "_graphql_upload", _fake_upload)

    async def _boom_render(*_args, **_kwargs):
        raise RuntimeError("chromium not installed")

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _boom_render)

    executor = _build_upload_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "file_name": "summary.pdf",
                "file_content": _MARKDOWN,
            },
        )
    )

    # No crash, no upload — a friendly fallback message instead.
    assert result.is_error is False
    assert uploaded["called"] is False
    assert "PDF rendering failed" in result.content


# ---------------------------------------------------------------------------
# source_file_id — attach a user-uploaded file to an entity BYTE-FOR-BYTE
# ---------------------------------------------------------------------------

_RAW_PDF = b"%PDF-1.7\n%original-uploaded-bytes\n1 0 obj<<>>endobj\n%%EOF"


def _executor_with_file(
    *,
    file_id: str = "up-1",
    filename: str = "threat-report.pdf",
    content_type: str = "application/pdf",
    data: bytes = _RAW_PDF,
):
    """Build an upload executor whose FileContext holds one inline uploaded file."""
    executor = _build_upload_executor()
    ctx = FileContext()
    ctx.add(
        FileRef.from_bytes(
            data=data,
            filename=filename,
            content_type=content_type,
            file_id=file_id,
        )
    )
    # Mirrors run_agent_loop -> executor.set_file_context(...) in production.
    executor.file_context = ctx
    return executor


@pytest.mark.asyncio
async def test_upload_from_source_file_id_forwards_original_bytes(monkeypatch):
    """source_file_id uploads the ORIGINAL bytes verbatim — the renderer is
    never invoked and file_name defaults to the uploaded file's own name."""
    captured = _patch_upload_capture(monkeypatch)

    async def _fail_render(*_args, **_kwargs):
        raise AssertionError("renderer must NOT run for a source_file_id upload")

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fail_render)

    executor = _executor_with_file()
    result = await executor.execute_tool(
        ToolCall(
            id="u1",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "source_file_id": "up-1",
            },
        )
    )

    assert result.is_error is False
    assert captured["file_bytes"] == _RAW_PDF
    assert captured["file_name"] == "threat-report.pdf"
    assert captured["mime_type"] == "application/pdf"
    inside, outside = fenced_and_open(result.content)
    assert outside == (
        "File 'up-1' uploaded successfully.\n  Entity: report--123\n  Status: complete\n"
    )
    assert inside.splitlines()[2:-1] == [
        "[source: OpenCTI file details]",
        "Name: threat-report.pdf",
        "Content type: application/pdf",
        "File ID: file-1",
        "Download URL: https://demo.opencti.io/storage/get/file-1",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_upload_from_source_file_id_names_the_file_only_inside_the_block(monkeypatch, hostile):
    """The stored name, and the OpenCTI file id and link built from it, reach
    the model only in the closing quarantine block."""
    name = HOSTILE_FILE_NAMES[hostile]

    async def _fake_upload(client, base_url, auth_headers, query, variables, path, file_name, data, mime):
        assert file_name == name
        return (
            {
                "stixCoreObjectEdit": {
                    "importPush": {
                        "id": f"import/Report/report--123/{file_name}",
                        "name": file_name,
                        "uploadStatus": "complete",
                        "metaData": {"mimetype": f"text/plain {file_name}"},
                    }
                }
            },
            None,
        )

    monkeypatch.setattr(opencti_knowledge, "_graphql_upload", _fake_upload)
    executor = _executor_with_file(filename=name, content_type="text/plain")
    result = await executor.execute_tool(
        ToolCall(
            id="u-hostile",
            name="upload_opencti_file",
            arguments={"entity_id": "report--123", "source_file_id": "up-1"},
        )
    )
    assert result.is_error is False
    outside = assert_name_only_inside(result.content, name)
    assert outside == "File 'up-1' uploaded successfully.\n  Entity: report--123\n  Status: complete\n"


@pytest.mark.asyncio
async def test_upload_from_source_file_id_honours_override_name(monkeypatch):
    """An explicit file_name renames the attached file while still uploading the
    original uploaded bytes."""
    captured = _patch_upload_capture(monkeypatch)

    async def _fail_render(*_args, **_kwargs):
        raise AssertionError("renderer must NOT run for a source_file_id upload")

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fail_render)

    executor = _executor_with_file()
    result = await executor.execute_tool(
        ToolCall(
            id="u2",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "source_file_id": "up-1",
                "file_name": "renamed.pdf",
            },
        )
    )

    assert result.is_error is False
    assert captured["file_name"] == "renamed.pdf"
    assert captured["file_bytes"] == _RAW_PDF


@pytest.mark.asyncio
async def test_upload_source_file_id_missing_is_rejected(monkeypatch):
    """An unknown source_file_id returns a friendly error and uploads nothing."""
    captured = _patch_upload_capture(monkeypatch)

    executor = _executor_with_file(file_id="up-1")
    result = await executor.execute_tool(
        ToolCall(
            id="u3",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "source_file_id": "missing",
            },
        )
    )

    # Recorded as a failure: nothing was uploaded, so the trace and
    # the per-tool error metrics must not show a success.
    assert result.is_error is True
    assert "not an available uploaded file" in result.content
    assert "file_bytes" not in captured


@pytest.mark.asyncio
async def test_upload_source_file_id_without_context(monkeypatch):
    """Without a FileContext the tool returns a friendly error, not a crash.

    Friendly still means FAILED: the result is flagged ``is_error=True`` so
    the trace, the ``tool_done`` event and the loop's per-tool error counters
    agree that nothing was uploaded.
    """
    captured = _patch_upload_capture(monkeypatch)

    executor = _build_upload_executor()  # no file_context set
    result = await executor.execute_tool(
        ToolCall(
            id="u4",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "source_file_id": "up-1",
            },
        )
    )

    assert result.is_error is True
    assert "no uploaded files are available" in result.content
    assert "file_bytes" not in captured


def test_upload_opencti_file_exposes_source_file_id_param():
    """The upload tool schema must advertise source_file_id and no longer hard-
    require file_content (so the LLM can forward an uploaded file by reference)."""
    executor = CallableToolExecutor()
    tools = opencti.build_tools(_TOKEN, executor)
    by_name = {t.name: t for t in tools}
    schema = by_name["upload_opencti_file"].input_schema
    assert "source_file_id" in schema["properties"]
    assert schema["required"] == ["entity_id"]


@pytest.mark.asyncio
async def test_upload_rejects_source_file_id_plus_file_content(monkeypatch):
    """source_file_id and file_content are mutually exclusive content sources —
    providing both returns a clear error and uploads nothing."""
    captured = _patch_upload_capture(monkeypatch)

    executor = _executor_with_file()
    result = await executor.execute_tool(
        ToolCall(
            id="u5",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "source_file_id": "up-1",
                "file_content": "# a summary the agent should not be authoring",
            },
        )
    )

    assert result.is_error is True
    assert "only one content source" in result.content
    assert "file_bytes" not in captured


@pytest.mark.asyncio
async def test_upload_source_file_id_normalizes_pdf_name(monkeypatch):
    """A PDF uploaded file attached under an override name with no extension
    gets a .pdf suffix (parity with the file_content render path), so the mime
    and the filename agree."""
    captured = _patch_upload_capture(monkeypatch)

    async def _fail_render(*_args, **_kwargs):
        raise AssertionError("renderer must NOT run for a source_file_id upload")

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fail_render)

    executor = _executor_with_file(
        filename="auditor.pdf", content_type="application/pdf"
    )
    result = await executor.execute_tool(
        ToolCall(
            id="u6",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "source_file_id": "up-1",
                "file_name": "renamed-report",  # no extension
            },
        )
    )

    assert result.is_error is False
    assert captured["file_name"] == "renamed-report.pdf"
    assert captured["mime_type"] == "application/pdf"
    assert captured["file_bytes"] == _RAW_PDF


@pytest.mark.asyncio
async def test_upload_empty_file_name_is_rejected(monkeypatch):
    """A blank / whitespace-only file_name must be rejected before upload —
    otherwise a PDF mime would normalise it to a bare ``.pdf`` with no base
    name (or upload a nameless file)."""
    captured = _patch_upload_capture(monkeypatch)

    async def _fail_render(*_args, **_kwargs):
        raise AssertionError("renderer must NOT run for an invalid file_name")

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fail_render)

    executor = _build_upload_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="upload_opencti_file",
            arguments={
                "entity_id": "report--123",
                "file_name": "   ",
                "file_content": _MARKDOWN,
                "mime_type": "application/pdf",
            },
        )
    )

    assert result.is_error is False
    assert "file_name is required" in result.content
    # Nothing was rendered or uploaded for an empty name.
    assert "file_bytes" not in captured
