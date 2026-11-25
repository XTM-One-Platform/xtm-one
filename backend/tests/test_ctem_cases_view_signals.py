"""A case opened from a signal with no product issue is visible to whoever sees the signal (CTEM-03-UI, #892)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.models.ctem_signal import CtemSignal
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


def _signal(**kw: Any) -> CtemSignal:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "trigger_path": "relevant_threat",
        "lens": "world",
        "source_provider": "opencti",
        "subject_kind": "threat",
        "subject_ref": "intrusion-set--1",
        "subject_label": "O365 Device Code Phishing Campaign",
        "first_seen_at": NOW,
    }
    base.update(kw)
    return CtemSignal(**base)


def test_a_visible_signal_member_is_named_never_shown_by_its_id() -> None:
    """A signal row says which signal and what it is (#1936)."""
    case = _case()
    signal = _signal()
    member = f"signal:{signal.id}"
    rows = [CtemCaseIssue(case_id=case.id, source="signal", external_id=str(signal.id), open=True)]
    events = [CtemCaseEvent(case_id=case.id, at=NOW, actor="system", kind="signal", issue=member)]
    view = view_of.ViewerView(signals={member}, signal_details={member: view_of.signal_detail(signal)})
    rendered = view_of.hydrate(case, rows, events, view)
    assert rendered is not None
    issue = rendered["issues"][0]
    assert issue["name"] == "O365 Device Code Phishing Campaign"
    assert issue["trigger_path"] == "relevant_threat"
    assert issue["trigger_path_label"] == "Relevant new threat or actor"
    assert issue["lens"] == "world" and issue["source_provider"] == "opencti" and issue["source_name"] == "OpenCTI"
    assert issue["subject_kind"] == "threat" and issue["assets"] == []
    assert issue["first_seen_at"] == NOW.isoformat()
    # The history row names the same signal.
    assert rendered["history"][0]["issue_label"] == "O365 Device Code Phishing Campaign"
    assert rendered["history"][0]["issue_path_label"] == "Relevant new threat or actor"


def test_a_nameless_signal_event_carries_its_path_label_never_its_id() -> None:
    """A signal with no name reads as its trigger path in History (UX round 1 B2 on #1945)."""
    case = _case()
    signal = _signal(subject_label=None, subject_ref=None)
    member = f"signal:{signal.id}"
    rows = [CtemCaseIssue(case_id=case.id, source="signal", external_id=str(signal.id), open=True)]
    events = [CtemCaseEvent(case_id=case.id, at=NOW, actor="system", kind="signal", issue=member)]
    view = view_of.ViewerView(signals={member}, signal_details={member: view_of.signal_detail(signal)})
    rendered = view_of.hydrate(case, rows, events, view)
    assert rendered is not None
    event = rendered["history"][0]
    assert event["issue_label"] is None and event["issue_path_label"] == "Relevant new threat or actor"


def test_a_signal_about_an_asset_names_it_as_its_asset() -> None:
    detail = view_of.signal_detail(_signal(subject_kind="asset", subject_ref="portal-edge-01", subject_label=None))
    assert detail["name"] == "portal-edge-01" and detail["assets"] == ["portal-edge-01"]


def test_a_signal_member_carries_what_its_module_pivot_needs() -> None:
    """A signal row opens its object in OpenCTI, OpenAEV or OpenCRQ (#1357, lost by #1945)."""
    cti = view_of.signal_detail(_signal(source_external_id="vulnerability--1"))
    assert cti["source_external_id"] == "vulnerability--1" and cti["source_link"] is None
    # An OpenAEV signal read from an OpenAEV issue names the finding; a listener delivery its page.
    finding = view_of.signal_detail(
        _signal(source_provider="openaev", source_external_id="f-1", evidence={"source_issue": "openaev:f-1"})
    )
    assert finding["source_external_id"] == "f-1"
    run = view_of.signal_detail(
        _signal(
            source_provider="openaev", source_external_id="s-1", evidence={"platform_url": "https://aev.example/s/1"}
        )
    )
    assert run["source_external_id"] is None and run["source_link"] == "https://aev.example/s/1"
    # OpenCRQ: the asset page the pass wrote; no id route is guessed.
    crq = view_of.signal_detail(
        _signal(source_provider="opencrq", evidence={"link": "https://crq.example/t1/assets/a1/overview"})
    )
    assert crq["source_link"] == "https://crq.example/t1/assets/a1/overview" and crq["source_external_id"] is None
    # A non-http link and a non-module source give nothing to pivot to.
    unsafe = view_of.signal_detail(_signal(evidence={"link": "javascript:alert(1)"}, source_external_id=None))
    assert unsafe["source_link"] is None and unsafe["source_external_id"] is None
    manual = view_of.signal_detail(_signal(source_provider="manual", source_external_id="x-1"))
    assert manual["source_external_id"] is None and manual["source_link"] is None


def test_a_hidden_signal_member_stays_restricted_with_nothing_of_it() -> None:
    case = _case()
    seen, hidden = _signal(), _signal(subject_label="secret campaign")
    rows = [CtemCaseIssue(case_id=case.id, source="signal", external_id=str(s.id), open=True) for s in (seen, hidden)]
    events = [CtemCaseEvent(case_id=case.id, at=NOW, actor="system", kind="signal", issue=f"signal:{hidden.id}")]
    member = f"signal:{seen.id}"
    view = view_of.ViewerView(signals={member}, signal_details={member: view_of.signal_detail(seen)})
    rendered = view_of.hydrate(case, rows, events, view)
    assert rendered is not None
    assert {"restricted": True} in rendered["issues"] and rendered["restricted_count"] == 1
    assert rendered["history"][0]["issue"] == "Restricted" and rendered["history"][0]["issue_label"] is None
    assert rendered["history"][0]["issue_path_label"] is None
    assert "secret campaign" not in str(rendered) and str(hidden.id) not in str(rendered)


async def test_signal_details_reads_only_the_members_given() -> None:
    signal = _signal()
    seen: list[str] = []

    class _Db:
        async def execute(self, query: Any) -> Any:
            seen.append(str(query))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [signal]))

    assert await view_of.signal_details(_Db(), set()) == {}
    assert seen == []
    details = await view_of.signal_details(_Db(), {f"signal:{signal.id}", "signal:not-a-uuid"})
    assert list(details) == [f"signal:{signal.id}"]
    assert details[f"signal:{signal.id}"]["name"] == "O365 Device Code Phishing Campaign"


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
