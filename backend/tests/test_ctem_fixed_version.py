"""CTEM-11 (#894, #946): the version a case must reach, read from OpenCTI.

dev-docs/ctem-cases.md section 4.2bis on fixtures: versions compare per part,
the fixed version is the smallest Software that remediates the vulnerability
in OpenCTI (a course of action is the fallback), a declared version OpenCTI
relates no vulnerability to is not affected, the group keys on the smallest
version that fixes every member, and nothing changes without a declared version.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.services import ctem_cases as cc
from app.services import ctem_cases_source as source
from app.services import ctem_fixed_version as fv
from app.services.organization_context import SoftwareEntry

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


def test_the_smallest_fix_above_the_declared_version_is_taken():
    assert fv.smallest_fix_above(["9.0.99", "9.0.98", "8.5.1"], "9.0.90") == "9.0.98"
    assert fv.smallest_fix_above(["8.5.1"], "9.0.90") is None
    assert fv.smallest_fix_above(["9.0.99", "x", None], None) == "9.0.99"


def test_same_version_folds_trailing_zeros_and_reads_text():
    assert fv.same_version("1.2", "1.2.0")
    assert fv.same_version("Release-A", "release-a")
    assert not fv.same_version("1.2", "1.3")
    assert not fv.same_version("", "")


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


def test_no_range_engine_is_left_in_xtm_one():
    """#946: affected ranges are OpenCTI's knowledge; XTM One holds no range engine and no NVD read."""
    from app.integrations.providers import vulncheck

    for gone in ("AffectedRange", "ranges_from_nvd2", "opencti_ranges", "resolve"):
        assert not hasattr(fv, gone)
    assert not hasattr(source, "provider_ranges_for") and not hasattr(source, "_enrichment_key")
    assert not hasattr(vulncheck, "nvd2_records")


# ---------------------------------------------------------------------------
# Grouping with a declared version (section 4.2bis)
# ---------------------------------------------------------------------------


def _cti(
    ext: str, fixed: str | None, *, installed: str | None = "1.27.1", not_affected: str | None = None
) -> cc.SourceIssue:
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


def test_a_member_not_affected_opens_no_case_and_is_listed_with_the_reason():
    out = _cti("2", None, not_affected="not affected: version 1.30.7 has no 'has' relation to CVE-2026-2 in OpenCTI")
    grouping = cc.group_issues([_cti("1", "1.31.9"), out])
    assert list(grouping.groups) == ["upgrade:f5/nginx@1.31.9"]
    assert grouping.not_affected == [out]
    assert grouping.no_known_fix == []


def test_a_member_turning_not_affected_is_resolved_in_its_case():
    case = cc.CaseSnapshot(id="c1", keys=["upgrade:f5/nginx@latest"], state="new", members={"opencti:9": True})
    out = _cti("9", None, not_affected="not affected: version 1.30.7 has no 'has' relation to CVE-2026-9 in OpenCTI")
    (plan,) = cc.plan_recompute([case], [out], closed=["opencti:9"])
    assert plan.resolve == ["opencti:9"]
    assert plan.new_state == "resolved"


def test_a_moved_folded_target_stays_on_the_case_that_holds_the_members() -> None:
    case = cc.CaseSnapshot(
        id="c1",
        keys=["upgrade:f5/nginx@1.30.9"],
        state="new",
        members={"opencti:1": True, "opencti:2": True},
    )
    plans = cc.plan_recompute([case], [_cti("1", "1.30.7"), _cti("2", "1.30.9"), _cti("3", "1.31.2")])
    assert [p.case_id for p in plans] == ["c1"]
    (plan,) = plans
    assert plan.add == ["opencti:3"]
    assert plan.add_keys == ["upgrade:f5/nginx@1.31.2"]
    assert plan.remediation is not None and plan.remediation.fixed_version == "1.31.2"
    assert plan.resolve == [] and plan.new_state is None


def test_a_latest_case_takes_the_key_the_first_time_a_version_is_declared() -> None:
    case = cc.CaseSnapshot(id="c1", keys=["upgrade:f5/nginx@latest"], state="new", members={"opencti:1": True})
    (plan,) = cc.plan_recompute([case], [_cti("1", "1.30.7")])
    assert plan.case_id == "c1"
    assert plan.add_keys == ["upgrade:f5/nginx@1.30.7"]
    assert plan.add == []


