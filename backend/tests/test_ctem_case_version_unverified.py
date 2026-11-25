"""A version-blind case is kept and marked "to validate", never discarded (#2004).

A CVE whose running versions OpenCTI cannot place stays in its case with its
priority and SLA (KEV or CVSS still set P1); the case carries
``version_unverified`` with its reason, cleared once OpenCTI places every
running version.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.ctem_case import CtemCase, CtemCaseIssue
from app.services import ctem_cases as rules
from app.services import ctem_cases_view as view_of
from app.services import ctem_version_match as version_match

NOW = datetime.now(UTC)


def _issue(external_id: str, outcomes: tuple[tuple[str, str], ...] = (), *, kev: bool = False) -> rules.SourceIssue:
    return rules.SourceIssue(
        source="opencti",
        external_id=external_id,
        name=f"CVE-2016-{external_id}",
        cve=f"CVE-2016-{external_id}",
        product="iOS",
        vendor="Apple",
        cvss=9.8 if kev else 6.1,
        kev=kev,
        assets=("phone-1",),
        first_seen_at=NOW,
        installed_version=outcomes[0][0] if len(outcomes) == 1 else None,
        version_outcomes=outcomes,
    )


def _case(severity: str | None = None) -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Upgrade Apple iOS",
        remediation_kind="upgrade",
        keys=["upgrade:apple/ios@latest"],
        pinned_issues=[],
        state="new",
        state_since=NOW,
        severity=severity,
    )


def _summary(issues: list[rules.SourceIssue], severity: str | None = None) -> dict:
    case = _case(severity)
    rows = [CtemCaseIssue(case_id=case.id, source="opencti", external_id=i.external_id, open=True) for i in issues]
    view = view_of.ViewerView(issues={i.member_id: i for i in issues}, open_ids={i.member_id for i in issues})
    row = view_of.summary(case, rows, view)
    assert row is not None
    return row


def test_all_unknown_versions_mark_the_case_to_validate() -> None:
    reason = version_match.unverified_reason([_issue("1", (("17.1", "unknown"),)), _issue("2", (("17.2", "unknown"),))])
    assert reason == f"{version_match.UNVERIFIED_NOT_KNOWN} 17.1, 17.2"


def test_mixed_outcomes_still_mark_the_case_with_the_unknown_versions_only() -> None:
    reason = version_match.unverified_reason([_issue("1", (("17.1", "affected"), ("17.2", "unknown")))])
    assert reason == f"{version_match.UNVERIFIED_NOT_KNOWN} 17.2"


def test_every_version_placed_by_opencti_clears_the_marker() -> None:
    issues = [_issue("1", (("17.1", "affected"),)), _issue("2", (("17.1", "affected"), ("16.0", "not_affected")))]
    assert version_match.unverified_reason(issues) is None


def test_no_running_version_reported_marks_the_case() -> None:
    assert version_match.unverified_reason([_issue("1")]) == version_match.UNVERIFIED_NOT_REPORTED


def test_openaev_findings_never_mark_the_case() -> None:
    finding = rules.SourceIssue(source="openaev", external_id="f1", name="CVE-2016-1", cve="CVE-2016-1")
    assert version_match.unverified_reason([finding]) is None


def test_a_p1_kev_case_with_unknown_versions_stays_p1_keeps_every_member_and_is_marked() -> None:
    issues = [_issue("1", (("17.1", "unknown"),), kev=True), _issue("2", (("17.1", "unknown"),))]
    row = _summary(issues)
    assert row["priority"] == "critical"
    assert row["open_issue_count"] == 2 and row["issue_count"] == 2
    assert row["version_unverified"] is True
    assert row["version_unverified_reason"] == f"{version_match.UNVERIFIED_NOT_KNOWN} 17.1"
    # A loop severity keeps its priority too.
    assert _summary(issues, severity="P1")["priority"] == "critical"


def test_a_placed_case_carries_no_marker() -> None:
    row = _summary([_issue("1", (("17.1", "affected"),), kev=True)])
    assert row["version_unverified"] is False and row["version_unverified_reason"] is None
