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


@pytest.mark.parametrize(
    ("gate", "key"),
    [
        ("ctem.analyst_review", "ctem:review:"),
        ("ctem.validation.non_readonly", "ctem:validation:"),
        ("ctem.validation.non_readonly", "ctem:f2:validate:"),
        ("ctem.validation.non_readonly", "ctem:f1:launch:"),
        ("ctem.remediation.decision", "ctem:remediation:"),
        ("ctem.mobilize", "ctem:f1:case-mobilize:"),
        ("ctem.mobilize", "ctem:f2:mobilize:"),
    ],
)
def test_a_waited_gate_rests_on_its_own_decision_key(gate: str, key: str) -> None:
    """#1954: no open decision of the gate, the waited event is refused with what to file."""
    refusal = order.waited_refusal(gate, _CASE, set())
    assert refusal and "create_work_item" in refusal and f"{key}{_CASE}" in refusal
    # Another gate's open decision is not this gate's.
    other = "ctem:review:" if key != "ctem:review:" else "ctem:remediation:"
    assert order.waited_refusal(gate, _CASE, {f"{other}{_CASE}"})
    # The same key of another case neither.
    assert order.waited_refusal(gate, _CASE, {f"{key}{uuid.uuid4()}"})
    assert order.waited_refusal(gate, _CASE, {f"{key}{_CASE}"}) is None


@pytest.mark.parametrize(
    ("gate", "key"),
    [
        ("ctem.production_change", "ctem:production-change:"),
        ("ctem.accept_risk", "ctem:accept-risk:"),
        ("ctem.close_without_retest", "ctem:close-without-retest:"),
        ("ctem.scoping.not_relevant", "ctem:scoping-not-relevant:"),
    ],
)
def test_every_catalog_gate_rests_on_its_own_key(gate: str, key: str) -> None:
    """#2464: another gate's open decision no longer makes this gate read filed."""
    assert order.waited_item_keys(gate, _CASE) == [f"{key}{_CASE}"]
    assert order.gate_of(f"{key}{_CASE}") == (gate, _CASE)
    assert order.waited_refusal(gate, _CASE, {f"ctem:review:{_CASE}"})
    assert order.waited_refusal(gate, _CASE, {f"{key}{_CASE}"}) is None


@pytest.mark.parametrize("state", ["new", "analyzing"])
def test_close_without_retest_is_refused_in_scoping(state: str) -> None:
    """#2798: the end-of-loop close points to the scoping gate on a case still in scoping."""
    refusal = order.close_at_scoping_refusal("ctem.close_without_retest", state, {"note", "gate"})
    assert refusal and "ctem.scoping.not_relevant" in refusal and "evidence request" in refusal


@pytest.mark.parametrize(
    ("state", "kinds"),
    [
        ("validating", set()),
        ("remediating", set()),
        ("waiting_decision", set()),
        ("analyzing", {"validation"}),
        ("analyzing", {"treatment_plan"}),
        ("new", {"ticket"}),
    ],
)
def test_close_without_retest_runs_past_scoping(state: str, kinds: set[str]) -> None:
    assert order.close_at_scoping_refusal("ctem.close_without_retest", state, kinds) is None


def test_only_close_without_retest_is_checked_for_scoping() -> None:
    for gate in ("ctem.scoping.not_relevant", "ctem.accept_risk", "ctem.analyst_review"):
        assert order.close_at_scoping_refusal(gate, "new", set()) is None