def test_a_patched_top_member_moves_the_target_down_on_the_same_case() -> None:
    case = cc.CaseSnapshot(
        id="c1",
        keys=["upgrade:f5/nginx@1.31.2"],
        state="new",
        members={"opencti:1": True, "opencti:3": True},
    )
    (plan,) = cc.plan_recompute([case], [_cti("1", "1.30.7")], closed=["opencti:3"])
    assert plan.add_keys == ["upgrade:f5/nginx@1.30.7"]
    assert plan.resolve == ["opencti:3"]
    assert plan.new_state is None


def test_an_unowned_key_of_another_product_still_opens_a_case() -> None:
    case = cc.CaseSnapshot(id="c1", keys=["upgrade:apache/tomcat@9.0.98"], state="new", members={"opencti:1": True})
    plans = cc.plan_recompute([case], [_cti("1", "1.30.7")])
    new = [p for p in plans if p.case_id is None]
    assert len(new) == 1 and new[0].add == ["opencti:1"]
    assert next(p for p in plans if p.case_id == "c1").add_keys == []


# ---------------------------------------------------------------------------
# The reader: OpenCTI's Software version, has and remediates (#946)
# ---------------------------------------------------------------------------

SOFTWARE = [
    {"id": "sw-90", "name": "tomcat", "vendor": "apache", "version": "9.0.90", "cpe": ""},
    {"id": "sw-80", "name": "tomcat", "vendor": "apache", "version": "9.0.80", "cpe": ""},
]
RELATIONS = [
    {"from": {"id": "sw-90"}, "to": {"id": "v1", "name": "CVE-2026-1"}},
    {"from": {"id": "sw-90"}, "to": {"id": "v2", "name": "CVE-2026-2"}},
    {"from": {"id": "sw-80"}, "to": {"id": "v3", "name": "CVE-2026-3"}},
]
FIXES = [
    {"from": {"id": "sw-98", "name": "tomcat", "vendor": "apache", "version": "9.0.98"}, "to": {"id": "v1"}},
    {"from": {"id": "sw-99", "name": "tomcat", "vendor": "apache", "version": "9.0.99"}, "to": {"id": "v1"}},
    {"from": {"id": "sw-99", "name": "tomcat", "vendor": "apache", "version": "9.0.99"}, "to": {"id": "v2"}},
    # Another product remediating the same CVE is not this product's fix.
    {"from": {"id": "ng-1", "name": "nginx", "vendor": "f5", "version": "1.31.0"}, "to": {"id": "v2"}},
]


def _stack(version: str = "") -> list[dict[str, str]]:
    return [{"product": "Tomcat", "vendor": "Apache", "cpe": "", "version": version, "label": "Apache Tomcat"}]


def test_the_fixed_version_is_the_smallest_remediating_software_above_the_declared_one():
    issues = source.opencti_issues(SOFTWARE, _stack("9.0.90"), RELATIONS, [], FIXES)
    by_id = {i.external_id: i for i in issues}
    assert by_id["v1"].fixed_version == "9.0.98" and by_id["v2"].fixed_version == "9.0.99"
    assert list(cc.group_issues(issues).groups) == ["upgrade:apache/tomcat@9.0.99"]


def test_no_has_on_the_declared_version_is_not_affected():
    issues = source.opencti_issues(SOFTWARE, _stack("9.0.90"), RELATIONS, [], FIXES)
    v3 = next(i for i in issues if i.external_id == "v3")
    assert v3.not_affected == "not affected: version 9.0.90 has no 'has' relation to CVE-2026-3 in OpenCTI"
    assert v3.version_note is None


def test_a_declared_version_unknown_to_opencti_is_kept_and_named():
    issues = source.opencti_issues(SOFTWARE, _stack("10.1.0"), RELATIONS, [], FIXES)
    assert all(i.not_affected is None for i in issues)
    assert {i.version_note for i in issues if i.external_id == "v3"} == {
        "version not known to OpenCTI: 10.1.0; fixed version unknown"
    }


