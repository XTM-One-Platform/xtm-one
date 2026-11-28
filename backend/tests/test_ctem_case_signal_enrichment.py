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
    # Unknown, never "not KEV" (#2225).
    assert issue.kev is None and issue.cvss is None and issue.epss is None and issue.fixed_version is None
    assert issue.unknown_cves == ("CVE-2099-0001",)
    row = _summary(enriched)
    assert row["open_issue_count"] == 1 and row["issue_count"] == 1
    assert row["kev_count"] is None and row["max_cvss"] is None
    assert row["members_facts_unread"] == 1 and row["facts_reason"] == view_of.FACTS_UNREAD_REASON
    assert row["version_unverified"] is True


def test_signal_read_without_opencti_has_unknown_kev_never_zero() -> None:
    """No OpenCTI answer for this reader (no credentials, OpenCTI failed): KEV unknown, not 0 (#2225)."""
    (issue,) = source.enrich_signal_issues([_signal("s1")], {"signal:s1": ["CVE-2023-28432"]}, [])
    assert issue.kev is None and issue.kev_cves == () and issue.unknown_cves == ("CVE-2023-28432",)
    row = _summary([issue])
    assert row["kev_count"] is None and row["max_cvss"] is None and row["max_epss"] is None
    assert row["members_facts_unread"] == 1 and row["facts_reason"] == view_of.FACTS_UNREAD_REASON
    # The scope restriction is a different reason: never mixed with the facts not read.
    assert row["aggregates_restricted"] is False


def test_signal_read_with_opencti_counts_its_kev_and_reads_facts_as_read() -> None:
    (issue,) = source.enrich_signal_issues([_signal("s1")], {"signal:s1": ["CVE-2023-28432"]}, KEV_VULNS)
    assert issue.kev is True and issue.unknown_cves == ()
    row = _summary([issue])
    assert row["kev_count"] == 1 and row["max_cvss"] == 7.5
    assert row["members_facts_unread"] == 0 and row["facts_reason"] is None


def test_a_known_non_kev_cve_reads_as_not_kev() -> None:
    vulns = [{"id": "v0", "name": "CVE-2023-00001", "x_opencti_cvss_base_score": 5.0}]
    (issue,) = source.enrich_signal_issues([_signal("s1")], {"signal:s1": ["CVE-2023-00001"]}, vulns)
    assert issue.kev is False and issue.unknown_cves == ()
    row = _summary([issue])
    assert row["kev_count"] == 0 and row["max_cvss"] == 5.0 and row["facts_reason"] is None


def test_a_kev_cve_beside_an_unread_one_keeps_the_member_kev_and_the_count_unknown() -> None:
    (issue,) = source.enrich_signal_issues(
        [_signal("s1")], {"signal:s1": ["CVE-2023-28432", "CVE-2099-0001"]}, KEV_VULNS
    )
    assert issue.kev is True and issue.unknown_cves == ("CVE-2099-0001",)
    # The case's count would only be a lower bound: unknown, with its reason.
    assert _summary([issue])["kev_count"] is None


def test_a_member_cached_before_unknown_cves_still_reads() -> None:
    (issue,) = source.enrich_signal_issues([_signal("s1")], {"signal:s1": ["CVE-2023-28432"]}, [])
    data = view_of._issue_json(issue)
    data.pop("unknown_cves")
    assert view_of._issue_from_json(data).unknown_cves == ()
    assert view_of._issue_from_json(view_of._issue_json(issue)).unknown_cves == ("CVE-2023-28432",)


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


def _octi(cve: str, *, kev: bool) -> rules.SourceIssue:
    return rules.SourceIssue(source="opencti", external_id=cve, name=cve, cve=cve, kev=kev)


