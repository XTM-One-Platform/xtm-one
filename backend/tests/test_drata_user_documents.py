"""Tests for Drata User Documents (personnel compliance evidence) tools.

Drata exposes manual personnel-compliance evidence via the v2 User Documents
endpoints (``/users/{userId}/documents``). This is the ONLY API write path for
**personnel offboarding evidence** — a distinct surface from the workspace
Evidence Library — so filing an artifact with ``doc_type='OFFBOARDING_EVIDENCE'``
is what satisfies a leaver's ``OFFBOARDING`` personnel compliance check.

These tests pin:
  * ``upload_drata_user_document`` renders agent Markdown to a real PDF and POSTs
    it as ``multipart/form-data`` with ``type`` + ``completionDate`` form fields.
  * ``user_id`` accepts a bare email and is rewritten to the documented
    ``email:<address>`` path form (the leaver-by-email reconciliation case).
  * ``doc_type`` defaults to ``OFFBOARDING_EVIDENCE`` and only the four uploadable
    types are accepted (list-only types like ``NIST_AI_TRAINING_EVIDENCE`` are
    rejected before any upload).
  * The narrower User Document file-type set is enforced — plain ``.md`` text is
    rejected even though the Evidence Library accepts it.
  * A user-uploaded file is forwarded byte-for-byte via ``source_file_id``.
  * list / get / delete hit the documented endpoints; get surfaces the signed
    download URL.
  * Tool schemas advertise the right required fields and enums.
"""

from __future__ import annotations

import base64

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.llm.base import FileRef
from app.services import pdf_render
from app.tools.base import CallableToolExecutor, FileContext, ToolCall
from tests.integration_file_fakes import HOSTILE_FILE_NAMES, assert_name_only_inside, fenced_and_open

_TOKEN = "https://public-api.drata.com/public/v2||test-token"
_MARKDOWN = "# Offboarding\n\n- Access revoked\n- Devices returned"


class _FakeResp:
    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload if payload is not None else {"id": 42}
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


def _patch_capture(monkeypatch, *, payload: dict | None = None) -> dict:
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


# ---------------------------------------------------------------------------
# upload_drata_user_document — the offboarding-evidence write path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_offboarding_renders_markdown_to_pdf(monkeypatch):
    captured = _patch_capture(monkeypatch, payload={"id": 7})

    render_calls: list[dict] = []

    async def _fake_render(content: str, *, title: str | None = None) -> bytes:
        render_calls.append({"content": content, "title": title})
        return b"%PDF-1.4\n%offboarding\n%%EOF"

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fake_render)

    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {
            "user_id": "123",
            "file_name": "offboarding.pdf",
            "file_content": _MARKDOWN,
            "completion_date": "2026-06-20",
        },
    )

    assert result.is_error is False
    assert render_calls, "the PDF renderer must run for a .pdf artifact"

    assert captured["method"] == "post"
    assert captured["url"].endswith("/users/123/documents")
    kwargs = captured["kwargs"]
    # Binary upload -> multipart/form-data, never JSON.
    assert "files" in kwargs and "json" not in kwargs
    fname, fbytes, mime = kwargs["files"]["file"]
    assert fname == "offboarding.pdf"
    assert fbytes.startswith(b"%PDF-")
    assert mime == "application/pdf"
    data = kwargs["data"]
    assert data["type"] == "OFFBOARDING_EVIDENCE"  # default
    assert data["completionDate"] == "2026-06-20"
    # Upload headers must not force application/json (httpx sets the boundary).
    assert "Content-Type" not in kwargs["headers"]
    assert "OFFBOARDING_EVIDENCE" in result.content
    assert "document ID: 7" in result.content


@pytest.mark.asyncio
async def test_upload_accepts_bare_email_user_ref(monkeypatch):
    """A bare email is rewritten to the documented ``email:`` path form, so a
    leaver reconciled from an HRIS by email can be addressed directly."""
    captured = _patch_capture(monkeypatch)

    async def _fake_render(_content: str, *, title: str | None = None) -> bytes:
        return b"%PDF-1.4\n%x\n%%EOF"

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fake_render)

    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {"user_id": "leaver@acme.com", "file_content": _MARKDOWN},
    )

    assert result.is_error is False
    assert captured["url"].endswith("/users/email:leaver@acme.com/documents")