def test_a_course_of_action_is_the_fallback_and_nothing_known_is_unknown():
    remediations = [{"from": {"standard_id": "course-of-action--1", "name": "Upgrade to 9.0.95"}, "to": {"id": "v3"}}]
    issues = source.opencti_issues(SOFTWARE, _stack(), RELATIONS, remediations, [])
    by_id = {i.external_id: i for i in issues}
    assert by_id["v3"].fixed_version == "9.0.95" and by_id["v3"].version_note is None
    assert by_id["v1"].fixed_version is None and by_id["v1"].version_note == "fixed version unknown"
    remediation = cc.remediation_of(by_id["v3"])
    assert remediation is not None and remediation.kind == "course_of_action" and remediation.fixed_version == "9.0.95"


def test_the_case_view_carries_the_version_fields_of_the_viewers_own_read():
    from types import SimpleNamespace

    from app.services import ctem_cases_view as view_mod

    issue = next(
        i for i in source.opencti_issues(SOFTWARE, _stack("9.0.90"), RELATIONS, [], FIXES) if i.external_id == "v1"
    )
    row = SimpleNamespace(source="opencti", external_id="v1", open=True)
    shown = view_mod._visible_issue(row, issue)
    assert shown["installed_version"] == "9.0.90" and shown["fixed_version"] == "9.0.98"
    assert shown["fixed_by"] == "tomcat 9.0.98" and shown["course_of_action_name"] is None
    assert shown["not_affected"] is False and shown["declared_version_known"] is True
    # A member the viewer cannot read now carries none of it.
    assert "fixed_version" not in view_mod._visible_issue(row, None)


def test_a_remediating_software_wins_over_a_course_of_action():
    remediations = [{"from": {"standard_id": "course-of-action--1", "name": "Upgrade to 9.1.0"}, "to": {"id": "v1"}}]
    issues = source.opencti_issues(SOFTWARE, _stack("9.0.90"), RELATIONS[:1], remediations, FIXES)
    assert issues[0].fixed_version == "9.0.98"


def test_each_issue_names_where_its_fixed_version_came_from():
    # The case page shows the remediating Software, or the course of action, never a guess (#928).
    remediations = [{"from": {"standard_id": "course-of-action--1", "name": "Upgrade to 9.0.95"}, "to": {"id": "v3"}}]
    issues = source.opencti_issues(SOFTWARE, _stack("9.0.90"), RELATIONS, remediations, FIXES)
    by_id = {i.external_id: i for i in issues}
    assert by_id["v1"].fixed_by == "tomcat 9.0.98" and by_id["v2"].fixed_by == "tomcat 9.0.99"
    assert by_id["v1"].declared_version_known is True
    unknown = source.opencti_issues(SOFTWARE, _stack("10.1.0"), RELATIONS, remediations, [])
    v3 = next(i for i in unknown if i.external_id == "v3")
    assert v3.fixed_version == "9.0.95" and v3.fixed_by is None and v3.declared_version_known is False
    assert all(i.declared_version_known is None for i in source.opencti_issues(SOFTWARE, _stack(), RELATIONS, [], []))


def test_the_declared_version_matches_on_the_cpe_too():
    software = [
        {"id": "sw-c", "name": "Tomcat", "vendor": "Apache", "version": "", "cpe": "cpe:2.3:a:apache:tomcat:9.0.90"}
    ]
    stack = [dict(_stack("9.0.90")[0], cpe="cpe:2.3:a:apache:tomcat:9.0.90")]
    relations = [{"from": {"id": "sw-c"}, "to": {"id": "v1", "name": "CVE-2026-1"}}]
    (issue,) = source.opencti_issues(software, stack, relations, [], FIXES)
    assert issue.not_affected is None and issue.version_note is None and issue.fixed_version == "9.0.98"


def _multi(*versions: str) -> list[dict[str, object]]:
    return [dict(_stack()[0], versions=list(versions))]


def test_every_installed_version_gets_its_own_outcome_from_opencti() -> None:
    # #1783: several versions run; each is checked against OpenCTI's Software of that version.
    issues = source.opencti_issues(SOFTWARE, _multi("9.0.80", "9.0.90", "10.1.0"), RELATIONS, [], FIXES)
    by_id = {i.external_id: i for i in issues}
    assert [(f.version, f.outcome) for f in by_id["v1"].version_outcomes] == [
        ("9.0.80", cc.VERSION_NOT_AFFECTED),
        ("9.0.90", cc.VERSION_AFFECTED),
        ("10.1.0", cc.VERSION_UNKNOWN),
    ]
    assert by_id["v1"].installed_version is None and by_id["v1"].not_affected is None
    assert by_id["v1"].version_note == "version not known to OpenCTI: 10.1.0; fixed version unknown"
    assert by_id["v1"].declared_version_known is True


