"""Tests for Drata Evidence Library artifact uploads.

Previously ``create_drata_evidence`` only created a *text* evidence-library
item — it could not attach a binary file, so an agent asked to "file the SOC 2
recap PDF" produced a structured text recap, never an actual ``.pdf`` artifact.

The tool now renders agent-authored Markdown / HTML to a REAL PDF (or DOCX)
server-side via the shared Playwright / python-docx renderers and uploads it via
``multipart/form-data`` (mirroring OpenCTI's ``upload_opencti_file``), accepts
pre-built bytes via ``file_encoding='base64'``, uploads ``.md`` / ``.txt`` /
``.html`` / ``.csv`` verbatim, and can link an external ``source_url`` artifact.

These tests pin:
  * ``.pdf`` file (text content) -> routed through ``pdf_render.render_text_to_pdf``
    and the *rendered PDF bytes* are uploaded as ``multipart/form-data`` with the
    Drata-required ``ownerId`` / ``filedAt`` / ``renewalScheduleType`` fields.
  * Missing ``file_name`` defaults to ``<name>.pdf`` (PDF is the default artifact).
  * ``file_encoding='base64'`` is treated as pre-built binary and NOT re-rendered.
  * Non-PDF targets (``.md``) are uploaded verbatim as UTF-8 (renderer untouched).
  * ``source_url`` uses the JSON variant with a clean numeric ``controlIds`` array.
  * No-artifact creation stays JSON and uses the documented ``controlIds`` field.
  * Attaching an artifact requires ``owner_id``; a renderer failure surfaces a
    friendly error and skips the upload; two sources / CUSTOM-without-date /
    unsupported extension / invalid base64 are all rejected.
  * ``update_drata_evidence`` files a NEW artifact version (evidence renewal).
"""

from __future__ import annotations

import base64

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.llm.base import FileRef
from app.services import docx_render, pdf_render
from app.tools.base import CallableToolExecutor, FileContext, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"
_MARKDOWN = "# SOC 2 Recap\n\n| Control | Status |\n|---|---|\n| AC-1 | Ready |"


class _FakeResp:
    """Minimal stand-in for ``httpx.Response`` used by the captured tool calls."""

    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload if payload is not None else {"id": 42}
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


def _patch_capture(monkeypatch, *, payload: dict | None = None) -> dict:
    """Capture the request the evidence tool hands to ``safe_request``."""
    captured: dict = {}

    async def _fake_safe_request(
        client, method, url, provider, action, *, expected_statuses=None, **kwargs
    ):
        captured["method"] = method
        captured["url"] = url
        captured["action"] = action
        captured["kwargs"] = kwargs
        return _FakeResp(payload=payload), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return captured


def _no_render(monkeypatch) -> None:
    async def _fail(*_args, **_kwargs):
        raise AssertionError("renderer must NOT be invoked for this case")

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fail)


def _build_executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    return executor


async def _call(executor, name, args):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))


@pytest.mark.asyncio
async def test_create_evidence_renders_markdown_to_pdf(monkeypatch):
    captured = _patch_capture(monkeypatch, payload={"id": 7})

    render_calls: list[dict] = []

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        render_calls.append({"content": content, "title": title})
        return b"%PDF-1.4\n%fake-rendered\n%%EOF"

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fake_render)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "SOC 2 Recap",
            "file_name": "soc2-recap.pdf",
            "file_content": _MARKDOWN,
            "owner_id": 5,
            "control_ids": "10, 11",
        },
    )

    assert result.is_error is False
    # The agent's Markdown is routed through the PDF renderer.
    assert render_calls, "PDF renderer must run for a .pdf artifact"
    assert "SOC 2 Recap" in render_calls[0]["content"]

    assert captured["method"] == "post"
    assert captured["url"].endswith("/workspaces/1/evidence-library")
    kwargs = captured["kwargs"]
    # Binary upload -> multipart/form-data, never JSON.
    assert "files" in kwargs and "json" not in kwargs
    fname, fbytes, mime = kwargs["files"]["file"]
    assert fname == "soc2-recap.pdf"
    assert fbytes.startswith(b"%PDF-")  # rendered PDF bytes, not raw Markdown
    assert mime == "application/pdf"

    data = kwargs["data"]
    assert data["name"] == "SOC 2 Recap"
    assert data["ownerId"] == "5"
    assert data["renewalScheduleType"] == "ONE_YEAR"  # default
    assert data["filedAt"]  # defaults to today
    assert data["controlIds"] == ["10", "11"]
    # Upload headers must NOT force application/json (httpx sets the boundary).
    assert "Content-Type" not in kwargs["headers"]
    assert "created with file" in result.content