def test_user_ref_validates_and_normalizes():
    """``_user_ref`` accepts only a numeric Drata user ID or a well-formed email
    (rewritten to the ``email:`` path form, prefix normalized to lowercase) and
    rejects anything that could inject into the request path - the value is
    interpolated into ``/users/{ref}/documents``."""
    from app.integrations.providers.drata._common import _user_ref

    # Accepted: numeric id passes through; emails get the documented prefix.
    assert _user_ref("12345") == "12345"
    assert _user_ref("leaver@acme.com") == "email:leaver@acme.com"
    assert _user_ref("email:leaver@acme.com") == "email:leaver@acme.com"
    # The email: prefix casing is normalized; the address casing is preserved.
    assert _user_ref("EMAIL:Leaver@Acme.com") == "email:Leaver@Acme.com"
    assert _user_ref("  leaver@acme.com  ") == "email:leaver@acme.com"
    # Rejected (-> "" so the caller returns a friendly error): path/query
    # injection (literal AND percent-encoded reserved chars, since the ref is
    # interpolated raw into the path), whitespace, and non-numeric/non-email junk.
    for bad in (
        "",
        "   ",
        "123/documents?type=X",
        "leaver@acme.com/../admin",
        "leaver@acme.com?x=1",
        "leaver@acme.com#frag",
        "evil%2f..%2fadmin@acme.com",  # %2F -> /
        "evil%3fx=1@acme.com",  # %3F -> ?
        "evil%23frag@acme.com",  # %23 -> #
        "evil%00@acme.com",  # NUL
        "12 34",
        "not-a-user",
        "email:123",
    ):
        assert _user_ref(bad) == "", bad


@pytest.mark.asyncio
async def test_upload_rejects_unsafe_user_ref(monkeypatch):
    """An unsafe user_id (path/query injection) is rejected before any request -
    the tool returns the friendly error and safe_request is never called."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {"user_id": "leaver@acme.com/../admin", "file_content": _MARKDOWN},
    )

    assert result.is_error is True  # a guarded rejection, not a raise
    assert "user_id is required" in result.content
    assert captured == {}  # no HTTP request was made


@pytest.mark.asyncio
async def test_upload_defaults_pdf_without_file_name(monkeypatch):
    captured = _patch_capture(monkeypatch)

    async def _fake_render(_content: str, *, title: str | None = None) -> bytes:
        return b"%PDF-1.5\n%defaulted\n%%EOF"

    monkeypatch.setattr(pdf_render, "render_text_to_pdf", _fake_render)

    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {"user_id": "5", "file_content": _MARKDOWN},
    )

    assert result.is_error is False
    fname, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fname == "offboarding-evidence.pdf"
    assert fbytes.startswith(b"%PDF-")
    assert mime == "application/pdf"


@pytest.mark.asyncio
async def test_upload_rejects_non_uploadable_doc_type(monkeypatch):
    """A valid list-filter type that is NOT in the upload enum (e.g.
    NIST_AI_TRAINING_EVIDENCE) is rejected before any request is made."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {
            "user_id": "5",
            "doc_type": "NIST_AI_TRAINING_EVIDENCE",
            "file_content": _MARKDOWN,
        },
    )

    assert result.is_error is True
    assert "invalid doc_type" in result.content
    assert "OFFBOARDING_EVIDENCE" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_upload_rejects_text_extension(monkeypatch):
    """User Documents do not accept plain text/markdown — a .md target is
    rejected with the NARROW accepted list, and nothing is uploaded."""
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {
            "user_id": "5",
            "file_name": "notes.md",
            "file_content": _MARKDOWN,
        },
    )

    assert result.is_error is True
    assert "not an accepted Drata evidence file type" in result.content
    # The narrow user-document set excludes .md / .txt / .csv / .json / .log.
    assert ".md" not in result.content.split("Accepted:", 1)[1]
    assert "method" not in captured


