"""The OpenCRQ treatment plan of a case (CTEM-18-UI, #1068).

``ctem_case_loop.record_treatment_plan`` (the fleet's write after a decision)
and ``ctem_cases_view.treatment_plan_for`` (what one viewer reads): the id must
be a UUID and the link the OpenCRQ page of that plan, the same plan twice
writes one event, and the plan's id, name and link go only to a viewer who
reads OpenCRQ.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules
from app.services import ctem_cases_view as view_of

NOW = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)
PLAN_ID = "4f1c2a8e-6b7d-4e1a-9c3b-2d5e8f7a1b90"
URL = f"https://crq.example.test/tenant-1/treatment-plans/{PLAN_ID}"
PLAN = {"id": PLAN_ID, "name": "Upgrade nginx before January", "url": URL}


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    def events(self) -> list[CtemCaseEvent]:
        return [r for r in self.added if isinstance(r, CtemCaseEvent) and r.kind == "treatment_plan"]


def _case() -> CtemCase:
    return CtemCase(id=uuid.uuid4(), title="Upgrade nginx")


def test_record_writes_one_event_with_id_name_and_link() -> None:
    db, case = FakeSession(), _case()
    loop.record_treatment_plan(db, case, PLAN, actor="agent-1", now=NOW)
    (event,) = db.events()
    assert event.actor == "agent-1"
    assert (event.to_value, event.reason, event.evidence) == (PLAN_ID, PLAN["name"], URL)
    assert event.from_value is None


def test_same_plan_again_writes_nothing() -> None:
    db, case = FakeSession(), _case()
    loop.record_treatment_plan(db, case, PLAN, {"id": PLAN_ID, "name": PLAN["name"], "url": URL}, now=NOW)
    assert db.events() == []


def test_a_new_plan_names_the_previous_one() -> None:
    db, case = FakeSession(), _case()
    other = str(uuid.uuid4())
    loop.record_treatment_plan(db, case, PLAN, {"id": other, "name": "Old", "url": None}, now=NOW)
    (event,) = db.events()
    assert event.from_value == other


@pytest.mark.parametrize(
    "plan",
    [
        {"id": "plan-1", "url": URL},
        {"id": PLAN_ID, "url": "javascript:alert(1)"},
        {"id": PLAN_ID, "url": f"https://crq.example.test/t/treatment-plans/{uuid.uuid4()}"},
        {"id": PLAN_ID, "url": f"https://crq.example.test/t/budget-reviews/{PLAN_ID}"},
        "not a plan",
    ],
)
def test_bad_id_or_link_is_refused_and_nothing_written(plan: object) -> None:
    db, case = FakeSession(), _case()
    with pytest.raises(rules.CaseRuleError):
        loop.record_treatment_plan(db, case, plan, now=NOW)  # type: ignore[arg-type]
    assert db.events() == []


def test_a_plan_without_link_is_kept() -> None:
    db, case = FakeSession(), _case()
    loop.record_treatment_plan(db, case, {"id": PLAN_ID.upper(), "name": "P"}, now=NOW)
    (event,) = db.events()
    assert event.to_value == PLAN_ID
    assert event.evidence is None


def _event(at: datetime, plan_id: str, name: str) -> CtemCaseEvent:
    return CtemCaseEvent(
        case_id=uuid.uuid4(), actor="a", kind="treatment_plan", at=at, to_value=plan_id, reason=name, evidence=URL
    )


def test_view_serves_the_newest_plan_whatever_the_order() -> None:
    older = _event(NOW - timedelta(days=1), str(uuid.uuid4()), "Old")
    newer = _event(NOW, PLAN_ID, "New")
    note = CtemCaseEvent(case_id=uuid.uuid4(), actor="a", kind="note", at=NOW + timedelta(hours=1))
    for events in ([older, newer, note], [note, newer, older]):
        shown = view_of.treatment_plan_for(events, view_of.ViewerView(opencrq=True))
        assert shown == {"id": PLAN_ID, "name": "New", "url": URL, "at": NOW.isoformat(), "restricted": False}


def test_viewer_without_opencrq_sees_only_that_a_plan_exists() -> None:
    events = [_event(NOW, PLAN_ID, "New")]
    shown = view_of.treatment_plan_for(events, view_of.ViewerView(opencrq=False))
    assert shown == {"restricted": True, "at": NOW.isoformat()}
    row = view_of._event(events[0], view_of.ViewerView(opencrq=False))
    assert (row["to"], row["reason"], row["evidence"]) == (None, None, None)
    assert row["kind"] == "treatment_plan"


def test_no_plan_is_none() -> None:
    assert view_of.treatment_plan_for([], view_of.ViewerView(opencrq=True)) is None