@pytest.mark.asyncio
async def test_create_evidence_defaults_to_pdf_without_file_name(monkeypatch):
    captured = _patch_capture(monkeypatch)

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        return b"%PDF-1.5\n%defaulted\n%%EOF"

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fake_render)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Quarterly Access Review",
            "file_content": _MARKDOWN,
            "owner_id": 3,
        },
    )

    assert result.is_error is False
    fname, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fname == "Quarterly Access Review.pdf"
    assert fbytes.startswith(b"%PDF-")
    assert mime == "application/pdf"


@pytest.mark.asyncio
async def test_create_evidence_base64_not_rerendered(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    raw_pdf = b"%PDF-1.7\n%already-binary\n%%EOF"
    encoded = base64.b64encode(raw_pdf).decode("ascii")

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Prebuilt",
            "file_name": "prebuilt.pdf",
            "file_content": encoded,
            "file_encoding": "base64",
            "owner_id": 2,
        },
    )

    assert result.is_error is False
    _, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fbytes == raw_pdf
    assert mime == "application/pdf"


@pytest.mark.asyncio
async def test_create_evidence_markdown_uploaded_verbatim(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Notes",
            "file_name": "notes.md",
            "file_content": _MARKDOWN,
            "owner_id": 2,
        },
    )

    assert result.is_error is False
    _, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fbytes == _MARKDOWN.encode("utf-8")
    assert mime == "text/markdown"


@pytest.mark.asyncio
async def test_create_evidence_url_uses_json(monkeypatch):
    captured = _patch_capture(monkeypatch, payload={"id": 9})

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Pen Test Report",
            "source_url": "https://example.com/report.pdf",
            "owner_id": 4,
            "control_ids": "1,2",
            "renewal_schedule_type": "SIX_MONTHS",
        },
    )

    assert result.is_error is False
    kwargs = captured["kwargs"]
    # URL artifact -> JSON variant (no multipart) so controlIds stays numeric.
    assert "json" in kwargs and "files" not in kwargs
    payload = kwargs["json"]
    assert payload["url"] == "https://example.com/report.pdf"
    assert payload["ownerId"] == 4
    assert payload["renewalScheduleType"] == "SIX_MONTHS"
    assert payload["controlIds"] == [1, 2]
    assert payload["name"] == "Pen Test Report"
    assert "URL artifact" in result.content


@pytest.mark.asyncio
async def test_create_evidence_no_artifact_uses_control_ids_json(monkeypatch):
    captured = _patch_capture(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {"workspace_id": 1, "name": "Policy Doc", "control_ids": "3,4"},
    )

    assert result.is_error is False
    kwargs = captured["kwargs"]
    assert "json" in kwargs and "files" not in kwargs
    payload = kwargs["json"]
    assert payload["name"] == "Policy Doc"
    # The documented v2 field is ``controlIds`` (numeric array), not ``controls``.
    assert payload["controlIds"] == [3, 4]
    assert "controls" not in payload


@pytest.mark.asyncio
async def test_create_evidence_requires_owner_for_artifact(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "No Owner",
            "file_name": "x.pdf",
            "file_content": _MARKDOWN,
        },
    )

    assert result.is_error is True
    assert "owner" in result.content.lower()
    # Nothing was uploaded when the required owner is missing.
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_render_failure_is_graceful(monkeypatch):
    captured = _patch_capture(monkeypatch)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("chromium not installed")

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _boom)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Doomed",
            "file_name": "doomed.pdf",
            "file_content": _MARKDOWN,
            "owner_id": 1,
        },
    )

    assert result.is_error is False
    assert "PDF rendering failed" in result.content
    # No upload attempted after a render failure.
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_rejects_two_sources(monkeypatch):
    captured = _patch_capture(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Both",
            "file_content": _MARKDOWN,
            "source_url": "https://example.com/x.pdf",
            "owner_id": 1,
        },
    )

    assert result.is_error is True
    assert "only one artifact source" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_custom_renewal_requires_date(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Custom",
            "file_name": "c.pdf",
            "file_content": _MARKDOWN,
            "owner_id": 1,
            "renewal_schedule_type": "CUSTOM",
        },
    )

    assert result.is_error is True
    assert "CUSTOM" in result.content
    assert "renewal_date" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_unsupported_extension(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Bad",
            "file_name": "malware.exe",
            "file_content": "x",
            "owner_id": 1,
        },
    )

    assert result.is_error is True
    assert "not an accepted Drata evidence file type" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_invalid_base64(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Bad64",
            "file_content": "not @ valid base64 %%%",
            "file_encoding": "base64",
            "owner_id": 1,
        },
    )

    assert result.is_error is False
    assert "Invalid base64 content" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_update_evidence_files_new_artifact(monkeypatch):
    captured = _patch_capture(monkeypatch, payload={"id": 5})

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        return b"%PDF-1.4\n%renewal\n%%EOF"

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fake_render)

    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_evidence",
        {
            "workspace_id": 1,
            "evidence_id": 5,
            "file_name": "renewal.pdf",
            "file_content": _MARKDOWN,
            "owner_id": 8,
        },
    )

    assert result.is_error is False
    assert captured["method"] == "put"
    assert captured["url"].endswith("/workspaces/1/evidence-library/5")
    kwargs = captured["kwargs"]
    fname, fbytes, _ = kwargs["files"]["file"]
    assert fname == "renewal.pdf"
    assert fbytes.startswith(b"%PDF-")
    assert kwargs["data"]["ownerId"] == "8"
    assert kwargs["data"]["renewalScheduleType"] == "ONE_YEAR"
    assert "updated with new file" in result.content


