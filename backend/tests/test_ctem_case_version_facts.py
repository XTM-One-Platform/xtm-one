"""CTEM case version facts, typed and fed from the device inventory (#2038).

Every version the device inventory reports for a case's product gets one typed
outcome (affected / not_affected / unknown, OpenCTI's ``has``), with where it
was read, its asset count and the fix above it; an unknown version is kept, never
dropped. A remediating Software named ``<product> <version>`` gives the fix. A
profile-only product reads as before and a view cached before the change still reads.
"""

from __future__ import annotations

from app.services import ctem_case_versions as case_versions
from app.services import ctem_cases as cc
from app.services import ctem_cases_source as source
from app.services import ctem_cases_view as view_mod
from app.services import ctem_version_match as version_match

LABEL = "Google Chrome"
VERSIONS = [f"147.0.{i}.1" for i in range(41)]
#: 191 assets spread over the 41 versions (4 or 5 each).
COUNTS = {v: (5 if i < 27 else 4) for i, v in enumerate(VERSIONS)}


def _stack() -> list[dict[str, object]]:
    # The store entity reached the matcher without versions (the partner retest).
    return [
        {
            "product": "Chrome",
            "vendor": "Google",
            "cpe": "",
            "version": "",
            "versions": [],
            "label": LABEL,
            "version_source": "profile",
        }
    ]


#: OpenCTI holds Software for the first ten versions; the first five carry the CVE.
SOFTWARE = [
    {"id": f"sw-{i}", "name": "Chrome", "vendor": "Google", "version": VERSIONS[i], "cpe": ""} for i in range(10)
]
RELATIONS = [{"from": {"id": f"sw-{i}"}, "to": {"id": "v1", "name": "CVE-2026-1"}} for i in range(5)]
FIXES = [{"from": {"id": "fix-1", "name": "Chrome 148.0.7778.168"}, "to": {"id": "v1"}}]


def test_the_inventory_counts_each_known_version_by_distinct_asset() -> None:
    rows = [
        ("Google LLC", "Google Chrome", "147.0.0.1", ["a1", "a2"]),
        ("Google", "Chrome", "147.0.0.1", ["a2", "a3"]),
        ("Google", "Chrome", "147.0.1.1", ["a4"]),
        ("Google", "Chrome", "unknown", ["a5"]),
        ("Mozilla", "Firefox", "120.0", ["a6"]),
    ]
    counts = case_versions.inventory_version_counts(rows, {}, [LABEL, "Mozilla Thunderbird"])
    assert counts == {LABEL: {"147.0.0.1": 3, "147.0.1.1": 1}}


def test_every_inventory_version_gets_a_typed_outcome_none_dropped() -> None:
    stack = source.with_inventory_versions(_stack(), {LABEL: COUNTS})
    assert stack[0]["version"] == "" and len(stack[0]["versions"]) == 41
    (issue,) = source.opencti_issues(SOFTWARE, stack, RELATIONS, [], FIXES)
    facts = issue.version_outcomes
    assert len(facts) == 41 and all(isinstance(f, cc.VersionFact) for f in facts)
    by_outcome = {o: [f for f in facts if f.outcome == o] for o in cc.VERSION_OUTCOMES}
    assert len(by_outcome["affected"]) == 5
    assert len(by_outcome["not_affected"]) == 5
    assert len(by_outcome["unknown"]) == 31
    assert sum(f.asset_count or 0 for f in facts) == 191
    assert {f.source for f in facts} == {cc.VERSION_SOURCE_INVENTORY}
    # One version does not run alone: no single installed version is stated.
    assert issue.installed_version is None and issue.not_affected is None
    # The unknown versions keep the case to validate, never discarded.
    reason = version_match.unverified_reason([issue])
    assert reason is not None and reason.startswith(version_match.UNVERIFIED_NOT_KNOWN)


def test_a_remediating_software_named_with_its_version_fills_the_fixed_version() -> None:
    stack = source.with_inventory_versions(_stack(), {LABEL: COUNTS})
    (issue,) = source.opencti_issues(SOFTWARE, stack, RELATIONS, [], FIXES)
    assert issue.fixed_version == "148.0.7778.168"
    assert issue.fixed_by == "Chrome 148.0.7778.168"
    affected = next(f for f in issue.version_outcomes if f.outcome == cc.VERSION_AFFECTED)
    assert affected.fixed_version == "148.0.7778.168"
    assert affected.fixed_source == cc.FIX_SOURCE_OPENCTI_SOFTWARE
    not_affected = next(f for f in issue.version_outcomes if f.outcome == cc.VERSION_NOT_AFFECTED)
    assert not_affected.fixed_version is None


