"""What a finding on the affected asset reaches, written on the case (CTEM-23, #1289).

``ctem_case_loop.record_attack_path`` (the triage write) and
``ctem_cases_view.attack_path_for`` (what one viewer reads): hops kept nearest
first with proven or inferred, invalid rows dropped, one event per change,
and the OpenCRQ asset names and simulation ids served only to a viewer who
reads OpenCRQ.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules
from app.services import ctem_cases_view as view_of

NOW = datetime(2026, 11, 22, 21, 0, tzinfo=UTC)


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    def events(self) -> list[CtemCaseEvent]:
        return [r for r in self.added if isinstance(r, CtemCaseEvent) and r.kind == "attack_path"]


def _case() -> CtemCase:
    return CtemCase(id=uuid.uuid4(), title="Upgrade OpenSSH", attack_path={})


def _record(db: FakeSession, case: CtemCase, reading: dict[str, Any], **kw: Any) -> CtemCase:
    return loop.record_attack_path(cast(Any, db), case, reading, **kw)


READING: dict[str, Any] = {
    "origin": {"asset": "Edge bastion", "exposure": "internet-facing"},
    "hops": [
        {
            "from": "Edge bastion",
            "to": "Jump host",
            "nature": "proven",
            "simulation": "sim-2026-11-20",
            "simulation_name": "Edge pivot drill",
            "simulation_url": "https://aev.example/admin/simulations/sim-2026-11-20",
            "seen_at": "2026-11-20T09:30:00Z",
        },
        {
            "from": "Jump host",
            "to": "Core banking",
            "nature": "guessed",
            "simulation_url": "javascript:alert(1)",
            "seen_at": "soon",
        },
        {"from": "", "to": "Nowhere", "nature": "proven"},
        "not a hop",
    ],
    "reached": [
        {"asset": "Jump host", "depth": 1, "criticality": "Medium"},
        {"asset": "Core banking", "depth": 2, "criticality": "very_high"},
        {"asset": ""},
    ],
    "primary": ["Credit decision", "Credit decision", ""],
}


def test_record_keeps_hops_nearest_first_and_one_event() -> None:
    db, case = FakeSession(), _case()
    _record(db, case, READING, actor="agent-1", now=NOW)
    stored = case.attack_path
    assert stored["origin"] == {"asset": "Edge bastion", "exposure": "internet_facing"}
    assert [(h["from"], h["to"], h["nature"]) for h in stored["hops"]] == [
        ("Edge bastion", "Jump host", "proven"),
        ("Jump host", "Core banking", "inferred"),
    ]
    assert stored["hops"][0]["simulation"] == "sim-2026-11-20"
    assert stored["hops"][0]["simulation_name"] == "Edge pivot drill"
    assert stored["hops"][0]["seen_at"] == "2026-11-20"
    assert stored["hops"][1]["simulation_url"] is None and stored["hops"][1]["seen_at"] is None
    assert [a["asset"] for a in stored["reached"]] == ["Jump host", "Core banking"]
    assert stored["reached"][0]["criticality"] == "medium"
    assert stored["primary"] == ["Credit decision"]
    assert stored["at"] == NOW.isoformat()
    [event] = db.events()
    assert event.actor == "agent-1"
    assert event.to_value == "2"
    assert event.evidence == "2 hop(s), 1 proven, 2 reached, 1 primary"
    assert "Edge bastion" not in (event.evidence or "")


def test_same_reading_twice_writes_one_event() -> None:
    db, case = FakeSession(), _case()
    _record(db, case, READING, now=NOW)
    _record(db, case, READING, now=NOW)
    assert len(db.events()) == 1


def test_an_origin_with_no_hop_is_a_reading() -> None:
    db, case = FakeSession(), _case()
    _record(db, case, {"origin": {"asset": "Isolated host"}}, now=NOW)
    assert case.attack_path["hops"] == []
    assert case.attack_path["origin"]["exposure"] == "unknown"
    assert len(db.events()) == 1


def test_no_origin_is_refused_and_nothing_written() -> None:
    db, case = FakeSession(), _case()
    with pytest.raises(rules.CaseRuleError):
        _record(db, case, {"hops": READING["hops"]}, now=NOW)
    assert case.attack_path == {}
    assert db.events() == []


def test_capped_hops() -> None:
    db, case = FakeSession(), _case()
    many = {"origin": {"asset": "a"}, "hops": [{"from": f"a{i}", "to": f"b{i}"} for i in range(60)]}
    _record(db, case, many, now=NOW)
    assert len(case.attack_path["hops"]) == loop.ATTACK_PATH_MAX


def test_viewer_without_opencrq_sees_the_shape_not_the_names() -> None:
    db, case = FakeSession(), _case()
    _record(db, case, READING, now=NOW)
    shown = view_of.attack_path_for(case, view_of.ViewerView(opencrq=False))
    assert shown is not None
    assert shown["origin"] == {"asset": view_of.RESTRICTED, "exposure": "internet_facing"}
    assert {h["from"] for h in shown["hops"]} == {view_of.RESTRICTED}
    assert [h["nature"] for h in shown["hops"]] == ["proven", "inferred"]
    assert all(h["simulation_name"] is None and h["simulation_url"] is None for h in shown["hops"])
    assert all("simulation" not in h for h in shown["hops"])
    assert shown["hops"][0]["seen_at"] == "2026-11-20"
    assert [a["depth"] for a in shown["reached"]] == [1, 2]
    assert shown["primary"] == [view_of.RESTRICTED]


def test_viewer_with_opencrq_sees_names_and_simulation() -> None:
    db, case = FakeSession(), _case()
    _record(db, case, READING, now=NOW)
    shown = view_of.attack_path_for(case, view_of.ViewerView(opencrq=True))
    assert shown is not None
    assert shown["hops"][0] == {
        "from": "Edge bastion",
        "to": "Jump host",
        "nature": "proven",
        "simulation_name": "Edge pivot drill",
        "simulation_url": "https://aev.example/admin/simulations/sim-2026-11-20",
        "seen_at": "2026-11-20",
    }
    assert shown["primary"] == ["Credit decision"]


def test_no_reading_yet_is_none() -> None:
    assert view_of.attack_path_for(_case(), view_of.ViewerView(opencrq=True)) is None
