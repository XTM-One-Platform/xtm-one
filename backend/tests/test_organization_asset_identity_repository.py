# SPDX-License-Identifier: Apache-2.0

"""One code repository across sources: the ``repository`` identity kind (#1554).

OpenAEV names a repository asset ``OpenCTI-Platform/opencti`` with no URL and no
cloud id; GitHub sends ``full_name``, ``html_url`` and the numeric id. Both are one
asset, merged on the repository path, while two GitHub repositories with different
numeric ids (a fork, a re-created one) stay apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from app.services import organization_asset_identity as ident
from app.services.organization_context import AssetEntry, AssetIdentity
from app.services.organization_context_code import github_repo_row, gitlab_project_row


def _github(repo_id: int, full_name: str) -> dict[str, object]:
    return github_repo_row(
        {
            "id": repo_id,
            "full_name": full_name,
            "name": full_name.rsplit("/", 1)[-1],
            "html_url": f"https://github.com/{full_name}",
            "visibility": "public",
            "owner": {"login": full_name.split("/", 1)[0]},
        }
    )


def _openaev(name: str, asset_id: str = "aev-1", **extra: object) -> dict[str, object]:
    return {
        "name": name,
        "category": "source_code",
        "subcategory": "code_repository",
        "source": "openaev",
        "external_id": asset_id,
        **extra,
    }


def _canon(rows: Sequence[Mapping[str, object]]) -> list[ident.Canonical]:
    return ident.dedupe_section("assets", rows)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("OpenCTI-Platform/opencti", "github.com/opencti-platform/opencti"),
        ("https://github.com/OpenCTI-Platform/opencti", "github.com/opencti-platform/opencti"),
        ("https://www.github.com/OpenCTI-Platform/opencti.git/", "github.com/opencti-platform/opencti"),
        ("git@github.com:OpenCTI-Platform/opencti.git", "github.com/opencti-platform/opencti"),
        ("ssh://git@github.com:22/OpenCTI-Platform/opencti.git", "github.com/opencti-platform/opencti"),
        ("github.com/OpenCTI-Platform/opencti", "github.com/opencti-platform/opencti"),
        ("https://github.com/OpenCTI-Platform/opencti/tree/master", "github.com/opencti-platform/opencti"),
        ("https://gitlab.acme.io/Group/Sub/Project", "gitlab.acme.io/group/sub/project"),
        ("https://gitlab.acme.io/group/sub/project/-/tree/main", "gitlab.acme.io/group/sub/project"),
        # A GitLab path without its host never takes a guessed one.
        ("group/sub/project", ""),
        ("opencti", ""),
        ("https://github.com/OpenCTI-Platform", ""),
        ("not a repo/at all", ""),
        ("../etc", ""),
    ],
)
def test_repository_normalisation(raw: str, expected: str) -> None:
    assert ident.normalize_identifier("repository", raw) == expected


def test_repository_ranks_right_after_the_cloud_resource_id() -> None:
    order = ident.DEFAULT_KEY_ORDER
    assert order.index("repository") == order.index("cloud_resource_id") + 1
    assert ident.KIND_LABELS["repository"] == "repository"
    # The identity settings accept it and a full order of every strong kind.
    assert AssetIdentity.model_validate({"key_order": list(order)}).key_order == list(order)


def test_openaev_and_github_rows_of_one_repository_are_one_asset() -> None:
    github = _github(4242, "OpenCTI-Platform/opencti")
    openaev = _openaev("OpenCTI-Platform/opencti")
    AssetEntry.model_validate(github)
    AssetEntry.model_validate(openaev)
    [asset] = _canon([github, openaev])
    assert asset.sources == ["github", "openaev"]
    assert asset.explanation() == "merged on repository"


@pytest.mark.parametrize(
    "spelling",
    [
        "opencti-platform/OpenCTI",
        "https://github.com/opencti-platform/opencti.git",
        "https://github.com/OpenCTI-Platform/opencti/",
    ],
)
def test_case_url_and_git_suffix_variants_merge(spelling: str) -> None:
    [asset] = _canon([_github(4242, "OpenCTI-Platform/opencti"), _openaev(spelling)])
    assert asset.explanation() == "merged on repository"


def test_an_openaev_row_with_a_url_is_keyed_on_it_first() -> None:
    openaev = _openaev("OpenCTI (main repo)", url="https://github.com/OpenCTI-Platform/opencti")
    [asset] = _canon([_github(4242, "OpenCTI-Platform/opencti"), openaev])
    assert asset.sources == ["github", "openaev"]


def test_two_github_repositories_with_different_ids_never_merge_on_the_path() -> None:
    """A repository deleted and re-created, or two hosts' rows: the numeric id outranks the path."""
    original = _github(4242, "OpenCTI-Platform/opencti")
    recreated = {**_github(9999, "OpenCTI-Platform/opencti"), "source": "github-mirror", "external_id": "9999"}
    assert len(_canon([original, recreated])) == 2


def test_a_renamed_repository_keeps_its_cloud_resource_id() -> None:
    before = {**_github(4242, "OpenCTI-Platform/opencti-old"), "source": "github-a"}
    after = {**_github(4242, "OpenCTI-Platform/opencti"), "source": "github-b"}
    [asset] = _canon([before, after])
    assert asset.explanation() == "merged on cloud resource id"


def test_a_fork_does_not_merge_with_its_upstream() -> None:
    upstream = _github(4242, "OpenCTI-Platform/opencti")
    fork = _github(5151, "someone/opencti")
    assert len(_canon([upstream, fork, _openaev("OpenCTI-Platform/opencti")])) == 2


def test_gitlab_subgroup_project_merges_with_a_hand_row_of_its_url() -> None:
    gitlab = gitlab_project_row(
        {
            "id": 77,
            "path_with_namespace": "Group/Sub/Project",
            "web_url": "https://gitlab.acme.io/Group/Sub/Project",
            "visibility": "private",
            "namespace": {"full_path": "Group/Sub"},
        },
        host="gitlab.acme.io",
    )
    hand = {
        "name": "Project",
        "category": "source_code",
        "subcategory": "code_repository",
        "source": "manual",
        "url": "https://gitlab.acme.io/group/sub/project.git",
    }
    [asset] = _canon([gitlab, hand])
    assert asset.sources == ["gitlab", "manual"]
    assert asset.explanation() == "merged on repository"


def test_a_row_of_another_category_never_gets_the_key() -> None:
    host = {"name": "a/b", "category": "host", "source": "intune", "external_id": "d-1"}
    pipeline = {
        "name": "a/b",
        "category": "source_code",
        "subcategory": "ci_cd_pipeline",
        "source": "manual",
        "url": "https://github.com/a/b",
    }
    for row in (host, pipeline):
        record = ident.section_record("assets", row)
        assert record.keys("repository") == set()
    assert len(_canon([host, pipeline, _openaev("a/b")])) == 3
