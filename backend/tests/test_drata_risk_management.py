"""Tests for the Drata risk-management write paths.

These pin the behaviours added when the Drata risk surface was completed so an
agent can run the programme end-to-end:

  * ``update_drata_risk`` can reassign a stale / departed owner — the v2 PUT
    accepts ``owners`` as ``[{"id": n}]`` and REPLACES the set with the IDs
    passed (the headline gap: the old tool exposed no owner field, so 8 ACTIVE
    risks stayed owned by departed personnel and had to be fixed in the UI).
  * Reviewers / categories / controls reassign the same way; treatment-gated
    fields are dropped when the call sets ``treatment_plan="UNTREATED"`` (Drata
    rejects them) while owners are always allowed.
  * ``create_drata_risk`` forwards the previously-missing fields (identifiedAt,
    reviewers, residual scores, completion dates).
  * ``list_drata_risks`` forwards the ``vendorId`` filter.
  * Risk Registers / Risk Library / Risk Notes / Risk Documents are wired to the
    documented v2 endpoints and request shapes.
  * ``get_drata_vendor_stats`` hits ``/vendors-stats`` (not the 400-returning
    ``/vendors/stats``) and always sends at least one required ``expand[]`` scope.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"


class _Resp:
    """Minimal ``httpx.Response`` stand-in for the captured tool calls."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


def _patch(monkeypatch, payload: dict) -> list[dict]:
    """Patch ``safe_request`` to capture each call and return a fixed payload."""
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
# update_drata_risk — owner reassignment (the headline fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_risk_reassigns_owners_as_id_objects(monkeypatch):
    """Owner reassignment PUTs ``owners: [{"id": n}]`` to the risk endpoint —
    the field that was missing, forcing owner changes into the Drata UI."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_risk",
        {"risk_id": 42, "owner_ids": "100, 200"},
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "put"
    assert call["url"].endswith("/risk-registers/1/risks/42")
    body = call["kwargs"]["json"]
    # v2 shape: array of {"id": n} objects, REPLACING the owner set.
    assert body["owners"] == [{"id": 100}, {"id": 200}]
    # The summary reports what changed.
    assert "owners" in result.content


@pytest.mark.asyncio
async def test_update_risk_replaces_reviewers_categories_controls(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_risk",
        {
            "risk_id": 7,
            "risk_register_id": 3,
            "treatment_plan": "MITIGATE",
            "reviewer_ids": "5",
            "category_ids": "11,12",
            "control_ids": "21",
            "identified_at": "2026-01-02",
            "anticipated_completion_date": "2026-03-01",
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["url"].endswith("/risk-registers/3/risks/7")
    body = call["kwargs"]["json"]
    assert body["reviewers"] == [{"id": 5}]
    assert body["categories"] == [{"id": 11}, {"id": 12}]
    assert body["controls"] == [{"id": 21}]
    assert body["identifiedAt"] == "2026-01-02"
    assert body["anticipatedCompletionDate"] == "2026-03-01"
    assert body["treatmentPlan"] == "MITIGATE"


@pytest.mark.asyncio
async def test_update_risk_drops_treatment_gated_fields_when_untreating(monkeypatch):
    """Setting treatment_plan=UNTREATED drops fields Drata forbids on untreated
    risks (reviewers, residual scores, treatment details, completion dates) so
    the call doesn't 400 — but owners (never gated) still go through."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_risk",
        {
            "risk_id": 9,
            "treatment_plan": "UNTREATED",
            "owner_ids": "100",
            "reviewer_ids": "5",
            "treatment_details": "should drop",
            "residual_impact": 2,
            "residual_likelihood": 2,
            "anticipated_completion_date": "2026-03-01",
            "completion_date": "2026-03-02",
        },
    )

    assert result.is_error is False
    body = calls[0]["kwargs"]["json"]
    assert body["treatmentPlan"] == "UNTREATED"
    assert body["owners"] == [{"id": 100}]  # owners always allowed
    for forbidden in (
        "reviewers",
        "treatmentDetails",
        "residualImpact",
        "residualLikelihood",
        "anticipatedCompletionDate",
        "completionDate",
    ):
        assert forbidden not in body