def test_an_issue_no_installed_version_holds_is_not_affected() -> None:
    issues = source.opencti_issues(SOFTWARE, _multi("9.0.80", "9.0.90"), RELATIONS, [], FIXES)
    by_id = {i.external_id: i for i in issues}
    # v3 is held by 9.0.80 only: affected; v1 and v2 by 9.0.90 only: affected too.
    assert all(i.not_affected is None for i in issues)
    assert {f.version: f.outcome for f in by_id["v3"].version_outcomes} == {
        "9.0.80": "affected",
        "9.0.90": "not_affected",
    }
    only_80 = source.opencti_issues(SOFTWARE, _multi("9.0.80"), RELATIONS, [], FIXES)
    assert {i.external_id for i in only_80 if i.not_affected} == {"v1", "v2"}


def test_the_fix_is_searched_above_the_highest_exposed_version() -> None:
    issues = source.opencti_issues(SOFTWARE, _multi("9.0.80", "9.0.90"), RELATIONS, [], FIXES)
    v1 = next(i for i in issues if i.external_id == "v1")
    assert v1.fixed_version == "9.0.98"
    # The upgrade members with a fix fold on one target; v3 (exposed on 9.0.80 only) has no fix in OpenCTI.
    assert set(cc.group_issues(issues).groups) == {"upgrade:apache/tomcat@9.0.99", "upgrade:apache/tomcat@latest"}


def test_the_case_view_lists_each_version_outcome_and_reads_it_back() -> None:
    from types import SimpleNamespace

    from app.services import ctem_cases_view as view_mod
    from app.services import ctem_version_match as matching

    issue = next(
        i
        for i in source.opencti_issues(SOFTWARE, _multi("9.0.80", "9.0.90"), RELATIONS, [], FIXES)
        if i.external_id == "v1"
    )
    assert view_mod._issue_from_json(view_mod._issue_json(issue)) == issue
    row = SimpleNamespace(source="opencti", external_id="v1", open=True)
    shown = view_mod._visible_issue(row, issue)  # type: ignore[arg-type]
    assert [(r["version"], r["outcome"], r["source"]) for r in shown["version_outcomes"]] == [
        ("9.0.80", "not_affected", None),
        ("9.0.90", "affected", None),
    ]
    affected = matching.affected_from_outcomes([shown])
    assert affected == "9.0.90 per OpenCTI; not affected: 9.0.80"
    match = matching.version_match(
        {"versions": [{"version": "9.0.90", "asset_count": 2}]},
        product_label="Apache Tomcat",
        fixed_version="9.0.98",
        affected=affected,
    )
    assert match["blind"] is False and "affected range unknown" not in matching.blind_reasons(match)


def test_no_version_placed_by_opencti_keeps_the_range_unknown() -> None:
    from app.services import ctem_version_match as matching

    assert matching.affected_from_outcomes([]) is None
    assert matching.affected_from_outcomes([{"version_outcomes": [{"version": "1.0", "outcome": "unknown"}]}]) is None
    closed = [{"open": False, "version_outcomes": [{"version": "1.0", "outcome": "affected"}]}]
    assert matching.affected_from_outcomes(closed) is None


def test_without_a_declared_version_every_has_counts_and_latest_stays():
    issues = source.opencti_issues(SOFTWARE, _stack(), RELATIONS, [], [])
    assert {i.external_id for i in issues} == {"v1", "v2", "v3"}
    assert all(i.not_affected is None and i.installed_version is None for i in issues)
    assert "upgrade:apache/tomcat@latest" in cc.group_issues(issues).groups


def test_the_tech_stack_entry_takes_an_optional_version():
    assert SoftwareEntry(product="nginx").version == ""
    assert SoftwareEntry(product="nginx", version="1.27.1").version == "1.27.1"


