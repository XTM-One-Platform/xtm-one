"""CTEM-11 (#894): the version a case must reach, and members outside their range.

dev-docs/ctem-cases.md section 4.2bis on fixtures: versions compare per part,
ranges come from NVD 2.0 configurations (or OpenCTI once it carries them, or a
course of action), the group keys on the smallest version that fixes every
member, and nothing changes when the profile declares no version.
"""

from __future__ import annotations

import pytest

from app.services import ctem_cases as cc
from app.services import ctem_cases_source as source
from app.services import ctem_fixed_version as fv
from app.services.organization_context import TechStackEntry

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("1.25.3", "1.25.5", -1),
        ("1.30.10", "1.30.7", 1),
        ("1.2", "1.2.0", 0),
        ("v2.0.1", "2.0.1", 0),
        ("1.0rc1", "1.0", -1),
        ("1.0-beta2", "1.0-rc1", -1),
        ("1:1.0", "2.0", 1),
        ("1.1.1w", "1.1.1", 1),
    ],
)
def test_versions_compare_per_part(left, right, expected):
    assert fv.compare_versions(left, right) == expected


@pytest.mark.parametrize("value", ["", "*", "-", "latest", None])
def test_a_value_without_a_number_is_not_a_version(value):
    assert fv.parse_version(value) is None
    assert fv.compare_versions(value or "", "1.0") is None


def test_a_range_contains_with_its_inclusive_flags():
    rng = fv.AffectedRange(start="1.25.0", start_inclusive=True, end="1.25.5", end_inclusive=False, fixed="1.25.5")
    assert rng.contains("1.25.0") is True
    assert rng.contains("1.25.3") is True
    assert rng.contains("1.25.5") is False
    assert rng.contains("1.24.9") is False
    assert rng.contains("unknown") is None


# ---------------------------------------------------------------------------
# NVD 2.0 configurations (the shape enrichment providers serve)
# ---------------------------------------------------------------------------

NVD2 = {
    "id": "CVE-2026-123633",
    "configurations": [
        {
            "nodes": [
                {
                    "cpeMatch": [
                        {
                            "vulnerable": True,
                            "criteria": "cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*",
                            "versionStartIncluding": "1.19.7",
                            "versionEndExcluding": "1.30.7",
                        },
                        {
                            "vulnerable": True,
                            "criteria": "cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*",
                            "versionStartIncluding": "1.31.0",
                            "versionEndExcluding": "1.31.9",
                        },
                        {"vulnerable": True, "criteria": "cpe:2.3:a:f5:nginx:1.18.0:*:*:*:*:*:*:*"},
                        {"vulnerable": False, "criteria": "cpe:2.3:o:linux:linux_kernel:-:*:*:*:*:*:*:*"},
                        {
                            "vulnerable": True,
                            "criteria": "cpe:2.3:a:other:proxy:*:*:*:*:*:*:*:*",
                            "versionEndExcluding": "9.9",
                        },
                    ]
                }
            ]
        }
    ],
}


def test_nvd2_ranges_keep_the_product_only():
    ranges = fv.ranges_from_nvd2(NVD2, vendor="F5", product="NGINX")
    assert [r.fixed for r in ranges] == ["1.30.7", "1.31.9", None]
    assert ranges[2].exact == "1.18.0"


def test_nvd2_ranges_refuse_another_vendor():
    assert fv.ranges_from_nvd2(NVD2, vendor="Apache", product="nginx") == []


# ---------------------------------------------------------------------------
# Resolver: sources in order, "not affected", "unknown"
# ---------------------------------------------------------------------------


def test_the_provider_range_holding_the_installed_version_gives_its_fix():
    ranges = fv.ranges_from_nvd2(NVD2, vendor="f5", product="nginx")
    assert fv.resolve(installed="1.27.1", provider=ranges) == fv.Resolution("1.30.7", fv.SOURCE_PROVIDER)
    assert fv.resolve(installed="1.31.2", provider=ranges).fixed_version == "1.31.9"


def test_without_an_installed_version_the_smallest_fix_is_taken():
    ranges = fv.ranges_from_nvd2(NVD2, vendor="f5", product="nginx")
    assert fv.resolve(installed=None, provider=ranges).fixed_version == "1.30.7"


