"""The cases on one asset (#1178, CTEM > Inventory asset page): ``GET /ctem-cases?asset=``.

A case is listed for an asset when an issue the viewer sees names that asset,
case and spaces folded; an issue the viewer cannot see never reveals a case.
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


WEB = rules.SourceIssue(source="openaev", external_id="f-1", name="CVE-2026-1", assets=("Web-01",))
DB = rules.SourceIssue(source="openaev", external_id="f-2", name="CVE-2026-2", assets=("db-01",))
HIDDEN = rules.SourceIssue(source="opencti", external_id="v-9", name="CVE-2026-9", assets=("web-01",))


def _rows(case: CtemCase, *issues: rules.SourceIssue) -> list[CtemCaseIssue]:
    return [CtemCaseIssue(case_id=case.id, source=i.source, external_id=i.external_id, open=True) for i in issues]


def _view(*issues: rules.SourceIssue) -> view_of.ViewerView:
    return view_of.ViewerView(issues={i.member_id: i for i in issues}, open_ids={i.member_id for i in issues})


def test_a_case_whose_seen_issue_names_the_asset_is_listed_whatever_the_case_and_spaces() -> None:
    web, db = _case("nginx"), _case("postgres")
    view = _view(WEB, DB)
    rows, counts = view_of.list_for_viewer([(web, _rows(web, WEB)), (db, _rows(db, DB))], view, asset="  web-01 ")
    assert [r["id"] for r in rows] == [str(web.id)]
    assert counts == {"new": 1}


def test_an_issue_the_viewer_cannot_see_never_lists_the_case() -> None:
    case = _case("tomcat")
    assert view_of.names_asset(_rows(case, HIDDEN), _view(WEB), "web-01") is False
    rows, _ = view_of.list_for_viewer([(case, _rows(case, HIDDEN))], _view(WEB), asset="web-01")
    assert rows == []


def test_no_asset_filters_nothing() -> None:
    web, db = _case("nginx"), _case("postgres")
    rows, _ = view_of.list_for_viewer([(web, _rows(web, WEB)), (db, _rows(db, DB))], _view(WEB, DB), asset="")
    assert len(rows) == 2
