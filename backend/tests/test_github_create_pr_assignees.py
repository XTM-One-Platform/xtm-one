"""create_github_pr assigns the pull request in the same call (issue #9957).

A fake ``safe_request`` answers the title-rule read (none declared), the pull
request creation and the Issues API assignee write: the tests check that the
assignees are written right after creation and read back in the result, that a
login GitHub drops is named, that a failed assignment never fails the creation,
and that a call without assignees writes nothing more than before.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_ASSIGN_URL = "https://api.github.com/repos/acme/app/issues/12/assignees"


class _GitHub:
    def __init__(self, assigned: list[str] | None = None, assign_error: str | None = None) -> None:
        self.assigned = assigned or []
        self.assign_error = assign_error
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/contents/.xtm-one/checks.toml"):
            return None, "Error: GitHub API error (404): Not Found"
        if method == "post" and url.endswith("/pulls"):
            pr = {"number": 12, "html_url": "https://github.com/acme/app/pull/12", "head": {"ref": "f", "sha": "abc"}}
            return httpx.Response(201, json=pr), None
        if method == "post" and url == _ASSIGN_URL:
            if self.assign_error:
                return None, self.assign_error
            body = {"number": 12, "assignees": [{"login": login} for login in self.assigned]}
            return httpx.Response(201, json=body), None
        raise AssertionError(f"unexpected {method} {url}")

    def writes(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u, _ in self.calls if m in ("post", "patch")]


def _tools():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}


@pytest.mark.asyncio
async def test_assignees_are_written_after_creation_and_read_back():
    fake = _GitHub(assigned=["octocat"])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="t", head="f", base="main", assignees=[" octocat ", ""]
        )
    assert result == "PR #12 created: https://github.com/acme/app/pull/12\nAssignees: octocat"
    assert fake.writes() == [("post", "https://api.github.com/repos/acme/app/pulls"), ("post", _ASSIGN_URL)]
    assert fake.calls[-1][2]["json"] == {"assignees": ["octocat"]}


@pytest.mark.asyncio
async def test_a_login_github_drops_is_named():
    fake = _GitHub(assigned=["octocat"])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="t", head="f", base="main", assignees=["Octocat", "ghost"]
        )
    assert result.endswith(
        "Assignees: octocat (not assigned: ghost; GitHub drops a login without access to the repository)"
    )


@pytest.mark.asyncio
async def test_a_failed_assignment_keeps_the_pull_request():
    fake = _GitHub(assign_error="Error: GitHub API error (403): Forbidden")
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="t", head="f", base="main", assignees=["octocat"]
        )
    first, note = result.split("\n", 1)
    assert first == "PR #12 created: https://github.com/acme/app/pull/12"
    assert note.startswith("Not assigned (octocat): Error: GitHub API error (403): Forbidden.")
    assert "update_github_issue" in note


@pytest.mark.asyncio
async def test_no_assignees_writes_nothing_more():
    fake = _GitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](repo="acme/app", title="t", head="f", base="main")
    assert result == "PR #12 created: https://github.com/acme/app/pull/12"
    assert fake.writes() == [("post", "https://api.github.com/repos/acme/app/pulls")]
