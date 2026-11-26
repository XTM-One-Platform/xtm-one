"""CTEM matcher: a repository's language package is not the installed server version (#2375).

The MinIO SDK declared in code repositories (``pkg:maven/io.minio/minio@7.2.20``,
``pkg:pypi/minio@7.2.20``) is not a MinIO server release. The matcher's inventory read now
follows the case read (#2223): language package rows count only for a CVE whose every
Software is a language package, so ``installed_version``, ``version_outcomes``,
``running_versions`` and ``asset_count`` come from the same rows. No DB / network.
"""

from __future__ import annotations

from typing import Any

from app.services import ctem_case_versions as case_versions
from app.services import ctem_cases as cc
from app.services import ctem_cases_source as source

LABEL = "minio"
SDK = "7.2.20"
SERVER = "RELEASE.2023-03-13T19-46-17Z"

#: Two repositories declare the SDK (Java and Python); no asset runs the server.
SDK_ROWS: list[tuple[Any, ...]] = [
    ("io.minio", "minio", SDK, ["repo-1"], True),
    ("", "minio", SDK, ["repo-2"], True),
]


def _stack() -> list[dict[str, Any]]:
    # A name-only store entity (no purl, no CPE): it matches the server Software by name.
    return [
        {
            "product": "minio",
            "vendor": "",
            "cpe": "",
            "purl": "",
            "version": "",
            "versions": [],
            "label": LABEL,
            "version_source": "software",
            "os_families": [],
        }
    ]


MINIO_SERVER = [{"id": "sw-server", "name": "minio", "vendor": "minio", "cpe": "cpe:2.3:a:minio:minio:*:*:*:*:*:*:*:*"}]
MINIO_SDK = [
    {"id": "sw-sdk", "name": "minio", "vendor": "minio", "cpe": "", "x_opencti_purl": "pkg:maven/io.minio/minio"}
]


def _relations(software_id: str) -> list[dict[str, Any]]:
    return [{"from": {"id": software_id}, "to": {"id": "v1", "name": "CVE-2023-28432"}}]


def _answers(rows: list[tuple[Any, ...]]) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    return case_versions.inventory_version_answers(rows, {}, [LABEL])


def test_the_inventory_read_leaves_language_package_rows_out_of_the_server_answer() -> None:
    server, packages = _answers(SDK_ROWS)
    assert server == {}
    assert packages == {LABEL: {SDK: 2}}
    # A server row is counted in both answers.
    server, packages = _answers([*SDK_ROWS, ("MinIO", "minio", SERVER, ["host-1"], False)])
    assert server == {LABEL: {SERVER: 1}}
    assert packages == {LABEL: {SERVER: 1, SDK: 2}}


def test_a_server_cve_never_reads_the_sdk_as_the_installed_version() -> None:
    server, packages = _answers(SDK_ROWS)
    stack = source.with_inventory_versions(_stack(), server, packages)
    assert "version_counts" not in stack[0] and stack[0]["versions"] == []
    (issue,) = source.opencti_issues(MINIO_SERVER, stack, _relations("sw-server"), [])
    assert issue.language_package is False
    assert issue.installed_version is None
    assert all(f.source != cc.VERSION_SOURCE_INVENTORY for f in issue.version_outcomes)
    assert SDK not in {f.version for f in issue.version_outcomes}


def test_a_package_cve_still_counts_the_repository_rows() -> None:
    server, packages = _answers(SDK_ROWS)
    stack = source.with_inventory_versions(_stack(), server, packages)
    (issue,) = source.opencti_issues(MINIO_SDK, stack, _relations("sw-sdk"), [])
    assert issue.language_package is True
    assert issue.installed_version == SDK
    (fact,) = issue.version_outcomes
    assert (fact.version, fact.source, fact.asset_count) == (SDK, cc.VERSION_SOURCE_INVENTORY, 2)


def test_inventory_outcomes_name_running_versions_and_never_exceed_the_assets() -> None:
    """Consistency: every ``inventory`` outcome is a running version and their assets sum within the count."""
    rows = [*SDK_ROWS, ("MinIO", "minio", SERVER, ["host-1"], False)]
    server, packages = _answers(rows)
    stack = source.with_inventory_versions(_stack(), server, packages)
    (issue,) = source.opencti_issues(MINIO_SERVER, stack, _relations("sw-server"), [])
    running = server[LABEL]
    inventory = [f for f in issue.version_outcomes if f.source == cc.VERSION_SOURCE_INVENTORY]
    assert [f.version for f in inventory] == [SERVER]
    assert all(f.version in running for f in inventory)
    assert sum(f.asset_count or 0 for f in inventory) <= len({"host-1"})
    assert issue.installed_version == SERVER


def test_without_the_package_answer_the_stack_reads_as_before() -> None:
    stack = source.with_inventory_versions(_stack(), {LABEL: {SERVER: 1}})
    assert stack[0]["version"] == SERVER and "package_view" not in stack[0]