class _FakeOpenCti:
    """OpenCTI as one user sees it: *visible* are the ids that user's markings let through."""

    def __init__(self, visible: set[str] | None = None):
        self.visible = visible
        self.headers: list[dict] = []
        self.queries: list[str] = []

    def _see(self, node_id: str) -> bool:
        return self.visible is None or node_id in self.visible

    async def __call__(self, client, base_url, headers, query, variables):
        self.headers.append(headers)
        if "stixCyberObservables" in query:
            self.queries.append("software")
            nodes = [n for n in SOFTWARE if self._see(n["id"])]
            return {"stixCyberObservables": {"edges": [{"node": n} for n in nodes]}}, None
        if "CtemCaseStackVulns" in query:
            self.queries.append("has")
            rels = [r for r in RELATIONS if r["from"]["id"] in variables["from"] and self._see(r["to"]["id"])]
            return {"stixCoreRelationships": {"edges": [{"node": r} for r in rels], "pageInfo": {}}}, None
        if "CtemCaseSoftwareRemediates" in query:
            self.queries.append("remediates")
            assert "remediates" in query and "Software" in query
            rels = [r for r in FIXES if r["to"]["id"] in variables["to"] and self._see(r["from"]["id"])]
            return {"stixCoreRelationships": {"edges": [{"node": r} for r in rels]}}, None
        self.queries.append("course_of_action")
        return {"stixCoreRelationships": {"edges": []}}, None


async def _octi_issues(headers: dict[str, str], stack: list[dict[str, str]]) -> list[Any]:
    """The issues of one OpenCTI read of *stack* (the read must answer)."""
    read = await source._read_opencti(cast(Any, None), "http://octi", headers, stack)
    assert read is not None
    return list(read.issues)


@pytest.mark.asyncio
async def test_the_opencti_read_takes_the_fixed_version_from_remediates(monkeypatch):
    fake = _FakeOpenCti()
    monkeypatch.setattr(source, "_graphql", fake)
    issues = await _octi_issues({"Authorization": "Bearer user"}, _stack("9.0.90"))
    assert {(i.external_id, i.fixed_version) for i in issues if not i.not_affected} == {
        ("v1", "9.0.98"),
        ("v2", "9.0.99"),
    }
    assert set(fake.queries) == {"software", "has", "course_of_action", "remediates"}
    # Every read is the acting user's own: the headers given, never another credential.
    assert all(h == {"Authorization": "Bearer user"} for h in fake.headers)


@pytest.mark.asyncio
async def test_a_restricted_user_gets_only_what_opencti_lets_them_read(monkeypatch):
    """KB 34 s.6.2: a user whose markings hide sw-98 and v2 sees no fix from it and no v2 member."""
    monkeypatch.setattr(source, "_graphql", _FakeOpenCti(visible={"sw-90", "sw-80", "v1", "v3", "sw-99"}))
    issues = await _octi_issues({"Authorization": "Bearer restricted"}, _stack("9.0.90"))
    assert {i.external_id for i in issues} == {"v1", "v3"}
    v1 = next(i for i in issues if i.external_id == "v1")
    # sw-98 is hidden from this user, so the fix they can read is 9.0.99, never the hidden 9.0.98.
    assert v1.fixed_version == "9.0.99" and v1.fixed_by == "tomcat 9.0.99"


@pytest.mark.asyncio
async def test_an_unreadable_remediates_read_keeps_the_issues(monkeypatch):
    fake = _FakeOpenCti()

    async def _graphql(client, base_url, headers, query, variables):
        if "CtemCaseSoftwareRemediates" in query:
            return None, "OpenCTI: 500"
        return await fake(client, base_url, headers, query, variables)

    monkeypatch.setattr(source, "_graphql", _graphql)
    issues = await _octi_issues({}, _stack())
    assert {i.external_id for i in issues} == {"v1", "v2", "v3"}
    assert all(i.fixed_version is None for i in issues)


# ---------------------------------------------------------------------------
# The fix of each platform (#1572)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "version", "cpe", "expected"),
    [
        ("chrome", "120.0.36", "cpe:2.3:a:google:chrome:120.0.36:*:*:*:*:windows:*:*", "Windows"),
        ("chrome", "120.0.37", "cpe:2.3:a:google:chrome:120.0.37:*:*:*:*:macos:*:*", "macOS"),
        ("chrome", "120.0.37", "cpe:2.3:a:google:chrome:120.0.37:*:*:*:*:*:x64:*", None),
        ("Chrome for Mac", "120.0.37", "", None),
        ("Chrome for macOS", "120.0.37", "", "macOS"),
        ("Chrome", "120.0.36 (Windows)", "", "Windows"),
        ("Chrome for Windows and macOS", "120.0.36", "", None),
        ("Chromebook helper", "1.0", "", None),
        ("tomcat", "9.0.98", "", None),
        ("Cisco IOS", "15.9(3)M9", "", None),
        ("ios_xe", "17.9.4a", "", None),
        ("ios", "15.9", "cpe:2.3:o:cisco:ios:15.9:*:*:*:*:*:*:*", None),
        ("Safari for iPhone_OS", "17.2", "", "iOS"),
        ("safari", "17.2", "cpe:2.3:a:apple:safari:17.2:*:*:*:*:ios:*:*", "iOS"),
    ],
)
def test_the_platform_of_a_remediating_software(name: str, version: str, cpe: str, expected: str | None) -> None:
    assert fv.platform_of_software(name, version, cpe) == expected


