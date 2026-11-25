# SPDX-License-Identifier: Apache-2.0

"""A repository's language package is not a running version of the server product (#2223).

A repository whose SBOM lists ``pkg:maven/io.minio/minio@7.2.20`` (the MinIO SDK)
does not run the MinIO server a CVE affects: the upgrade case's running versions
leave that row out, unless the vulnerable OpenCTI Software is itself a package.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models.organization_asset import OrganizationAsset, OrganizationAssetSoftware
from app.services import ctem_case_versions as versions
from app.services import ctem_cases as rules
from app.services import ctem_product_identity as identity
from app.services import ctem_source_signals as source_signals


def _sql(*conditions: object) -> str:
    statement = select(OrganizationAssetSoftware.id).where(*conditions)  # type: ignore[arg-type]
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))  # type: ignore[no-untyped-call]


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"cpe": "cpe:2.3:a:minio:minio:*:*:*:*:*:*:*:*"}, None),
        ({"x_opencti_purl": "pkg:pypi/minio@7.2.0"}, "pypi"),
        ({"x_opencti_purl": "pkg:maven/io.minio/minio@8.5.7"}, "maven"),
        ({"cpe": "cpe:2.3:a:minio:minio:*:*:*:*:*:python:*:*"}, "pypi"),
        ({"x_opencti_purl": "pkg:deb/debian/openssl@3.0"}, None),
        ({"name": "minio"}, None),
    ],
)
def test_the_software_ecosystem_is_read_from_its_purl_else_its_cpe_target(
    node: dict[str, str], expected: str | None
) -> None:
    assert identity.software_ecosystem(node) == expected


def test_a_case_leaves_out_language_package_rows_unless_a_signal_names_a_package() -> None:
    soft = OrganizationAssetSoftware
    assert versions.package_case_rows(soft, None) == []
    rows = versions.package_case_rows(soft, uuid.uuid4())
    assert len(rows) == 1
    sql = _sql(*rows)
    assert "pkg:maven/%" in sql and "pkg:pypi/%" in sql and "pkg:npm/%" in sql
    assert "pkg:deb" not in sql and "pkg:rpm" not in sql
    assert "ctem_signals" in sql and "language_package" in sql and "EXISTS" in sql


def test_the_os_case_rule_is_unchanged() -> None:
    soft, asset = OrganizationAssetSoftware, OrganizationAsset
    assert versions.os_case_rows(soft, asset, None) == []
    assert len(versions.os_case_rows(soft, asset, "Windows")) == 2


def _issue(language_package: bool) -> rules.SourceIssue:
    return rules.SourceIssue(
        source="opencti",
        external_id="vulnerability--1",
        name="CVE-2023-28432",
        cve="CVE-2023-28432",
        product="minio",
        vendor="minio",
        first_seen_at=datetime(2026, 11, 25, tzinfo=UTC),
        language_package=language_package,
    )


def test_a_server_cve_signal_does_not_count_repository_packages() -> None:
    issue = _issue(False)
    fix = rules.remediation_of(issue)
    assert fix is not None and fix.key == "upgrade:minio/minio@latest" and not fix.language_package
    signal = source_signals.signal_of(issue, fix)
    assert signal is not None
    assert "language_package" not in signal.evidence["case_fix"]


def test_a_package_cve_signal_keeps_the_repository_rows() -> None:
    issue = _issue(True)
    fix = rules.remediation_of(issue)
    assert fix is not None and fix.language_package
    signal = source_signals.signal_of(issue, fix)
    assert signal is not None
    assert signal.evidence["case_fix"]["language_package"] is True
