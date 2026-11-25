"""A case issue opened from a signal is enriched from OpenCTI and unverified by default (#2041).

A ``new_vulnerability`` signal that names CVE ids takes OpenCTI's facts on them
(KEV, CVSS, EPSS, the fixed version a course of action names), as an OpenCTI
vulnerability issue does. With no version outcome it marks the case
``version_unverified`` (missing data is unverified, never verified), and a CVE
OpenCTI does not know keeps the member with its facts unknown, never discarded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.models.ctem_case import CtemCase, CtemCaseIssue
from app.services import ctem_cases as rules
from app.services import ctem_cases_source as source
from app.services import ctem_cases_view as view_of
from app.services import ctem_version_match as version_match
from app.services.ctem_scope_gate import signal_cve_ids

NOW = datetime.now(UTC)

KEV_VULNS = [
    {"id": "v1", "name": "CVE-2023-28432", "x_opencti_cvss_base_score": 7.5, "x_opencti_cisa_kev": True},
    {
        "id": "v2",
        "name": "CVE-2023-28434",
        "x_opencti_cvss_base_score": 8.8,
        "x_opencti_epss_score": 0.9,
        "x_opencti_cisa_kev": True,
    },
]


def _signal(external_id: str) -> rules.SourceIssue:
    return rules.SourceIssue(
        source="signal", external_id=external_id, name="minio", assets=("s3-1",), first_seen_at=NOW
    )


def _summary(issues: list[rules.SourceIssue]) -> dict[str, Any]:
    case = CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Upgrade minio",
        remediation_kind="upgrade",
        keys=["upgrade:minio/minio@latest"],
        pinned_issues=[],
        state="new",
        state_since=NOW,
    )
    rows = [CtemCaseIssue(case_id=case.id, source=i.source, external_id=i.external_id, open=True) for i in issues]
    view = view_of.ViewerView(issues={i.member_id: i for i in issues}, open_ids={i.member_id for i in issues})
    row = view_of.summary(case, rows, view)
    assert row is not None
    return row


def test_signal_cve_ids_reads_subject_label_and_evidence() -> None:
    ids = signal_cve_ids("minio", "cve-2023-28432 in minio", {"cves": ["CVE-2023-28434", "CVE-2023-28432"]})
    assert ids == ["CVE-2023-28432", "CVE-2023-28434"]
    assert signal_cve_ids("host-1", None, None) == []


def test_signal_only_case_with_kev_cves_known_to_opencti_takes_their_facts_and_is_unverified() -> None:
    signals = [_signal("s1"), _signal("s2")]
    cves = {"signal:s1": ["CVE-2023-28432"], "signal:s2": ["CVE-2023-28434"]}
    coa = [{"from": {"id": "c1", "name": "Upgrade to 2023-03-20"}, "to": {"id": "v2"}}]
    enriched = source.enrich_signal_issues(signals, cves, KEV_VULNS, coa)
    assert [i.cve for i in enriched] == ["CVE-2023-28432", "CVE-2023-28434"]
    assert all(i.kev for i in enriched)
    row = _summary(enriched)
    assert row["kev_count"] == 2
    assert row["max_cvss"] == 8.8
    assert row["max_epss"] == 0.9
    assert row["priority"] == "critical"
    assert row["version_unverified"] is True
    assert row["version_unverified_reason"] == version_match.UNVERIFIED_NOT_REPORTED
    assert row["open_issue_count"] == 2


def test_signal_whose_cve_opencti_does_not_know_is_kept_unverified_with_unknown_facts() -> None:
    enriched = source.enrich_signal_issues([_signal("s1")], {"signal:s1": ["CVE-2099-0001"]}, KEV_VULNS)
    (issue,) = enriched
    assert issue.cve == "CVE-2099-0001"
    assert issue.kev is False and issue.cvss is None and issue.epss is None and issue.fixed_version is None
    row = _summary(enriched)
    assert row["open_issue_count"] == 1 and row["issue_count"] == 1
    assert row["kev_count"] == 0 and row["max_cvss"] is None
    assert row["version_unverified"] is True


def test_signal_without_cve_is_left_as_it_is_and_does_not_mark_the_case() -> None:
    plain = _signal("s1")
    assert source.enrich_signal_issues([plain], {}, KEV_VULNS) == [plain]
    assert version_match.unverified_reason([plain]) is None


def test_signal_echoing_an_opencti_issue_is_judged_by_that_issue_and_counted_once() -> None:
    octi = rules.SourceIssue(
        source="opencti",
        external_id="v1",
        name="CVE-2023-28432",
        cve="CVE-2023-28432",
        cvss=7.5,
        kev=True,
        version_outcomes=(("7.2.20", "affected"),),
        installed_version="7.2.20",
    )
    (signal,) = source.enrich_signal_issues([_signal("s1")], {"signal:s1": ["CVE-2023-28432"]}, KEV_VULNS)
    assert version_match.unverified_reason([octi, signal]) is None
    row = _summary([octi, signal])
    assert row["kev_count"] == 1
    assert row["version_unverified"] is False


def test_opencti_issue_path_is_unchanged() -> None:
    octi = rules.SourceIssue(
        source="opencti", external_id="v1", name="CVE-2016-1", cve="CVE-2016-1", version_outcomes=(("17.1", "unknown"),)
    )
    assert version_match.unverified_reason([octi]) == f"{version_match.UNVERIFIED_NOT_KNOWN} 17.1"
