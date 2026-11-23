"""The CTEM threat package is validated through the analyst review gate (#1480).

- The view: a package whose page is its OpenCTI draft reads ``drafted``, its
  coverage ``coverage_drafted`` and never ``coverage_requested``; past the case
  SLA it is ``draft_overdue``. Once the Grouping page and the live ids are
  written (validated, new internal id included), ``coverage_requested``.
- The gate: ``ctem.analyst_review`` names the coverage request and
  ``apply_opencti_draft``, with a grant on ``draft_id``, so the approved
  request covers the validation of the draft its answer names (one decision);
  another draft asks again.
- The tool: ``apply_opencti_draft`` is offered with Live writes off only when
  the run's fleet gates it (``gate_tool_names``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.agents.builtin_fleet_docs import _ctem_gates
from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as loop
from app.services import ctem_cases_view as view_of
from app.services import fleet_autonomy

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
GROUPING = "8a3e2f10-4c5d-4b6e-9f70-1a2b3c4d5e6f"
LIVE_GROUPING = "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b"
DRAFT = "5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a"
COVERAGE = "0c6f9d3a-2b1e-4f5a-8d7c-6e5f4a3b2c1d"
LIVE_COVERAGE = "9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4"
DRAFT_URL = f"https://cti.example.test/dashboard/data/import/draft/{DRAFT}"
LIVE_URL = f"https://cti.example.test/dashboard/analyses/groupings/{LIVE_GROUPING}"


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


def _events(db: FakeSession) -> list[CtemCaseEvent]:
    return [r for r in db.added if isinstance(r, CtemCaseEvent) and r.kind == "threat_package"]


def _record(db: FakeSession, case: CtemCase, package: dict[str, Any], at: datetime) -> None:
    previous = view_of.threat_package_of(_events(db))
    loop.record_threat_package(cast(Any, db), case, package, previous, actor="agent-1", now=at)


def _shown(
    events: list[CtemCaseEvent],
    view: view_of.ViewerView,
    *,
    sla_due_at: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    shown = view_of.threat_package_for(events, view, sla_due_at=sla_due_at, now=now)
    assert shown is not None
    return shown


def _drafted() -> dict[str, Any]:
    return {"grouping_id": GROUPING, "name": "CVE-2026-1234", "url": DRAFT_URL, "coverage_id": COVERAGE}


def test_drafted_package_never_reads_coverage_requested() -> None:
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, _drafted(), NOW)
    shown = _shown(_events(db), view_of.ViewerView(opencti=True), now=NOW)
    assert shown["state"] == "drafted"
    assert shown["coverage_requested"] is False
    assert shown["coverage_drafted"] is True
    assert shown["draft_overdue"] is False
    restricted = _shown(_events(db), view_of.ViewerView(opencti=False), now=NOW)
    assert (restricted["state"], restricted["coverage_requested"]) == ("drafted", False)


def test_drafted_package_past_the_case_sla_is_overdue() -> None:
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, _drafted(), NOW)
    shown = _shown(
        _events(db), view_of.ViewerView(opencti=True), sla_due_at=NOW + timedelta(hours=1), now=NOW + timedelta(hours=2)
    )
    assert shown["draft_overdue"] is True
    naive_due = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    shown = _shown(_events(db), view_of.ViewerView(opencti=True), sla_due_at=naive_due, now=NOW)
    assert shown["draft_overdue"] is True


@pytest.mark.parametrize(
    ("grouping", "coverage"),
    [(GROUPING, COVERAGE), (LIVE_GROUPING, LIVE_COVERAGE), (f"grouping--{LIVE_GROUPING}", LIVE_COVERAGE)],
    ids=["same-ids", "new-internal-ids", "standard-id"],
)
def test_validated_package_switches_to_the_live_grouping(grouping: str, coverage: str) -> None:
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, _drafted(), NOW)
    page = f"https://cti.example.test/dashboard/analyses/groupings/{grouping.removeprefix('grouping--')}"
    _record(
        db,
        case,
        {**_drafted(), "grouping_id": grouping, "url": page, "coverage_id": coverage},
        NOW + timedelta(minutes=5),
    )
    assert len(_events(db)) == 2
    shown = _shown(
        _events(db), view_of.ViewerView(opencti=True), sla_due_at=NOW - timedelta(days=1), now=NOW + timedelta(hours=1)
    )
    assert (shown["grouping_id"], shown["url"], shown["coverage_id"]) == (grouping, page, coverage)
    assert shown["state"] == "validated"
    assert shown["coverage_requested"] is True
    assert shown["coverage_drafted"] is False
    assert shown["draft_overdue"] is False


def test_validated_package_without_coverage_is_not_requested() -> None:
    db, case = FakeSession(), CtemCase(id=uuid.uuid4(), title="Upgrade nginx")
    _record(db, case, {**_drafted(), "url": LIVE_URL, "grouping_id": LIVE_GROUPING, "coverage_id": ""}, NOW)
    shown = _shown(_events(db), view_of.ViewerView(opencti=True), now=NOW)
    assert (shown["state"], shown["coverage_requested"], shown["coverage_drafted"]) == ("validated", False, False)


def _review_gate() -> dict[str, Any]:
    gates = _ctem_gates(launch_tools=True, mobilize_tools=True, mobilize_action="mobilize")
    return next(g for g in gates if g["key"] == "ctem.analyst_review")


def test_analyst_review_gate_names_the_draft_validation_with_a_grant() -> None:
    gate = _review_gate()
    names = [t["name"] for t in gate["tools"]]
    assert names == ["manage_opencti_security_coverage", "apply_opencti_draft"]
    assert gate["grant"] == {"objects": ["draft_id"], "fresh": []}
    assert "publishes the package and sends the coverage to OpenAEV" in gate["action"]
    assert "apply_opencti_draft" in fleet_autonomy.gate_tool_names([gate])


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(id=call_id, name=name, arguments=arguments)


def test_approved_coverage_request_covers_the_validation_of_its_draft() -> None:
    from app.tools.base import HumanInputRequestedError

    gate = {**_review_gate(), "pin": fleet_autonomy.PIN_WAIT}
    rules = fleet_autonomy.gated_tool_rules("full_gates", [gate])
    request = _call("c1", "manage_opencti_security_coverage", {"action": "create", "covered_id": GROUPING})
    check = fleet_autonomy.make_fleet_autonomy_gate(None, rules, approved_call_ids={"c1"})
    check(request)
    answer = SimpleNamespace(
        is_error=False,
        content=f"Created security coverage 'x' (id: {COVERAGE}) on Grouping {GROUPING} in OpenCTI draft {DRAFT}: ...",
    )
    check.observe(request, answer)
    check(_call("c2", "apply_opencti_draft", {"draft_id": DRAFT}))
    with pytest.raises(HumanInputRequestedError):
        check(_call("c3", "apply_opencti_draft", {"draft_id": str(uuid.uuid4())}))


def test_unapproved_validation_waits() -> None:
    from app.tools.base import HumanInputRequestedError

    gate = {**_review_gate(), "pin": fleet_autonomy.PIN_WAIT}
    check = fleet_autonomy.make_fleet_autonomy_gate(None, fleet_autonomy.gated_tool_rules("full_gates", [gate]))
    with pytest.raises(HumanInputRequestedError):
        check(_call("c1", "apply_opencti_draft", {"draft_id": DRAFT}))


def _tool_names(**kwargs: Any) -> set[str]:
    from app.integrations.providers.opencti.knowledge import register_knowledge
    from app.tools.base import CallableToolExecutor

    defs = register_knowledge("https://cti.example.test", {}, CallableToolExecutor(), autonomous=True, **kwargs)
    return {d.name for d in defs}


def test_apply_draft_is_offered_without_live_writes_only_when_a_fleet_gate_decides_it() -> None:
    assert "apply_opencti_draft" not in _tool_names()
    assert "apply_opencti_draft" in _tool_names(gated_draft_validation=True)
    assert "create_opencti_label" not in _tool_names(gated_draft_validation=True)
    assert "apply_opencti_draft" in _tool_names(live_writes=True)
