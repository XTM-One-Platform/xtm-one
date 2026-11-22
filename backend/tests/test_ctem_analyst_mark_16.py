"""The analyst's mark in OpenCTI answers the CTEM review gate (CTEM-16 slice 2, #1089).

Pinned without a network or a database:
- the mark is the policy's label (default analyst-reviewed), on a Grouping only;
- only the pending coverage requests on the marked Grouping are approved;
- a pause holding any other call is never answered by the mark;
- the resolution entry names the mark as the answer;
- the decision is stamped with the mark's surface and the run's phone buttons are voided;
- an unverified delivery approves nothing;
- the listen-only assignment provisions Grouping updates and never runs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS, CTEM_F2_ANALYST_MARK_ASSIGNMENT
from app.services import ctem_analyst_mark as mark
from app.services import opencti_trigger_provisioning as prov
from app.services.organization_context import PrioritizationPolicy

GROUPING = "grouping--0b6b3b1e-7e2a-4b0e-9e27-7b2c3f1d2a10"
INTERNAL = "7d4c9a2e-1f3b-4a5c-8d6e-9f0a1b2c3d4e"


def _delivery(labels: list[str], kind: str = "grouping") -> dict:
    return {
        "type": "update",
        "data": {
            "id": GROUPING,
            "type": kind,
            "labels": labels,
            "extensions": {"extension-definition--opencti": {"id": INTERNAL}},
        },
    }


def _coverage(call_id: str, covered: str, action: str = "create") -> dict:
    return {
        "tool_call_id": call_id,
        "tool_name": mark.COVERAGE_TOOL,
        "arguments": {"action": action, "covered_id": covered, "name": "NGINX HTTP/2 RCE"},
    }


def test_the_label_is_the_policy_one_else_the_default():
    assert mark.mark_label(None) == "analyst-reviewed"
    assert mark.mark_label(PrioritizationPolicy()) == "analyst-reviewed"
    assert mark.mark_label(PrioritizationPolicy(analyst_review_label="CTI OK")) == "cti ok"


def test_a_labelled_grouping_is_marked_by_both_ids():
    assert mark.marked_grouping(_delivery(["Analyst-Reviewed"]), "analyst-reviewed") == {GROUPING, INTERNAL}


@pytest.mark.parametrize(
    "payload",
    [_delivery(["tlp:amber"]), _delivery(["analyst-reviewed"], kind="report"), {"data": "x"}, {}],
)
def test_nothing_else_is_a_mark(payload):
    assert mark.marked_grouping(payload, "analyst-reviewed") == set()


def test_only_the_coverage_requests_on_the_marked_grouping_are_approved():
    pending = [
        _coverage("a", INTERNAL),
        _coverage("b", "grouping--other"),
        _coverage("c", GROUPING, action="update"),
        {"tool_call_id": "d", "tool_name": "create_jira_issue", "arguments": {}},
    ]
    assert mark.waiting_calls(pending, {GROUPING, INTERNAL}) == ["a"]


def test_a_pause_holding_another_call_is_not_answered():
    pending = [_coverage("a", GROUPING), {"tool_call_id": "d", "tool_name": "create_jira_issue", "arguments": {}}]
    assert mark.approvals_for(pending, ["a"]) is None


def test_a_pause_of_coverage_requests_alone_is_approved_whole():
    pending = [_coverage("a", GROUPING), _coverage("b", INTERNAL)]
    decisions = mark.approvals_for(pending, ["b", "a"])
    assert decisions == {
        "a": {"tool_call_id": "a", "decision": "approve", "rejection_reason": None},
        "b": {"tool_call_id": "b", "decision": "approve", "rejection_reason": None},
    }
    assert mark.approvals_for([], []) is None


def test_the_resolution_entry_names_the_mark():
    now = datetime(2026, 11, 22, 6, 30, tzinfo=UTC)
    entry = mark.resolution_entry("Approval required before running", "analyst-reviewed", ["a"], now)
    assert entry["resolution"] == "answered"
    assert entry["surface"] == mark.SURFACE
    assert entry["responded_by"] == "OpenCTI analyst mark"
    assert "analyst-reviewed" in entry["response"] and entry["responded_at"] == now.isoformat()


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list:
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Db:
    def __init__(self, run) -> None:
        self.run = run

    async def execute(self, _statement) -> _Result:
        return _Result([self.run])


@pytest.mark.asyncio
async def test_the_mark_answer_is_stamped_with_its_surface_and_voids_the_run_buttons(monkeypatch):
    from types import SimpleNamespace

    from app.models.decision_hooks import answer_surface
    from app.services import assignment_engine, push_action_tokens

    run = SimpleNamespace(
        id="run-1",
        status="awaiting_human",
        approval_decisions=None,
        pending_approvals=[_coverage("a", GROUPING)],
        human_input_history=[],
        human_input_question="Approval required before running",
    )
    seen: dict[str, object] = {}

    async def _resume(_db, resumed_run):
        seen["surface"] = answer_surface.get()
        seen["decisions"] = resumed_run.approval_decisions

    async def _void(run_id):
        seen["voided"] = run_id

    monkeypatch.setattr(assignment_engine, "resume_assignment_run", _resume)
    monkeypatch.setattr(push_action_tokens, "void_run_tokens", _void)
    assert await mark.resume_marked(_Db(run), {GROUPING}, "analyst-reviewed") == ["run-1"]
    assert seen["surface"] == mark.SURFACE and answer_surface.get() is None
    assert seen["voided"] == "run-1" and list(seen["decisions"]) == ["a"]


@pytest.mark.asyncio
async def test_an_unverified_or_foreign_delivery_approves_nothing():
    assert await mark.note_event("opencti", _delivery(["analyst-reviewed"]), verified=False) == []
    assert await mark.note_event("openaev", _delivery(["analyst-reviewed"]), verified=True) == []


def test_the_listen_only_assignment_provisions_grouping_updates():
    assert CTEM_F2_ANALYST_MARK_ASSIGNMENT in BUILTIN_ASSIGNMENTS
    config = CTEM_F2_ANALYST_MARK_ASSIGNMENT["trigger_config"]
    spec = prov.read_provision_spec(config)
    assert spec["entity_types"] == ["Grouping"] and spec["event_types"] == ["update"]
    assert config["filter"] == {"_ctem_listen_only": {"exists": True}}
    assert CTEM_F2_ANALYST_MARK_ASSIGNMENT["enabled"] is False
