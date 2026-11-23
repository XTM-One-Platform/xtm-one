"""Loop gates on the case history (CTEM-34, #916): every gate shows, passed automatically or waited.

``ctem_case_loop.record_gate`` writes one ``gate`` event per gate pass or
wait, keyed by the CTEM catalog key; an unknown key or outcome is refused and
nothing is written. The loop trace reads it from the case history.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.agents.builtin_fleet_docs import CTEM_GATE_KEYS, CTEM_MOBILIZE_GATE
from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules

NOW = datetime(2026, 11, 21, 19, 40, tzinfo=UTC)


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    def gates(self) -> list[CtemCaseEvent]:
        return [r for r in self.added if isinstance(r, CtemCaseEvent) and r.kind == "gate"]


def _case() -> CtemCase:
    return CtemCase(id=uuid.uuid4(), title="Close the exposure of portal", state="remediating")


def test_an_automatic_pass_is_written_with_what_let_it_run() -> None:
    db, case = FakeSession(), _case()
    loop.record_gate(db, case, key="ctem.remediation.decision", outcome="passed_automatically", by="level low", now=NOW)
    (event,) = db.gates()
    assert (event.from_value, event.to_value, event.reason) == (
        "ctem.remediation.decision",
        "passed_automatically",
        "level low",
    )
    assert event.at == NOW and event.case_id == case.id
    assert case.state == "remediating"


def test_a_waited_then_answered_mobilize_gate_is_two_events() -> None:
    db, case = FakeSession(), _case()
    loop.record_gate(db, case, key=CTEM_MOBILIZE_GATE, outcome="waited", now=NOW)
    loop.record_gate(db, case, key=CTEM_MOBILIZE_GATE, outcome="answered", by="user-1", option="A", now=NOW)
    assert [(e.to_value, e.reason, e.evidence) for e in db.gates()] == [
        ("waited", None, None),
        ("answered", "user-1", "A"),
    ]


@pytest.mark.parametrize("key", CTEM_GATE_KEYS)
def test_every_catalog_key_is_accepted(key: str) -> None:
    db = FakeSession()
    loop.record_gate(db, _case(), key=key, outcome="passed_automatically")
    assert len(db.gates()) == 1


@pytest.mark.parametrize(
    ("key", "outcome"),
    [("ticket.create", "waited"), ("", "waited"), (CTEM_MOBILIZE_GATE, "skipped")],
)
def test_an_unknown_key_or_outcome_is_refused_and_nothing_is_written(key: str, outcome: str) -> None:
    db = FakeSession()
    with pytest.raises(rules.CaseRuleError):
        loop.record_gate(db, _case(), key=key, outcome=outcome)
    assert db.gates() == []
