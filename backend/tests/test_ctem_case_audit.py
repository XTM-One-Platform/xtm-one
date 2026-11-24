# SPDX-License-Identifier: Apache-2.0

"""The CTEM case audit trail (#1490): phase mapping, autonomy classes, gate stories, phase durations."""

from datetime import UTC, datetime

from app.services import ctem_case_audit as audit

PERSON = "11111111-1111-1111-1111-111111111111"
AGENT = "22222222-2222-2222-2222-222222222222"


def _e(at: str, kind: str, actor: str = AGENT, **kw):
    return {"at": f"2026-11-23T{at}:00+00:00", "actor": actor, "kind": kind, "issue": None, **kw}


def _history():
    # Served newest first, like hydrate does.
    return list(
        reversed(
            [
                _e("10:28", "created", "system", to="key"),
                _e("10:28", "announced", "system", to="ctem_case.opened", reason="catch-up"),
                _e("10:30", "owner", PERSON, to=PERSON),
                _e("10:30", "note", reason="Loop opened (run 80e33969-b749-4870-9031-d24ab0e53353). Case read: 914."),
                _e("11:08", "note", reason="Relevance verdict (task c3636c8b): RELEVANT."),
                _e("11:09", "threat_package", to="grouping"),
                _e("11:09", "gate", **{"from": "ctem.analyst_review", "to": "waited", "reason": "full_gates"}),
                _e("11:56", "gate", **{"from": "ctem.validation.non_readonly", "to": "waited"}),
                _e("11:56", "note", reason="Validation plan (task bcb82849, PLAN ONLY). Step (a)."),
                _e("11:58", "gate", **{"from": "ctem.mobilize", "to": "passed_automatically", "reason": "runbook x"}),
                _e("12:10", "gate", PERSON, **{"from": "ctem.analyst_review", "to": "answered", "reason": PERSON}),
            ]
        )
    )


def test_every_event_gets_a_phase_and_an_autonomy_class():
    out = audit.annotate(_history(), people=[PERSON])
    assert all(e["phase"] in audit.PHASES for e in out)
    assert all(e["autonomy"] in audit.AUTONOMY_CLASSES for e in out)


def test_phase_mapping_by_kind_gate_and_note():
    out = {(e["kind"], e["at"][11:16]): e for e in audit.annotate(_history(), people=[PERSON])}
    assert out[("created", "10:28")]["phase"] == "discovery"
    assert out[("note", "11:08")]["phase"] == "prioritization"
    assert out[("threat_package", "11:09")]["phase"] == "prioritization"
    assert out[("gate", "11:56")]["phase"] == "validation"
    assert out[("note", "11:56")]["phase"] == "validation"
    assert out[("gate", "11:58")]["phase"] == "remediation"
    # A carry kind takes the phase the case is in.
    assert out[("owner", "10:30")]["phase"] == "discovery"


def test_autonomy_classes():
    out = {(e["kind"], e["at"][11:16]): e for e in audit.annotate(_history(), people=[PERSON])}
    assert out[("note", "11:08")]["autonomy"] == audit.AUTONOMOUS
    assert out[("gate", "11:56")]["autonomy"] == audit.WAITING
    assert out[("gate", "11:58")]["autonomy"] == audit.VALIDATED_BY_POLICY
    assert out[("gate", "12:10")]["autonomy"] == audit.VALIDATED_BY_PERSON
    assert out[("owner", "10:30")]["autonomy"] == audit.CHANGED
    assert out[("created", "10:28")]["autonomy"] == audit.SYSTEM
    assert out[("owner", "10:30")]["actor_type"] == audit.ACTOR_PERSON
    assert out[("note", "11:08")]["actor_type"] == audit.ACTOR_AGENT


def test_a_waiting_gate_names_its_answer_and_the_answer_its_wait():
    out = {(e["kind"], e["at"][11:16]): e for e in audit.annotate(_history(), people=[PERSON])}
    asked = out[("gate", "11:09")]
    assert asked["answer"]["outcome"] == "answered"
    assert asked["answer"]["by"] == PERSON
    assert out[("gate", "12:10")]["waited_since"] == asked["at"]
    assert out[("gate", "11:56")]["answer"] is None


def test_note_headline_is_its_first_sentence_and_refs_are_extracted():
    out = {(e["kind"], e["at"][11:16]): e for e in audit.annotate(_history(), people=[PERSON])}
    note = out[("note", "10:30")]
    assert note["headline"] == "Loop opened (run 80e33969-b749-4870-9031-d24ab0e53353)."
    assert note["refs"] == [{"kind": "run", "id": "80e33969-b749-4870-9031-d24ab0e53353", "full": True}]
    assert out[("note", "11:08")]["refs"] == [{"kind": "task", "id": "c3636c8b", "full": False}]


