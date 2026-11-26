"""CTEM decisions of the same kind for one person are asked once (#2451): the pure rules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.services import ctem_decision_groups as groups

NOW = datetime(2026, 11, 26, 7, 0, tzinfo=UTC)


def _case(**over: object) -> groups.CaseFacts:
    base: dict[str, object] = {
        "case_id": str(uuid.uuid4()),
        "title": "Measure kibana-1 for the first time (no test evidence yet)",
        "trigger_path": "control_decay",
        "severity": "P3",
        "sla_due_at": NOW + timedelta(days=30),
    }
    base.update(over)
    return groups.CaseFacts(**base)  # type: ignore[arg-type]


def test_a_first_measure_case_is_groupable() -> None:
    assert groups.group_kind(_case()) == groups.FIRST_MEASURE


def test_any_other_case_stays_individual() -> None:
    assert groups.group_kind(_case(title="Re-test the control on kibana-1")) is None
    assert groups.group_kind(_case(trigger_path="new_vulnerability", title="Upgrade Chrome")) is None


def test_a_p1_near_its_sla_keeps_its_own_card() -> None:
    assert groups.stays_individual(_case(severity="P1", sla_due_at=NOW + timedelta(hours=10)), NOW)
    assert not groups.stays_individual(_case(severity="P1", sla_due_at=NOW + timedelta(days=10)), NOW)
    assert not groups.stays_individual(_case(severity="P2", sla_due_at=NOW + timedelta(hours=1)), NOW)


def test_the_group_key_names_no_case() -> None:
    key = groups.group_key(groups.FIRST_MEASURE, "user-abc")
    assert groups.is_group_key(key)
    assert groups.case_id_of(key) is None
    case_id = str(uuid.uuid4())
    assert groups.case_id_of(f"ctem:validation:{case_id}") == case_id
    assert groups.case_id_of("ctem:review:" + case_id) is None


def _members(n: int) -> list[dict[str, object]]:
    return [
        {
            "item_id": str(uuid.uuid4()),
            "case_id": str(uuid.uuid4()),
            "title": f"Measure host-{i} for the first time (no test evidence yet)",
            "severity": "P2" if i % 2 else "P3",
            "due_at": (NOW + timedelta(days=i + 1)).isoformat(),
            "priority": "medium",
        }
        for i in range(n)
    ]


def test_the_card_counts_the_cases_and_fits_its_limits() -> None:
    members = _members(500)
    ask = groups.build_ask(groups.FIRST_MEASURE, members, cases_url="https://x/ctem/cases")
    assert "500" in ask["question"]
    assert len(ask["context"]) <= 800
    assert "more on the cases list" in ask["context"]
    assert [o["key"] for o in ask["options"]] == [
        groups.APPROVE_ALL,
        groups.APPROVE_SELECTED,
        groups.DECLINE_ALL,
        groups.LATER,
    ]
    assert ask["options"][-1]["keeps_waiting"] is True
    assert ask["recommendation"] == groups.APPROVE_ALL
    assert ask["links"][0]["url"] == "https://x/ctem/cases"
    from app.services.decision_ask import normalize_ask, person_ask_missing

    assert normalize_ask(ask) is not None
    assert person_ask_missing(ask) == []


def test_the_due_date_is_the_earliest_of_the_group() -> None:
    members = _members(3)
    assert groups.earliest_due(members) == NOW + timedelta(days=1)
    assert groups.earliest_due([{"due_at": None}]) is None


def test_approve_selected_reads_the_card_numbers_and_ranges() -> None:
    members = _members(6)
    chosen = groups.selected_members(members, "approve 1 and 4-5")
    assert [m["case_id"] for m in chosen] == [members[0]["case_id"], members[3]["case_id"], members[4]["case_id"]]
    assert groups.selected_members(members, "all of them") == []
    assert groups.selected_members(members, "9") == []
    assert groups.selected_members(members, "skip host-2 and kibana3") == []