@pytest.mark.asyncio
async def test_upload_rejects_zero_sources(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {"user_id": "5"},
    )

    assert result.is_error is True
    assert "exactly one artifact source" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_upload_rejects_two_sources(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {
            "user_id": "5",
            "file_content": _MARKDOWN,
            "source_file_id": "up-1",
        },
    )

    assert result.is_error is True
    assert "exactly one artifact source" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
async def test_upload_png_base64(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    raw_png = b"\x89PNG\r\n\x1a\n\x00fake"
    encoded = base64.b64encode(raw_png).decode("ascii")

    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {
            "user_id": "5",
            "file_name": "termination.png",
            "file_content": encoded,
            "file_encoding": "base64",
        },
    )

    assert result.is_error is False
    fname, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fname == "termination.png"
    assert fbytes == raw_png
    assert "png" in mime


@pytest.mark.asyncio
async def test_upload_omits_completion_date_when_absent(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {
            "user_id": "5",
            "file_name": "x.pdf",
            "file_content": base64.b64encode(b"%PDF-1.4\nx\n%%EOF").decode("ascii"),
            "file_encoding": "base64",
        },
    )

    assert result.is_error is False
    assert "completionDate" not in captured["kwargs"]["data"]


# ---------------------------------------------------------------------------
# source_file_id — forward a user-uploaded file byte-for-byte
# ---------------------------------------------------------------------------

_RAW_PDF = b"%PDF-1.7\n%signed-termination-letter\n%%EOF"


def _executor_with_file(
    *,
    file_id: str = "up-1",
    filename: str = "termination_letter.pdf",
    content_type: str = "application/pdf",
    data: bytes = _RAW_PDF,
) -> CallableToolExecutor:
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
    executor.file_context = ctx
    return executor


@pytest.mark.asyncio
async def test_upload_from_uploaded_file_forwards_bytes(monkeypatch):
    captured = _patch_capture(monkeypatch, payload={"id": 88})
    _no_render(monkeypatch)

    executor = _executor_with_file()
    result = await _call(
        executor,
        "upload_drata_user_document",
        {"user_id": "leaver@acme.com", "source_file_id": "up-1"},
    )

    assert result.is_error is False
    inside, outside = fenced_and_open(result.content)
    assert outside == (
        "Filed the OFFBOARDING_EVIDENCE document for user email:leaver@acme.com (document ID: 88). "
        "Verify with get_drata_user_document(user_id='email:leaver@acme.com', document_id=88).\n"
    )
    assert "Name: termination_letter.pdf" in inside.splitlines()
    assert captured["method"] == "post"
    assert captured["url"].endswith("/users/email:leaver@acme.com/documents")
    fname, fbytes, mime = captured["kwargs"]["files"]["file"]
    assert fname == "termination_letter.pdf"
    assert fbytes == _RAW_PDF
    assert mime == "application/pdf"
    assert captured["kwargs"]["data"]["type"] == "OFFBOARDING_EVIDENCE"


@pytest.mark.asyncio
async def test_upload_uploaded_file_unsupported_extension(monkeypatch):
    captured = _patch_capture(monkeypatch)
    _no_render(monkeypatch)

    executor = _executor_with_file(
        filename="report.txt", content_type="text/plain", data=b"plain text"
    )
    result = await _call(
        executor,
        "upload_drata_user_document",
        {"user_id": "5", "source_file_id": "up-1"},
    )

    assert result.is_error is True
    assert "not an accepted Drata evidence file type" in result.content
    assert "method" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", ".pdf"])
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_upload_names_a_forwarded_file_only_inside_the_block(monkeypatch, hostile, suffix):
    """The stored name reaches the model only in the closing block:
    on the refusal of its extension (no suffix) and on the filed answer (.pdf)."""
    name = HOSTILE_FILE_NAMES[hostile] + suffix
    captured = _patch_capture(monkeypatch, payload={"id": 88})
    _no_render(monkeypatch)

    executor = _executor_with_file(filename=name)
    result = await _call(
        executor,
        "upload_drata_user_document",
        {"user_id": "5", "source_file_id": "up-1"},
    )

    outside = assert_name_only_inside(result.content, name)
    if suffix:
        assert result.is_error is False
        assert outside.startswith("Filed the OFFBOARDING_EVIDENCE document for user 5 (document ID: 88).")
        assert captured["kwargs"]["files"]["file"][0] == name
    else:
        assert result.is_error is True
        assert outside.startswith("Error: file 'up-1' is not an accepted Drata evidence file type")
        assert "method" not in captured


