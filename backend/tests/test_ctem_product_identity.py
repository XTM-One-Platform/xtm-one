"""CTEM reconcile: product identity before a bare-name match (#2036).

Only a field known on BOTH sides rejects a match; with nothing known beyond
the name, the bare name still matches. No DB / network: pure functions.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from app.models.organization_asset import OrganizationAsset, OrganizationAssetSoftware
from app.services import ctem_case_versions as versions
from app.services import ctem_metrics_collector as collector
from app.services import ctem_product_identity as identity
from app.services import organization_software_store as sw

MINIO_SERVER = {"name": "minio", "vendor": "minio", "cpe": "cpe:2.3:a:minio:minio:*:*:*:*:*:*:*:*"}
ES_SERVER = {"name": "elasticsearch", "vendor": "elastic", "cpe": "cpe:2.3:a:elasticsearch:elasticsearch:1.4.2:*:*:*:*:*:*:*"}
AUTODESK_INSTALLER = {"name": "installer", "cpe": "cpe:2.3:a:autodesk:installer:*:*:*:*:*:windows:*:*"}
ORION_WINDOWS = {"name": "orion", "cpe": "cpe:2.3:a:solarwinds:orion:2020.2:*:*:*:*:windows:*:*"}


def _entry(product: str, **fields) -> dict:
    software = {"vendor": "", "product": product, "cpe": "", "versions": [], **fields}
    return sw.matcher_entries([software])[0]


@pytest.mark.parametrize(
    "entry",
    [
        _entry("minio", purl="pkg:pypi/minio", versions=["7.2.7"]),
        _entry("minio", vendor="minio", purl="pkg:pypi/minio", versions=["7.2.7"]),
        _entry("minio", vendor="io.minio", purl="pkg:maven/io.minio/minio", versions=["8.5.7"]),
    ],
)
def test_a_python_or_java_package_never_matches_the_minio_server(entry):
    assert collector._stack_matches(MINIO_SERVER, [entry]) is None


def test_the_elasticsearch_client_never_matches_the_server():
    entry = _entry("elasticsearch", purl="pkg:pypi/elasticsearch@9.4.1", versions=["9.4.1"])
    assert collector._stack_matches(ES_SERVER, [entry]) is None
    # The same package known to OpenCTI by its purl still matches.
    assert collector._stack_matches({"name": "elasticsearch", "x_opencti_purl": "pkg:pypi/elasticsearch@9.4.1"}, [entry])


def test_a_package_matches_a_cpe_naming_its_ecosystem():
    entry = _entry("requests", purl="pkg:pypi/requests")
    node = {"name": "requests", "cpe": "cpe:2.3:a:python:requests:2.31.0:*:*:*:*:python:*:*"}
    assert collector._stack_matches(node, [entry]) == "requests"


def test_a_windows_only_product_never_matches_software_running_only_on_macos():
    installer = _entry("Installer", versions=["6.2.0"], os_families=["macOS"])
    orion = _entry("Orion", versions=["1.1"], os_families=["macOS"])
    assert collector._stack_matches(AUTODESK_INSTALLER, [installer]) is None
    assert collector._stack_matches(ORION_WINDOWS, [orion]) is None
    on_windows = _entry("Orion", os_families=["Windows"])
    assert collector._stack_matches(ORION_WINDOWS, [on_windows]) == "Orion"


def test_two_known_cpes_of_different_products_never_match():
    entry = _entry("Installer", cpe="cpe:2.3:a:apple:installer:*:*:*:*:*:*:*:*")
    assert collector._stack_matches(AUTODESK_INSTALLER, [entry]) is None


def test_a_name_only_software_still_matches_by_name():
    """Sam, 25 Nov 2026: unknown CPE, purl, vendor or OS keeps the bare-name match."""
    bare = _entry("Orion", versions=["1.1"])
    assert bare["purl"] == "" and bare["os_families"] == []
    assert collector._stack_matches(ORION_WINDOWS, [bare]) == "Orion"
    assert collector._stack_matches({"name": "orion"}, [bare]) == "Orion"
    assert collector._stack_matches(MINIO_SERVER, [_entry("minio")]) == "minio"


def test_a_field_known_on_one_side_only_never_rejects():
    package = _entry("minio", purl="pkg:pypi/minio")
    assert collector._stack_matches({"name": "minio"}, [package]) == "minio"
    on_macos = _entry("Orion", os_families=["macOS"])
    assert collector._stack_matches({"name": "orion", "vendor": "solarwinds"}, [on_macos]) == "Orion"


def test_os_families_is_empty_when_one_asset_reports_none():
    assert identity.os_families(["macOS", "macOS", "Windows"]) == ["Windows", "macOS"]
    assert identity.os_families(["macOS", None]) == []
    assert identity.os_families([]) == []


def test_cpe_and_purl_fields():
    assert identity.purl_fields("pkg:maven/io.minio/minio@8.5.7?type=jar") == ("maven", "io.minio/minio")
    assert identity.ecosystem("pkg:deb/debian/openssl@3.0") is None
    assert identity.platform_of("cpe:2.3:o:microsoft:windows_10:*:*:*:*:*:*:*:*") == "Windows"
    assert identity.platform_of("cpe:2.3:a:autodesk:installer:*:*:*:*:*:windows:*:*") == "Windows"
    assert identity.platform_of("cpe:2.3:a:minio:minio:*:*:*:*:*:*:*:*") is None


def test_an_os_case_reads_no_package_row_and_no_asset_of_another_os():
    """The windows case: a repository's ``windows`` crate and a macOS laptop never run Windows."""
    soft, asset = OrganizationAssetSoftware, OrganizationAsset
    assert versions.os_case_rows(soft, asset, None) == []
    rows = versions.os_case_rows(soft, asset, "Windows")
    sql = " ".join(str(c.compile(dialect=postgresql.dialect())) for c in rows)
    assert len(rows) == 2
    assert "organization_asset_software.purl" in sql and "organization_assets.fields" in sql and "IS NULL" in sql
