"""Software as an entity, the pure half (#1495): rows, dependency edges, CPE and PURL forms, SBOM dependencies."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services import organization_software_store as sw
from app.services import sbom_parser
from app.services.organization_asset_store import sbom_components

T1 = datetime(2026, 11, 1, tzinfo=UTC)
T2 = datetime(2026, 11, 20, tzinfo=UTC)


def test_software_rows_group_versions_under_one_software_with_the_cased_spelling() -> None:
    rows = sw.software_rows(
        [
            {"vendor": "google", "product": "chrome", "version": "130.0", "sources": ["intune"], "last_seen": T1},
            {
                "vendor": "Google",
                "product": "Chrome",
                "version": "131.0",
                "cpe": "cpe:2.3:a:google:chrome:131.0:*:*:*:*:*:*:*",
                "sources": ["defender"],
                "first_seen": T1,
                "last_seen": T2,
            },
            {"vendor": "", "product": "zlib", "version": "", "sources": ["sbom"]},
        ]
    )
    chrome = rows[("google", "chrome")]
    assert (chrome.vendor, chrome.product) == ("Google", "Chrome")
    assert set(chrome.versions) == {"130.0", "131.0"}
    assert chrome.sources == {"intune", "defender"}
    assert chrome.reported_cpes == {"cpe:2.3:a:google:chrome:*:*:*:*:*:*:*:*"}
    assert chrome.versions["131.0"]["cpe"].endswith(":131.0:*:*:*:*:*:*:*")
    assert chrome.last_seen == T2
    assert set(rows[("", "zlib")].versions) == {"unknown"}


def test_cpe_and_purl_forms() -> None:
    assert sw.product_cpe("cpe:2.3:a:f5:nginx:1.27.1:*:*:*:*:*:*:*") == "cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*"
    assert sw.version_cpe("cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*", "1.27.1").split(":")[5] == "1.27.1"
    assert sw.version_cpe("cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*", "unknown") == ""
    assert sw.product_purl("pkg:npm/%40scope/lib@1.2.3?arch=x") == "pkg:npm/%40scope/lib"
    assert sw.product_purl("pkg:generic/nginx") == "pkg:generic/nginx"
    assert sw.display_name("Microsoft", "Microsoft Teams") == "Microsoft Teams"
    assert sw.display_name("F5", "nginx") == "F5 nginx"


CYCLONEDX = {
    "bomFormat": "CycloneDX",
    "components": [
        {
            "bom-ref": "app",
            "name": "web-app",
            "version": "2.0",
            "components": [{"bom-ref": "ossl", "name": "openssl", "version": "3.0.15"}],
        },
        {"bom-ref": "z", "name": "zlib", "version": "NOASSERTION"},
    ],
    "dependencies": [{"ref": "app", "dependsOn": ["ossl", "z", "missing"]}, {"ref": "ossl", "dependsOn": ["z"]}],
}

SPDX = {
    "spdxVersion": "SPDX-2.3",
    "packages": [
        {"SPDXID": "SPDXRef-app", "name": "api", "versionInfo": "1.0"},
        {"SPDXID": "SPDXRef-req", "name": "requests", "versionInfo": "2.32.3"},
        {"SPDXID": "SPDXRef-url", "name": "urllib3", "versionInfo": "2.2.3"},
    ],
    "relationships": [
        {"spdxElementId": "SPDXRef-app", "relationshipType": "DEPENDS_ON", "relatedSpdxElement": "SPDXRef-req"},
        {"spdxElementId": "SPDXRef-url", "relationshipType": "DEPENDENCY_OF", "relatedSpdxElement": "SPDXRef-req"},
    ],
}


def test_cyclonedx_dependencies_are_read_and_an_unknown_ref_dropped() -> None:
    sbom = sbom_parser.parse(CYCLONEDX)
    by_name = {c.product: c for c in sbom.components}
    assert sbom.depends_on(by_name["web-app"]) == ["openssl 3.0.15", "zlib"]
    assert sbom.depends_on(by_name["openssl"]) == ["zlib"]
    assert sbom.depends_on(by_name["zlib"]) == []


def test_spdx_depends_on_and_its_inverse() -> None:
    sbom = sbom_parser.parse(SPDX)
    by_name = {c.product: c for c in sbom.components}
    assert sbom.depends_on(by_name["api"]) == ["requests 2.32.3"]
    assert sbom.depends_on(by_name["requests"]) == ["urllib3 2.2.3"]


def test_dependency_edges_of_one_asset_resolve_labels_to_versions_with_bundling() -> None:
    components = sbom_components(sbom_parser.parse(CYCLONEDX))
    edges = sw.dependency_edges(components)
    assert (("", "web-app", "2.0"), ("", "openssl", "3.0.15"), "depends_on") in edges
    assert (("", "web-app", "2.0"), ("", "zlib", "unknown"), "depends_on") in edges
    assert (("", "openssl", "3.0.15"), ("", "zlib", "unknown"), "depends_on") in edges
    assert (("", "openssl", "3.0.15"), ("", "web-app", "2.0"), "bundled_in") in edges
    assert len(edges) == 4


def test_sbom_component_types_map_to_software_types() -> None:
    assert sbom_parser.software_type_of("library") == "library"
    assert sbom_parser.software_type_of("Framework") == "library"
    assert sbom_parser.software_type_of("operating-system") == "operating_system"
    assert sbom_parser.software_type_of("OPERATING_SYSTEM") == "operating_system"
    assert sbom_parser.software_type_of("firmware") == "firmware"
    assert sbom_parser.software_type_of("container") == "container_image"
    assert sbom_parser.software_type_of("application") == "application"
    assert sbom_parser.software_type_of("file") == "application"
    assert sbom_parser.software_type_of(None) == "application"
    spdx = sbom_parser.parse(
        {"spdxVersion": "SPDX-2.3", "packages": [{"SPDXID": "a", "name": "z", "primaryPackagePurpose": "LIBRARY"}]}
    )
    assert spdx.components[0].software_type == "library"
    rows = sbom_components(
        sbom_parser.parse({"bomFormat": "CycloneDX", "components": [{"name": "z", "type": "library"}]})
    )
    assert rows[0]["type"] == "library"


def test_software_types_keep_a_declared_type_over_application_and_drop_unknown_values() -> None:
    types = sw.software_types(
        [
            {"vendor": "", "product": "zlib", "type": "application"},
            {"vendor": "", "product": "ZLIB", "type": "library"},
            {"vendor": "", "product": "zlib", "type": "application"},
            {"vendor": "Google", "product": "Chrome"},
            {"vendor": "", "product": "odd", "type": "widget"},
        ]
    )
    assert types == {("", "zlib"): "library"}


def test_a_label_naming_nothing_on_the_asset_gives_no_edge() -> None:
    assert sw.dependency_edges([{"product": "a", "version": "1", "depends_on": ["b 2"], "bundled_in": "c"}]) == []


def test_the_matcher_reads_each_software_with_its_one_known_version() -> None:
    entries = sw.matcher_entries(
        [
            {"vendor": "Fortinet", "product": "FortiOS", "cpe": "cpe:2.3:o:fortinet:fortios:*", "versions": ["7.2.4"]},
            {"vendor": "Google", "product": "Chrome", "cpe": "", "versions": ["130.0", "131.0", "unknown"]},
            {"vendor": "Microsoft", "product": "Microsoft Teams", "cpe": "", "versions": ["unknown"]},
        ]
    )
    assert entries == [
        {
            "product": "FortiOS",
            "vendor": "Fortinet",
            "cpe": "cpe:2.3:o:fortinet:fortios:*",
            "version": "7.2.4",
            "label": "Fortinet FortiOS",
        },
        # Two known versions run: no installed version is stated, never a guess.
        {"product": "Chrome", "vendor": "Google", "cpe": "", "version": "", "label": "Google Chrome"},
        # Only unknown: empty, never the word unknown as a version to match.
        {"product": "Microsoft Teams", "vendor": "Microsoft", "cpe": "", "version": "", "label": "Microsoft Teams"},
    ]


def test_a_typed_profile_row_no_software_carries_is_still_matched() -> None:
    entries = sw.matcher_entries(
        [{"vendor": "Google", "product": "Chrome", "cpe": "", "versions": ["131.0"]}],
        [
            # The same software typed by hand: its CPE fills the gap, its version never wins.
            {"vendor": "google", "product": "chrome", "cpe": "cpe:2.3:a:google:chrome:*", "version": "99"},
            # A product with no vendor joins the one software of that product.
            {"vendor": "", "product": "CHROME", "cpe": "", "version": ""},
            {"vendor": "", "product": "Jira", "cpe": "", "version": ""},
            {"vendor": "", "product": "  ", "cpe": "", "version": ""},
        ],
    )
    assert [(e["label"], e["cpe"], e["version"]) for e in entries] == [
        ("Google Chrome", "cpe:2.3:a:google:chrome:*", "131.0"),
        ("Jira", "", ""),
    ]
