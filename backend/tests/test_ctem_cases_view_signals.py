"""A case opened from a signal with no product issue is visible to whoever sees the signal (CTEM-03-UI, #892)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.models.ctem_case import CtemCase, CtemCaseIssue
from app.services import ctem_cases_view as view_of

NOW = datetime.now(UTC)


def _case() -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Close the exposure of portal-edge-01",
        remediation_kind="close_exposure",
        keys=["close_exposure:portal-edge-01"],
        pinned_issues=[],
        state="new",
        state_since=NOW,
        severity="P1",
    )


def test_a_signal_only_case_is_shown_to_a_viewer_of_its_signal() -> None:
    case = _case()
    signal_id = uuid.uuid4()
    rows = [CtemCaseIssue(case_id=case.id, source="signal", external_id=str(signal_id), open=True)]
    view = view_of.ViewerView(signals={f"signal:{signal_id}"})
    rendered = view_of.hydrate(case, rows, [], view)
    assert rendered is not None
    assert rendered["issues"] == [{"source": "signal", "external_id": str(signal_id), "open": True}]
    assert rendered["details_unavailable"] == [] and rendered["restricted_count"] == 0
    # One case, one urgency (UX round 1 on #897, B3): P1 shows as critical, never "low".
    assert rendered["priority"] == "critical" and rendered["priority_basis"] == "loop severity"


def test_a_case_row_carries_its_dates_and_signal_count() -> None:
    """The list's Created, Age, Last change and Source signals columns (#1176)."""
    case = _case()
    case.created_at = NOW
    case.updated_at = NOW
    signal_ids = [uuid.uuid4(), uuid.uuid4()]
    rows = [CtemCaseIssue(case_id=case.id, source="signal", external_id=str(s), open=True) for s in signal_ids]
    view = view_of.ViewerView(signals={f"signal:{s}" for s in signal_ids})
    row = view_of.summary(case, rows, view)
    assert row is not None
    assert row["created_at"] == NOW.isoformat() and row["updated_at"] == NOW.isoformat()
    assert row["signal_count"] == 2


def test_case_priority_follows_the_loop_severity_else_the_default_policy() -> None:
    assert view_of.case_priority("P1", []) == "critical"
    assert view_of.case_priority("P2", []) == "high"
    assert view_of.case_priority("P3", []) == "medium"
    assert view_of.case_priority(None, []) == "low"


def test_a_signal_only_case_stays_hidden_from_who_cannot_see_the_signal() -> None:
    case = _case()
    rows = [CtemCaseIssue(case_id=case.id, source="signal", external_id=str(uuid.uuid4()), open=True)]
    assert view_of.hydrate(case, rows, [], view_of.ViewerView()) is None
    listed, counts = view_of.list_for_viewer([(case, rows)], view_of.ViewerView())
    assert listed == [] and counts == {}


async def test_signal_members_reads_the_callers_own_signals_unless_admin() -> None:
    seen: list[str] = []
    member = uuid.uuid4()

    class _Db:
        def __init__(self, user: Any) -> None:
            self.user = user

        async def get(self, _model: Any, _id: Any) -> Any:
            return self.user

        async def execute(self, query: Any) -> Any:
            seen.append(str(query))
            return SimpleNamespace(scalars=lambda: [member])

    caller = uuid.uuid4()
    assert await view_of.signal_members(_Db(SimpleNamespace(role="user", is_admin=False)), str(caller)) == {
        f"signal:{member}"
    }
    assert "recorded_by" in seen[-1]
    await view_of.signal_members(_Db(SimpleNamespace(role="admin", is_admin=True)), str(caller))
    assert "recorded_by" not in seen[-1]