@pytest.mark.asyncio
async def test_update_risk_requires_a_field(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(executor, "update_drata_risk", {"risk_id": 1})
    assert "at least one field" in result.content
    assert not calls  # nothing sent


@pytest.mark.asyncio
async def test_update_risk_accepts_iso8601_dates(monkeypatch):
    """The tool advertises YYYY-MM-DD OR full ISO-8601 dates; a full timestamp
    is forwarded unchanged (the handler passes dates straight through), so the
    description must not claim a date-only format the handler doesn't enforce."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_risk",
        {
            "risk_id": 8,
            "treatment_plan": "MITIGATE",
            "identified_at": "2026-01-02T15:04:05Z",
            "completion_date": "2026-03-01T00:00:00Z",
        },
    )

    assert result.is_error is False
    body = calls[0]["kwargs"]["json"]
    assert body["identifiedAt"] == "2026-01-02T15:04:05Z"
    assert body["completionDate"] == "2026-03-01T00:00:00Z"


# ---------------------------------------------------------------------------
# create_drata_risk — newly-forwarded fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_risk_forwards_new_fields(monkeypatch):
    calls = _patch(monkeypatch, {"id": 1, "riskId": "RISK-1", "title": "T", "score": 9})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_risk",
        {
            "title": "Departed-owner risk",
            "description": "desc",
            "treatment_plan": "MITIGATE",
            "owner_ids": "100",
            "reviewer_ids": "5",
            "identified_at": "2026-01-01",
            "residual_impact": 2,
            "residual_likelihood": 3,
            "completion_date": "2026-02-01",
        },
    )

    assert result.is_error is False
    body = calls[0]["kwargs"]["json"]
    assert body["owners"] == [{"id": 100}]
    assert body["reviewers"] == [{"id": 5}]
    assert body["identifiedAt"] == "2026-01-01"
    assert body["residualImpact"] == 2
    assert body["residualLikelihood"] == 3
    assert body["completionDate"] == "2026-02-01"


@pytest.mark.asyncio
async def test_create_risk_untreated_drops_reviewers_and_residual(monkeypatch):
    calls = _patch(monkeypatch, {"id": 1, "riskId": "RISK-1", "title": "T"})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_risk",
        {
            "title": "T",
            "description": "d",
            "owner_ids": "100",
            "reviewer_ids": "5",
            "residual_impact": 2,
        },
    )

    assert result.is_error is False
    body = calls[0]["kwargs"]["json"]
    # Default treatment_plan is UNTREATED -> owners stay, treatment-gated drop.
    assert body["owners"] == [{"id": 100}]
    assert "reviewers" not in body
    assert "residualImpact" not in body


@pytest.mark.asyncio
async def test_create_risk_coerces_string_ints(monkeypatch):
    """The tool-test path can deliver schema integers as strings; create coerces
    impact/likelihood/residual_* so Drata receives real numbers, not strings
    (parity with update_drata_risk)."""
    calls = _patch(monkeypatch, {"id": 1, "riskId": "RISK-1", "title": "T"})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_risk",
        {
            "title": "T",
            "description": "d",
            "impact": "4",
            "likelihood": "5",
            "treatment_plan": "MITIGATE",
            "residual_impact": "2",
            "residual_likelihood": "1",
        },
    )

    assert result.is_error is False
    body = calls[0]["kwargs"]["json"]
    assert body["impact"] == 4
    assert body["likelihood"] == 5
    assert body["residualImpact"] == 2
    assert body["residualLikelihood"] == 1


# ---------------------------------------------------------------------------
# list_drata_risks — vendor filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_risks_forwards_vendor_filter(monkeypatch):
    calls = _patch(monkeypatch, {"data": [], "pagination": {}})
    executor = _build_executor()
    result = await _call(executor, "list_drata_risks", {"vendor_id": 55})

    assert result.is_error is False
    params = calls[0]["kwargs"]["params"]
    assert params.get("vendorId") == 55


@pytest.mark.asyncio
async def test_list_risks_shows_owner_emails(monkeypatch):
    """Owner emails are surfaced so an agent can spot departed-personnel owners."""
    _patch(
        monkeypatch,
        {
            "data": [
                {
                    "id": 1,
                    "riskId": "AC-04",
                    "title": "Stale",
                    "status": "ACTIVE",
                    "owners": [
                        {
                            "firstName": "Sophie",
                            "lastName": "Hubaut",
                            "email": "sophie@old.example",
                        }
                    ],
                }
            ],
            "pagination": {},
        },
    )
    executor = _build_executor()
    result = await _call(executor, "list_drata_risks", {"status": "ACTIVE"})

    assert result.is_error is False
    assert "sophie@old.example" in result.content


@pytest.mark.asyncio
async def test_list_risks_owner_without_name_no_duplicate_email(monkeypatch):
    """When Drata returns an owner with only an email (no first/last name),
    the row shows the email once, not the duplicated 'email <email>' form."""
    _patch(
        monkeypatch,
        {
            "data": [
                {
                    "id": 1,
                    "riskId": "AC-04",
                    "title": "Stale",
                    "status": "ACTIVE",
                    "owners": [{"email": "departed@old.example"}],
                }
            ],
            "pagination": {},
        },
    )
    executor = _build_executor()
    result = await _call(executor, "list_drata_risks", {"status": "ACTIVE"})

    assert result.is_error is False
    assert "departed@old.example" in result.content
    assert "departed@old.example <departed@old.example>" not in result.content


# ---------------------------------------------------------------------------
# Risk registers CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_risk_register_sends_flat_owner_ids(monkeypatch):
    """Risk register ownerIds / workspaceIds are flat numeric arrays (not the
    ``[{"id": n}]`` shape risks/controls/owners use)."""
    calls = _patch(monkeypatch, {"id": 9, "name": "Reg"})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_risk_register",
        {"name": "Reg", "owner_ids": "1,2,3", "workspace_ids": "10"},
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/risk-registers")
    body = call["kwargs"]["json"]
    assert body["ownerIds"] == [1, 2, 3]
    assert body["workspaceIds"] == [10]


@pytest.mark.asyncio
async def test_delete_risk_register(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor, "delete_drata_risk_register", {"risk_register_id": 4}
    )
    assert result.is_error is False
    assert calls[0]["method"] == "delete"
    assert calls[0]["url"].endswith("/risk-registers/4")


@pytest.mark.asyncio
async def test_update_risk_register_tolerates_null_name(monkeypatch):
    """A null name on the tool-test path must not raise AttributeError; it is
    simply omitted from the update payload."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_risk_register",
        {"risk_register_id": 4, "name": None, "description": "Updated"},
    )

    assert result.is_error is False
    body = calls[0]["kwargs"]["json"]
    assert "name" not in body
    assert body["description"] == "Updated"


