"""SBOM parsing (#1178): CycloneDX nesting, SPDX relationships, unknown versions, size cap.

Ported from the OpenAEV parser tests of the closed CTEM-20 branch.
"""

from __future__ import annotations

import json

import pytest

from app.services import organization_asset_store as store
from app.services import sbom_parser as sp

CYCLONEDX = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.5",
    "metadata": {"component": {"name": "ingress-nginx", "version": "1.15.1"}},
    "components": [
        {
            "bom-ref": "img",
            "name": "ingress-nginx-controller",
            "version": "1.15.1",
            "group": "kubernetes",
            "components": [
                {
                    "bom-ref": "nginx",
                    "name": "nginx",
                    "version": "1.27.1",
                    "publisher": "F5",
                    "cpe": "cpe:2.3:a:f5:nginx:1.27.1:*:*:*:*:*:*:*",
                    "purl": "pkg:generic/nginx@1.27.1",
                }
            ],
        },
        {"name": "zlib", "version": "NOASSERTION", "supplier": {"name": "zlib project"}},
        {"name": "curl"},
        {"version": "1.0"},
    ],
}

SPDX = {
    "spdxVersion": "SPDX-2.3",
    "name": "web-app",
    "packages": [
        {"SPDXID": "SPDXRef-app", "name": "web-app", "versionInfo": "2.1.0", "supplier": "Organization: Filigran"},
        {
            "SPDXID": "SPDXRef-openssl",
            "name": "openssl",
            "versionInfo": "3.0.15",
            "supplier": "Organization: OpenSSL Software Foundation",
            "externalRefs": [
                {"referenceType": "cpe23Type", "referenceLocator": "cpe:2.3:a:openssl:openssl:3.0.15:*:*:*:*:*:*:*"},
                {"referenceType": "purl", "referenceLocator": "pkg:generic/openssl@3.0.15"},
            ],
        },
        {"SPDXID": "SPDXRef-libc", "name": "libc", "versionInfo": "NOASSERTION", "supplier": "NOASSERTION"},
        {"SPDXID": "SPDXRef-orphan", "name": "orphan", "versionInfo": "0.1"},
    ],
    "relationships": [
        {"spdxElementId": "SPDXRef-app", "relationshipType": "CONTAINS", "relatedSpdxElement": "SPDXRef-openssl"},
        {"spdxElementId": "SPDXRef-libc", "relationshipType": "CONTAINED_BY", "relatedSpdxElement": "SPDXRef-app"},
        {"spdxElementId": "SPDXRef-app", "relationshipType": "DEPENDS_ON", "relatedSpdxElement": "SPDXRef-orphan"},
    ],
}


def test_cyclonedx_nested_components_carry_their_parent_and_vendor() -> None:
    sbom = sp.parse(CYCLONEDX)
    assert sbom.format == "cyclonedx"
    by_name = {c.product: c for c in sbom.components}
    assert set(by_name) == {"ingress-nginx-controller", "nginx", "zlib", "curl"}  # no name: skipped
    nginx = by_name["nginx"]
    assert nginx.parent_ref == "img"
    assert nginx.vendor == "F5"
    assert nginx.cpe and nginx.cpe.startswith("cpe:2.3:a:f5:nginx")
    assert nginx.purl == "pkg:generic/nginx@1.27.1"
    assert sbom.bundled_in(nginx) == "ingress-nginx-controller 1.15.1"
    assert by_name["ingress-nginx-controller"].vendor == "kubernetes"
    assert by_name["zlib"].vendor == "zlib project"


def test_missing_or_noassertion_versions_are_unknown_never_empty() -> None:
    rows = store.sbom_components(sp.parse(CYCLONEDX))
    versions = {r["product"]: r["version"] for r in rows}
    assert versions["zlib"] == "unknown"
    assert versions["curl"] == "unknown"
    assert all(r["version"] for r in rows)


def test_spdx_contains_and_contained_by_give_the_parent_and_external_refs_the_ids() -> None:
    sbom = sp.parse(SPDX)
    assert sbom.format == "spdx"
    by_name = {c.product: c for c in sbom.components}
    assert by_name["openssl"].parent_ref == "SPDXRef-app"
    assert by_name["libc"].parent_ref == "SPDXRef-app"
    assert by_name["orphan"].parent_ref is None  # DEPENDS_ON is not a bundling
    assert by_name["openssl"].vendor == "OpenSSL Software Foundation"
    assert by_name["openssl"].cpe and "openssl:openssl:3.0.15" in by_name["openssl"].cpe
    assert by_name["openssl"].purl == "pkg:generic/openssl@3.0.15"
    assert by_name["libc"].vendor is None and by_name["libc"].version is None
    rows = {r["product"]: r for r in store.sbom_components(sbom)}
    assert rows["openssl"]["bundled_in"] == "web-app 2.1.0"
    assert rows["libc"]["version"] == "unknown"


def test_document_key_names_what_the_sbom_describes() -> None:
    assert store.sbom_document_key(CYCLONEDX, "f.json") == "ingress-nginx 1.15.1"
    assert store.sbom_document_key(SPDX, "f.json") == "web-app"
    assert store.sbom_document_key({"bomFormat": "CycloneDX"}, "image.cdx.json") == "image.cdx.json"


def test_a_component_listed_twice_is_kept_once() -> None:
    doc = {"bomFormat": "CycloneDX", "components": [{"name": "a", "version": "1"}, {"name": "A", "version": "1"}]}
    assert len(store.sbom_components(sp.parse(doc))) == 1


def test_an_sbom_over_the_cap_is_refused_not_cut() -> None:
    doc = {"bomFormat": "CycloneDX", "components": [{"name": f"c{i}"} for i in range(sp.MAX_COMPONENTS + 1)]}
    with pytest.raises(sp.SbomError, match="more than 10000"):
        sp.parse(doc)


def test_deep_nesting_does_not_recurse() -> None:
    node: dict[str, object] = {"name": "leaf", "version": "1"}
    for i in range(3000):
        node = {"name": f"n{i}", "components": [node]}
    sbom = sp.parse({"bomFormat": "CycloneDX", "components": [node]})
    assert len(sbom.components) == 3001


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        json.dumps([1, 2]).encode(),
        json.dumps({"hello": "world"}).encode(),
        b"[" * 100000 + b"]" * 100000,
    ],
    ids=["text", "list", "unknown-json", "deep-json"],
)
def test_a_file_that_is_not_an_sbom_is_refused(data: bytes) -> None:
    with pytest.raises(sp.SbomError):
        sp.parse_bytes(data)


def test_the_import_endpoint_is_for_administrators_only() -> None:
    from fastapi.routing import APIRoute

    from app.api.deps import require_admin
    from app.api.v1.organization_context import router

    route = next(r for r in router.routes if isinstance(r, APIRoute) and r.path.endswith("/assets/{asset_id}/sbom"))
    assert route.methods == {"POST"}
    assert require_admin in [d.call for d in route.dependant.dependencies]


def test_spdx_supplier_forms() -> None:
    assert sp.spdx_supplier("Organization: F5, Inc.") == "F5, Inc."
    assert sp.spdx_supplier("Person: Jane") == "Jane"
    assert sp.spdx_supplier("NOASSERTION") is None
    assert sp.spdx_supplier("Acme") == "Acme"
