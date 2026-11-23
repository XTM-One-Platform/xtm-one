# SPDX-License-Identifier: Apache-2.0

"""The gate decision records of a CTEM case (#1518): decision row + gate events, one record per gate."""

from datetime import UTC, datetime
from typing import Any

from app.services import ctem_case_audit as audit
from app.services import ctem_case_gate_records as gates

PERSON = "11111111-1111-1111-1111-111111111111"
AGENT = "22222222-2222-2222-2222-222222222222"
CASE = "33333333-3333-3333-3333-333333333333"
ITEM = "44444444-4444-4444-4444-444444444444"
NOW = datetime(2026, 11, 23, 13, 0, tzinfo=UTC)
NAMES = {PERSON: "Sam", AGENT: "Loop orchestrator"}


def _at(hhmm: str) -> str:
    return f"2026-11-23T{hhmm}:00+00:00"


def _e(at: str, kind: str, actor: str = AGENT, **kw: Any) -> dict[str, Any]:
    return {"at": _at(at), "actor": actor, "kind": kind, "issue": None, **kw}


def _gate(at: str, key: str, outcome: str, actor: str = AGENT, **kw: Any) -> dict[str, Any]:
    return _e(at, "gate", actor, **{"from": key, "to": outcome, **kw})


def _history(*events: dict[str, Any]) -> list[dict[str, Any]]:
    return audit.annotate(list(reversed(events)), people=[PERSON])


def _decision(status: str = "answered", **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "d1",
        "public_id": f"work_item:{ITEM}",
        "status": status,
        "ask": {
            "question": "Run it?",
            "options": [{"key": "A", "label": "Run the scenario"}, {"key": "B", "label": "Skip"}],
        },
        "recipient": f"user:{PERSON}",
        "answer": None,
        "answered_by": None,
        "answered_at": None,
        "surface": None,
        "deadline": None,
        "created_at": _at("11:56"),
    }
    row.update(kw)
    return {"gate": "ctem.validation.non_readonly", "work_item_id": ITEM, "created_by_agent_id": AGENT, "decision": row}


def _one(records: list[dict[str, Any]], key: str = "ctem.validation.non_readonly") -> dict[str, Any]:
    return next(r for r in records if r["key"] == key)


def test_item_keys_cover_the_shared_gate_prefixes() -> None:
    keys = gates.item_keys(CASE)
    assert keys[f"ctem:review:{CASE}"] == "ctem.analyst_review"
    assert keys[f"ctem:validation:{CASE}"] == "ctem.validation.non_readonly"
    assert keys[f"ctem:remediation:{CASE}"] == "ctem.remediation.decision"


def test_answered_on_the_decision_without_a_case_event_is_answered() -> None:
    history = _history(
        _gate("11:56", "ctem.validation.non_readonly", "waited"), _e("12:10", "note", reason="Scenario launched.")
    )
    row = _decision(
        answer={"option": "A", "text": "Go, read-only first"},
        answered_by=PERSON,
        answered_at=_at("12:06"),
        surface="web",
    )
    record = _one(gates.resolve(history, [row], names=NAMES, now=NOW))
    assert record["status"] == gates.ANSWERED
    assert record["answered_by"] == {"id": PERSON, "name": "Sam"}
    assert record["option"] == {"key": "A", "label": "Run the scenario"}
    assert record["comment"] == "Go, read-only first"
    assert record["surface"] == "web"
    assert record["wait_seconds"] == 600.0
    assert record["recorded_on_case"] is False
    assert record["asked_of"] == {"type": "user", "id": PERSON, "name": "Sam"}
    assert record["asked_by_agent"] == {"id": AGENT, "name": "Loop orchestrator"}
    assert record["next"]["kind"] == "note"
    # The UI names the next event with the History words, which read from / to (a state, a gate outcome).
    assert "from" in record["next"] and "to" in record["next"]
    assert record["decision_id"] == f"work_item:{ITEM}"


def test_answered_event_without_a_decision_row() -> None:
    history = _history(
        _gate("11:09", "ctem.analyst_review", "waited"),
        _gate("12:10", "ctem.analyst_review", "answered", PERSON, reason=PERSON, evidence="B"),
    )
    record = _one(gates.resolve(history, [], names=NAMES, now=NOW), "ctem.analyst_review")
    assert record["status"] == gates.ANSWERED
    assert record["answered_by"]["name"] == "Sam"
    assert record["option"] == {"key": "B", "label": None}
    assert record["wait_seconds"] == 61 * 60.0
    assert record["recorded_on_case"] is True


