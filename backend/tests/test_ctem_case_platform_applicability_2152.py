"""CTEM case membership reads the platform a CVE applies to (#2152).

A CVE OpenCTI relates only to Software built for one operating system (a Windows-only
VirtualBox 1.6.x driver flaw) does not join the case when every asset running the product
reports another platform: the member is not affected, with its reason. Any unknown (a
vulnerable Software naming no platform, an asset with no platform) keeps the member.
"""

from __future__ import annotations

from app.services import ctem_case_versions as case_versions
from app.services import ctem_cases_source as source
from app.services import ctem_fixed_version as fixed_versions

LABEL = "Oracle VirtualBox"
WINDOWS_CPE = "cpe:2.3:a:oracle:vm_virtualbox:1.6.0:*:*:*:*:windows:*:*"


def _stack(platforms: list[str] | None) -> list[dict[str, object]]:
    entry: dict[str, object] = {
        "product": "VirtualBox",
        "vendor": "Oracle",
        "cpe": "",
        "version": "",
        "versions": ["7.1.8", "7.2.4"],
        "label": LABEL,
        "version_source": "profile",
    }
    stack = [entry]
    return source.with_inventory_platforms(stack, {LABEL: platforms} if platforms else {})


def _software(cpe: str) -> list[dict[str, object]]:
    return [
        {"id": "sw-old", "name": "VirtualBox", "vendor": "Oracle", "version": "1.6.0", "cpe": cpe},
        {"id": "sw-718", "name": "VirtualBox", "vendor": "Oracle", "version": "7.1.8", "cpe": ""},
    ]


RELATIONS = [{"from": {"id": "sw-old"}, "to": {"id": "v1", "name": "CVE-2008-3431", "x_opencti_cisa_kev": True}}]


def test_a_windows_only_cve_on_macos_assets_is_not_affected_with_its_reason() -> None:
    (issue,) = source.opencti_issues(_software(WINDOWS_CPE), _stack(["macOS"]), RELATIONS, [])
    assert issue.not_affected == f"{fixed_versions.NOT_AFFECTED_PLATFORM_PREFIX} Windows only; running on macOS"


def test_one_asset_on_the_cve_platform_keeps_the_member() -> None:
    (issue,) = source.opencti_issues(_software(WINDOWS_CPE), _stack(["Windows", "macOS"]), RELATIONS, [])
    assert issue.not_affected is None


def test_a_vulnerable_software_naming_no_platform_keeps_the_member() -> None:
    (issue,) = source.opencti_issues(_software(""), _stack(["macOS"]), RELATIONS, [])
    assert issue.not_affected is None


def test_assets_with_an_unknown_platform_keep_the_member() -> None:
    (issue,) = source.opencti_issues(_software(WINDOWS_CPE), _stack(None), RELATIONS, [])
    assert issue.not_affected is None


def test_the_inventory_answers_a_product_only_when_every_asset_reports_its_platform() -> None:
    rows = [
        ("Oracle", "VirtualBox", 3, ["macOS"]),
        ("Oracle Corporation", "Oracle VirtualBox", 2, ["macOS", "Linux"]),
        ("Google", "Chrome", 4, ["Windows", None]),
        ("Mozilla", "Firefox", 1, ["Windows"]),
    ]
    answer = case_versions.inventory_platform_sets(rows, [LABEL, "Google Chrome", "Mozilla Thunderbird"])
    assert answer == {LABEL: ("Linux", "macOS")}


def test_the_platform_rule_is_pure_and_never_reads_unknown_as_absent() -> None:
    assert source.platform_not_affected(["Windows"], ["macOS", "Linux"]) is not None
    assert source.platform_not_affected(["Windows", None], ["macOS"]) is None
    assert source.platform_not_affected([], ["macOS"]) is None
    assert source.platform_not_affected(["Windows"], []) is None
    assert source.platform_not_affected(["windows"], ["Windows"]) is None
