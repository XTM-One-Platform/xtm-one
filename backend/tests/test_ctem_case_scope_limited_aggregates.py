"""A case aggregate this viewer cannot fully read is null with a reason, never a silent 0.

A viewer who sees a case only through some of its members (a signal, a member of a product that
did not answer, or an issue outside their scope) cannot count its assets or primary assets: the
detail aggregates are ``None`` with ``aggregates_restricted`` and ``aggregates_reason``. The list
answers ``hidden_cases`` beside the rows, and the single case read follows the list's asset-count
rule (#1704), so both agree for one case and one viewer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.ctem_case import CtemCase, CtemCaseIssue
from app.services import ctem_case_version_history as version_history
from app.services import ctem_cases as rules
from app.services import ctem_cases_view as view_of

NOW = datetime.now(UTC)


def _case(state: str = "new") -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Upgrade Acme Browser",
        remediation_kind="upgrade",
        keys=["upgrade:acme-browser"],
        pinned_issues=[],
        state=state,
        state_since=NOW,
        severity="P2",
    )


def _issue(external_id: str, *, assets: tuple[str, ...], pa_rows: tuple[str, ...]) -> rules.SourceIssue:
    return rules.SourceIssue(
        source="opencti",
        external_id=external_id,
        name=f"CVE-2026-{external_id}",
        cve=f"CVE-2026-{external_id}",
        cvss=8.1,
        epss=0.2,
        kev=True,
        assets=assets,
        primary_assets=pa_rows,
        first_seen_at=NOW,
    )


def _members(case: CtemCase, signal: str) -> list[CtemCaseIssue]:
    return [
        CtemCaseIssue(case_id=case.id, source="signal", external_id=signal, open=True),
        CtemCaseIssue(case_id=case.id, source="opencti", external_id="1", open=True),
        CtemCaseIssue(case_id=case.id, source="opencti", external_id="2", open=True),
    ]


def _admin_view(signal: str) -> view_of.ViewerView:
    issues = {
        "opencti:1": _issue("1", assets=("srv-a", "srv-b"), pa_rows=("srv-a",)),
        "opencti:2": _issue("2", assets=("srv-c",), pa_rows=("srv-c",)),
    }
    return view_of.ViewerView(issues=issues, open_ids=set(issues), signals={f"signal:{signal}"})


def test_a_signal_only_viewer_reads_null_with_a_reason_never_zero() -> None:
    case = _case()
    signal = str(uuid.uuid4())
    rows = _members(case, signal)
    own = view_of.summary(case, rows, view_of.ViewerView(signals={f"signal:{signal}"}))
    assert own is not None
    for key in view_of.DETAIL_AGGREGATES:
        assert own[key] is None, key
    assert own["aggregates_restricted"] is True
    assert own["aggregates_reason"] == view_of.AGGREGATES_RESTRICTED_REASON
    assert own["members_without_details"] == 2
    # Nothing of the members leaks: no name, id or asset of a member this viewer cannot read.
    rendered = view_of.hydrate(case, rows, [], view_of.ViewerView(signals={f"signal:{signal}"}))
    assert rendered is not None
    assert "srv-a" not in str(rendered) and "CVE-2026-1" not in str(rendered)

    admin = view_of.summary(case, rows, _admin_view(signal))
    assert admin is not None
    assert admin["primary_asset_count"] == 2 and admin["asset_count"] == 3 and admin["kev_count"] == 2
    assert admin["aggregates_restricted"] is False and admin["aggregates_reason"] is None
    assert admin["members_without_details"] == 0


def test_an_unavailable_member_restricts_the_aggregates_too() -> None:
    case = _case()
    signal = str(uuid.uuid4())
    rows = _members(case, signal)
    view = _admin_view(signal)
    view.issues.pop("opencti:2")
    view.unavailable.add("opencti:2")
    row = view_of.summary(case, rows, view)
    assert row is not None
    assert row["primary_asset_count"] is None and row["aggregates_restricted"] is True
    assert row["members_without_details"] == 1


def test_a_fully_read_case_with_no_primary_asset_is_a_real_zero() -> None:
    case = _case()
    rows = [CtemCaseIssue(case_id=case.id, source="opencti", external_id="1", open=True)]
    view = view_of.ViewerView(issues={"opencti:1": _issue("1", assets=("srv-a",), pa_rows=())}, open_ids={"opencti:1"})
    row = view_of.summary(case, rows, view)
    assert row is not None
    assert row["primary_asset_count"] == 0 and row["asset_count"] == 1
    assert row["aggregates_restricted"] is False and row["members_without_details"] == 0


def test_a_signal_only_case_with_no_product_member_is_not_restricted() -> None:
    """A case made of signals alone has no issue details for anyone: nothing is hidden from this viewer."""
    case = _case()
    signal = str(uuid.uuid4())
    rows = [CtemCaseIssue(case_id=case.id, source="signal", external_id=signal, open=True)]
    row = view_of.summary(case, rows, view_of.ViewerView(signals={f"signal:{signal}"}))
    assert row is not None
    assert row["aggregates_restricted"] is False and row["primary_asset_count"] == 0


def test_hidden_cases_counts_the_open_cases_the_viewer_sees_none_of() -> None:
    signal = str(uuid.uuid4())
    seen = _case()
    hidden_open = [_case() for _ in range(3)]
    hidden_closed = _case(state="resolved")
    cases = [(seen, _members(seen, signal))] + [
        (c, _members(c, str(uuid.uuid4()))) for c in [*hidden_open, hidden_closed]
    ]
    view = view_of.ViewerView(signals={f"signal:{signal}"})
    listed, _ = view_of.list_for_viewer(cases, view)
    assert len(listed) == 1
    assert view_of.list_visibility(cases, view)["hidden_cases"] == 3
    # Issues opencti:1 and opencti:2 are members of every case here: a reader of them sees them all.
    assert view_of.list_visibility(cases, _admin_view(signal))["hidden_cases"] == 0
    everyone = view_of.ViewerView(
        signals={f"signal:{s.external_id}" for _, m in cases for s in m if s.source == "signal"}
    )
    assert view_of.list_visibility(cases, everyone)["hidden_cases"] == 0


def test_list_row_and_case_read_agree_on_the_asset_count_for_one_viewer() -> None:
    """#1704 on the single read: same asset_count, source and restricted marker as the list row."""
    case = _case()
    signal = str(uuid.uuid4())
    rows = _members(case, signal)
    for view in (view_of.ViewerView(signals={f"signal:{signal}"}), _admin_view(signal)):
        listed, _ = view_of.list_for_viewer([(case, rows)], view)
        row = version_history.with_asset_count(listed[0], [])
        rendered = version_history.with_asset_count(view_of.hydrate(case, rows, [], view) or {}, [])
        for key in ("asset_count", "asset_count_source", "aggregates_restricted", "primary_asset_count"):
            assert row[key] == rendered[key], key
    own = version_history.with_asset_count(
        view_of.summary(case, rows, view_of.ViewerView(signals={f"signal:{signal}"})) or {}, []
    )
    assert own["asset_count"] is None and own["asset_count_source"] == "unknown"
    assert own["asset_count_reason"] == version_history.NO_COUNT_RESTRICTED