def test_an_installed_version_outside_every_range_is_not_affected():
    ranges = fv.ranges_from_nvd2(NVD2, vendor="f5", product="nginx")
    resolution = fv.resolve(installed="1.30.7", provider=ranges)
    assert resolution.fixed_version is None
    assert resolution.not_affected == "not affected: version 1.30.7 is outside the affected ranges"


def test_opencti_ranges_win_then_a_course_of_action_then_the_provider():
    octi = [fv.AffectedRange(end="1.30.8", fixed="1.30.8")]
    provider = [fv.AffectedRange(end="1.30.7", fixed="1.30.7")]
    assert fv.resolve(installed="1.27.1", opencti=octi, course_of_action_version="1.31.0", provider=provider) == (
        fv.Resolution("1.30.8", fv.SOURCE_OPENCTI)
    )
    assert fv.resolve(installed="1.27.1", course_of_action_version="1.31.0", provider=provider) == (
        fv.Resolution("1.31.0", fv.SOURCE_COURSE_OF_ACTION)
    )


def test_nothing_known_is_unknown_never_guessed():
    assert fv.resolve(installed="1.27.1") == fv.Resolution()


def test_the_opencti_hook_answers_nothing_until_opencti_carries_ranges(monkeypatch):
    assert fv.opencti_ranges({"id": "v1", "name": "CVE-1"}) == []
    # Once it answers, the reader uses it before any other source (a stub stands in for CTEM-09).
    monkeypatch.setattr(fv, "opencti_ranges", lambda vuln: [fv.AffectedRange(end="9.0.98", fixed="9.0.98")])
    issues = source.opencti_issues(SOFTWARE, _stack("9.0.90"), RELATIONS, [])
    assert {i.fixed_version for i in issues} == {"9.0.98"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Upgrade to 9.0.98", "9.0.98"),
        ("Upgrade Apache Tomcat to version 9.0.98", "9.0.98"),
        ("Update NGINX to v1.30.7", "1.30.7"),
        ("Disable AJP", None),
        ("Upgrade to the latest release", None),
        (None, None),
    ],
)
def test_a_course_of_action_names_its_version_only_when_it_is_a_plain_upgrade(name, expected):
    assert fv.version_from_course_of_action(name) == expected


def test_the_smallest_version_fixing_every_member_is_the_greatest_fix():
    assert fv.smallest_fixing_every(["1.30.7", "1.30.10", "1.30.9"]) == "1.30.10"
    assert fv.smallest_fixing_every(["x"]) is None


# ---------------------------------------------------------------------------
# Grouping with a declared version (section 4.2bis)
# ---------------------------------------------------------------------------


def _cti(ext, fixed, *, installed="1.27.1", not_affected=None):
    return cc.SourceIssue(
        source="opencti",
        external_id=ext,
        name=f"CVE-2026-{ext}",
        cve=f"CVE-2026-{ext}",
        product="nginx",
        vendor="F5",
        fixed_version=fixed,
        installed_version=installed,
        not_affected=not_affected,
    )


def test_one_product_with_a_declared_version_is_one_case_on_the_smallest_common_fix():
    grouping = cc.group_issues([_cti("1", "1.30.7"), _cti("2", "1.30.9"), _cti("3", None)])
    assert sorted(grouping.groups) == ["upgrade:f5/nginx@1.30.9", "upgrade:f5/nginx@latest"]
    assert grouping.remediations["upgrade:f5/nginx@1.30.9"].title == "Upgrade F5 nginx to 1.30.9"
    assert len(grouping.groups["upgrade:f5/nginx@1.30.9"]) == 2


def test_without_a_declared_version_two_fixes_stay_two_cases():
    grouping = cc.group_issues([_cti("1", "1.30.7", installed=None), _cti("2", "1.30.9", installed=None)])
    assert sorted(grouping.groups) == ["upgrade:f5/nginx@1.30.7", "upgrade:f5/nginx@1.30.9"]


