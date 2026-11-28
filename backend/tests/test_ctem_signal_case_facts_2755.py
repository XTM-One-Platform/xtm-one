"""A case opened from a tool-recorded software signal keeps its CVE, product and running version (#2755).

A ``new_vulnerability`` signal with ``subject_kind: software``, a composite subject
reference and evidence ``{cve, kev, running_version, assets, software}`` opened a case
with empty CVE rollups beside a KEV count of 1, no product label, a key built from the
raw reference and "no running version reported". Each of the four causes is pinned here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from app.models.ctem_case import CtemCaseIssue
from app.models.ctem_signal import CtemSignal
from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules
from app.services import ctem_cases_source as source
from app.services import ctem_cases_view as view_of
from app.services import ctem_version_match as version_match
from app.services.builtin_tools.ctem_cases import _cve_rollups

NOW = datetime.now(UTC)
CVE = "CVE-2024-9680"
REF = f"host-1:thunderbird:115.10.1:{CVE}"
EVIDENCE = {
    "cve": CVE,
    "kev": True,
    "running_version": "115.10.1",
    "assets": ["host-1"],
    "software": "Mozilla Thunderbird",
}
KEV_VULNS = [{"id": "v1", "name": CVE, "x_opencti_cvss_base_score": 9.8, "x_opencti_cisa_kev": True}]


def _signal(**kw: object) -> CtemSignal:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "trigger_path": "new_vulnerability",
        "subject_kind": "software",
        "subject_ref": REF,
        "subject_label": REF,
        "evidence": dict(EVIDENCE),
    }
    base.update(kw)
    return cast("CtemSignal", SimpleNamespace(**base))


def _member(**kw: object) -> rules.SourceIssue:
    base: dict[str, object] = {
        "source": "signal",
        "external_id": "s1",
        "name": REF,
        "assets": ("host-1",),
        "first_seen_at": NOW,
        "installed_version": source.signal_running_version(EVIDENCE),
    }
    base.update(kw)
    return rules.SourceIssue(**base)  # type: ignore[arg-type]


def test_cve_rollups_of_a_signal_only_case_name_its_kev_cve() -> None:
    (enriched,) = source.enrich_signal_issues([_member()], {"signal:s1": [CVE]}, KEV_VULNS)
    row = CtemCaseIssue(case_id=uuid.uuid4(), source="signal", external_id="s1", open=True)
    shown = view_of._visible_issue(row, enriched, {"name": REF})
    assert shown["cve"] == CVE and shown["kev"] is True and shown["cvss"] == 9.8
    rollups = _cve_rollups({}, [shown])
    assert rollups["cve_ids"] == [CVE]
    assert rollups["kev_cve_ids"] == [CVE]
    assert rollups["lead_cve"] == CVE


def test_cve_rollups_count_every_kev_cve_a_signal_names() -> None:
    vulns = [*KEV_VULNS, {"id": "v2", "name": "CVE-2024-9681", "x_opencti_cisa_kev": True}]
    (enriched,) = source.enrich_signal_issues([_member()], {"signal:s1": [CVE, "CVE-2024-9681"]}, vulns)
    row = CtemCaseIssue(case_id=uuid.uuid4(), source="signal", external_id="s1", open=True)
    rollups = _cve_rollups({}, [view_of._visible_issue(row, enriched)])
    assert rollups["kev_cve_ids"] == [CVE, "CVE-2024-9681"]
    assert rollups["cve_ids"] == [CVE, "CVE-2024-9681"]


def test_a_signal_row_without_a_cve_carries_no_cve_fields() -> None:
    row = CtemCaseIssue(case_id=uuid.uuid4(), source="signal", external_id="s1", open=True)
    shown = view_of._visible_issue(row, _member(installed_version=None))
    assert "cve" not in shown and "kev" not in shown


def test_product_label_falls_back_to_the_evidence_software() -> None:
    assert loop._product_label(_signal()) == "Mozilla Thunderbird"


def test_product_label_of_a_reader_fix_still_wins() -> None:
    evidence = {**EVIDENCE, "case_fix": {"product_label": "Thunderbird ESR"}}
    assert loop._product_label(_signal(evidence=evidence)) == "Thunderbird ESR"


def test_product_label_is_never_a_composite_reference() -> None:
    assert loop._product_label(_signal(evidence={"cve": CVE})) is None
    assert loop._product_label(_signal(subject_ref="NGINX", subject_label="NGINX", evidence={})) == "NGINX"


def test_fix_of_a_composite_software_ref_keys_on_the_evidence_product() -> None:
    fix = loop.fix_of(_signal())
    assert fix.kind == "upgrade"
    assert fix.key == "upgrade:-/mozilla thunderbird@latest"
    assert fix.title == "Upgrade Mozilla Thunderbird to a fixed version"
    # The key an open case opened before #2755 holds is still joined.
    assert fix.aliases == (f"upgrade:-/{REF.lower()}@latest",)
    # Another spelling of the same product's reference lands on the same case.
    other = loop.fix_of(_signal(subject_ref="host-2:thunderbird:115.9:CVE-2024-9680"))
    assert other.key == fix.key


def test_fix_of_a_software_signal_naming_its_fixed_version_keys_on_it() -> None:
    fix = loop.fix_of(_signal(evidence={**EVIDENCE, "fixed_version": "115.16.0"}))
    assert fix.key == "upgrade:-/mozilla thunderbird@115.16.0"
    assert fix.fixed_version == "115.16.0" and fix.title == "Upgrade Mozilla Thunderbird to 115.16.0"


def test_fix_of_a_plain_software_subject_is_unchanged() -> None:
    fix = loop.fix_of(_signal(subject_ref="NGINX", subject_label="NGINX", evidence={}))
    assert fix.key == "upgrade:-/nginx@latest" and fix.aliases == ()
    assert fix.title == "Upgrade NGINX to a fixed version"


def test_signal_running_version_reads_the_evidence() -> None:
    assert source.signal_running_version(EVIDENCE) == "115.10.1"
    assert source.signal_running_version({"running_version": ["1.2", "1.3", "1.2"]}) == "1.2, 1.3"
    assert source.signal_running_version({}) is None
    assert source.signal_running_version(None) is None


def test_unverified_reason_judges_a_signal_on_its_reported_running_version() -> None:
    (enriched,) = source.enrich_signal_issues([_member()], {"signal:s1": [CVE]}, KEV_VULNS)
    assert version_match.unverified_reason([enriched]) is None
    (blind,) = source.enrich_signal_issues([_member(installed_version=None)], {"signal:s1": [CVE]}, KEV_VULNS)
    assert version_match.unverified_reason([blind]) == version_match.UNVERIFIED_NOT_REPORTED
