"""A completing pull request must close its issue: refused at write, named at merge.

A last slice that writes ``Refs #N (slice 4 of 4)`` merged the work and left
the issue open. ``create_github_pr`` / ``update_github_pr`` refuse such a body
with nothing written; ``merge_github_pr`` ends its answer with the referenced
issues still open. A fake ``safe_request`` answers the kind reads
(``/issues/N``), the PR read, the GraphQL issue read and the writes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.issue_close import completing_refs

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

_PULLS = {("acme/app", 5)}
_ISSUES = {("acme/app", 7), ("acme/app", 8)}


class _GitHub:
    """Kind reads from _PULLS / _ISSUES, the PR read with *pr*, GraphQL with *issues*, writes recorded."""

    def __init__(self, pr: dict[str, Any] | None = None, issues: dict[int, dict[str, Any]] | None = None) -> None:
        self.pr = pr or {"title": "fix: a thing (#7)", "body": ""}
        self.issues = issues or {}
        self.calls: list[tuple[str, str]] = []
        self.queries: list[str] = []

    async def __call__(
        self, client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[Any, str | None]:
        self.calls.append((method, url))
        if method == "get" and "/issues/" in url:
            repo, raw_number = url.split("/repos/")[1].split("/issues/")
            key = (repo, int(raw_number))
            if key in _PULLS:
                return httpx.Response(200, json={"number": key[1], "pull_request": {"url": "x"}}), None
            if key in _ISSUES:
                return httpx.Response(200, json={"number": key[1]}), None
            return None, "Error: GitHub API error (404): Not Found"
        if method == "get" and url.endswith("/contents/.xtm-one/checks.toml"):
            return None, "Error: GitHub API error (404): Not Found"
        if method == "get" and "/contents/" in url:
            return None, "Error: GitHub API error (404): Not Found"
        if method == "get" and url.endswith("/commits"):
            return httpx.Response(200, json=[]), None
        if method == "get" and "/commits/" in url:
            return httpx.Response(200, json={"commit": {"verification": {"verified": True, "reason": "valid"}}}), None
        if method == "get" and "/pulls/" in url:
            return httpx.Response(200, json=self.pr), None
        if method == "post" and url.endswith("/pulls"):
            pr = {"number": 12, "html_url": "https://github.com/acme/app/pull/12", "head": {"ref": "f", "sha": "a"}}
            return httpx.Response(201, json=pr), None
        if method == "patch":
            return httpx.Response(200, json={"state": "open"}), None
        if method == "put" and url.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "message": "Pull Request successfully merged"}), None
        if method == "post" and url.endswith("/graphql"):
            query = str((kwargs.get("json") or {}).get("query") or "")
            if "issueOrPullRequest" not in query:  # the review-loop verdict read
                return httpx.Response(
                    200, json={"data": {"repository": {"pullRequest": {"comments": {"nodes": []}}}}}
                ), None
            self.queries.append(query)
            block: dict[str, Any] = {"nameWithOwner": "acme/app"}
            for issue_number, node in self.issues.items():
                block[f"i{issue_number}"] = node
            return httpx.Response(200, json={"data": {"r0": block}}), None
        if method == "get":
            return None, "Error: GitHub API error (404): Not Found"
        raise AssertionError(f"unexpected {method} {url}")

    def writes(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u in self.calls if m in ("post", "patch", "put") and not u.endswith("/graphql")]


def _tools() -> dict[str, Any]:
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}


def _issue(state: str, title: str, open_prs: int = 0) -> dict[str, Any]:
    nodes = [
        {"source": {"number": 100 + i, "state": "OPEN", "repository": {"nameWithOwner": "acme/app"}}}
        for i in range(open_prs)
    ]
    nodes.append({"source": {"number": 99, "state": "MERGED", "repository": {"nameWithOwner": "acme/app"}}})
    return {"__typename": "Issue", "state": state, "title": title, "timelineItems": {"nodes": nodes}}


def test_completing_refs_reads_the_last_slice_markers() -> None:
    assert completing_refs("Refs #7 (slice 4 of 4)", "acme/app") == [("acme/app", 7)]
    assert completing_refs("Refs #7 (S4 of 4)", "acme/app") == [("acme/app", 7)]
    assert completing_refs("This is the last slice.\n\nRefs #7", "acme/app") == [("acme/app", 7)]
    assert completing_refs("Completes #7.", "acme/app") == [("acme/app", 7)]
    assert completing_refs("Part of #7, the final part of the program", "acme/app") == [("acme/app", 7)]


def test_completing_refs_lets_an_earlier_slice_and_a_closing_keyword_pass() -> None:
    assert completing_refs("Refs #7 (S2 of 4)", "acme/app") == []
    assert completing_refs("Refs #7 (slice 2 of 4)", "acme/app") == []
    assert completing_refs("Closes #7 (S4 of 4)", "acme/app") == []
    assert completing_refs("Refs #7 for context.", "acme/app") == []
    assert completing_refs("", "acme/app") == []


@pytest.mark.asyncio
async def test_create_refuses_a_last_slice_written_refs() -> None:
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="feat: the end (#7)", head="f", base="main", body="Refs #7 (slice 4 of 4)"
        )
    assert result.startswith("Error: create PR refused, nothing was written")
    assert "acme/app#7 is an issue: write 'Closes #7'" in result
    assert fake.writes() == []


@pytest.mark.asyncio
async def test_create_passes_an_earlier_slice_and_a_closing_last_slice() -> None:
    for body in ("Refs #7 (S2 of 4)", "Closes #7 (S4 of 4)"):
        fake = _GitHub()
        with patch(_SAFE_REQUEST, new=fake):
            result = await _tools()["create_github_pr"](
                repo="acme/app", title="feat: a slice (#7)", head="f", base="main", body=body
            )
        assert result.startswith("PR #12 created"), (body, result)


@pytest.mark.asyncio
async def test_a_last_slice_naming_a_pull_request_is_not_refused_here() -> None:
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="feat: the end (#7)", head="f", base="main", body="Closes #7. Last slice after #5."
        )
    assert result.startswith("PR #12 created")


@pytest.mark.asyncio
async def test_update_refuses_a_last_slice_body() -> None:
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["update_github_pr"](repo="acme/app", number=12, body="Final slice. Refs #8")
    assert result.startswith("Error: update PR #12 refused")
    assert "acme/app#8 is an issue" in result
    assert fake.writes() == []


@pytest.mark.asyncio
async def test_merge_names_a_referenced_issue_left_open() -> None:
    fake = _GitHub(
        {"title": "feat: the end (#7)", "body": "Refs #7", "base": {"ref": "main", "repo": {"default_branch": "main"}}},
        {7: _issue("OPEN", "the program", open_prs=0)},
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](repo="acme/app", number=12, merge_method="squash")
    assert "merged (squash)" in result
    assert "Still open after this merge: #7 the program (0 open PRs reference it)." in result
    assert "update_github_issue (state_reason completed)" in result
    assert fake.writes() == [("put", "https://api.github.com/repos/acme/app/pulls/12/merge")]


@pytest.mark.asyncio
async def test_merge_with_closes_reads_nothing_and_adds_no_line() -> None:
    fake = _GitHub(
        {"title": "feat: the end", "body": "Closes #7", "base": {"ref": "main", "repo": {"default_branch": "main"}}},
        {7: _issue("CLOSED", "the program")},
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](repo="acme/app", number=12, merge_method="squash")
    assert "merged (squash)" in result
    assert "Still open" not in result
    assert fake.queries == []


@pytest.mark.asyncio
async def test_merge_into_another_branch_names_the_closed_by_keyword_issue() -> None:
    fake = _GitHub(
        {"title": "feat: x", "body": "Closes #7", "base": {"ref": "feature/x", "repo": {"default_branch": "main"}}},
        {7: _issue("OPEN", "the program", open_prs=1)},
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](repo="acme/app", number=12, merge_method="squash")
    assert "Still open after this merge: #7 the program (1 open PRs reference it)." in result
    assert "GitHub closes nothing on a merge into 'feature/x'" in result
