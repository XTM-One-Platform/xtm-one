"""The CTEM cases list filters by the type of the signals behind a case (#2556)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.api.v1 import ctem_cases as api
from app.models.ctem_case import CtemCase, CtemCaseIssue
from app.models.ctem_signal import TRIGGER_PATHS
from app.services import ctem_cases_view as view_of

NOW = datetime.now(UTC)


def _case(trigger_path: str = "new_vulnerability") -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Close the exposure",
        remediation_kind="close_exposure",
        keys=[],
        pinned_issues=[],
        state="new",
        state_since=NOW,
        severity="P2",
        trigger_path=trigger_path,
    )


def _signal(case: CtemCase) -> tuple[CtemCaseIssue, str]:
    signal_id = str(uuid.uuid4())
    return CtemCaseIssue(case_id=case.id, source="signal", external_id=signal_id, open=True), f"signal:{signal_id}"


def _world() -> tuple[list[tuple[CtemCase, list[CtemCaseIssue]]], view_of.ViewerView, dict[str, str]]:
    """Three cases: one on a new vulnerability and a leak, one on a threat, one with no signal seen."""
    vuln, threat, legacy = _case(), _case("relevant_threat"), _case()
    v1, v1_id = _signal(vuln)
    v2, v2_id = _signal(vuln)
    t1, t1_id = _signal(threat)
    hidden, _ = _signal(legacy)  # a signal of another reader: the legacy case keeps no visible signal
    product = CtemCaseIssue(case_id=legacy.id, source="opencti", external_id="vuln-1", open=True)
    view = view_of.ViewerView(
        issues={},
        unavailable={"opencti:vuln-1"},
        signals={v1_id, v2_id, t1_id},
        signal_details={
            v1_id: {"trigger_path": "new_vulnerability"},
            v2_id: {"trigger_path": "credential_leak"},
            t1_id: {"trigger_path": "relevant_threat"},
        },
    )
    cases = [(vuln, [v1, v2]), (threat, [t1]), (legacy, [hidden, product])]
    return cases, view, {"vuln": str(vuln.id), "threat": str(threat.id), "legacy": str(legacy.id)}


def test_each_row_carries_the_trigger_paths_of_the_signals_its_reader_sees() -> None:
    cases, view, ids = _world()
    rows, _ = view_of.list_for_viewer(cases, view, limit=10)
    by_id = {r["id"]: r["signal_types"] for r in rows}
    assert by_id == {
        ids["vuln"]: ["credential_leak", "new_vulnerability"],
        ids["threat"]: ["relevant_threat"],
        ids["legacy"]: [],
    }


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        (["credential_leak"], {"vuln"}),
        (["relevant_threat", "new_vulnerability"], {"vuln", "threat"}),
        (["none"], {"legacy"}),
        (["none", "relevant_threat"], {"threat", "legacy"}),
        (["blind_spot"], set()),
        ([], {"vuln", "threat", "legacy"}),
    ],
)
def test_the_signal_type_filter_keeps_the_cases_with_one_of_the_types(wanted: list[str], expected: set[str]) -> None:
    cases, view, ids = _world()
    rows, counts = view_of.list_for_viewer(cases, view, signal_type=wanted, limit=10)
    assert {r["id"] for r in rows} == {ids[name] for name in expected}
    # Like the state filter, it narrows the page, never the counts by state.
    assert counts == {"new": 3}


def test_the_signal_type_filter_combines_with_the_other_filters() -> None:
    cases, view, _ = _world()
    rows, _ = view_of.list_for_viewer(cases, view, signal_type=["relevant_threat"], priority="critical", limit=10)
    assert rows == []


def test_a_visible_signal_whose_details_were_unreadable_falls_back_to_the_case_trigger_path() -> None:
    case = _case("surface_drift")
    row, member = _signal(case)
    view = view_of.ViewerView(signals={member}, signal_details={})
    assert view_of.signal_types(case, [row], view) == ["surface_drift"]


def test_the_api_accepts_every_trigger_path_and_none_and_refuses_anything_else() -> None:
    assert api._signal_type_filter(None) == []
    assert api._signal_type_filter([*TRIGGER_PATHS, "none", " "]) == [*TRIGGER_PATHS, "none"]
    with pytest.raises(HTTPException) as refused:
        api._signal_type_filter(["new_vulnerability", "kev"])
    assert refused.value.status_code == 422
    assert "kev" in str(refused.value.detail)


def test_the_api_names_a_label_for_every_trigger_path() -> None:
    assert list(api.SIGNAL_TYPE_LABELS) == list(TRIGGER_PATHS)
    assert all(label for label in api.SIGNAL_TYPE_LABELS.values())
