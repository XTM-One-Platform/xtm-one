# SPDX-License-Identifier: Apache-2.0

"""The loop moves a CTEM case through its statuses by itself (#1503).

One case walked by the loop writers (no model, no database: the session is a
recorder): opened ``new``, triage -> ``analyzing``, a gate waits ->
``waiting_decision``, answered -> ``remediating``, the ticket done with a
re-test plan -> ``validating``, a failed re-test -> ``remediating``, a
pending re-test -> ``validating``, the conclusive pass -> ``resolved``. Every
move is a ``state`` event naming its actor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from app.models.ctem_case import CASE_STATES, CLOSED_STATES, OPEN_STATES, CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as loop
from app.services import ctem_case_status as status
from app.services import ctem_cases as rules

AGENT = str(uuid.uuid4())
T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


class _Recorder:
    """The session the writers add to: keeps the rows (passed through ``_db``)."""

    def __init__(self) -> None:
        self.rows: list[Any] = []

    def add(self, row: Any) -> None:
        self.rows.append(row)

    def states(self) -> list[tuple[str | None, str | None, str]]:
        return [
            (r.from_value, r.to_value, r.actor) for r in self.rows if isinstance(r, CtemCaseEvent) and r.kind == "state"
        ]


def _db(recorder: _Recorder) -> Any:
    """The recorder typed as the session the writers take."""
    return cast(Any, recorder)


def _case(state: str = "new", **over: Any) -> CtemCase:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "state": state,
        "state_since": T0,
        "tickets": [],
        "validations": [],
        "validation_plan": True,
        "exploitation": [],
        "attack_path": None,
        "accepted_reason": None,
        "accepted_until": None,
        "resolution_kind": None,
        "vertical": "cyber",
        "trigger_path": "new_vulnerability",
    }
    fields.update(over)
    case = CtemCase()
    for key, value in fields.items():
        setattr(case, key, value)
    return case


def test_status_sets_are_the_lifecycle() -> None:
    lifecycle = (*OPEN_STATES, *CLOSED_STATES)
    assert lifecycle == CASE_STATES
    assert status.normalize("needs_attention") == "new"
    assert status.normalize("in_progress") == "remediating"
    assert status.normalize("validating") == "validating"


def test_triage_starts_the_analysis_once() -> None:
    db = _Recorder()
    case = _case()
    loop.record_exploitation(
        _db(db),
        case,
        [{"asset": "edge-01", "techniques": ["T1190"], "exposure": "internet_facing"}],
        actor=AGENT,
        now=T0,
    )
    assert case.state == "analyzing"
    assert db.states() == [("new", "analyzing", AGENT)]
    loop.record_exploitation(
        _db(db), case, [{"asset": "edge-02", "techniques": ["T1190"], "exposure": "internal"}], actor=AGENT, now=T0
    )
    assert len(db.states()) == 1


def test_gate_waits_then_the_answer_moves_on() -> None:
    db = _Recorder()
    case = _case("analyzing")
    loop.record_gate(
        _db(db), case, key="ctem.remediation.decision", outcome="waited", by="the CISO", actor=AGENT, now=T0
    )
    assert case.state == "waiting_decision"
    later = T0 + timedelta(hours=2)
    loop.record_gate(
        _db(db), case, key="ctem.remediation.decision", outcome="answered", by="the CISO", actor=AGENT, now=later
    )
    assert case.state == "remediating"
    assert case.state_since == later
    moves = db.states()
    assert moves == [("analyzing", "waiting_decision", AGENT), ("waiting_decision", "remediating", AGENT)]
    reasons = [r.reason for r in db.rows if isinstance(r, CtemCaseEvent) and r.kind == "state"]
    assert "the CISO" in (reasons[0] or "")


@pytest.mark.parametrize(
    ("key", "option"),
    [
        ("ctem.validation.non_readonly", "do_not_run"),
        ("ctem.validation.non_readonly", "C: Do not run it"),
        ("ctem.remediation.decision", "reject"),
        ("ctem.mobilize", "Rejected"),
        ("ctem.production_change", "defer"),
    ],
)
def test_a_refused_gate_never_takes_its_next_step(key: str, option: str) -> None:
    db = _Recorder()
    case = _case("analyzing")
    loop.record_gate(_db(db), case, key=key, outcome="waited", by="the CISO", actor=AGENT, now=T0)
    loop.record_gate(_db(db), case, key=key, outcome="answered", by="the CISO", option=option, actor=AGENT, now=T0)
    assert case.state == "analyzing"
    assert case.state not in ("validating", "remediating")
    reason = [r.reason for r in db.rows if isinstance(r, CtemCaseEvent) and r.kind == "state"][-1] or ""
    assert "refused" in reason and option in reason


@pytest.mark.parametrize("option", ["A", "approve", "Run it against the asset", ""])
def test_an_approving_answer_takes_the_next_step(option: str) -> None:
    case = _case("waiting_decision")
    moved = status.after_gate(
        case, T0, key="ctem.validation.non_readonly", outcome="answered", by="x", actor=AGENT, option=option
    )
    assert moved is not None and case.state == "validating"


@pytest.mark.parametrize("option", ["deny_list_ip", "defer_to_window", "reject_inbound_smb", "B: Deny list the IP"])
def test_a_treatment_key_holding_a_refusal_word_takes_the_next_step(option: str) -> None:
    case = _case("waiting_decision")
    moved = status.after_gate(
        case, T0, key="ctem.remediation.decision", outcome="answered", by="x", actor=AGENT, option=option
    )
    assert moved is not None and case.state == "remediating"


def test_a_refusal_never_moves_a_case_not_waiting_on_it() -> None:
    case = _case("remediating")
    assert (
        status.after_gate(case, T0, key="ctem.mobilize", outcome="answered", by="x", actor=AGENT, option="reject")
        is None
    )
    assert case.state == "remediating"


def test_an_automatic_pass_only_moves_forward() -> None:
    db = _Recorder()
    case = _case("validating")
    loop.record_gate(
        _db(db), case, key="ctem.mobilize", outcome="passed_automatically", by="autonomy", actor=AGENT, now=T0
    )
    assert case.state == "validating"
    assert db.states() == []


def test_a_gate_never_moves_a_closed_case() -> None:
    db = _Recorder()
    case = _case("resolved")
    loop.record_gate(_db(db), case, key="ctem.remediation.decision", outcome="waited", by="x", actor=AGENT, now=T0)
    assert case.state == "resolved"


def test_ticket_in_flight_then_done_hands_to_validation() -> None:
    db = _Recorder()
    case = _case("analyzing")
    loop.record_ticket(_db(db), case, integration="jira", key="SEC-1", status="open", actor=AGENT, now=T0)
    assert case.state == "remediating"
    loop.record_ticket(
        _db(db), case, integration="jira", key="SEC-1", status="done", actor=AGENT, now=T0 + timedelta(days=1)
    )
    assert case.state == "validating"


def test_one_case_walks_the_whole_loop_with_no_hand_edit() -> None:
    db = _Recorder()
    case = _case()
    loop.record_exploitation(_db(db), case, [{"asset": "edge-01", "techniques": ["T1190"]}], actor=AGENT, now=T0)
    loop.record_gate(
        _db(db), case, key="ctem.remediation.decision", outcome="waited", by="the CISO", actor=AGENT, now=T0
    )
    loop.record_gate(
        _db(db), case, key="ctem.remediation.decision", outcome="answered", by="the CISO", actor=AGENT, now=T0
    )
    loop.record_ticket(_db(db), case, integration="jira", key="SEC-1", status="in_progress", actor=AGENT, now=T0)
    loop.record_ticket(_db(db), case, integration="jira", key="SEC-1", status="done", actor=AGENT, now=T0)
    assert [m[1] for m in db.states()] == ["analyzing", "waiting_decision", "remediating", "validating"]
    assert all(m[2] == AGENT for m in db.states())


@pytest.mark.parametrize(
    ("start", "result", "expected"),
    [
        ("remediating", "pending", "validating"),
        ("validating", "failed", "remediating"),
        ("new", "pending", "validating"),
        ("resolved", "pending", None),
    ],
)
def test_validation_moves(start: str, result: str, expected: str | None) -> None:
    case = _case(start)
    moved = status.after_validation(case, T0, result=result, actor=AGENT, evidence="openaev:run-1")
    assert (moved.to_value if moved else None) == expected


def test_reopen_goes_back_to_remediation_when_a_fix_was_in_flight() -> None:
    assert status.reopen_target(_case("resolved", tickets=[{"key": "SEC-1"}])) == "remediating"
    assert status.reopen_target(_case("resolved")) == "new"


def test_a_person_moving_to_remediating_names_the_owner() -> None:
    with pytest.raises(rules.CaseRuleError):
        rules.check_transition(current="new", target="remediating", actor_kind="user", owner_user_id=None, now=T0)
    rules.check_transition(current="new", target="remediating", actor_kind="agent", owner_user_id=None, now=T0)
    with pytest.raises(rules.CaseRuleError):
        rules.check_transition(current="new", target="waiting_decision", actor_kind="user", owner_user_id=None, now=T0)