@pytest.mark.asyncio
async def test_update_evidence_metadata_only_json(monkeypatch):
    captured = _patch_capture(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_evidence",
        {"workspace_id": 1, "evidence_id": 5, "status": "READY", "control_ids": "7"},
    )

    assert result.is_error is False
    kwargs = captured["kwargs"]
    assert "json" in kwargs and "files" not in kwargs
    payload = kwargs["json"]
    assert payload["status"] == "READY"
    assert payload["controlIds"] == [7]


@pytest.mark.asyncio
async def test_create_evidence_artifact_includes_type_multipart(monkeypatch):
    """evidence_type must be sent on the artifact path too — as a multipart form
    field — not only on the no-artifact JSON path."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Policy Recap",
            "file_name": "policy.md",
            "file_content": _MARKDOWN,
            "evidence_type": "POLICY",
            "owner_id": 6,
        },
    )

    assert result.is_error is False
    assert captured["kwargs"]["data"]["type"] == "POLICY"


@pytest.mark.asyncio
async def test_create_evidence_url_artifact_includes_type_json(monkeypatch):
    """The URL-artifact JSON path carries evidence_type as ``type`` too."""
    captured = _patch_capture(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Pen Test Report",
            "source_url": "https://example.com/report.pdf",
            "evidence_type": "DOCUMENT",
            "owner_id": 6,
        },
    )

    assert result.is_error is False
    assert captured["kwargs"]["json"]["type"] == "DOCUMENT"


@pytest.mark.asyncio
async def test_update_evidence_artifact_without_owner_is_allowed(monkeypatch):
    """owner_id is optional when filing a new artifact version on update — the
    item keeps its existing owner — so the upload proceeds and omits ownerId
    (rather than erroring like create does)."""
    captured = _patch_capture(monkeypatch, payload={"id": 5})
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_evidence",
        {
            "workspace_id": 1,
            "evidence_id": 5,
            "file_name": "renewal.md",
            "file_content": _MARKDOWN,
        },
    )

    assert result.is_error is False
    assert captured["method"] == "put"
    data = captured["kwargs"]["data"]
    # No owner supplied — the request omits ownerId (Drata keeps the item's
    # existing owner) instead of erroring or sending a null.
    assert "ownerId" not in data
    assert data["renewalScheduleType"] == "ONE_YEAR"
    assert "updated with new file" in result.content


def test_evidence_id_tools_require_workspace_id():
    """The get/update/delete evidence handlers take workspace_id positionally
    (no Python default), so their tool schema MUST mark it required - otherwise
    an LLM call omitting workspace_id passes schema validation and then
    TypeErrors at runtime when the executor calls the handler with **kwargs.
    create_drata_evidence already requires it."""
    executor = CallableToolExecutor()
    tools = drata.build_tools(_TOKEN, executor)
    by_name = {t.name: t for t in tools}
    for name in (
        "get_drata_evidence",
        "update_drata_evidence",
        "delete_drata_evidence",
        "create_drata_evidence",
    ):
        schema = by_name[name].input_schema
        required = schema.get("required", [])
        assert "workspace_id" in required, f"{name} must require workspace_id"
        # A required field must not advertise a misleading default (the executor
        # never applies schema defaults), matching other required-workspace_id
        # Drata tools like get_drata_control.
        ws = schema["properties"]["workspace_id"]
        assert "default" not in ws, f"{name} workspace_id must not set a default"


@pytest.mark.asyncio
async def test_update_evidence_artifact_forwards_status(monkeypatch):
    """status must be forwarded on the artifact path too (as a multipart form
    field), honouring "only provided fields are changed" when a status change
    accompanies a new artifact version - not only on the no-artifact JSON path."""
    captured = _patch_capture(monkeypatch, payload={"id": 5})
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_evidence",
        {
            "workspace_id": 1,
            "evidence_id": 5,
            "file_name": "renewal.md",
            "file_content": _MARKDOWN,
            "owner_id": 8,
            "status": "READY",
        },
    )

    assert result.is_error is False
    assert captured["kwargs"]["data"]["status"] == "READY"


@pytest.mark.asyncio
async def test_create_evidence_renders_docx(monkeypatch):
    """A .docx artifact is rendered server-side via docx_render.render_text_to_docx
    and the rendered bytes are uploaded as multipart with the DOCX MIME type (the
    PDF renderer must not run)."""
    captured = _patch_capture(monkeypatch, payload={"id": 11})
    _no_render(monkeypatch)  # the PDF renderer must NOT run for a .docx target

    render_calls: list[dict] = []

    async def _fake_docx(content: str, *, title: str | None = None) -> bytes:
        render_calls.append({"content": content, "title": title})
        return b"PK\x03\x04fake-docx"

    monkeypatch.setattr(docx_render, "render_text_to_docx", _fake_docx)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Access Review",
            "file_name": "review.docx",
            "file_content": _MARKDOWN,
            "owner_id": 4,
        },
    )

    assert result.is_error is False
    assert render_calls, "DOCX renderer must run for a .docx artifact"
    fname, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fname == "review.docx"
    assert fbytes == b"PK\x03\x04fake-docx"  # rendered DOCX bytes, not raw Markdown
    assert (
        mime
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


@pytest.mark.asyncio
async def test_update_evidence_url_artifact_uses_json(monkeypatch):
    """Updating with source_url files a URL artifact via the JSON variant (not
    multipart), with a numeric controlIds array and forwarded metadata."""
    captured = _patch_capture(monkeypatch, payload={"id": 5})

    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_evidence",
        {
            "workspace_id": 1,
            "evidence_id": 5,
            "source_url": "https://example.com/renewal.pdf",
            "owner_id": 8,
            "control_ids": "3, 4",
            "renewal_schedule_type": "SIX_MONTHS",
        },
    )

    assert result.is_error is False
    assert captured["method"] == "put"
    assert captured["url"].endswith("/workspaces/1/evidence-library/5")
    kwargs = captured["kwargs"]
    # URL artifact -> JSON variant (no multipart) so controlIds stays numeric.
    assert "json" in kwargs and "files" not in kwargs
    payload = kwargs["json"]
    assert payload["url"] == "https://example.com/renewal.pdf"
    assert payload["controlIds"] == [3, 4]
    assert payload["ownerId"] == 8
    assert payload["renewalScheduleType"] == "SIX_MONTHS"
    assert "URL artifact" in result.content


@pytest.mark.asyncio
async def test_create_evidence_binary_extension_requires_base64(monkeypatch):
    """A binary extension (e.g. .png) with text content is rejected - encoding
    agent text as UTF-8 would upload a corrupt artifact - so base64 is required
    and nothing is uploaded."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Screenshot",
            "file_name": "evidence.png",
            "file_content": "not real png bytes",
            "owner_id": 2,
        },
    )

    assert result.is_error is True
    assert "binary file type" in result.content
    assert "base64" in result.content
    # The corrupt artifact is never sent.
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_png_base64_uploads(monkeypatch):
    """A binary format uploads fine when provided pre-built as base64 (decoded
    as-is, not re-encoded)."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    raw_png = b"\x89PNG\r\n\x1a\n\x00fake"
    encoded = base64.b64encode(raw_png).decode("ascii")

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Screenshot",
            "file_name": "evidence.png",
            "file_content": encoded,
            "file_encoding": "base64",
            "owner_id": 2,
        },
    )

    assert result.is_error is False
    fname, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fname == "evidence.png"
    assert fbytes == raw_png
    assert "png" in mime


@pytest.mark.asyncio
async def test_create_evidence_base64_with_whitespace_decodes(monkeypatch):
    """Line-wrapped / whitespace-padded base64 (copy/paste, 76-char wrapping) is
    unwrapped before decoding, so the original bytes upload instead of being
    rejected by the strict validator."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    raw = b"\x89PNG\r\n\x1a\n\x00wrapped-binary-payload-bytes"
    encoded = base64.b64encode(raw).decode("ascii")
    # Leading + interior + trailing ASCII whitespace, as real payloads carry.
    wrapped = (
        "  " + "\n".join(encoded[i : i + 8] for i in range(0, len(encoded), 8)) + "\n"
    )
    assert wrapped != encoded  # sanity: the input really contains whitespace

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Screenshot",
            "file_name": "evidence.png",
            "file_content": wrapped,
            "file_encoding": "base64",
            "owner_id": 2,
        },
    )

    assert result.is_error is False
    _fname, fbytes, _mime = captured["kwargs"]["files"]["file"]
    assert fbytes == raw


