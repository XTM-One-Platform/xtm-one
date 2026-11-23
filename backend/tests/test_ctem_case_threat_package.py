"""The threat package of a case (CTEM-16-UI, #1115).

``ctem_case_loop.record_threat_package`` (the review step's write: the OpenCTI
Grouping the orchestrator drafted and the security coverage requested on it)
and ``ctem_cases_view.threat_package_for`` (what one viewer reads): the
Grouping id must be an OpenCTI id and the link its Grouping page, the same
package twice writes one event, and the name, ids and link go only to a viewer
who reads OpenCTI; every viewer of the case sees that a package was drafted and
whether its coverage was requested.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules
from app.services import ctem_cases_view as view_of

NOW = datetime(2026, 11, 22, 7, 0, tzinfo=UTC)
GROUPING = "8a3e2f10-4c5d-4b6e-9f70-1a2b3c4d5e6f"
URL = f"https://cti.example.test/dashboard/analyses/groupings/{GROUPING}"
COVERAGE = "security-coverage--0c6f9d3a-2b1e-4f5a-8d7c-6e5f4a3b2c1d"
PACKAGE = {"grouping_id": GROUPING, "name": "CVE-2026-1234 on nginx", "url": URL, "coverage_id": COVERAGE}


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    def events(self) -> list[CtemCaseEvent]:
        return [r for r in self.added if isinstance(r, CtemCaseEvent) and r.kind == "threat_package"]


def _case() -> CtemCase:
    return CtemCase(id=uuid.uuid4(), title="Upgrade nginx")


def test_record_writes_one_event_with_grouping_name_link_and_coverage() -> None:
    db, case = FakeSession(), _case()
    loop.record_threat_package(db, case, PACKAGE, actor="agent-1", now=NOW)
    (event,) = db.events()
    assert event.actor == "agent-1"
    assert (event.to_value, event.reason, event.evidence, event.from_value) == (
        GROUPING,
        PACKAGE["name"],
        URL,
        COVERAGE,
    )


def test_same_package_again_writes_nothing() -> None:
    db, case = FakeSession(), _case()
    loop.record_threat_package(db, case, PACKAGE, dict(PACKAGE), now=NOW)
    assert db.events() == []


def test_coverage_added_later_writes_a_new_event() -> None:
    db, case = FakeSession(), _case()
    drafted = {**PACKAGE, "coverage_id": None}
    loop.record_threat_package(db, case, PACKAGE, drafted, now=NOW)
    (event,) = db.events()
    assert event.from_value == COVERAGE


DRAFT_URL = "https://cti.example.test/dashboard/data/import/draft/0f1e2d3c-4b5a-4968-8776-5a4b3c2d1e0f"


def test_drafted_package_links_the_draft_page() -> None:
    """#1453: a Grouping inside an OpenCTI draft has no live page, so its link is the draft's."""
    db, case = FakeSession(), _case()
    loop.record_threat_package(db, case, {**PACKAGE, "url": DRAFT_URL}, now=NOW)
    (event,) = db.events()
    assert (event.to_value, event.evidence) == (GROUPING, DRAFT_URL)


def test_validated_package_switches_to_the_grouping_page() -> None:
    db, case = FakeSession(), _case()
    drafted = {**PACKAGE, "url": DRAFT_URL}
    loop.record_threat_package(db, case, PACKAGE, drafted, now=NOW)
    (event,) = db.events()
    assert event.evidence == URL


def test_stix_id_and_no_link_are_kept() -> None:
    db, case = FakeSession(), _case()
    loop.record_threat_package(db, case, {"grouping_id": f"grouping--{GROUPING}"}, now=NOW)
    (event,) = db.events()
    assert event.to_value == f"grouping--{GROUPING}"
    assert (event.evidence, event.from_value, event.reason) == (None, None, None)


@pytest.mark.parametrize(
    "package",
    [
        {"grouping_id": "grouping-1", "url": URL},
        {"grouping_id": GROUPING, "url": "javascript:alert(1)"},
        {"grouping_id": GROUPING, "url": f"https://cti.example.test/dashboard/analyses/groupings/{uuid.uuid4()}"},
        {"grouping_id": GROUPING, "url": f"https://cti.example.test/dashboard/analyses/reports/{GROUPING}"},
        {"grouping_id": GROUPING, "url": "https://cti.example.test/dashboard/data/import/draft/not-a-draft-id"},
        {"grouping_id": f"report--{GROUPING}"},
        "not a package",
    ],
)
def test_bad_id_or_link_is_refused_and_nothing_written(package: object) -> None:
    db, case = FakeSession(), _case()
    with pytest.raises(rules.CaseRuleError):
        loop.record_threat_package(db, case, package, now=NOW)  # type: ignore[arg-type]
    assert db.events() == []


def _event(at: datetime, grouping: str, name: str, coverage: str | None) -> CtemCaseEvent:
    return CtemCaseEvent(
        case_id=uuid.uuid4(),
        actor="a",
        kind="threat_package",
        at=at,
        to_value=grouping,
        reason=name,
        evidence=URL,
        from_value=coverage,
    )


def test_view_serves_the_newest_package_whatever_the_order() -> None:
    older = _event(NOW - timedelta(hours=1), GROUPING, "Draft", None)
    newer = _event(NOW, GROUPING, "Final", COVERAGE)
    note = CtemCaseEvent(case_id=uuid.uuid4(), actor="a", kind="note", at=NOW + timedelta(hours=1))
    for events in ([older, newer, note], [note, newer, older]):
        shown = view_of.threat_package_for(events, view_of.ViewerView(opencti=True))
        assert shown == {
            "grouping_id": GROUPING,
            "name": "Final",
            "url": URL,
            "coverage_id": COVERAGE,
            "state": "validated",
            "coverage_requested": True,
            "coverage_drafted": False,
            "draft_overdue": False,
            "at": NOW.isoformat(),
            "restricted": False,
        }


def test_viewer_without_opencti_sees_only_that_a_package_exists() -> None:
    events = [_event(NOW, GROUPING, "Final", COVERAGE)]
    shown = view_of.threat_package_for(events, view_of.ViewerView(opencti=False))
    assert shown == {
        "restricted": True,
        "state": "validated",
        "coverage_requested": True,
        "coverage_drafted": False,
        "draft_overdue": False,
        "at": NOW.isoformat(),
    }
    row = view_of._event(events[0], view_of.ViewerView(opencti=False))
    assert (row["from"], row["to"], row["reason"], row["evidence"]) == (None, None, None, None)
    assert row["kind"] == "threat_package"


def test_opencti_access_survives_the_view_cache() -> None:
    cached = view_of.ViewerView.from_json(view_of.ViewerView(opencti=True).to_json())
    assert cached.opencti is True


def test_no_package_is_none() -> None:
    assert view_of.threat_package_for([], view_of.ViewerView(opencti=True)) is None
