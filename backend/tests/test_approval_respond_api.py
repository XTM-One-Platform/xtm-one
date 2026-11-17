"""Decision validation for the approval resume endpoint.

``POST /runs/{id}/respond`` now accepts either a free-text answer (unchanged)
or a set of per-call approval decisions. These tests pin the validation rules
that protect the resumed conversation:

* Every pending call must be decided exactly once. A partial set is refused
  rather than partially applied — resuming with an undecided call leaves its
  ``tool_use`` block without a ``tool_result``, which strict-mode LLM APIs
  reject, making the run unresumable.
* A verdict the endpoint does not recognise is refused by the schema rather
  than forwarded, so an unknown value cannot become consent downstream.

The helper is tested directly rather than through HTTP: the rules are pure
validation, and driving them through the endpoint would need a live run, DB
session and auth for no extra coverage.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.assignments import _apply_decisions_to_run
from app.api.v1.schemas.assignment import ApprovalDecision

SCHEMA = {
    "type": "object",
    "properties": {"target": {"type": "string"}},
    "required": ["target"],
}


def _run(*proposals):
    return SimpleNamespace(
        pending_approvals=list(proposals),
        approval_decisions=None,
        human_input_history=None,
        human_input_question="Approval required",
    )


def _proposal(call_id: str, tool_name: str = "delete_entity", schema=SCHEMA):
    return {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "tool_description": "deletes things",
        "arguments": {"target": "old"},
        "input_schema": schema,
        "source": "test",
    }


def _decision(call_id: str, decision: str = "approve", **kwargs):
    return ApprovalDecision(tool_call_id=call_id, decision=decision, **kwargs)


_USER = SimpleNamespace(id="u1", email="u@example.com", full_name="U")


def test_full_decision_set_is_stored():
    run = _run(_proposal("c1"), _proposal("c2"))

    _apply_decisions_to_run(run, [_decision("c1"), _decision("c2", "reject")], _USER)

    assert set(run.approval_decisions) == {"c1", "c2"}
    assert run.approval_decisions["c2"]["decision"] == "reject"


def test_partial_decision_set_is_refused():
    """The dangerous case: a missing decision would orphan a tool_use block."""
    run = _run(_proposal("c1"), _proposal("c2"))

    with pytest.raises(HTTPException) as exc:
        _apply_decisions_to_run(run, [_decision("c1")], _USER)

    assert exc.value.status_code == 422
    assert exc.value.detail["missing"] == ["c2"]
    assert run.approval_decisions is None, "a refused set must not be partially applied"


def test_unknown_tool_call_id_is_refused():
    run = _run(_proposal("c1"))

    with pytest.raises(HTTPException) as exc:
        _apply_decisions_to_run(run, [_decision("c1"), _decision("c99")], _USER)

    assert exc.value.status_code == 422
    assert exc.value.detail["unknown"] == ["c99"]


def test_duplicate_decision_for_one_call_is_refused():
    """Two verdicts for one call is ambiguous — refuse rather than pick one."""
    run = _run(_proposal("c1"))

    with pytest.raises(HTTPException) as exc:
        _apply_decisions_to_run(run, [_decision("c1"), _decision("c1", "reject")], _USER)

    assert exc.value.status_code == 422


def test_run_without_pending_approvals_is_refused():
    """A free-text pause must be answered with text, not decisions."""
    run = _run()

    with pytest.raises(HTTPException) as exc:
        _apply_decisions_to_run(run, [_decision("c1")], _USER)

    assert exc.value.status_code == 400


def test_repeated_calls_to_the_same_tool_are_decided_independently():
    """Same tool twice in a turn — two proposals, two decisions."""
    run = _run(_proposal("c1"), _proposal("c2"))

    _apply_decisions_to_run(
        run, [_decision("c1", "approve"), _decision("c2", "reject")], _USER
    )

    assert run.approval_decisions["c1"]["decision"] == "approve"
    assert run.approval_decisions["c2"]["decision"] == "reject"


def test_resolution_is_recorded_in_history():
    """The durable trail must show a decision was made, like the free-text path."""
    run = _run(_proposal("c1"))

    _apply_decisions_to_run(run, [_decision("c1")], _USER)

    assert run.human_input_history
    assert "c1:approve" in run.human_input_history[-1]["response"]