@pytest.mark.asyncio
async def test_create_evidence_blank_source_url_fails_fast(monkeypatch):
    """A whitespace-only source_url takes the artifact path (truthy) but must not
    POST an invalid empty url to Drata - it fails fast with a clear error and
    sends nothing."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "External report",
            "source_url": "   ",
            "owner_id": 2,
        },
    )

    assert result.is_error is True
    assert "source_url is empty" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_dotted_name_without_file_name_defaults_to_pdf(monkeypatch):
    """With no file_name, an evidence name containing a dot (e.g. "SOC2 v1.2
    recap") must default to a PDF artifact - not parse ".2 recap" as the
    extension and reject it."""
    captured = _patch_capture(monkeypatch)

    render_calls: list[dict] = []

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        render_calls.append({"content": content, "title": title})
        return b"%PDF-1.4\n%fake\n%%EOF"

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fake_render)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "SOC2 v1.2 recap",
            "file_content": _MARKDOWN,
            "owner_id": 3,
        },
    )

    assert result.is_error is False
    assert render_calls, "a dotted evidence name with no file_name must still render a PDF"
    # The dotted base name is used verbatim, with a .pdf extension appended.
    assert render_calls[0]["title"] == "SOC2 v1.2 recap"
    fname, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fname == "SOC2 v1.2 recap.pdf"
    assert fbytes.startswith(b"%PDF")
    assert "pdf" in mime


@pytest.mark.asyncio
async def test_create_evidence_whitespace_source_url_does_not_block_file(monkeypatch):
    """A whitespace-only source_url is effectively empty, so it must NOT trip the
    "two sources" guard - the file_content upload proceeds normally."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Note",
            "file_name": "note.md",
            "file_content": "# hello",
            "source_url": "   ",
            "owner_id": 2,
        },
    )

    assert result.is_error is False
    assert "only one artifact source" not in result.content
    fname, _fbytes, _mime = captured["kwargs"]["files"]["file"]
    assert fname == "note.md"