# ---------------------------------------------------------------------------
# list / get / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_user_documents_filters_by_type(monkeypatch):
    captured = _patch_capture(monkeypatch)

    async def _fake_fetch_all(client, url, headers, params=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        return (
            [
                {
                    "id": 1,
                    "name": "Offboarding",
                    "type": "OFFBOARDING_EVIDENCE",
                    "renewalDate": "2027-01-01",
                    "createdAt": "2026-06-20",
                }
            ],
            None,
            False,
        )

    monkeypatch.setattr(drata_common, "_paginated_fetch_all", _fake_fetch_all)

    executor = _build_executor()
    result = await _call(
        executor,
        "list_drata_user_documents",
        {"user_id": "leaver@acme.com", "doc_type": "OFFBOARDING_EVIDENCE"},
    )

    assert result.is_error is False
    assert captured["url"].endswith("/users/email:leaver@acme.com/documents")
    assert captured["params"]["type"] == "OFFBOARDING_EVIDENCE"
    assert "Offboarding" in result.content


@pytest.mark.asyncio
async def test_list_user_documents_rejects_invalid_type(monkeypatch):
    _patch_capture(monkeypatch)
    executor = _build_executor()
    result = await _call(
        executor,
        "list_drata_user_documents",
        {"user_id": "5", "doc_type": "BOGUS"},
    )
    assert result.is_error is True
    assert "invalid doc_type" in result.content


@pytest.mark.asyncio
async def test_get_user_document_surfaces_download_url(monkeypatch):
    captured = _patch_capture(
        monkeypatch,
        payload={
            "id": 9,
            "userId": 123,
            "name": "Offboarding",
            "type": "OFFBOARDING_EVIDENCE",
            "renewalDate": "2027-01-01",
            "createdAt": "2026-06-20",
            "updatedAt": "2026-06-20",
            "downloadUrl": {"signedUrl": "https://files.drata.example/abc"},
        },
    )

    executor = _build_executor()
    result = await _call(
        executor,
        "get_drata_user_document",
        {"user_id": "123", "document_id": 9},
    )

    assert result.is_error is False
    assert captured["method"] == "get"
    assert captured["url"].endswith("/users/123/documents/9")
    assert captured["kwargs"]["params"]["expand[]"] == ["downloadUrl"]
    assert "https://files.drata.example/abc" in result.content
    assert "OFFBOARDING_EVIDENCE" in result.content


@pytest.mark.asyncio
async def test_delete_user_document(monkeypatch):
    captured = _patch_capture(monkeypatch)

    executor = _build_executor()
    result = await _call(
        executor,
        "delete_drata_user_document",
        {"user_id": "leaver@acme.com", "document_id": 9},
    )

    assert result.is_error is False
    assert captured["method"] == "delete"
    assert captured["url"].endswith("/users/email:leaver@acme.com/documents/9")
    assert "deleted" in result.content


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_user_document_tools_exposed_with_expected_schema():
    executor = CallableToolExecutor()
    tools = drata.build_tools(_TOKEN, executor)
    by_name = {t.name: t for t in tools}

    for name in (
        "upload_drata_user_document",
        "list_drata_user_documents",
        "get_drata_user_document",
        "delete_drata_user_document",
    ):
        assert name in by_name, f"{name} must be exposed as a tool"

    upload = by_name["upload_drata_user_document"].input_schema
    assert upload["required"] == ["user_id"]
    # Only the four uploadable types are offered, defaulting to offboarding.
    doc_type = upload["properties"]["doc_type"]
    assert set(doc_type["enum"]) == {
        "MFA_EVIDENCE",
        "SEC_TRAINING",
        "HIPAA_TRAINING_EVIDENCE",
        "OFFBOARDING_EVIDENCE",
    }
    assert doc_type["default"] == "OFFBOARDING_EVIDENCE"
    # source_file_id must be advertised so the LLM can forward an uploaded file.
    assert "source_file_id" in upload["properties"]