def test_announced_headline_says_what_happened():
    out = {(e["kind"], e["at"][11:16]): e for e in audit.annotate(_history(), people=[PERSON])}
    assert "catch-up" in out[("announced", "10:28")]["headline"]
    assert audit.headline({"kind": "announced", "reason": "backfill"}).startswith(
        "Announced to the loop by the backfill"
    )


def test_order_is_kept():
    history = _history()
    out = audit.annotate(history, people=[PERSON])
    assert [e["at"] for e in out] == [e["at"] for e in history]


def test_phases_entered_exited_durations_and_current():
    out = audit.annotate(_history(), people=[PERSON])
    now = datetime(2026, 11, 23, 12, 30, tzinfo=UTC)
    summary = audit.phases(out, now=now)
    rows = {r["phase"]: r for r in summary["phases"]}
    assert [r["phase"] for r in summary["phases"]] == list(audit.PHASES)
    assert summary["current"] == "prioritization"  # the 12:10 answer is a prioritization gate
    assert rows["discovery"]["entered_at"].startswith("2026-11-23T10:28")
    assert rows["discovery"]["exited_at"].startswith("2026-11-23T11:08")
    assert rows["discovery"]["seconds"] == 40 * 60
    assert rows["scoping"]["entered_at"] is None and rows["scoping"]["events"] == 0
    assert rows["prioritization"]["current"] is True
    assert sum(r["events"] for r in summary["phases"]) == len(out)
    assert rows["validation"]["waiting"] == 1
    assert rows["prioritization"]["waiting"] == 0
    assert sum(rows["remediation"]["autonomy"].values()) == rows["remediation"]["events"]


def test_events_without_a_date_still_get_a_phase():
    out = audit.annotate([{"at": None, "actor": "system", "kind": "note", "reason": "x"}])
    assert out[0]["phase"] == "discovery"
    assert audit.phases(out)["current"] is None


def test_current_phase_is_the_phase_of_the_newest_event() -> None:
    # The Cases list groups and lays out its Kanban by it (#1503).
    assert audit.current_phase(_history()) == "prioritization"  # type: ignore[no-untyped-call]
    assert audit.current_phase([]) is None
    assert audit.current_phase([_e("09:00", "ticket", to="open")]) == "remediation"


def test_open_wait_is_the_newest_unanswered_waiting_gate() -> None:
    # analyst_review waited 11:09 and was answered 12:10; validation.non_readonly waits since 11:56.
    assert audit.open_wait(_history()) == {  # type: ignore[no-untyped-call]
        "gate": "ctem.validation.non_readonly",
        "by": None,
        "since": "2026-11-23T11:56:00+00:00",
    }
    assert audit.open_wait([_e("09:00", "ticket", to="open")]) is None


def test_open_wait_falls_back_to_the_move_into_waiting_decision() -> None:
    # UX r1 B2 on #1515: a person set Waiting for decision by hand, no gate waits.
    person = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
    events = [
        _e("09:00", "state", actor="system", **{"from": None, "to": "new"}),
        _e("10:00", "state", actor=person, reason="needs the CISO", **{"from": "analyzing", "to": "waiting_decision"}),
    ]
    assert audit.open_wait(events) == {"gate": None, "by": person, "since": "2026-11-23T10:00:00+00:00"}  # type: ignore[no-untyped-call]
    # Moved by the system: since when, nobody named.
    system_move = [_e("10:00", "state", actor="system", **{"from": "new", "to": "waiting_decision"})]
    assert audit.open_wait(system_move) == {"gate": None, "by": None, "since": "2026-11-23T10:00:00+00:00"}  # type: ignore[no-untyped-call]


def test_run_phase():
    out = audit.annotate(_history(), people=[PERSON])
    assert audit.run_phase("2026-11-23T10:28:30+00:00", out) == "discovery"
    assert audit.run_phase("2026-11-23T11:57:00+00:00", out) == "validation"
    assert audit.run_phase("2026-11-23T09:00:00+00:00", out) == "discovery"


def test_every_gate_key_has_a_phase():
    from app.agents.builtin_fleet_docs import CTEM_GATE_KEYS

    assert set(CTEM_GATE_KEYS) <= set(audit.GATE_PHASE)