@pytest.mark.asyncio
async def test_update_evidence_whitespace_source_url_does_not_block_file(monkeypatch):
    """Same as create: a whitespace-only source_url must not block the
    file_content upload on update."""
    captured = _patch_capture(monkeypatch, payload={"id": 5})
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_evidence",
        {
            "workspace_id": 1,
            "evidence_id": 5,
            "file_name": "note.md",
            "file_content": "# hello",
            "source_url": "   ",
        },
    )

    assert result.is_error is False
    assert "only one artifact source" not in result.content
    assert captured["method"] == "put"
    fname, _fbytes, _mime = captured["kwargs"]["files"]["file"]
    assert fname == "note.md"


@pytest.mark.asyncio
async def test_update_evidence_whitespace_name_uses_evidence_id_fallback(monkeypatch):
    """A whitespace-only name with no file_name must fall back to the stable
    "evidence-<id>" base filename, never a bare ".pdf"."""
    captured = _patch_capture(monkeypatch, payload={"id": 7})

    async def _fake_render(_content: str, *, title: str | None = None) -> bytes:
        return b"%PDF-1.4\n%fake\n%%EOF"

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fake_render)

    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_evidence",
        {
            "workspace_id": 1,
            "evidence_id": 7,
            "name": "   ",
            "file_content": _MARKDOWN,
        },
    )

    assert result.is_error is False
    fname, fbytes, _mime = captured["kwargs"]["files"]["file"]
    assert fname == "evidence-7.pdf"
    assert fbytes.startswith(b"%PDF")


# ---------------------------------------------------------------------------
# source_file_id — forward a user-uploaded file as the artifact BYTE-FOR-BYTE
# ---------------------------------------------------------------------------
#
# When the user uploads a file in chat (or attaches it to the assignment
# trigger), the agent must be able to file THAT file as the evidence artifact
# as-is, instead of re-authoring a lossy summary it can only build from the
# extracted text / vision block it saw. The bytes are pulled from the run's
# FileContext via ``source_file_id`` and uploaded verbatim (no render, no
# base64 round-trip).

_RAW_PDF = b"%PDF-1.7\n%original-auditor-bytes\n1 0 obj<<>>endobj\n%%EOF"


