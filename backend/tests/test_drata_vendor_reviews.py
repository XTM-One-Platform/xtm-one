"""Tests for the Drata vendor security review + internal assessment tooling.

These pin the workflow that lets an agent fill a vendor's internal assessment
end-to-end:

  * create / list / get security reviews (the internal assessments) hit the
    documented v2 endpoints with the right payloads;
  * update_drata_vendor_security_review forwards the reviewer-side SOC form
    (socForm) so the assessment can be filled programmatically;
  * the review can be finalized via the actions endpoint;
  * supporting questionnaires / SOC report documents upload as multipart;
  * the previously-mislabeled questionnaire tools now hit the real
    /vendors/{id}/questionnaires endpoints (the old "send" tool POSTed a
    questionnaire-shaped body to /security-reviews, which the Create Security
    Review schema rejects).
"""

from __future__ import annotations

import json

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


def _patch(monkeypatch, payload: dict) -> list[dict]:
    calls: list[dict] = []

    async def _fake_safe_request(
        client, method, url, provider, action, *, expected_statuses=None, **kwargs
    ):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        return _Resp(payload), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return calls


def _build_executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    return executor


async def _call(executor, name, args):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))


# ---------------------------------------------------------------------------
# Create / list / get security reviews
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_security_review_sends_required_v2_fields(monkeypatch):
    calls = _patch(monkeypatch, {"id": 9, "type": "SOC_REPORT", "status": "IN_PROGRESS"})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_vendor_security_review",
        {
            "vendor_id": 1,
            "review_deadline_at": "2026-09-30",
            "security_review_status": "IN_PROGRESS",
            "security_review_type": "SOC_REPORT",
            "title": "AWS SOC 2 2026",
            "requester_user_id": 42,
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/vendors/1/security-reviews")
    body = call["kwargs"]["json"]
    assert body["reviewDeadlineAt"] == "2026-09-30"
    assert body["securityReviewStatus"] == "IN_PROGRESS"
    assert body["securityReviewType"] == "SOC_REPORT"
    assert body["title"] == "AWS SOC 2 2026"
    assert body["requesterUserId"] == 42


@pytest.mark.asyncio
async def test_create_security_review_requires_deadline(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor, "create_drata_vendor_security_review", {"vendor_id": 1}
    )
    assert "review_deadline_at is required" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_create_security_review_rejects_bad_enum(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_vendor_security_review",
        {
            "vendor_id": 1,
            "review_deadline_at": "2026-09-30",
            "security_review_type": "SOC2",
        },
    )
    assert "invalid security_review_type" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_list_security_reviews_forwards_filters(monkeypatch):
    calls = _patch(monkeypatch, {"data": [], "pagination": {}})
    executor = _build_executor()
    result = await _call(
        executor,
        "list_drata_vendor_security_reviews",
        {"vendor_id": 1, "status": "IN_PROGRESS, NOT_YET_STARTED", "decision": "pending"},
    )

    assert result.is_error is False
    params = calls[0]["kwargs"]["params"]
    assert params["status[]"] == ["IN_PROGRESS", "NOT_YET_STARTED"]
    assert params["decision[]"] == ["PENDING"]
    assert "user" in params["expand[]"]


@pytest.mark.asyncio
async def test_list_security_reviews_rejects_bad_enum(monkeypatch):
    calls = _patch(monkeypatch, {"data": [], "pagination": {}})
    executor = _build_executor()
    result = await _call(
        executor,
        "list_drata_vendor_security_reviews",
        {"vendor_id": 1, "status": "IN_PROGRESS, BOGUS"},
    )
    assert "invalid status" in result.content
    assert "BOGUS" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_get_security_review_surfaces_missing_soc_fields(monkeypatch):
    _patch(
        monkeypatch,
        {
            "id": 9,
            "title": "AWS SOC 2",
            "type": "SOC_REPORT",
            "status": "IN_PROGRESS",
            "decision": "PENDING",
            "socReviewForm": {
                "readyToFinalize": False,
                "reviewerInformation": {
                    "isComplete": False,
                    "missingRequiredFields": ["reviewerInformation.reviewer"],
                },
                "complianceScope": {"isComplete": True, "missingRequiredFields": []},
            },
        },
    )
    executor = _build_executor()
    result = await _call(
        executor,
        "get_drata_vendor_security_review",
        {"vendor_id": 1, "security_review_id": 9},
    )

    assert result.is_error is False
    assert "ready to finalize: False" in result.content
    assert "reviewerInformation: INCOMPLETE" in result.content
    assert "reviewerInformation.reviewer" in result.content
    assert "complianceScope: complete" in result.content


# ---------------------------------------------------------------------------
# Fill the internal assessment (socForm) + finalize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_security_review_forwards_soc_form(monkeypatch):
    calls = _patch(monkeypatch, {"id": 9})
    executor = _build_executor()
    soc = {
        "reviewerInformation": {"reviewerUserId": 42, "reportIssueDate": "2026-01-15"},
        "reportOpinion": {"reportOpinion": "UNQUALIFIED", "encompassBusinessNeeds": True},
    }
    result = await _call(
        executor,
        "update_drata_vendor_security_review",
        {
            "vendor_id": 1,
            "security_review_id": 9,
            "title": "AWS SOC 2 2026",
            "soc_form_json": json.dumps(soc),
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "put"
    assert call["url"].endswith("/vendors/1/security-reviews/9")
    body = call["kwargs"]["json"]
    assert body["title"] == "AWS SOC 2 2026"
    assert body["socForm"] == soc


@pytest.mark.asyncio
async def test_update_security_review_rejects_bad_json(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_vendor_security_review",
        {"vendor_id": 1, "security_review_id": 9, "soc_form_json": "{not json"},
    )
    assert "not valid JSON" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_update_security_review_rejects_non_string_soc_form(monkeypatch):
    """A non-string soc_form_json (e.g. a dict via the tool-test path) returns a
    friendly error instead of raising AttributeError on .strip()."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_vendor_security_review",
        {
            "vendor_id": 1,
            "security_review_id": 9,
            "soc_form_json": {"reviewerInformation": {}},
        },
    )
    assert "must be a JSON string" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_update_security_review_requires_a_field(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_vendor_security_review",
        {"vendor_id": 1, "security_review_id": 9},
    )
    assert "provide title and/or soc_form_json" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_perform_action_finalize(monkeypatch):
    calls = _patch(
        monkeypatch,
        {"success": True, "newStatus": "COMPLETED", "message": "finalized"},
    )
    executor = _build_executor()
    result = await _call(
        executor,
        "perform_drata_vendor_security_review_action",
        {"vendor_id": 1, "security_review_id": 9, "action": "finalize"},
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/vendors/1/security-reviews/9/actions")
    assert call["kwargs"]["json"] == {"action": "finalize"}
    assert "COMPLETED" in result.content


@pytest.mark.asyncio
async def test_perform_action_rejects_unknown(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "perform_drata_vendor_security_review_action",
        {"vendor_id": 1, "security_review_id": 9, "action": "approve"},
    )
    assert "invalid action" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_list_actions_empty_message(monkeypatch):
    _patch(monkeypatch, {"data": []})
    executor = _build_executor()
    result = await _call(
        executor,
        "list_drata_vendor_security_review_actions",
        {"vendor_id": 1, "security_review_id": 9},
    )
    assert result.is_error is False
    assert "No actions available" in result.content


# ---------------------------------------------------------------------------
# Attachments: security questionnaires + documents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_security_questionnaire_review_level(monkeypatch):
    calls = _patch(monkeypatch, {"id": 5, "title": "Q"})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_vendor_security_questionnaire",
        {
            "vendor_id": 1,
            "security_review_id": 9,
            "file_content": "answers",
            "file_name": "responses.csv",
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["url"].endswith("/vendors/1/security-reviews/9/security-questionnaires")
    files = call["kwargs"]["files"]
    assert files[0][0] == "files"
    assert files[0][1][0] == "responses.csv"


@pytest.mark.asyncio
async def test_upload_security_questionnaire_vendor_level(monkeypatch):
    calls = _patch(monkeypatch, {"id": 5})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_vendor_security_questionnaire",
        {"vendor_id": 1, "file_content": "answers", "file_name": "responses.csv"},
    )

    assert result.is_error is False
    assert calls[0]["url"].endswith("/vendors/1/security-questionnaires")


@pytest.mark.asyncio
async def test_upload_security_questionnaire_requires_one_source(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_vendor_security_questionnaire",
        {"vendor_id": 1},
    )
    assert "exactly one artifact source" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_upload_vendor_document_attaches_to_review(monkeypatch):
    calls = _patch(monkeypatch, {"id": 7, "name": "soc.csv", "type": "SOC_DOCUMENT"})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_vendor_document",
        {
            "vendor_id": 1,
            "document_type": "SOC_DOCUMENT",
            "security_review_id": 9,
            "file_content": "report",
            "file_name": "soc.csv",
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/vendors/1/documents")
    # Single-file field name is `file`; type + securityReviewId go in form data.
    assert "file" in call["kwargs"]["files"]
    data = call["kwargs"]["data"]
    assert data["type"] == "SOC_DOCUMENT"
    assert data["securityReviewId"] == "9"


@pytest.mark.asyncio
async def test_upload_vendor_document_rejects_bad_type(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_vendor_document",
        {"vendor_id": 1, "document_type": "NOPE", "file_content": "x", "file_name": "x.csv"},
    )
    assert "invalid document_type" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_upload_vendor_document_requires_type(monkeypatch):
    """document_type is required by the API, so an empty value fails fast
    instead of POSTing a multipart body without the type field."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_vendor_document",
        {"vendor_id": 1, "file_content": "x", "file_name": "x.csv"},
    )
    assert "document_type is required" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_upload_vendor_document_handles_non_string_file_args(monkeypatch):
    """The tool-test path bypasses jsonschema, so file args can arrive as
    non-strings (e.g. source_file_id=None). _resolve_vendor_file must return a
    friendly error instead of raising AttributeError on .strip()."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_vendor_document",
        {
            "vendor_id": 1,
            "document_type": "SOC_DOCUMENT",
            "source_file_id": None,
            "file_name": None,
        },
    )
    assert result.is_error is True
    assert "exactly one artifact source" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_list_vendor_documents_filter_and_expand(monkeypatch):
    calls = _patch(monkeypatch, {"data": [], "pagination": {}})
    executor = _build_executor()
    result = await _call(
        executor,
        "list_drata_vendor_documents",
        {"vendor_id": 1, "document_type": "compliance_report"},
    )

    assert result.is_error is False
    params = calls[0]["kwargs"]["params"]
    assert params["type"] == "COMPLIANCE_REPORT"
    assert params["expand[]"] == ["downloadUrl"]


@pytest.mark.asyncio
async def test_get_vendor_document_expands_download_url(monkeypatch):
    """get_drata_vendor_document must request expand[]=downloadUrl so the signed
    URL it documents is actually returned (matching list_drata_vendor_documents)."""
    calls = _patch(
        monkeypatch,
        {
            "id": 7,
            "name": "soc.pdf",
            "type": "SOC_DOCUMENT",
            "downloadUrl": {"signedUrl": "https://signed.example/soc.pdf"},
        },
    )
    executor = _build_executor()
    result = await _call(
        executor, "get_drata_vendor_document", {"vendor_id": 1, "document_id": 7}
    )

    assert result.is_error is False
    assert calls[0]["url"].endswith("/vendors/1/documents/7")
    assert calls[0]["kwargs"]["params"]["expand[]"] == ["downloadUrl"]
    assert "https://signed.example/soc.pdf" in result.content


# ---------------------------------------------------------------------------
# Vendor questionnaires (sent to the vendor) — the corrected endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_vendor_questionnaires_hits_questionnaires_endpoint(monkeypatch):
    """Regression: the old tool listed /security-reviews under this name."""
    calls = _patch(monkeypatch, {"data": [], "pagination": {}})
    executor = _build_executor()
    result = await _call(executor, "list_drata_vendor_questionnaires", {"vendor_id": 1})

    assert result.is_error is False
    assert calls[0]["url"].endswith("/vendors/1/questionnaires")
    assert "/security-reviews" not in calls[0]["url"]


@pytest.mark.asyncio
async def test_send_vendor_questionnaire_uses_correct_endpoint_and_body(monkeypatch):
    """Regression: the old tool POSTed {type, contactEmail, message} to
    /security-reviews (rejected by Create Security Review). It now POSTs the
    documented body to /vendors/{id}/questionnaires."""
    calls = _patch(monkeypatch, {"id": 3})
    executor = _build_executor()
    result = await _call(
        executor,
        "send_drata_vendor_questionnaire",
        {
            "vendor_id": 1,
            "email": "[email protected]",
            "questionnaire_id": 2,
            "email_content": "Please complete our review.",
            "security_review_id": 9,
            "email_subject": "Security review",
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["url"].endswith("/vendors/1/questionnaires")
    assert "/security-reviews" not in call["url"]
    body = call["kwargs"]["json"]
    assert body["email"] == "[email protected]"
    assert body["questionnaireId"] == 2
    assert body["emailContent"] == "Please complete our review."
    assert body["securityReviewId"] == 9
    assert body["emailSubject"] == "Security review"


@pytest.mark.asyncio
async def test_send_vendor_questionnaire_requires_ids(monkeypatch):
    """questionnaire_id and security_review_id are required by the API, so
    omitting them fails fast instead of POSTing 0 (which Drata rejects)."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    missing_q = await _call(
        executor,
        "send_drata_vendor_questionnaire",
        {
            "vendor_id": 1,
            "email": "contact@vendor.example",
            "email_content": "hi",
            "security_review_id": 9,
        },
    )
    assert "questionnaire_id is required" in missing_q.content
    missing_sr = await _call(
        executor,
        "send_drata_vendor_questionnaire",
        {
            "vendor_id": 1,
            "email": "contact@vendor.example",
            "email_content": "hi",
            "questionnaire_id": 2,
        },
    )
    assert "security_review_id is required" in missing_sr.content
    assert not calls


@pytest.mark.asyncio
async def test_vendor_tools_coerce_non_string_inputs(monkeypatch):
    """Non-string args (object/number/bool/None) must not crash the handlers.

    The executor dispatches straight to the handler without jsonschema
    coercion, so a tool-test payload like document_type={...} or email=None
    used to raise AttributeError from .strip()/.split(). _coerce_str maps a
    non-string to "" so the existing required-field / enum guards return a
    friendly error (or treat an optional filter as absent) instead of raising.
    """
    calls = _patch(monkeypatch, {"data": [], "pagination": {}})
    executor = _build_executor()

    # Required string fields degrade to the friendly "required" error.
    missing_doc = await _call(
        executor,
        "upload_drata_vendor_document",
        {"vendor_id": 1, "document_type": {"unexpected": "object"}},
    )
    assert "document_type is required" in missing_doc.content
    assert "has no attribute" not in missing_doc.content

    bad_email = await _call(
        executor,
        "send_drata_vendor_questionnaire",
        {
            "vendor_id": 1,
            "email": None,
            "email_content": {"obj": 1},
            "questionnaire_id": 2,
            "security_review_id": 9,
        },
    )
    assert "recipient email is required" in bad_email.content
    assert "has no attribute" not in bad_email.content

    missing_deadline = await _call(
        executor,
        "create_drata_vendor_security_review",
        {"vendor_id": 1, "review_deadline_at": 20260930},
    )
    assert "review_deadline_at is required" in missing_deadline.content
    assert "has no attribute" not in missing_deadline.content

    assert not calls  # all three bailed out before any HTTP call

    # Optional CSV / enum filters degrade to "no filter" rather than raising.
    listed = await _call(
        executor,
        "list_drata_vendor_security_reviews",
        {"vendor_id": 1, "status": True, "review_type": 123, "decision": None},
    )
    assert listed.is_error is False
    assert "has no attribute" not in listed.content
    params = calls[0]["kwargs"]["params"]
    assert "status[]" not in params
    assert "type[]" not in params
    assert "decision[]" not in params

    listed_docs = await _call(
        executor,
        "list_drata_vendor_documents",
        {"vendor_id": 1, "document_type": 123},
    )
    assert listed_docs.is_error is False
    assert "has no attribute" not in listed_docs.content


# ---------------------------------------------------------------------------
# Atomic create-with-file (SOC report upload + review in one call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_security_review_with_file_posts_multipart(monkeypatch):
    calls = _patch(monkeypatch, {"id": 11, "type": "SOC_REPORT"})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_vendor_security_review_with_file",
        {
            "vendor_id": 1,
            "title": "AWS SOC 2 Type 2 2026",
            "review_deadline_at": "2026-09-30",
            "security_review_status": "IN_PROGRESS",
            "requester_user_id": 42,
            "document_type": "COMPLIANCE_REPORT",
            "file_content": "SOC 2 analysis",
            "file_name": "aws-soc2.csv",
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/vendors/1/security-reviews/with-file")
    # Multipart: scalar fields ride in `data`, the report rides in `files`.
    data = call["kwargs"]["data"]
    assert data["title"] == "AWS SOC 2 Type 2 2026"
    assert data["reviewDeadlineAt"] == "2026-09-30"
    assert data["securityReviewStatus"] == "IN_PROGRESS"
    assert data["securityReviewType"] == "SOC_REPORT"  # default
    assert data["requesterUserId"] == "42"  # stringified for multipart
    assert data["documentType"] == "COMPLIANCE_REPORT"
    assert "file" in call["kwargs"]["files"]
    assert call["kwargs"]["files"]["file"][0] == "aws-soc2.csv"


@pytest.mark.asyncio
async def test_create_security_review_with_file_requires_title_and_deadline(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    missing_title = await _call(
        executor,
        "create_drata_vendor_security_review_with_file",
        {
            "vendor_id": 1,
            "review_deadline_at": "2026-09-30",
            "file_content": "x",
            "file_name": "x.csv",
        },
    )
    assert "title is required" in missing_title.content
    missing_deadline = await _call(
        executor,
        "create_drata_vendor_security_review_with_file",
        {"vendor_id": 1, "title": "T", "file_content": "x", "file_name": "x.csv"},
    )
    assert "review_deadline_at is required" in missing_deadline.content
    assert not calls


@pytest.mark.asyncio
async def test_create_security_review_with_file_requires_one_source(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_vendor_security_review_with_file",
        {"vendor_id": 1, "title": "T", "review_deadline_at": "2026-09-30"},
    )
    assert "exactly one artifact source" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_create_security_review_with_file_rejects_bad_enum(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_vendor_security_review_with_file",
        {
            "vendor_id": 1,
            "title": "T",
            "review_deadline_at": "2026-09-30",
            "security_review_type": "SOC2",
            "file_content": "x",
            "file_name": "x.csv",
        },
    )
    assert "invalid security_review_type" in result.content
    assert not calls


# ---------------------------------------------------------------------------
# Delete security review (honest degradation when Drata has no delete route)
# ---------------------------------------------------------------------------


class _StatusResp:
    """A response double that exposes status_code / is_success for the delete tool."""

    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self._payload = payload or {}
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


def _patch_status(monkeypatch, status_code: int) -> list[dict]:
    """Mirror safe_request's expected_statuses contract for status-branching tools."""
    calls: list[dict] = []

    async def _fake_safe_request(
        client, method, url, provider, action, *, expected_statuses=None, **kwargs
    ):
        calls.append({"method": method, "url": url, "expected_statuses": expected_statuses})
        resp = _StatusResp(status_code)
        if resp.is_success:
            return resp, None
        return resp, f"Drata API error ({action}): HTTP {status_code}"

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return calls


@pytest.mark.asyncio
async def test_delete_security_review_success(monkeypatch):
    calls = _patch_status(monkeypatch, 204)
    executor = _build_executor()
    result = await _call(
        executor,
        "delete_drata_vendor_security_review",
        {"vendor_id": 1, "security_review_id": 9},
    )

    assert result.is_error is False
    assert "deleted" in result.content.lower()
    call = calls[0]
    assert call["method"] == "delete"
    assert call["url"].endswith("/vendors/1/security-reviews/9")


@pytest.mark.asyncio
async def test_delete_security_review_unsupported_returns_guidance(monkeypatch):
    calls = _patch_status(monkeypatch, 405)
    executor = _build_executor()
    result = await _call(
        executor,
        "delete_drata_vendor_security_review",
        {"vendor_id": 1, "security_review_id": 9},
    )

    assert result.is_error is False
    # Honest, actionable message — not a fabricated success or opaque error.
    assert "does not currently support" in result.content
    assert "reopen" in result.content.lower()
    assert "Drata UI" in result.content
    # It opted into the failed-response contract so it could branch on the code.
    assert 405 in (calls[0]["expected_statuses"] or set())


@pytest.mark.asyncio
async def test_delete_security_review_404_is_ambiguous(monkeypatch):
    """A 404 is NOT treated as definitive proof the route is unsupported - it can
    equally mean the vendor/review id doesn't exist. The message must surface
    that ambiguity and point at a way to verify existence first."""
    calls = _patch_status(monkeypatch, 404)
    executor = _build_executor()
    result = await _call(
        executor,
        "delete_drata_vendor_security_review",
        {"vendor_id": 1, "security_review_id": 9},
    )

    assert result.is_error is False
    assert "ambiguous" in result.content
    assert "does not exist" in result.content
    # Points at the verification path rather than asserting "unsupported".
    assert "get_drata_vendor_security_review" in result.content
    assert "NOT deleted" in result.content
    assert 404 in (calls[0]["expected_statuses"] or set())


@pytest.mark.asyncio
async def test_delete_security_review_requires_ids(monkeypatch):
    calls = _patch_status(monkeypatch, 204)
    executor = _build_executor()
    result = await _call(
        executor,
        "delete_drata_vendor_security_review",
        {"vendor_id": 0, "security_review_id": 9},
    )
    assert "required" in result.content
    assert not calls


# ---------------------------------------------------------------------------
# Delete vendor document - the API has no delete route, so the tool attempts
# the delete and degrades to honest guidance (mirrors the security-review delete).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_vendor_document_success(monkeypatch):
    calls = _patch_status(monkeypatch, 204)
    executor = _build_executor()
    result = await _call(
        executor,
        "delete_drata_vendor_document",
        {"vendor_id": 1, "document_id": 7},
    )

    assert result.is_error is False
    assert "deleted" in result.content.lower()
    call = calls[0]
    assert call["method"] == "delete"
    assert call["url"].endswith("/vendors/1/documents/7")


@pytest.mark.asyncio
async def test_delete_vendor_document_unsupported_returns_guidance(monkeypatch):
    calls = _patch_status(monkeypatch, 405)
    executor = _build_executor()
    result = await _call(
        executor,
        "delete_drata_vendor_document",
        {"vendor_id": 1, "document_id": 7},
    )

    assert result.is_error is False
    # Honest, actionable message - not a fabricated success or opaque error.
    assert "does not currently support" in result.content
    assert "Drata UI" in result.content
    assert "Reports & documents" in result.content
    # Must steer away from the destructive workarounds the API does expose.
    assert "remove the whole vendor" in result.content.lower()
    assert 405 in (calls[0]["expected_statuses"] or set())


@pytest.mark.asyncio
async def test_delete_vendor_document_404_is_ambiguous(monkeypatch):
    """A 404 is NOT proof the route is unsupported - the document/vendor id may
    simply not exist - so the message surfaces the ambiguity and a verify path."""
    calls = _patch_status(monkeypatch, 404)
    executor = _build_executor()
    result = await _call(
        executor,
        "delete_drata_vendor_document",
        {"vendor_id": 1, "document_id": 7},
    )

    assert result.is_error is False
    assert "ambiguous" in result.content
    assert "does not exist" in result.content
    assert "get_drata_vendor_document" in result.content
    assert "NOT deleted" in result.content
    assert 404 in (calls[0]["expected_statuses"] or set())


@pytest.mark.asyncio
async def test_delete_vendor_document_requires_ids(monkeypatch):
    calls = _patch_status(monkeypatch, 204)
    executor = _build_executor()
    result = await _call(
        executor,
        "delete_drata_vendor_document",
        {"vendor_id": 0, "document_id": 7},
    )
    assert "required" in result.content
    assert not calls


# ---------------------------------------------------------------------------
# Vendor security owner (userId) on create / update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_vendor_sets_security_owner(monkeypatch):
    calls = _patch(monkeypatch, {"id": 1})
    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_vendor",
        {"vendor_id": 1, "user_id": 77, "vendor_type": "supplier"},
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "put"
    assert call["url"].endswith("/vendors/1")
    body = call["kwargs"]["json"]
    assert body["userId"] == 77
    assert body["type"] == "SUPPLIER"


@pytest.mark.asyncio
async def test_create_vendor_sets_security_owner(monkeypatch):
    calls = _patch(monkeypatch, {"id": 5})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_vendor",
        {"name": "Acme", "user_id": 77},
    )

    assert result.is_error is False
    assert calls[0]["kwargs"]["json"]["userId"] == 77


@pytest.mark.asyncio
async def test_create_vendor_normalizes_vendor_type(monkeypatch):
    calls = _patch(monkeypatch, {"id": 6})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_vendor",
        {"name": "Acme", "vendor_type": "supplier"},
    )

    assert result.is_error is False
    # create normalizes vendor_type the same way update does (-> uppercased) so a
    # lowercase value doesn't trip Drata's enum validation.
    assert calls[0]["kwargs"]["json"]["type"] == "SUPPLIER"


@pytest.mark.asyncio
async def test_update_vendor_requires_a_field(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(executor, "update_drata_vendor", {"vendor_id": 1})
    assert "provide at least one field" in result.content
    assert not calls