def test_rejected_answer_keeps_the_option_label() -> None:
    history = _history(_gate("11:56", "ctem.validation.non_readonly", "waited"))
    row = _decision(answer={"option": "B"}, answered_by=PERSON, answered_at=_at("12:00"), surface="teams_card")
    record = _one(gates.resolve(history, [row], names=NAMES, now=NOW))
    assert record["option"]["label"] == "Skip"
    assert record["surface"] == "teams_card"


def test_expired_and_cancelled_decisions() -> None:
    history = _history(_gate("11:56", "ctem.validation.non_readonly", "waited"))
    expired = _one(gates.resolve(history, [_decision("expired")], now=NOW))
    assert expired["status"] == gates.EXPIRED
    assert expired["answered_by"] is None
    assert expired["wait_seconds"] is None
    cancelled = _one(gates.resolve(history, [_decision("cancelled")], now=NOW))
    assert cancelled["status"] == gates.CANCELLED


def test_waiting_counts_the_wait_to_now() -> None:
    history = _history(_gate("11:56", "ctem.validation.non_readonly", "waited"))
    record = _one(gates.resolve(history, [_decision("open", deadline=_at("18:00"))], now=NOW))
    assert record["status"] == gates.WAITING
    assert record["wait_seconds"] == 64 * 60.0
    assert record["deadline"] == _at("18:00")
    assert record["next"] is None


def test_policy_pass_names_its_reason_kind() -> None:
    history = _history(
        _gate(
            "11:58", "ctem.mobilize", "passed_automatically", reason="pre-approved runbook patch-windows (level medium)"
        ),
        _gate("11:59", "ctem.remediation.decision", "passed_automatically", reason="within the approval threshold"),
        _gate(
            "12:00", "ctem.validation.non_readonly", "passed_automatically", reason="safe mode: read-only checks only"
        ),
        _gate("12:01", "ctem.analyst_review", "passed_automatically", reason="fleet level"),
    )
    records = {r["key"]: r for r in gates.resolve(history, [], now=NOW)}
    assert records["ctem.mobilize"]["reason"] == {"kind": "runbook", "runbook": "patch-windows"}
    assert records["ctem.remediation.decision"]["reason"] == {"kind": "within_threshold"}
    assert records["ctem.validation.non_readonly"]["reason"] == {"kind": "safe_mode"}
    assert records["ctem.analyst_review"]["reason"] == {"kind": "fleet_level"}
    assert all(r["status"] == gates.PASSED_AUTOMATICALLY for r in records.values())
    assert records["ctem.mobilize"]["passed_at"] == _at("11:58")


def test_records_follow_the_ctem_order_and_skip_gates_never_met() -> None:
    history = _history(
        _gate("11:56", "ctem.validation.non_readonly", "waited"), _gate("11:09", "ctem.analyst_review", "waited")
    )
    keys = [r["key"] for r in gates.resolve(history, [], now=NOW)]
    assert keys == ["ctem.analyst_review", "ctem.validation.non_readonly"]


def test_attach_extends_answer_on_the_waiting_event() -> None:
    history = _history(_gate("11:56", "ctem.validation.non_readonly", "waited"))
    row = _decision(answer={"option": "A", "text": "ok"}, answered_by=PERSON, answered_at=_at("12:06"), surface="web")
    records = gates.resolve(history, [row], names=NAMES, now=NOW)
    gates.attach(history, records)
    asked = next(e for e in history if e["kind"] == "gate")
    assert asked["answer"]["outcome"] == "answered"
    assert asked["answer"]["by"] == PERSON
    assert asked["answer"]["by_name"] == "Sam"
    assert asked["answer"]["option_label"] == "Run the scenario"
    assert asked["answer"]["comment"] == "ok"
    assert asked["answer"]["wait_seconds"] == 600.0
    assert asked["answer"]["recorded_on_case"] is False


def test_attach_keeps_what_the_case_event_said() -> None:
    history = _history(
        _gate("11:09", "ctem.analyst_review", "waited"),
        _gate("12:10", "ctem.analyst_review", "answered", PERSON, reason=PERSON, evidence="B"),
    )
    gates.attach(history, gates.resolve(history, [], names=NAMES, now=NOW))
    asked = next(e for e in history if e["kind"] == "gate" and e["to"] == "waited")
    assert asked["answer"]["at"] == _at("12:10")
    assert asked["answer"]["option"] == "B"
    assert asked["answer"]["recorded_on_case"] is True
