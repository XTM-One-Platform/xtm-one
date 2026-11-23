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


def test_a_label_naming_nothing_on_the_asset_gives_no_edge() -> None:
    assert sw.dependency_edges([{"product": "a", "version": "1", "depends_on": ["b 2"], "bundled_in": "c"}]) == []