# ---------------------------------------------------------------------------
# Risk insights
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_insights_filters_and_formats(monkeypatch):
    calls = _patch(
        monkeypatch,
        {
            "scored": 63,
            "remaining": 8,
            "riskPosture": [{"title": "Low", "count": 5}],
            "treatmentOverview": {"untreated": 5, "mitigate": 33},
            "categoryBreakdown": [
                {
                    "category": {"name": "Access Control"},
                    "severity": [{"title": "Low", "count": 5}],
                }
            ],
        },
    )
    executor = _build_executor()
    result = await _call(
        executor,
        "get_drata_risk_insights",
        {"risk_register_id": 1, "owner_ids": "1,2", "status": "ACTIVE"},
    )

    assert result.is_error is False
    assert calls[0]["url"].endswith("/risk-registers/1/insights")
    params = calls[0]["kwargs"]["params"]
    assert params["ownerIds[]"] == [1, 2]
    assert params["status[]"] == ["ACTIVE"]
    assert "63 scored" in result.content
    assert "Access Control" in result.content


# ---------------------------------------------------------------------------
# Risk library
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_risk_library_by_ids(monkeypatch):
    calls = _patch(
        monkeypatch,
        {"data": [{"id": 100, "riskId": "AC-04", "title": "Pw", "registerId": 1}]},
    )
    executor = _build_executor()
    result = await _call(
        executor,
        "copy_drata_risk_library",
        {"register_id": 1, "risk_ids": "AA-01, AA-02"},
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/risk-library/action-copy")
    body = call["kwargs"]["json"]
    assert body["bulkActionType"] == "COPY_BY_IDS"
    assert body["riskIds"] == ["AA-01", "AA-02"]
    assert body["registerId"] == 1
    assert "Copied 1 risk" in result.content


@pytest.mark.asyncio
async def test_copy_risk_library_by_groups(monkeypatch):
    calls = _patch(monkeypatch, {"data": []})
    executor = _build_executor()
    result = await _call(
        executor,
        "copy_drata_risk_library",
        {"register_id": 2, "risk_groups": "cloud_environment"},
    )

    assert result.is_error is False
    body = calls[0]["kwargs"]["json"]
    assert body["bulkActionType"] == "COPY_BY_GROUPS"
    assert body["riskGroups"] == ["CLOUD_ENVIRONMENT"]


@pytest.mark.asyncio
async def test_copy_risk_library_rejects_unknown_group(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor, "copy_drata_risk_library", {"risk_groups": "NOPE"}
    )
    assert "invalid risk group" in result.content
    assert not calls  # nothing sent


@pytest.mark.asyncio
async def test_copy_risk_library_requires_input(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(executor, "copy_drata_risk_library", {})
    assert "provide risk_ids or risk_groups" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_copy_risk_library_rejects_both_ids_and_groups(monkeypatch):
    """risk_ids and risk_groups are mutually exclusive (a single bulkActionType
    per call), so passing both is rejected instead of silently dropping the
    groups under COPY_BY_IDS."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "copy_drata_risk_library",
        {"risk_ids": "AA-01", "risk_groups": "CLOUD_ENVIRONMENT"},
    )
    assert "not both" in result.content
    assert not calls  # nothing sent


# ---------------------------------------------------------------------------
# Risk notes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_risk_note_posts_comment(monkeypatch):
    calls = _patch(monkeypatch, {"id": 123, "comment": "x"})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_risk_note",
        {"risk_id": 42, "comment": "Owner reassigned to new DPP lead"},
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/risk-registers/1/risks/42/notes")
    assert call["kwargs"]["json"] == {"comment": "Owner reassigned to new DPP lead"}


@pytest.mark.asyncio
async def test_delete_risk_note(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "delete_drata_risk_note",
        {"risk_id": 42, "note_id": 123, "risk_register_id": 2},
    )
    assert result.is_error is False
    assert calls[0]["method"] == "delete"
    assert calls[0]["url"].endswith("/risk-registers/2/risks/42/notes/123")


# ---------------------------------------------------------------------------
# Risk documents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_risk_document_multipart_field_is_documents(monkeypatch):
    """A text document is uploaded as multipart under the ``documents`` field to
    the risk documents endpoint (no PDF render needed for .md)."""
    calls = _patch(monkeypatch, {"data": [{"id": 5, "name": "note.md"}]})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_risk_document",
        {
            "risk_id": 42,
            "file_content": "# Risk memo",
            "file_name": "note.md",
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/risk-registers/1/risks/42/documents")
    files = call["kwargs"]["files"]
    assert files[0][0] == "documents"
    assert files[0][1][0] == "note.md"


@pytest.mark.asyncio
async def test_upload_risk_document_requires_one_source(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(executor, "upload_drata_risk_document", {"risk_id": 42})
    assert "exactly one artifact source" in result.content
    assert not calls


# ---------------------------------------------------------------------------
# Vendor statistics — the broken endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_stats_uses_correct_endpoint_and_default_scopes(monkeypatch):
    """The fix: hit ``/vendors-stats`` (not the 400-returning ``/vendors/stats``)
    and always send at least one required ``expand[]`` scope (all by default)."""
    calls = _patch(
        monkeypatch,
        {
            "risk": [{"key": "HIGH", "count": 15}, {"key": "LOW", "count": 45}],
            "status": [{"key": "ACTIVE", "count": 118}],
        },
    )
    executor = _build_executor()
    result = await _call(executor, "get_drata_vendor_stats", {})

    assert result.is_error is False
    call = calls[0]
    assert call["url"].endswith("/vendors-stats")
    assert not call["url"].endswith("/vendors/stats")
    scopes = call["kwargs"]["params"]["expand[]"]
    assert set(scopes) == set(drata_common._VENDOR_STATS_SCOPES)
    assert "risk: HIGH=15, LOW=45" in result.content


@pytest.mark.asyncio
async def test_vendor_stats_explicit_scopes_and_include_archived(monkeypatch):
    calls = _patch(monkeypatch, {"risk": []})
    executor = _build_executor()
    result = await _call(
        executor,
        "get_drata_vendor_stats",
        {"scopes": "risk, status", "include_archived": True},
    )

    assert result.is_error is False
    params = calls[0]["kwargs"]["params"]
    assert params["expand[]"] == ["risk", "status"]
    assert params["includeArchived"] is True


@pytest.mark.asyncio
async def test_vendor_stats_rejects_invalid_scope(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor, "get_drata_vendor_stats", {"scopes": "risk, bogus"}
    )
    assert "invalid scope" in result.content
    assert not calls  # nothing sent


@pytest.mark.asyncio
async def test_vendor_stats_normalizes_scope_case_and_dedupes(monkeypatch):
    """Scopes are matched case-insensitively, mapped back to the canonical
    camelCase Drata enum, and de-duped, so "RISK, risk, haspii" resolves to
    ["risk", "hasPii"] instead of hitting an avoidable invalid-scope error."""
    calls = _patch(monkeypatch, {"risk": [], "hasPii": []})
    executor = _build_executor()
    result = await _call(
        executor, "get_drata_vendor_stats", {"scopes": "RISK, risk, haspii"}
    )

    assert result.is_error is False
    params = calls[0]["kwargs"]["params"]
    assert params["expand[]"] == ["risk", "hasPii"]