def test_a_member_outside_its_range_opens_no_case_and_is_listed_with_the_reason():
    out = _cti("2", None, not_affected="not affected: version 1.30.7 is outside the affected ranges")
    grouping = cc.group_issues([_cti("1", "1.31.9"), out])
    assert list(grouping.groups) == ["upgrade:f5/nginx@1.31.9"]
    assert grouping.not_affected == [out]
    assert grouping.no_known_fix == []


def test_a_member_turning_not_affected_is_resolved_in_its_case():
    case = cc.CaseSnapshot(
        id="c1", keys=["upgrade:f5/nginx@latest"], state="needs_attention", members={"opencti:9": True}
    )
    out = _cti("9", None, not_affected="not affected: version 1.30.7 is outside the affected ranges")
    (plan,) = cc.plan_recompute([case], [out], closed=["opencti:9"])
    assert plan.resolve == ["opencti:9"]
    assert plan.new_state == "resolved"


# ---------------------------------------------------------------------------
# The reader: stack version in, fixed version out
# ---------------------------------------------------------------------------

SOFTWARE = [{"id": "sw-1", "name": "tomcat", "vendor": "apache", "cpe": ""}]
RELATIONS = [
    {"from": {"id": "sw-1"}, "to": {"id": "v1", "name": "CVE-2026-1"}},
    {"from": {"id": "sw-1"}, "to": {"id": "v2", "name": "CVE-2026-2"}},
]


def _stack(version=""):
    return [{"product": "Tomcat", "vendor": "Apache", "cpe": "", "version": version, "label": "Apache Tomcat"}]


def test_the_reader_fills_the_fixed_version_from_provider_ranges():
    ranges = {
        "CVE-2026-1|apache tomcat": [fv.AffectedRange(start="9.0.0", end="9.0.98", fixed="9.0.98")],
        "CVE-2026-2|apache tomcat": [fv.AffectedRange(start="9.0.0", end="9.0.99", fixed="9.0.99")],
    }
    issues = source.opencti_issues(SOFTWARE, _stack("9.0.90"), RELATIONS, [], ranges)
    assert {i.external_id: i.fixed_version for i in issues} == {"v1": "9.0.98", "v2": "9.0.99"}
    grouping = cc.group_issues(issues)
    assert list(grouping.groups) == ["upgrade:apache/tomcat@9.0.99"]


def test_the_reader_marks_a_member_outside_its_range():
    ranges = {"CVE-2026-1|apache tomcat": [fv.AffectedRange(start="9.0.0", end="9.0.98", fixed="9.0.98")]}
    issues = source.opencti_issues(SOFTWARE, _stack("10.1.0"), RELATIONS[:1], [], ranges)
    assert issues[0].not_affected and issues[0].not_affected.startswith("not affected: version 10.1.0")


def test_the_reader_keeps_latest_when_no_version_is_known():
    issues = source.opencti_issues(SOFTWARE, _stack(), RELATIONS[:1], [])
    assert issues[0].fixed_version is None and issues[0].not_affected is None
    assert list(cc.group_issues(issues).groups) == ["upgrade:apache/tomcat@latest"]


def test_a_stack_without_version_keys_still_reads():
    stack = [{"product": "Tomcat", "vendor": "Apache", "cpe": "", "label": "Apache Tomcat"}]
    issues = source.opencti_issues(SOFTWARE, stack, RELATIONS[:1], [])
    assert issues[0].installed_version is None


def test_a_course_of_action_that_is_an_upgrade_carries_its_version():
    remediations = [{"from": {"standard_id": "course-of-action--1", "name": "Upgrade to 9.0.98"}, "to": {"id": "v1"}}]
    issues = source.opencti_issues(SOFTWARE, _stack(), RELATIONS[:1], remediations)
    assert issues[0].fixed_version == "9.0.98"
    remediation = cc.remediation_of(issues[0])
    assert remediation is not None and remediation.kind == "course_of_action" and remediation.fixed_version == "9.0.98"


def test_the_tech_stack_entry_takes_an_optional_version():
    assert TechStackEntry(product="nginx").version == ""
    assert TechStackEntry(product="nginx", version="1.27.1").version == "1.27.1"