def test_multi_cve_signal_keeps_a_kev_status_that_comes_from_its_second_cve() -> None:
    vulns = [{"id": "v0", "name": "CVE-2023-00001"}, *KEV_VULNS]
    (signal,) = source.enrich_signal_issues([_signal("s1")], {"signal:s1": ["CVE-2023-00001", "CVE-2023-28434"]}, vulns)
    assert signal.cve == "CVE-2023-00001" and signal.kev_cves == ("CVE-2023-28434",)
    # Its first CVE is carried by a non-KEV product member: the KEV of its second CVE still counts.
    assert _summary([_octi("CVE-2023-00001", kev=False), signal])["kev_count"] == 1


def test_multi_cve_signal_does_not_double_count_a_later_cve_a_product_member_carries() -> None:
    (signal,) = source.enrich_signal_issues(
        [_signal("s1")], {"signal:s1": ["CVE-2023-28432", "CVE-2023-28434"]}, KEV_VULNS
    )
    assert signal.kev_cves == ("CVE-2023-28432", "CVE-2023-28434")
    # The product member carries the second CVE only: 2 KEV CVEs, each counted once.
    assert _summary([_octi("CVE-2023-28434", kev=True), signal])["kev_count"] == 2
    assert _summary([_octi("CVE-2023-28432", kev=True), _octi("CVE-2023-28434", kev=True), signal])["kev_count"] == 2


def test_two_signals_naming_the_same_kev_cve_count_it_once() -> None:
    enriched = source.enrich_signal_issues(
        [_signal("s1"), _signal("s2")], {"signal:s1": ["CVE-2023-28432"], "signal:s2": ["CVE-2023-28432"]}, KEV_VULNS
    )
    assert _summary(enriched)["kev_count"] == 1


def test_a_signal_row_carries_the_facts_its_case_counts_so_the_tables_agree_with_the_cards() -> None:
    """Sam, 28 Nov 2026: KEV 1 and Max CVSS 9.8 on the cards, the Issues row and Affected assets empty.

    The signal member's OpenCTI facts are counted by the case aggregates; its rendered row and the
    Affected assets rows built from it carry the same CVE, CVSS, EPSS and KEV.
    """
    from app.services.ctem_case_versions import affected_assets

    (issue,) = source.enrich_signal_issues([_signal("s1")], {"signal:s1": ["CVE-2023-28434"]}, KEV_VULNS)
    member = CtemCaseIssue(case_id=uuid.uuid4(), source="signal", external_id="s1", open=True)
    row = view_of._visible_issue(member, issue, {"name": "minio", "assets": []})
    assert (row["cve"], row["cvss"], row["epss"], row["kev"]) == ("CVE-2023-28434", 8.8, 0.9, True)
    assert row["assets"] == ["s3-1"]
    card = _summary([issue])
    assert card["kev_count"] == 1 and card["max_cvss"] == row["cvss"]
    (asset,) = affected_assets([row])["assets"]
    assert asset["kev"] is True and asset["max_cvss"] == 8.8


def test_a_signal_row_whose_facts_were_not_read_leaves_them_unknown() -> None:
    (issue,) = source.enrich_signal_issues([_signal("s1")], {"signal:s1": ["CVE-2023-28432"]}, [])
    member = CtemCaseIssue(case_id=uuid.uuid4(), source="signal", external_id="s1", open=True)
    row = view_of._visible_issue(member, issue, {"name": "minio"})
    # Unknown, never "not KEV" (#2225): the page draws an empty cell.
    assert row["kev"] is None and row["cvss"] is None and row["cve"] == "CVE-2023-28432"
    # A signal naming no CVE carries no fact at all.
    bare = view_of._visible_issue(member, _signal("s1"), {"name": "minio"})
    assert "kev" not in bare and "cvss" not in bare


def test_opencti_issue_path_is_unchanged() -> None:
    octi = rules.SourceIssue(
        source="opencti", external_id="v1", name="CVE-2016-1", cve="CVE-2016-1", version_outcomes=(("17.1", "unknown"),)
    )
    assert version_match.unverified_reason([octi]) == f"{version_match.UNVERIFIED_NOT_KNOWN} 17.1"