def test_fixes_by_platform_keep_the_smallest_fix_of_each_platform() -> None:
    fixes = [("120.0.36", "Windows"), ("120.0.40", "Windows"), ("120.0.37", "macOS")]
    assert fv.fixes_by_platform(fixes, None) == {"Windows": "120.0.36", "macOS": "120.0.37"}
    # A remediating version with no platform keeps the fix generic.
    assert fv.fixes_by_platform([*fixes, ("120.0.38", None)], None) == {}
    # ...unless it is below the declared version (it is not a fix of it).
    assert fv.fixes_by_platform([*fixes, ("119.0", None)], "120.0.1") == {"Windows": "120.0.36", "macOS": "120.0.37"}


def test_fold_by_platform_takes_the_greatest_fix_and_goes_generic_on_one_untagged_member() -> None:
    assert fv.fold_by_platform([{"Windows": "1.2", "macOS": "1.3"}, {"Windows": "1.4"}]) == {
        "Windows": "1.4",
        "macOS": "1.3",
    }
    assert fv.fold_by_platform([{"Windows": "1.2"}, {}]) == {}
    assert fv.fold_by_platform([]) == {}


PLATFORM_FIXES = [
    {
        "from": {
            "id": "sw-w",
            "name": "tomcat",
            "vendor": "apache",
            "version": "9.0.98",
            "cpe": "cpe:2.3:a:apache:tomcat:9.0.98:*:*:*:*:windows:*:*",
        },
        "to": {"id": "v1"},
    },
    {
        "from": {
            "id": "sw-m",
            "name": "tomcat",
            "vendor": "apache",
            "version": "9.0.99",
            "cpe": "cpe:2.3:a:apache:tomcat:9.0.99:*:*:*:*:macos:*:*",
        },
        "to": {"id": "v1"},
    },
]


def test_the_reader_keeps_the_fix_of_each_platform_and_the_case_carries_it() -> None:
    issues = source.opencti_issues(SOFTWARE, _stack("9.0.90"), RELATIONS[:1], [], PLATFORM_FIXES)
    (issue,) = issues
    assert issue.fixed_version == "9.0.98"
    assert issue.fixed_versions == (("Windows", "9.0.98"), ("macOS", "9.0.99"))
    grouping = cc.group_issues(issues)
    (remediation,) = grouping.remediations.values()
    assert dict(remediation.fixed_versions) == {"Windows": "9.0.98", "macOS": "9.0.99"}


def test_a_remediating_software_without_platform_keeps_the_fix_generic() -> None:
    issues = source.opencti_issues(SOFTWARE, _stack("9.0.90"), RELATIONS[:1], [], [*PLATFORM_FIXES, FIXES[0]])
    assert issues[0].fixed_version == "9.0.98" and issues[0].fixed_versions == ()


def test_the_signal_and_the_opened_case_carry_the_fix_of_each_platform() -> None:
    from app.services import ctem_case_loop as loop
    from app.services import ctem_source_signals as signals

    (issue,) = source.opencti_issues(SOFTWARE, _stack("9.0.90"), RELATIONS[:1], [], PLATFORM_FIXES)
    fix = cc.remediation_of(issue)
    assert fix is not None
    signal = signals.signal_of(issue, fix)
    assert signal is not None
    assert signal.evidence["case_fix"]["fixed_versions"] == {"Windows": "9.0.98", "macOS": "9.0.99"}
    named = loop.source_fix_of(signal.evidence)
    assert named is not None and named.fixed_versions == {"Windows": "9.0.98", "macOS": "9.0.99"}
    assert loop.platform_fixes_of({"": "1", "Linux": "", "iOS": " 2 "}) == {"iOS": "2"}
    assert loop.platform_fixes_of("x") == {}
