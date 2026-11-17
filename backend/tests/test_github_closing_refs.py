"""A closing keyword that points at a pull request is refused before a PR is written or merged.

GitHub closes the target of ``fix owner/repo#N`` when the PR carrying it merges;
aimed at another pull request it silently closes that PR. A fake
``safe_request`` answers the kind reads (``/issues/N``), the PR read and the
writes: the tests check the parser, that each of create / update / merge
refuses with nothing written, that an issue reference and an unreadable one go
on as before, and that the refusal names the reference.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.closing_refs import closing_refs

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

_PULLS = {("acme/other", 3740), ("acme/app", 5)}
_ISSUES = {("acme/app", 7), ("acme/other", 9)}


class _GitHub:
    """Answer kind reads from _PULLS / _ISSUES (else 404), the PR read with *pr*, and record writes."""

    def __init__(self, pr: dict | None = None, commits: list | None = None) -> None:
        self.pr = pr or {"title": "fix: a thing (#7)", "body": ""}
        self.commits = commits if commits is not None else []
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url))
        if method == "get" and "/issues/" in url:
            repo, number = url.split("/repos/")[1].split("/issues/")
            key = (repo, int(number))
            if key in _PULLS:
                return httpx.Response(200, json={"number": key[1], "pull_request": {"url": "x"}}), None
            if key in _ISSUES:
                return httpx.Response(200, json={"number": key[1]}), None
            return None, "Error: GitHub API error (404): Not Found"
        if method == "get" and url.endswith("/contents/.xtm-one/checks.toml"):
            return None, "Error: GitHub API error (404): Not Found"
        if method == "get" and url.endswith("/commits"):
            if self.commits is None:
                return None, "Error: GitHub API error (502): Bad Gateway"
            return httpx.Response(200, json=self.commits), None
        if method == "get" and "/pulls/" in url:
            return httpx.Response(200, json=self.pr), None
        if method == "post" and url.endswith("/pulls"):
            pr = {"number": 12, "html_url": "https://github.com/acme/app/pull/12", "head": {"ref": "f", "sha": "a"}}
            return httpx.Response(201, json=pr), None
        if method == "patch":
            return httpx.Response(200, json={"state": "open"}), None
        if method == "put" and url.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "message": "Pull Request successfully merged"}), None
        if method == "post" and url.endswith("/graphql"):  # the review-loop verdict read: no marker
            return httpx.Response(
                200, json={"data": {"repository": {"pullRequest": {"comments": {"nodes": []}}}}}
            ), None
        raise AssertionError(f"unexpected {method} {url}")

    def writes(self) -> list[tuple[str, str]]:
        # The GraphQL post is the review-loop verdict read, never a write.
        return [(m, u) for m, u in self.calls if m in ("post", "patch", "put") and not u.endswith("/graphql")]


def _tools():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}


def test_closing_refs_reads_every_keyword_form():
    text = (
        "Closes #7. Pool fix acme/other#3740 awaiting deploy. Resolved: #7 again.\n"
        "fixes https://github.com/acme/other/pull/12 and closed https://github.com/acme/other/issues/9.\n"
        "See #8, prefix#9 and suffix-fix #10 are not closing; nor is fix(scope): #11 or hotfix #12."
    )
    assert closing_refs(text, "acme/app") == [
        ("acme/app", 7, None),
        ("acme/other", 3740, None),
        ("acme/other", 12, "pull"),
        ("acme/other", 9, "issues"),
    ]


def test_a_conventional_title_carries_no_closing_reference():
    assert closing_refs("fix(work-items): dispatch an item at once (#7196)", "acme/app") == []
    assert closing_refs("", "acme/app") == []


@pytest.mark.asyncio
async def test_create_refuses_a_cross_repository_pull_request_with_nothing_written():
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app",
            title="fix: a thing (#7)",
            head="f",
            base="main",
            body="Closes #7\n\nPool fix acme/other#3740 awaiting deploy.",
        )
    assert result.startswith("Error: create PR refused, nothing was written")
    assert "body: acme/other#3740 is a pull request" in result
    assert "acme/app#7" not in result
    assert fake.writes() == []


@pytest.mark.asyncio
async def test_create_with_issue_references_only_goes_on():
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="fix: a thing (#7)", head="f", base="main", body="Closes #7, fixes acme/other#9"
        )
    assert result.startswith("PR #12 created")
    assert fake.writes() == [("post", "https://api.github.com/repos/acme/app/pulls")]


@pytest.mark.asyncio
async def test_an_unreadable_reference_never_blocks():
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="fix: a thing (#7)", head="f", base="main", body="Fixes private/repo#1"
        )
    assert result.startswith("PR #12 created")


@pytest.mark.asyncio
async def test_a_pull_url_is_refused_without_a_read():
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["update_github_pr"](
            repo="acme/app", number=12, body="Resolves https://github.com/acme/other/pull/44"
        )
    assert result.startswith("Error: update PR #12 refused")
    assert "acme/other#44 is a pull request" in result
    assert fake.calls == []


@pytest.mark.asyncio
async def test_update_refuses_a_same_repository_pull_request():
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["update_github_pr"](repo="acme/app", number=12, body="Fixes #5")
    assert "acme/app#5 is a pull request" in result
    assert fake.writes() == []


@pytest.mark.asyncio
async def test_update_of_state_alone_reads_nothing():
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["update_github_pr"](repo="acme/app", number=12, state="closed")
    assert "updated" in result
    assert fake.writes() == [("patch", "https://api.github.com/repos/acme/app/pulls/12")]


@pytest.mark.asyncio
async def test_merge_refuses_when_the_pr_body_closes_another_pull_request():
    fake = _GitHub({"title": "fix: x (#7)", "body": "Closes #7\nNot run: Pool fix acme/other#3740 awaiting deploy."})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](repo="acme/app", number=12, merge_method="squash")
    assert result.startswith("Error: merge PR #12 refused, nothing was written")
    assert "pull request body: acme/other#3740 is a pull request" in result
    assert fake.writes() == []


@pytest.mark.asyncio
async def test_merge_refuses_a_commit_message_closing_a_pull_request():
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](
            repo="acme/app", number=12, merge_method="squash", commit_message="fixes #5"
        )
    assert "commit_message: acme/app#5 is a pull request" in result
    assert fake.writes() == []


@pytest.mark.asyncio
async def test_merge_with_an_issue_reference_merges():
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](repo="acme/app", number=12, merge_method="squash")
    assert "merged (squash)" in result
    assert fake.writes() == [("put", "https://api.github.com/repos/acme/app/pulls/12/merge")]


def _commit(sha: str, message: str) -> dict:
    return {"sha": sha, "commit": {"message": message}}


_COMMITS_URL = "https://api.github.com/repos/acme/app/pulls/12/commits"


@pytest.mark.asyncio
async def test_default_squash_refuses_a_commit_message_closing_a_pull_request():
    """No commit_message: GitHub builds the squash message from the commits, so they are checked."""
    fake = _GitHub(
        commits=[
            _commit("a" * 40, "feat: first slice (#7)"),
            _commit("b1c2d3e4f5a6" + "0" * 28, "fix: second slice (#7)\n\nPool fix acme/other#3740 awaiting deploy"),
        ]
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](
            repo="acme/app", number=12, merge_method="squash", commit_title="fix: a thing (#7)"
        )
    assert result.startswith("Error: merge PR #12 refused, nothing was written")
    assert "commit b1c2d3e4f5a6: acme/other#3740 is a pull request" in result
    assert "commit aaaaaaaaaaaa" not in result
    assert ("get", _COMMITS_URL) in fake.calls
    assert fake.writes() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("merge_method", ["merge", "rebase"])
async def test_merge_and_rebase_check_the_commit_messages(merge_method):
    """A merge commit or a rebase lands the PR's commits as they are, whatever commit_message says."""
    fake = _GitHub(commits=[_commit("c" * 40, "Fixes #5")])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](
            repo="acme/app", number=12, merge_method=merge_method, commit_message="Closes #7"
        )
    assert "commit cccccccccccc: acme/app#5 is a pull request" in result
    assert fake.writes() == []


@pytest.mark.asyncio
async def test_squash_with_a_commit_message_does_not_read_the_commits():
    """An explicit squash message replaces the commit list: nothing to read, the merge goes on."""
    fake = _GitHub(commits=[_commit("d" * 40, "Fixes #5")])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](
            repo="acme/app", number=12, merge_method="squash", commit_message="Closes #7"
        )
    assert "merged (squash)" in result
    assert ("get", _COMMITS_URL) not in fake.calls
    assert fake.writes() == [("put", "https://api.github.com/repos/acme/app/pulls/12/merge")]


@pytest.mark.asyncio
async def test_default_squash_with_issue_references_in_commits_merges():
    fake = _GitHub(commits=[_commit("e" * 40, "fix: a thing\n\nCloses #7, fixes acme/other#9")])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](repo="acme/app", number=12, merge_method="squash")
    assert "merged (squash)" in result


@pytest.mark.asyncio
async def test_unreadable_commits_never_block_the_merge():
    fake = _GitHub()
    fake.commits = None
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["merge_github_pr"](repo="acme/app", number=12, merge_method="squash")
    assert "merged (squash)" in result
