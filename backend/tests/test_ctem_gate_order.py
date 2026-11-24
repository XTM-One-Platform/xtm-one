# SPDX-License-Identifier: Apache-2.0

"""The order in which a CTEM case's decisions wait for a person (#1606)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services import ctem_gate_order as order

_CASE = "3f0b8c52-8a7e-4c3e-9d0f-0c1d2e3f4a5b"
_ASK = {
    "question": "File the remediation ticket for Chrome 131 and notify its owner?",
    "context": "Validation proved the exploit lands on 2 of 3 hosts (OpenAEV inject 42, 23 Nov).",
    "options": [{"key": "A", "label": "File it"}, {"key": "B", "label": "Hold"}],
    "recommendation": "A",
}


class _Result:
    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[str]:
        return self._keys


class _Db:
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        self.queries = 0

    async def execute(self, _query: Any) -> _Result:
        self.queries += 1
        return _Result(self.keys)


def test_gate_keys_name_their_gate_and_case() -> None:
    assert order.gate_of(f"ctem:f2:mobilize:{_CASE}") == ("ctem.mobilize", _CASE)
    assert order.gate_of(f"ctem:f1:case-mobilize:{_CASE}") == ("ctem.mobilize", _CASE)
    assert order.gate_of(f"ctem:review:{_CASE}") == ("ctem.analyst_review", _CASE)
    assert order.gate_of(f"ctem:remediation:{_CASE}") == ("ctem.remediation.decision", _CASE)
    assert order.gate_of(f"ctem:f1:launch:{_CASE}") == ("ctem:f1:launch", _CASE)
    assert order.gate_of("dev:ctem-loop") is None
    assert order.gate_of("ctem:f2:mobilize:") is None


def test_mobilize_waits_behind_review_and_every_validation_key() -> None:
    keys = order.prerequisite_keys(_CASE)
    assert f"ctem:review:{_CASE}" in keys
    assert f"ctem:validation:{_CASE}" in keys
    assert f"ctem:f2:validate:{_CASE}" in keys
    assert not any("mobilize" in k for k in keys)


async def test_mobilize_parked_while_validation_waits_is_a_warning_not_a_refusal() -> None:
    """#1675: the order is the agent's judgement; the item is written with a warning."""
    db = _Db([f"ctem:validation:{_CASE}"])
    warning = await order.mobilize_before_review_warning(
        db, f"ctem:f2:mobilize:{_CASE}", True, user_id=uuid.uuid4(), workspace_id=None
    )
    assert warning and f"ctem:validation:{_CASE}" in warning


async def test_mobilize_once_review_and_validation_are_answered_has_no_warning() -> None:
    db = _Db([])
    assert (
        await order.mobilize_before_review_warning(
            db, f"ctem:f2:mobilize:{_CASE}", True, user_id=uuid.uuid4(), workspace_id=uuid.uuid4()
        )
        is None
    )
    assert db.queries == 1


async def test_autonomous_and_other_gates_are_never_warned() -> None:
    db = _Db([f"ctem:review:{_CASE}"])
    # Autonomous mode: no person waits.
    assert (
        await order.mobilize_before_review_warning(
            db, f"ctem:f2:mobilize:{_CASE}", False, user_id=uuid.uuid4(), workspace_id=None
        )
        is None
    )
    # A validation decision is never behind anything.
    assert (
        await order.mobilize_before_review_warning(
            db, f"ctem:validation:{_CASE}", True, user_id=uuid.uuid4(), workspace_id=None
        )
        is None
    )
    assert db.queries == 0


def test_a_waiting_gate_question_is_never_changed_in_place() -> None:
    changed = {**_ASK, "question": "File the ticket and the detection rule for Chrome 131?"}
    with pytest.raises(ValueError, match="never changed in place"):
        order.refuse_question_change(f"ctem:f2:mobilize:{_CASE}", _ASK, changed, waiting=True)
    # Same question with new evidence, an answered gate, or a non-gate item: allowed.
    order.refuse_question_change(f"ctem:f2:mobilize:{_CASE}", _ASK, {**_ASK, "context": "more"}, waiting=True)
    order.refuse_question_change(f"ctem:f2:mobilize:{_CASE}", _ASK, changed, waiting=False)
    order.refuse_question_change("dev:x", _ASK, changed, waiting=True)


def test_a_gate_card_never_sends_the_person_to_a_run() -> None:
    pointing = {**_ASK, "context": "Evidence: see run 4a1fe00c for the details."}
    with pytest.raises(ValueError, match="carries its question, evidence and options"):
        order.refuse_run_pointer(f"ctem:validation:{_CASE}", pointing)
    order.refuse_run_pointer(f"ctem:validation:{_CASE}", _ASK)
    order.refuse_run_pointer("dev:x", pointing)
