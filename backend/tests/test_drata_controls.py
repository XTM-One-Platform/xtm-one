"""Tests for the Drata controls management tooling - scope surfacing/filtering,
the corrected create/update payloads, and the requirement/scope endpoints.

The headline regression: a control's in-scope / out-of-scope status IS in the v2
API (the ``archivedAt`` field; the ``isArchived`` list filter; the
``markInScope`` / ``markOutOfScope`` Perform Control Action), but our tooling
never surfaced or filtered on it. These tests pin that it now does.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"


class _Resp:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.content = b"{}"
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


def _patch(monkeypatch, payload: dict, status_code: int = 200) -> list[dict]:
    calls: list[dict] = []

    async def _fake_safe_request(
        client, method, url, provider, action, *, expected_statuses=None, **kwargs
    ):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        return _Resp(payload, status_code), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return calls


def _build_executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    return executor


async def _call(executor, name, args):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))


_TWO_CONTROLS = {
    "data": [
        {
            "id": 196,
            "code": "DCF-542",
            "name": "Representative for Non-EU Controllers/Processors",
            "archivedAt": "2025-01-02T00:00:00Z",
            "frameworkTags": ["GDPR"],
            "flags": {"isReady": True},
            "owners": {"data": []},
        },
        {
            "id": 100,
            "code": "DCF-1",
            "name": "Access Control",
            "archivedAt": None,
            "frameworkTags": ["SOC_2"],
            "flags": {"isReady": False},
            "owners": {"data": [{"id": 42, "firstName": "Sam", "lastName": "H"}]},
        },
    ],
    "pagination": {},
}


# ---------------------------------------------------------------------------
# Scope filter + surfacing on list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_controls_sends_is_archived_scope_filter(monkeypatch):
    calls = _patch(monkeypatch, {"data": [], "pagination": {}})
    executor = _build_executor()
    result = await _call(
        executor, "list_drata_controls", {"is_archived": "false"}
    )

    assert result.is_error is False
    params = calls[0]["kwargs"]["params"]
    assert params.get("isArchived") is False


@pytest.mark.asyncio
async def test_list_controls_surfaces_scope(monkeypatch):
    _patch(monkeypatch, _TWO_CONTROLS)
    executor = _build_executor()
    result = await _call(executor, "list_drata_controls", {})

    assert result.is_error is False
    # DCF-542 is archived -> out of scope (with date); DCF-1 -> in scope.
    assert "Scope: out of scope (since 2025-01-02T00:00:00Z)" in result.content
    assert "Scope: in scope" in result.content


@pytest.mark.asyncio
async def test_list_controls_compact_shows_scope(monkeypatch):
    _patch(monkeypatch, _TWO_CONTROLS)
    executor = _build_executor()
    result = await _call(executor, "list_drata_controls", {"compact": "true"})

    assert result.is_error is False
    assert "scope=out of scope" in result.content
    assert "scope=in scope" in result.content


@pytest.mark.asyncio
async def test_list_controls_new_bool_filters_sent(monkeypatch):
    calls = _patch(monkeypatch, {"data": [], "pagination": {}})
    executor = _build_executor()
    result = await _call(
        executor,
        "list_drata_controls",
        {
            "has_policy": "true",
            "has_passing_test": "false",
            "is_monitored": "true",
            "is_enabled": "true",
            "policy_id": 7,
            "ticket_status": "in_progress",
        },
    )

    assert result.is_error is False
    params = calls[0]["kwargs"]["params"]
    assert params["hasPolicy"] is True
    assert params["hasPassingTest"] is False
    assert params["isMonitored"] is True
    assert params["isEnabled"] is True
    assert params["policyId"] == 7
    assert params["ticketStatus"] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_list_controls_framework_tag_filters_client_side(monkeypatch):
    _patch(monkeypatch, _TWO_CONTROLS)
    executor = _build_executor()
    result = await _call(
        executor, "list_drata_controls", {"framework_tag": "gdpr"}
    )

    assert result.is_error is False
    assert "DCF-542" in result.content
    assert "DCF-1" not in result.content


# ---------------------------------------------------------------------------
# Scope surfacing on get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_control_surfaces_out_of_scope_and_rationale(monkeypatch):
    calls = _patch(
        monkeypatch,
        {
            "id": 196,
            "code": "DCF-542",
            "name": "Representative for Non-EU Controllers/Processors",
            "archivedAt": "2025-01-02T00:00:00Z",
            "rationale": "Filigran is EU-established; non-EU representative N/A.",
            "flags": {"isReady": False, "hasEvidence": False},
            "testIds": {"passing": [1, 2], "failing": [3], "monitored": [1, 2, 4]},
            "evidenceIds": {"validExternalEvidenceIds": [10]},
        },
    )
    executor = _build_executor()
    result = await _call(
        executor,
        "get_drata_control",
        {"workspace_id": 1, "control_id": "code:DCF-542"},
    )

    assert result.is_error is False
    # code: prefix is passed straight through in the path.
    assert calls[0]["url"].endswith("/workspaces/1/controls/code:DCF-542")
    assert "Scope: OUT OF SCOPE (since 2025-01-02T00:00:00Z)" in result.content
    assert "Out-of-scope rationale: Filigran is EU-established" in result.content
    assert "2 passing, 1 failing, 3 monitored" in result.content
    # All the get expands are requested.
    expand = calls[0]["kwargs"]["params"]["expand[]"]
    assert "testIds" in expand and "evidenceIds" in expand and "topics" in expand


@pytest.mark.asyncio
async def test_get_control_surfaces_in_scope(monkeypatch):
    _patch(
        monkeypatch,
        {"id": 1, "code": "DCF-1", "name": "Access", "archivedAt": None, "flags": {}},
    )
    executor = _build_executor()
    result = await _call(
        executor, "get_drata_control", {"workspace_id": 1, "control_id": 1}
    )
    assert result.is_error is False
    assert "Scope: IN SCOPE" in result.content


# ---------------------------------------------------------------------------
# Perform Control Action (set scope)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_control_scope_in(monkeypatch):
    calls = _patch(monkeypatch, {"id": 196})
    executor = _build_executor()
    result = await _call(
        executor,
        "set_drata_control_scope",
        {"workspace_id": 1, "control_id": 196, "in_scope": "true"},
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/workspaces/1/controls/196/actions")
    assert call["kwargs"]["json"] == {"action": "markInScope"}
    assert "marked in scope" in result.content


@pytest.mark.asyncio
async def test_set_control_scope_out_requires_rationale(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "set_drata_control_scope",
        {"workspace_id": 1, "control_id": 196, "in_scope": "false"},
    )
    assert "rationale is required" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_set_control_scope_out_with_rationale(monkeypatch):
    calls = _patch(monkeypatch, {"id": 196})
    executor = _build_executor()
    result = await _call(
        executor,
        "set_drata_control_scope",
        {
            "workspace_id": 1,
            "control_id": 196,
            "in_scope": "false",
            "rationale": "Not applicable - EU established",
        },
    )

    assert result.is_error is False
    body = calls[0]["kwargs"]["json"]
    assert body["action"] == "markOutOfScope"
    assert body["rationale"] == "Not applicable - EU established"
    assert "marked out of scope" in result.content


@pytest.mark.asyncio
async def test_set_control_scope_already_in_state_204(monkeypatch):
    calls = _patch(monkeypatch, {}, status_code=204)
    executor = _build_executor()
    result = await _call(
        executor,
        "set_drata_control_scope",
        {"workspace_id": 1, "control_id": 196, "in_scope": "true"},
    )
    assert result.is_error is False
    assert "already in scope" in result.content
    assert calls  # the request was still issued


# ---------------------------------------------------------------------------
# Create / update mappings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_control_sends_mapping_arrays_no_framework_tags(monkeypatch):
    calls = _patch(monkeypatch, {"id": 9, "code": "DRA-9", "name": "X"})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_control",
        {
            "workspace_id": 1,
            "name": "X",
            "description": "d",
            "owner_ids": "3",
            "requirement_ids": "1,2",
            "policy_ids": "5",
        },
    )

    assert result.is_error is False
    body = calls[0]["kwargs"]["json"]
    assert body["ownersIds"] == [3]
    assert body["requirementIds"] == [1, 2]
    assert body["policyIds"] == [5]
    # The ineffective frameworkTags field is no longer sent.
    assert "frameworkTags" not in body


@pytest.mark.asyncio
async def test_update_control_sends_code_and_mappings(monkeypatch):
    calls = _patch(monkeypatch, {"id": 9})
    executor = _build_executor()
    result = await _call(
        executor,
        "update_drata_control",
        {
            "workspace_id": 1,
            "control_id": 9,
            "code": "DRA-69",
            "requirement_ids": "1,2,3",
            "test_ids": "7",
        },
    )

    assert result.is_error is False
    body = calls[0]["kwargs"]["json"]
    assert body["code"] == "DRA-69"
    assert body["requirementIds"] == [1, 2, 3]
    assert body["testIds"] == [7]


# ---------------------------------------------------------------------------
# Requirements list / reset / compare
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_control_requirements_framework_filter(monkeypatch):
    calls = _patch(monkeypatch, {"data": [], "pagination": {}})
    executor = _build_executor()
    result = await _call(
        executor,
        "list_drata_control_requirements",
        {"workspace_id": 1, "control_id": 9, "framework_tag": "soc_2, iso27001"},
    )

    assert result.is_error is False
    assert calls[0]["url"].endswith("/workspaces/1/controls/9/requirements")
    assert calls[0]["kwargs"]["params"]["frameworkTag[]"] == ["SOC_2", "ISO27001"]


@pytest.mark.asyncio
async def test_reset_requirement_mappings_caps_at_100(monkeypatch):
    calls = _patch(monkeypatch, {"results": []})
    executor = _build_executor()
    ids = ",".join(str(i) for i in range(101))
    result = await _call(
        executor,
        "reset_drata_control_requirement_mappings",
        {"workspace_id": 1, "control_ids": ids},
    )
    assert "at most 100" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_reset_requirement_mappings_reports_results(monkeypatch):
    calls = _patch(
        monkeypatch,
        {
            "results": [
                {"controlId": 1, "success": True},
                {"controlId": 2, "success": False, "error": "Custom Control"},
            ]
        },
    )
    executor = _build_executor()
    result = await _call(
        executor,
        "reset_drata_control_requirement_mappings",
        {"workspace_id": 1, "control_ids": "1,2"},
    )

    assert result.is_error is False
    assert calls[0]["url"].endswith("/controls/action-reset-mappings")
    assert calls[0]["kwargs"]["json"] == {"controlIds": [1, 2]}
    assert "1 succeeded, 1 failed" in result.content
    assert "Custom Control" in result.content


@pytest.mark.asyncio
async def test_compare_requirements_sends_control_ids(monkeypatch):
    calls = _patch(
        monkeypatch,
        {
            "data": [
                {
                    "controlId": 1,
                    "alignedRequirements": [{"name": "CC1.1"}],
                    "userMappedRequirements": [],
                    "templateMappedRequirements": [{"name": "CC1.1"}],
                }
            ]
        },
    )
    executor = _build_executor()
    result = await _call(
        executor,
        "compare_drata_control_requirements",
        {"workspace_id": 1, "control_ids": "1,2"},
    )

    assert result.is_error is False
    assert calls[0]["url"].endswith("/controls-requirement-comparison")
    assert calls[0]["kwargs"]["params"]["controlIds[]"] == [1, 2]
    assert "Control 1" in result.content
    assert "CC1.1" in result.content