def test_a_remediating_name_whose_trailing_word_is_no_version_is_not_matched() -> None:
    fixes = [{"from": {"id": "x", "name": "Chrome Enterprise"}, "to": {"id": "v1"}}]
    (issue,) = source.opencti_issues(SOFTWARE, _stack(), RELATIONS, [], fixes)
    assert issue.fixed_version is None


def test_one_inventory_version_is_the_installed_version() -> None:
    stack = source.with_inventory_versions(_stack(), {LABEL: {VERSIONS[0]: 3}})
    (issue,) = source.opencti_issues(SOFTWARE, stack, RELATIONS, [], FIXES)
    assert issue.installed_version == VERSIONS[0]
    assert issue.version_outcomes == (
        cc.VersionFact(
            version=VERSIONS[0],
            outcome=cc.VERSION_AFFECTED,
            source=cc.VERSION_SOURCE_INVENTORY,
            asset_count=3,
            fixed_version="148.0.7778.168",
            fixed_source=cc.FIX_SOURCE_OPENCTI_SOFTWARE,
            fixed_by="Chrome 148.0.7778.168",
        ),
    )


def test_a_profile_only_product_reads_as_before() -> None:
    stack = [dict(_stack()[0], version=VERSIONS[2], versions=[VERSIONS[2]])]
    assert source.with_inventory_versions(stack, {}) == stack
    (issue,) = source.opencti_issues(SOFTWARE, stack, RELATIONS, [], [])
    (fact,) = issue.version_outcomes
    assert (fact.version, fact.outcome, fact.source, fact.asset_count) == (
        VERSIONS[2],
        cc.VERSION_AFFECTED,
        cc.VERSION_SOURCE_PROFILE,
        None,
    )


def test_a_version_the_inventory_does_not_report_keeps_its_own_source() -> None:
    stack = [dict(_stack()[0], version=VERSIONS[2], versions=[VERSIONS[2]], version_source="software")]
    stack = source.with_inventory_versions(stack, {LABEL: {VERSIONS[0]: 3}})
    assert stack[0]["version_source"] == cc.VERSION_SOURCE_SOFTWARE
    (issue,) = source.opencti_issues(SOFTWARE, stack, RELATIONS, [], FIXES)
    by_version = {f.version: (f.source, f.asset_count) for f in issue.version_outcomes}
    assert by_version == {
        VERSIONS[0]: (cc.VERSION_SOURCE_INVENTORY, 3),
        VERSIONS[2]: (cc.VERSION_SOURCE_SOFTWARE, None),
    }


def test_a_view_cached_before_the_typed_facts_still_reads() -> None:
    stack = source.with_inventory_versions(_stack(), {LABEL: {VERSIONS[0]: 2, VERSIONS[20]: 1}})
    (issue,) = source.opencti_issues(SOFTWARE, stack, RELATIONS, [], FIXES)
    stored = view_mod._issue_json(issue)
    assert view_mod._issue_from_json(stored) == issue
    # Before #2038 the cache held [version, outcome] pairs.
    legacy = dict(stored, version_outcomes=[[VERSIONS[0], "affected"], [VERSIONS[20], "unknown"]])
    read = view_mod._issue_from_json(legacy)
    assert read.version_outcomes == (
        cc.VersionFact(version=VERSIONS[0], outcome="affected"),
        cc.VersionFact(version=VERSIONS[20], outcome="unknown"),
    )
    assert version_match.unverified_reason([read]) == f"{version_match.UNVERIFIED_NOT_KNOWN} {VERSIONS[20]}"


def test_the_case_view_shows_each_typed_fact() -> None:
    from types import SimpleNamespace

    stack = source.with_inventory_versions(_stack(), {LABEL: {VERSIONS[0]: 2}})
    (issue,) = source.opencti_issues(SOFTWARE, stack, RELATIONS, [], FIXES)
    row = SimpleNamespace(source="opencti", external_id="v1", open=True)
    shown = view_mod._visible_issue(row, issue)  # type: ignore[arg-type]
    assert shown["version_outcomes"] == [
        {
            "version": VERSIONS[0],
            "outcome": "affected",
            "source": "inventory",
            "asset_count": 2,
            "fixed_version": "148.0.7778.168",
            "fixed_source": "opencti_software",
            "fixed_by": "Chrome 148.0.7778.168",
        }
    ]
    assert version_match.affected_from_outcomes([shown]) == f"{VERSIONS[0]} per OpenCTI"