def _ev(kind: str, to: str, what: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(kind=kind, to_value=to, reason=what)


def test_an_open_evidence_request_holds_every_close_at_scoping() -> None:
    """#2798: a fact scoping could not read is asked, and no close gate runs until it is answered."""
    events = [_ev("note", "", "x"), _ev("evidence_request", "open", "Confirm polkit version on the 39 Linux servers")]
    pending = order.open_evidence_requests(events)
    assert pending == ["Confirm polkit version on the 39 Linux servers"]
    for gate in ("ctem.scoping.not_relevant", "ctem.close_without_retest"):
        refusal = order.evidence_pending_refusal(gate, "analyzing", pending)
        assert refusal and "polkit" in refusal and "Nothing was changed" in refusal
    assert order.evidence_pending_refusal("ctem.analyst_review", "analyzing", pending) is None
    assert order.evidence_pending_refusal("ctem.scoping.not_relevant", "validating", pending) is None


def test_record_evidence_request_writes_one_event_and_checks_its_fields() -> None:
    from types import SimpleNamespace

    from app.services import ctem_case_loop as loop
    from app.services import ctem_cases as rules

    added: list[Any] = []
    db: Any = SimpleNamespace(add=added.append)
    case: Any = SimpleNamespace(id=uuid.uuid4())
    loop.record_evidence_request(db, case, {"what": "Confirm polkit version", "assets": "39 Linux servers"}, actor="a")
    assert [(e.kind, e.to_value, e.reason, e.evidence) for e in added] == [
        ("evidence_request", "open", "Confirm polkit version", "39 Linux servers")
    ]
    for bad in ({"assets": "x"}, {"what": "x"}, {"what": "x", "assets": "y", "status": "closed"}):
        with pytest.raises(rules.CaseRuleError):
            loop.record_evidence_request(db, case, bad, actor="a")


def test_an_answered_evidence_request_releases_the_close() -> None:
    events = [
        _ev("evidence_request", "open", "Confirm polkit version on the 39 Linux servers"),
        _ev("evidence_request", "answered", "confirm  polkit version on the 39 linux servers"),
    ]
    assert order.open_evidence_requests(events) == []
    assert order.evidence_pending_refusal("ctem.scoping.not_relevant", "new", []) is None


def test_a_gate_outside_the_catalog_rests_on_any_decision_of_the_case() -> None:
    assert order.waited_item_keys("ctem.other", _CASE) == []
    assert order.waited_refusal("ctem.other", _CASE, set())
    assert order.waited_refusal("ctem.other", _CASE, {f"ctem:evidence:{_CASE}"}) is None


async def test_open_gate_item_keys_reads_once() -> None:
    db = _Db([f"ctem:review:{_CASE}", None])  # type: ignore[list-item]
    assert await order.open_gate_item_keys(db, _CASE) == {f"ctem:review:{_CASE}"}
    assert db.queries == 1


def test_a_gate_card_never_sends_the_person_to_a_run() -> None:
    pointing = {**_ASK, "context": "Evidence: see run 4a1fe00c for the details."}
    with pytest.raises(ValueError, match="carries its question, evidence and options"):
        order.refuse_run_pointer(f"ctem:validation:{_CASE}", pointing)
    order.refuse_run_pointer(f"ctem:validation:{_CASE}", _ASK)
    order.refuse_run_pointer("dev:x", pointing)


@pytest.mark.parametrize("key", ["ctem:evidence:", "ctem:version-check:", "ctem:scoping:"])
def test_a_move_to_waiting_decision_rests_on_an_open_decision_of_the_case(key: str) -> None:
    """#2782: an agent's bare move to waiting_decision needs an open ctem:<x>:<case id> decision."""
    refusal = order.waiting_state_refusal(_CASE, set())
    assert refusal and "create_work_item" in refusal and _CASE in refusal and "nothing was written" in refusal
    # Another case's decision is not this case's.
    assert order.waiting_state_refusal(_CASE, {f"{key}{uuid.uuid4()}"})
    assert order.waiting_state_refusal(_CASE, {f"dev:x:{_CASE}"})
    assert order.waiting_state_refusal(_CASE, {f"{key}{_CASE}"}) is None
    # A catalog gate's open decision counts too.
    assert order.waiting_state_refusal(_CASE, {f"ctem:review:{_CASE}"}) is None


async def test_pending_decisions_lists_the_open_decisions_of_the_case(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import work_item as work_item_service

    seen: dict[str, Any] = {}

    async def _list(**kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        return [
            {
                "id": "b",
                "correlation_key": f"ctem:version-check:{_CASE}",
                "title": "Check",
                "needs_human": True,
                "due_at": None,
                "ask": {"deadline": "2026-11-30T10:00:00+00:00"},
                "created_at": "2026-11-28T11:00:00",
            },
            {
                "id": "a",
                "correlation_key": f"ctem:evidence:{_CASE}",
                "title": "Evidence",
                "needs_human": True,
                "due_at": "2026-11-29T10:00:00+00:00",
                "created_at": "2026-11-28T10:00:00",
            },
            {"id": "c", "correlation_key": f"ctem:evidence:{_CASE}", "title": "Worked", "needs_human": False},
            {"id": "d", "correlation_key": f"ctem:evidence:{_CASE}x", "title": "Other", "needs_human": True},
        ]

    monkeypatch.setattr(work_item_service, "list_work_items", _list)
    pending = await order.pending_decisions(_CASE, user_id="u1")
    assert seen["user_id"] == "u1" and seen["open_only"] is True
    assert seen["correlation_key_prefix"] == "ctem:" and seen["search"] == _CASE
    assert pending == [
        {
            "work_item_id": "a",
            "key": f"ctem:evidence:{_CASE}",
            "title": "Evidence",
            "deadline": "2026-11-29T10:00:00+00:00",
        },
        {
            "work_item_id": "b",
            "key": f"ctem:version-check:{_CASE}",
            "title": "Check",
            "deadline": "2026-11-30T10:00:00+00:00",
        },
    ]


async def test_pending_decisions_never_fail_the_case_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import work_item as work_item_service

    async def _broken(**_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("no database")

    monkeypatch.setattr(work_item_service, "list_work_items", _broken)
    assert await order.pending_decisions(_CASE, user_id="u1") == []