def _executor_with_file(
    *,
    file_id: str = "up-1",
    filename: str = "SOC2_Type2_Report.pdf",
    content_type: str = "application/pdf",
    data: bytes = _RAW_PDF,
) -> CallableToolExecutor:
    """Build a Drata executor whose FileContext holds one inline uploaded file."""
    executor = _build_executor()
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
async def test_create_evidence_from_uploaded_file_uploads_original_bytes(monkeypatch):
    """source_file_id forwards the ORIGINAL uploaded bytes verbatim — the PDF
    renderer is never invoked and the exact bytes hit Drata as multipart."""
    captured = _patch_capture(monkeypatch, payload={"id": 77})
    _no_render(monkeypatch)  # the renderer must NOT run on the passthrough path

    executor = _executor_with_file()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "SOC 2 Type 2 Report",
            "source_file_id": "up-1",
            "owner_id": 5,
        },
    )

    assert result.is_error is False
    assert captured["method"] == "post"
    kwargs = captured["kwargs"]
    assert "files" in kwargs and "json" not in kwargs
    fname, fbytes, mime = kwargs["files"]["file"]
    # The uploaded file's own name + the ORIGINAL bytes, byte-for-byte.
    assert fname == "SOC2_Type2_Report.pdf"
    assert fbytes == _RAW_PDF
    assert mime == "application/pdf"
    assert kwargs["data"]["ownerId"] == "5"
    assert kwargs["data"]["renewalScheduleType"] == "ONE_YEAR"
    assert "created with file" in result.content


@pytest.mark.asyncio
async def test_create_evidence_from_uploaded_file_honours_override_name(monkeypatch):
    """An explicit file_name renames the artifact while still uploading the
    original uploaded bytes."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _executor_with_file()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Renamed",
            "source_file_id": "up-1",
            "file_name": "renamed-evidence.pdf",
            "owner_id": 5,
        },
    )

    assert result.is_error is False
    fname, fbytes, _ = captured["kwargs"]["files"]["file"]
    assert fname == "renamed-evidence.pdf"
    assert fbytes == _RAW_PDF


@pytest.mark.asyncio
async def test_create_evidence_from_uploaded_file_missing_id(monkeypatch):
    """An unknown source_file_id fails fast with a friendly error and uploads
    nothing."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _executor_with_file(file_id="up-1")
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Missing",
            "source_file_id": "does-not-exist",
            "owner_id": 5,
        },
    )

    assert result.is_error is True
    assert "not an available uploaded file" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_source_file_id_without_context(monkeypatch):
    """Without a FileContext (no uploads in scope) the tool returns a friendly
    error rather than crashing."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()  # no file_context set
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "NoCtx",
            "source_file_id": "up-1",
            "owner_id": 5,
        },
    )

    assert result.is_error is True
    assert "no uploaded files are available" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_rejects_file_content_plus_source_file_id(monkeypatch):
    """source_file_id and file_content are mutually exclusive artifact sources."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _executor_with_file()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Both",
            "source_file_id": "up-1",
            "file_content": _MARKDOWN,
            "owner_id": 5,
        },
    )

    assert result.is_error is True
    assert "only one artifact source" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_uploaded_file_unsupported_extension(monkeypatch):
    """An uploaded file whose extension Drata does not accept is rejected before
    upload (the original bytes are never forwarded)."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _executor_with_file(
        filename="payload.exe", content_type="application/octet-stream"
    )
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Bad upload",
            "source_file_id": "up-1",
            "owner_id": 5,
        },
    )

    assert result.is_error is True
    assert "not an accepted Drata evidence file type" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_create_evidence_uploaded_file_requires_owner(monkeypatch):
    """Owner is still required when filing an artifact from an uploaded file."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _executor_with_file()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "No owner",
            "source_file_id": "up-1",
        },
    )

    assert result.is_error is True
    assert "owner" in result.content.lower()
    assert "method" not in captured


@pytest.mark.asyncio
async def test_update_evidence_from_uploaded_file_files_new_version(monkeypatch):
    """update_drata_evidence files a NEW artifact version from an uploaded file,
    uploading the original bytes verbatim via PUT."""
    captured = _patch_capture(monkeypatch, payload={"id": 5})
    _no_render(monkeypatch)

    executor = _executor_with_file(filename="renewal.pdf")
    result = await _call(
        executor,
        "update_drata_evidence",
        {
            "workspace_id": 1,
            "evidence_id": 5,
            "source_file_id": "up-1",
            "owner_id": 8,
        },
    )

    assert result.is_error is False
    assert captured["method"] == "put"
    assert captured["url"].endswith("/workspaces/1/evidence-library/5")
    fname, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fname == "renewal.pdf"
    assert fbytes == _RAW_PDF
    assert mime == "application/pdf"
    assert "updated with new file" in result.content


