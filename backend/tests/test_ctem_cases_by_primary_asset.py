"""The cases on one primary asset (#1980 S2, the primary asset page): ``GET /ctem-cases?primary_asset=``.

A case is listed when an issue the viewer sees reaches that primary asset, case and
spaces folded; an issue the viewer cannot see never reveals a case.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.ctem_case import CtemCase, CtemCaseIssue
from app.services import ctem_cases as rules
from app.services import ctem_cases_view as view_of

NOW = datetime.now(UTC)


def _case(title: str) -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title=title,
        remediation_kind="upgrade",
        keys=[f"upgrade:{title}"],
        pinned_issues=[],
        state="new",
        state_since=NOW,
    )


PAY = rules.SourceIssue(source="signal", external_id="s-1", name="CVE-2026-1", primary_assets=("Payments Platform",))
HR = rules.SourceIssue(source="signal", external_id="s-2", name="CVE-2026-2", primary_assets=("HR records",))
HIDDEN = rules.SourceIssue(source="signal", external_id="s-9", name="CVE-2026-9", primary_assets=("payments platform",))


def _rows(case: CtemCase, *issues: rules.SourceIssue) -> list[CtemCaseIssue]:
    return [CtemCaseIssue(case_id=case.id, source=i.source, external_id=i.external_id, open=True) for i in issues]


def _view(*issues: rules.SourceIssue) -> view_of.ViewerView:
    return view_of.ViewerView(issues={i.member_id: i for i in issues}, open_ids={i.member_id for i in issues})


def test_a_case_whose_seen_issue_reaches_the_primary_asset_is_listed_whatever_the_case_and_spaces() -> None:
    pay, hr = _case("nginx"), _case("postgres")
    rows, counts = view_of.list_for_viewer(
        [(pay, _rows(pay, PAY)), (hr, _rows(hr, HR))], _view(PAY, HR), primary_asset="  payments   PLATFORM "
    )
    assert [r["id"] for r in rows] == [str(pay.id)]
    assert counts == {"new": 1}


def test_an_issue_the_viewer_cannot_see_never_lists_the_case() -> None:
    case = _case("tomcat")
    assert view_of.reaches_primary_asset(_rows(case, HIDDEN), _view(PAY), "payments platform") is False
    rows, _ = view_of.list_for_viewer([(case, _rows(case, HIDDEN))], _view(PAY), primary_asset="payments platform")
    assert rows == []
