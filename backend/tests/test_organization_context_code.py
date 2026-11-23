# SPDX-License-Identifier: Apache-2.0

"""GitHub and GitLab repositories as asset sources: mapping, SBOM, paging and identity merge."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services import organization_asset_identity as ident
from app.services import organization_context_code as code
from app.services.organization_context import AssetEntry

GITHUB_REPO = {
    "id": 4242,
    "name": "api",
    "full_name": "acme/api",
    "private": True,
    "visibility": "private",
    "owner": {"login": "Acme"},
    "html_url": "https://github.com/acme/api",
    "default_branch": "main",
    "archived": False,
    "fork": False,
    "pushed_at": "2026-11-20T10:00:00Z",
    "language": "Python",
    "topics": ["payments"],
}

GITLAB_PROJECT = {
    "id": 77,
    "name": "web",
    "path_with_namespace": "acme/platform/web",
    "visibility": "internal",
    "namespace": {"full_path": "acme/platform"},
    "web_url": "https://gitlab.acme.io/acme/platform/web",
    "default_branch": "develop",
    "archived": True,
    "last_activity_at": "2026-11-01T08:00:00Z",
    "topics": [],
}

GITHUB_SBOM = {
    "sbom": {
        "spdxVersion": "SPDX-2.3",
        "name": "com.github.acme/api",
        "packages": [
            {
                "SPDXID": "SPDXRef-com.github.acme-api",
                "name": "com.github.acme/api",
                "versionInfo": "main",
                "externalRefs": [
                    {"referenceType": "purl", "referenceLocator": "pkg:github/acme/api@main"},
                ],
            },
            {
                "SPDXID": "SPDXRef-pip-jinja2",
                "name": "pip:jinja2",
                "versionInfo": "3.1.2",
                "externalRefs": [{"referenceType": "purl", "referenceLocator": "pkg:pypi/jinja2@3.1.2"}],
            },
            {"SPDXID": "SPDXRef-npm-left-pad", "name": "npm:left-pad", "versionInfo": "NOASSERTION"},
        ],
    }
}


def test_github_repository_row_is_a_code_repository_keyed_on_host_and_id() -> None:
    row = code.github_repo_row(GITHUB_REPO)
    assert row["category"] == "source_code" and row["subcategory"] == "code_repository"
    assert row["source"] == "github" and row["external_id"] == "4242"
    assert row["cloud_resource_id"] == "github.com:repository:4242"
    assert row["internet_facing"] == "no"
    assert "owner:acme" in row["tags"] and "language:python" in row["tags"] and "topic:payments" in row["tags"]
    assert "default-branch:main" in row["tags"]
    assert row["status"] == "active" and row["last_seen"] == "2026-11-20T10:00:00Z"
    AssetEntry.model_validate(row)  # the profile accepts it


def test_gitlab_project_row_internal_is_not_internet_facing_and_archived_is_inactive() -> None:
    row = code.gitlab_project_row(GITLAB_PROJECT, host="gitlab.acme.io")
    assert row["name"] == "acme/platform/web"
    assert row["cloud_resource_id"] == "gitlab.acme.io:repository:77"
    assert row["internet_facing"] == "no"
    assert row["status"] == "inactive" and "archived" in row["tags"]
    AssetEntry.model_validate(row)


def test_filters_leave_out_archived_and_forks_unless_asked() -> None:
    archived = {**GITHUB_REPO, "archived": True}
    fork = {**GITHUB_REPO, "fork": True}
    row = code.github_repo_row(GITHUB_REPO)
    assert code.keep_repository(row, GITHUB_REPO, {})
    assert not code.keep_repository(row, archived, {})
    assert code.keep_repository(row, archived, {"include_archived": "true"})
    assert not code.keep_repository(row, fork, {})
    assert code.keep_repository(row, fork, {"include_forks": ["true"]})


def test_github_sbom_lists_dependencies_with_purl_and_drops_the_repository_itself() -> None:
    software = code.github_sbom_software(GITHUB_SBOM, repository="acme/api")
    products = {s["product"]: s for s in software}
    assert "com.github.acme/api" not in products
    assert products["pip:jinja2"]["version"] == "3.1.2"
    assert products["pip:jinja2"]["purl"] == "pkg:pypi/jinja2@3.1.2"
    assert products["npm:left-pad"]["version"] == "unknown"  # never an empty version


def test_gitlab_dependencies_get_a_purl_from_their_package_manager() -> None:
    software = code.gitlab_dependency_software(
        [
            {"name": "lodash", "version": "4.17.20", "package_manager": "yarn"},
            {"name": "org.apache.logging.log4j:log4j-core", "version": "2.14.1", "package_manager": "maven"},
            {"name": "mystery", "version": "", "package_manager": "unknown-pm"},
        ]
    )
    by = {s["product"]: s for s in software}
    assert by["lodash"]["purl"] == "pkg:npm/lodash@4.17.20"
    assert by["org.apache.logging.log4j:log4j-core"]["purl"] == "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1"
    assert by["mystery"]["purl"] == "" and by["mystery"]["version"] == "unknown"


def test_next_link_reads_the_rel_next_url() -> None:
    header = '<https://api.github.com/orgs/acme/repos?page=2>; rel="next", <https://x?page=9>; rel="last"'
    assert code.next_link(header) == "https://api.github.com/orgs/acme/repos?page=2"
    assert code.next_link('<https://x?page=9>; rel="last"') is None


def test_the_same_repository_from_two_sources_merges_on_its_resource_id() -> None:
    github_row = code.github_repo_row(GITHUB_REPO)
    other = {"source": "openaev", "external_id": "aev-1", "cloud_resource_id": "GitHub.com:repository:4242"}
    records = [
        ident.SourceRecord(source=r["source"], external_id=r["external_id"], identifiers=ident._asset_ids(r))
        for r in (github_row, other)
    ]
    canon = ident.match_records(records)
    assert len(canon) == 1
    assert canon[0].sources == ["github", "openaev"]


def test_a_renamed_repository_keeps_its_identity() -> None:
    before = code.github_repo_row(GITHUB_REPO)
    after = code.github_repo_row({**GITHUB_REPO, "full_name": "acme/api-v2", "name": "api-v2"})
    assert before["cloud_resource_id"] == after["cloud_resource_id"]


def test_code_sources_rank_as_scm() -> None:
    assert ident.source_class("github") == "scm" and ident.source_class("gitlab") == "scm"


class _Resp:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self) -> Any:
        return self._body


class _Client:
    def __init__(self, routes: dict[str, _Resp]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _Resp:
        self.calls.append(url)
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp
        return _Resp(404, {})


def test_github_read_pages_repositories_and_reads_each_sbom() -> None:
    api = code.GITHUB_API
    client = _Client(
        {
            f"{api}/orgs/acme/repos?per_page=100&type=all&page=2": _Resp(200, []),
            f"{api}/orgs/acme/repos": _Resp(
                200,
                [GITHUB_REPO, {**GITHUB_REPO, "id": 5, "full_name": "acme/old", "archived": True}],
                {"Link": f'<{api}/orgs/acme/repos?per_page=100&type=all&page=2>; rel="next"'},
            ),
            f"{api}/repos/acme/api/dependency-graph/sbom": _Resp(200, GITHUB_SBOM),
        }
    )
    read = asyncio.run(
        code.read_repositories("github", "t", api, {"organizations": ["acme"]}, limit=100, client=client)
    )
    assert read.error is None
    assert [r["external_id"] for r in read.rows] == ["4242"]  # the archived one is left out
    assert {s["product"] for s in read.software["4242"]} == {"pip:jinja2", "npm:left-pad"}


def test_github_without_dependency_graph_still_syncs_the_repository() -> None:
    api = code.GITHUB_API
    client = _Client({f"{api}/orgs/acme/repos": _Resp(200, [GITHUB_REPO])})
    read = asyncio.run(code.read_repositories("github", "t", api, {"organizations": "acme"}, limit=100, client=client))
    assert [r["external_id"] for r in read.rows] == ["4242"]
    assert read.software_unavailable == {"4242": code.SBOM_NOT_ENABLED}
    assert read.software["4242"] == []


def test_gitlab_dependency_list_off_tier_degrades_gracefully() -> None:
    api = "https://gitlab.acme.io/api/v4"
    project = {**GITLAB_PROJECT, "archived": False}
    client = _Client(
        {
            f"{api}/groups/acme%2Fplatform/projects": _Resp(200, [project]),
            f"{api}/projects/77/dependencies": _Resp(403, {"message": "403 Forbidden"}),
        }
    )
    read = asyncio.run(
        code.read_repositories("gitlab", "t", api, {"groups": ["acme/platform"]}, limit=100, client=client)
    )
    assert read.error is None and [r["external_id"] for r in read.rows] == ["77"]
    assert read.rows[0]["cloud_resource_id"] == "gitlab.acme.io:repository:77"
    assert read.software_unavailable == {"77": code.SBOM_TIER}


GITLAB_DEPS = [
    {
        "name": "lodash",
        "version": "4.17.20",
        "package_manager": "yarn",
        "dependency_file_path": "yarn.lock",
        "vulnerabilities": [
            {"id": 9001, "name": "Prototype pollution", "severity": "High", "url": "https://g/v/9001"},
            {"id": 9001, "name": "duplicate", "severity": "high"},
            {"name": "no id"},
        ],
    },
    {"name": "pako", "version": "2.2.0", "package_manager": "yarn", "vulnerabilities": []},
]


def test_gitlab_dependency_vulnerabilities_become_findings_on_the_repository() -> None:
    row = code.gitlab_project_row({**GITLAB_PROJECT, "archived": False}, host="gitlab.acme.io")
    findings = code.gitlab_dependency_findings(GITLAB_DEPS, project=row, host="gitlab.acme.io")
    assert len(findings) == 1  # one per vulnerability id, an id-less one skipped
    f = findings[0]
    assert f["external_id"] == "77:9001" and f["package"] == "lodash" and f["version"] == "4.17.20"
    assert f["severity"] == "high" and f["purl"] == "pkg:npm/lodash@4.17.20"
    assert f["repository"] == "acme/platform/web" and f["identity_key"] == "gitlab.acme.io:repository:77"


def test_gitlab_read_carries_the_findings_of_a_whole_list_only() -> None:
    api = "https://gitlab.acme.io/api/v4"
    project = {**GITLAB_PROJECT, "archived": False}
    client = _Client(
        {
            f"{api}/groups/acme%2Fplatform/projects": _Resp(200, [project]),
            f"{api}/projects/77/dependencies": _Resp(200, GITLAB_DEPS),
        }
    )
    read = asyncio.run(
        code.read_repositories("gitlab", "t", api, {"groups": ["acme/platform"]}, limit=100, client=client)
    )
    assert [f["external_id"] for f in read.findings["77"]] == ["77:9001"]
    off_tier = _Client(
        {
            f"{api}/groups/acme%2Fplatform/projects": _Resp(200, [project]),
            f"{api}/projects/77/dependencies": _Resp(403, {}),
        }
    )
    read = asyncio.run(
        code.read_repositories("gitlab", "t", api, {"groups": ["acme/platform"]}, limit=100, client=off_tier)
    )
    assert read.findings == {}  # not read: nothing closes


def test_github_reads_no_findings_from_the_sbom() -> None:
    api = code.GITHUB_API
    client = _Client(
        {
            f"{api}/orgs/acme/repos": _Resp(200, [GITHUB_REPO]),
            f"{api}/repos/acme/api/dependency-graph/sbom": _Resp(200, GITHUB_SBOM),
        }
    )
    read = asyncio.run(
        code.read_repositories("github", "t", api, {"organizations": ["acme"]}, limit=100, client=client)
    )
    assert read.findings == {}  # Dependabot alerts come as deliveries (slice 2)


def test_a_refused_listing_names_the_scope_to_grant() -> None:
    api = code.GITHUB_API
    client = _Client({f"{api}/user/repos": _Resp(403, {})})
    read = asyncio.run(code.read_repositories("github", "t", api, {}, limit=100, client=client))
    assert read.error and "read:org" in read.error and not read.rows


def test_dependencies_can_be_switched_off() -> None:
    api = code.GITHUB_API
    client = _Client({f"{api}/orgs/acme/repos": _Resp(200, [GITHUB_REPO])})
    read = asyncio.run(
        code.read_repositories(
            "github", "t", api, {"organizations": ["acme"], "skip_dependencies": "true"}, limit=100, client=client
        )
    )
    assert read.rows and read.software == {}
    assert not any("dependency-graph" in c for c in client.calls)


@pytest.mark.parametrize("source", ["github", "gitlab"])
def test_code_sources_are_asset_sources(source: str) -> None:
    from app.services import organization_asset_store as store
    from app.services import organization_context_import as imp

    assert source in imp.SECTION_SOURCES["assets"]
    assert imp.SOURCE_FILTERS[("assets", source)]
    assert source in store.ENDPOINT_SOFTWARE_FORMATS