def test_evidence_tools_expose_source_file_id_param():
    """The create/update evidence tool schemas must advertise source_file_id so
    the LLM knows it can forward an uploaded file by reference."""
    executor = CallableToolExecutor()
    tools = drata.build_tools(_TOKEN, executor)
    by_name = {t.name: t for t in tools}
    for name in ("create_drata_evidence", "update_drata_evidence"):
        props = by_name[name].input_schema["properties"]
        assert "source_file_id" in props, f"{name} must expose source_file_id"


@pytest.mark.asyncio
async def test_create_evidence_uploaded_file_guesses_mime_from_name(monkeypatch):
    """When the uploaded file's content type is the generic octet-stream, the
    artifact MIME is guessed from the (validated) extension so Drata receives a
    meaningful type — parity with the render path's guess_content_type(name)."""
    captured = _patch_capture(monkeypatch, payload={"id": 91})
    _no_render(monkeypatch)

    # Uploaded as a bare octet-stream blob, but the name carries a known ext.
    executor = _executor_with_file(
        filename="auditor-report.pdf", content_type="application/octet-stream"
    )
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "Octet upload",
            "source_file_id": "up-1",
            "owner_id": 5,
        },
    )

    assert result.is_error is False
    fname, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fname == "auditor-report.pdf"
    assert fbytes == _RAW_PDF
    assert mime == "application/pdf"


@pytest.mark.asyncio
async def test_get_evidence_uses_valid_expand_enum(monkeypatch):
    """get_drata_evidence must only request expand values that Drata's v2
    ``EvidenceLibraryExpandEnum`` accepts (``user`` / ``controls`` /
    ``renewalSchemaAndVersions``). The old ``owner`` / ``customFields`` values
    are not in the enum and Drata rejected the whole request with HTTP 400.

    Also pins the response parsing to the documented shape: the owner lives in
    ``user`` (not ``owner``) and the renewal date is nested under
    ``renewalSchema`` (not a top-level ``renewalDate``)."""
    captured = _patch_capture(
        monkeypatch,
        payload={
            "id": 5,
            "name": "SOC 2 Recap",
            "description": "Recap doc",
            "status": "READY",
            "createdAt": "2026-01-01T00:00:00Z",
            "user": {"firstName": "Sally", "lastName": "Smith"},
            "renewalSchema": {
                "renewalDate": "2027-01-01",
                "renewalScheduleType": "ONE_YEAR",
            },
            "versions": [
                {"id": 1, "version": 1, "name": "soc2.pdf", "type": "FILE", "current": True}
            ],
            "controls": [{"code": "AC-1", "name": "Access Control"}],
        },
    )

    executor = _build_executor()
    result = await _call(
        executor,
        "get_drata_evidence",
        {"workspace_id": 1, "evidence_id": 5},
    )

    assert result.is_error is False
    expand = captured["kwargs"]["params"]["expand[]"]
    assert set(expand) <= {"user", "controls", "renewalSchemaAndVersions"}
    assert "owner" not in expand
    assert "customFields" not in expand
    # Owner from ``user``, renewal from ``renewalSchema`` — not "N/A".
    assert "Sally Smith" in result.content
    assert "2027-01-01" in result.content
    assert "ONE_YEAR" in result.content
    # Artifact versions are surfaced so the agent can confirm the stored file.
    assert "soc2.pdf" in result.content
    assert "AC-1" in result.content


# ---------------------------------------------------------------------------
# Artifact-level verification — version detail endpoint + read-your-write
# ---------------------------------------------------------------------------
#
# After filing a new artifact version (evidence renewal), the agent needs a
# deterministic way to confirm WHICH file is now stored. The v2 Get Evidence
# Library Version endpoint
# (GET .../evidence-library/{id}/versions/{versionId}?expand[]=downloadUrl)
# returns the stored file name + a download URL — the proper per-artifact
# verification path. The create/update responses already carry the new
# ``versions`` array, so the new version id is surfaced immediately too.


@pytest.mark.asyncio
async def test_get_evidence_version_returns_file_name_and_download_url(monkeypatch):
    """get_drata_evidence_version hits the versions/{id} endpoint with
    expand[]=downloadUrl and surfaces the stored file name + download URL so the
    agent can verify the exact artifact byte-for-byte."""
    captured = _patch_capture(
        monkeypatch,
        payload={
            "id": 789,
            "name": "Filigran_MRL_SOC2.pdf",
            "version": 3,
            "type": "FILE",
            "source": "",
            "current": True,
            "filedAt": "2026-06-18",
            "createdAt": "2026-06-18T08:00:00Z",
            "updatedAt": "2026-06-18T08:00:00Z",
            "downloadUrl": "https://drata-files.example.com/signed/abc123",
        },
    )

    executor = _build_executor()
    result = await _call(
        executor,
        "get_drata_evidence_version",
        {"workspace_id": 1, "evidence_id": 240, "version_id": 789},
    )

    assert result.is_error is False
    assert captured["method"] == "get"
    assert captured["url"].endswith("/workspaces/1/evidence-library/240/versions/789")
    # Only the documented expand value is requested.
    assert captured["kwargs"]["params"]["expand[]"] == ["downloadUrl"]
    # The verifiable artifact details are surfaced.
    assert "Filigran_MRL_SOC2.pdf" in result.content
    assert "https://drata-files.example.com/signed/abc123" in result.content
    assert "Version: 3" in result.content
    assert "(current)" in result.content


@pytest.mark.asyncio
async def test_get_evidence_version_handles_missing_download_url(monkeypatch):
    """A URL/ticket artifact has no signed downloadUrl — the tool says so
    explicitly rather than printing a blank line."""
    _patch_capture(
        monkeypatch,
        payload={
            "id": 5,
            "name": "external-link",
            "version": 1,
            "type": "URL",
            "current": True,
            "downloadUrl": "",
        },
    )

    executor = _build_executor()
    result = await _call(
        executor,
        "get_drata_evidence_version",
        {"workspace_id": 1, "evidence_id": 9, "version_id": 5},
    )

    assert result.is_error is False
    assert "Download URL: not returned" in result.content


@pytest.mark.asyncio
async def test_create_evidence_surfaces_new_artifact_version(monkeypatch):
    """The create response body carries the new ``versions`` array, so the
    success message reports the current version id and points at the
    verification tool — a read-your-write confirmation needing no re-read."""
    _patch_capture(
        monkeypatch,
        payload={
            "id": 240,
            "versions": [
                {"id": 700, "current": False},
                {"id": 789, "current": True},
            ],
        },
    )
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_evidence",
        {
            "workspace_id": 1,
            "name": "SOC 2 MRL",
            "file_name": "mrl.md",
            "file_content": _MARKDOWN,
            "owner_id": 5,
        },
    )

    assert result.is_error is False
    assert "created with file" in result.content
    assert "version_id=789" in result.content
    assert "2 total" in result.content
    assert "get_drata_evidence_version" in result.content


@pytest.mark.asyncio
async def test_update_evidence_surfaces_new_artifact_version(monkeypatch):
    """The update (renewal) response also carries the new current version id,
    surfaced so the agent can confirm the renewal landed without re-reading."""
    _patch_capture(
        monkeypatch,
        payload={"id": 240, "versions": [{"id": 790, "current": True}]},
    )
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_evidence",
        {
            "workspace_id": 1,
            "evidence_id": 240,
            "file_name": "renewal.md",
            "file_content": _MARKDOWN,
            "owner_id": 8,
        },
    )

    assert result.is_error is False
    assert "updated with new file" in result.content
    assert "version_id=790" in result.content
    assert "get_drata_evidence_version" in result.content


@pytest.mark.asyncio
async def test_get_evidence_surfaces_updated_at_and_version_ids(monkeypatch):
    """get_drata_evidence surfaces the item's updatedAt (freshness signal) and
    each artifact's version_id, plus a tip to fetch the file via the version
    endpoint — so the agent can chain straight into verification."""
    _patch_capture(
        monkeypatch,
        payload={
            "id": 240,
            "name": "SOC 2 MRL",
            "createdAt": "2026-06-01T00:00:00Z",
            "updatedAt": "2026-06-18T08:00:00Z",
            "versions": [
                {"id": 789, "version": 3, "type": "FILE", "current": True},
            ],
        },
    )

    executor = _build_executor()
    result = await _call(
        executor,
        "get_drata_evidence",
        {"workspace_id": 1, "evidence_id": 240},
    )

    assert result.is_error is False
    assert "Updated: 2026-06-18T08:00:00Z" in result.content
    assert "version_id=789" in result.content
    assert "get_drata_evidence_version" in result.content


def test_get_evidence_version_tool_schema_requires_all_ids():
    """get_drata_evidence_version takes workspace_id/evidence_id/version_id
    positionally (no Python defaults), so the schema must mark all three
    required — otherwise an LLM call omitting one passes schema validation and
    then TypeErrors at runtime."""
    executor = CallableToolExecutor()
    tools = drata.build_tools(_TOKEN, executor)
    by_name = {t.name: t for t in tools}
    assert "get_drata_evidence_version" in by_name, (
        "get_drata_evidence_version must be exposed as a tool"
    )
    schema = by_name["get_drata_evidence_version"].input_schema
    required = schema.get("required", [])
    for field in ("workspace_id", "evidence_id", "version_id"):
        assert field in required, f"get_drata_evidence_version must require {field}"
        assert "default" not in schema["properties"][field]
