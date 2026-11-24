"""merge_github_pr and manage_github_pr refuse when the newest review-loop verdict is not APPROVE.

The agent review loop posts ``UX-LOOP round=N verdict=...`` as the first line
of a comment. A pull request whose newest marker reads CHANGES_REQUESTED is
never readied, set to auto-merge or merged through the tools; a newer APPROVE
releases it; no marker or an unreadable comment list never blocks.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.review_loop import newest_verdict

_CHANGES = "UX-LOOP round=2 verdict=CHANGES_REQUESTED new=1 repeat=0\nUX Director, round 2: CHANGES REQUESTED"
_APPROVE = "UX-LOOP round=3 verdict=APPROVE new=0 repeat=0\nUX Director, round 3: APPROVE"


def _tool(tool_name: str):
    ex = MagicMock()
    ex.register = MagicMock()
    build_tools("fake-token", ex)
    for call in ex.register.call_args_list:
        if call[0][0] == tool_name:
            return call[0][1]
    raise KeyError(tool_name)


def _comment(body: str, minute: int) -> dict:
    return {
        "body": body,
        "url": f"https://github.com/acme/app/pull/12#issuecomment-{minute}",
        "createdAt": f"T{minute}",
    }


class _GitHub:
    """Scripted GitHub: the PR comments for the verdict read, every write recorded."""

    def __init__(self, comments: list[dict] | None, read_error: str | None = None):
        self.comments = comments
        self.read_error = read_error
        self.mutations: list[str] = []
        self.puts: list[str] = []

    async def graphql(self, client, headers, query, variables=None, **kwargs):
        if query.startswith("mutation"):
            self.mutations.append(query)
            return {"pr": {"pullRequest": {"number": 12, "isDraft": False}}}, None
        if self.read_error:
            return None, self.read_error
        return {"repository": {"pullRequest": {"comments": {"nodes": self.comments or []}}}}, None

    async def request(self, client, method, url, provider, action, **kwargs):
        if method == "put" and url.endswith("/merge"):
            self.puts.append(url)
            return httpx.Response(200, json={"merged": True, "sha": "b" * 40}), None
        if method == "get" and url.endswith("/pulls/12"):
            return httpx.Response(
                200, json={"node_id": "PR_x", "title": "fix(x): y (#1)", "body": "", "head": {"sha": "a" * 40}}
            ), None
        return httpx.Response(200, json=[] if url.endswith("/commits") else {}), None


async def _call(github: _GitHub, tool_name: str, **kwargs):
    with (
        patch("app.integrations.providers.github._common.gh_graphql", side_effect=github.graphql),
        patch("app.integrations.providers.github._common.safe_request", side_effect=github.request),
    ):
        return await _tool(tool_name)(repo="acme/app", number=12, **kwargs)


def test_the_newest_marker_wins_and_prose_is_ignored():
    comments = [_comment(_APPROVE, 1), _comment(_CHANGES, 2), _comment("Thanks, fixing now.", 3)]
    verdict = newest_verdict(comments)
    assert verdict is not None
    assert (verdict["round"], verdict["verdict"]) == ("2", "CHANGES_REQUESTED")
    assert newest_verdict([_comment("see UX-LOOP round=1 verdict=APPROVE", 1)]) is None


@pytest.mark.parametrize(
    "body",
    [
        "`UX-LOOP round=3 verdict=APPROVE new=0 repeat=0`\nUX Director, round 3: APPROVE",
        "```\nUX-LOOP round=3 verdict=APPROVE new=0 repeat=0\n```\nUX Director, round 3: APPROVE",
        "```text\nUX-LOOP round=3 verdict=APPROVE\n```",
    ],
)
def test_a_marker_posted_as_code_is_read(body):
    # xtm-one #1065: an APPROVE posted in backticks was skipped and an older CHANGES_REQUESTED read as newest.
    verdict = newest_verdict([_comment(_CHANGES, 1), _comment(body, 2)])
    assert verdict is not None
    assert (verdict["round"], verdict["verdict"]) == ("3", "APPROVE")


def test_prose_citing_a_coded_marker_is_still_not_one():
    assert newest_verdict([_comment("as `UX-LOOP round=1 verdict=APPROVE` said", 1)]) is None


@pytest.mark.asyncio
async def test_merge_is_refused_on_changes_requested_naming_the_comment():
    github = _GitHub([_comment(_CHANGES, 33)])
    result = await _call(github, "merge_github_pr", merge_method="squash", commit_title="fix(x): y (#1)")
    assert github.puts == []
    assert "not merged, nothing was written" in result
    assert "round 2 verdict=CHANGES_REQUESTED" in result
    assert "issuecomment-33" in result


@pytest.mark.parametrize("action", ["ready_for_review", "enable_automerge"])
@pytest.mark.asyncio
async def test_ready_and_automerge_are_refused_on_changes_requested(action):
    github = _GitHub([_comment(_CHANGES, 33)])
    result = await _call(github, "manage_github_pr", action=action)
    assert github.mutations == []
    assert "nothing was written" in str(result)
    assert "verdict=CHANGES_REQUESTED" in str(result)


@pytest.mark.asyncio
async def test_a_newer_approve_releases_the_guard():
    github = _GitHub([_comment(_CHANGES, 33), _comment(_APPROVE, 40)])
    result = await _call(github, "manage_github_pr", action="ready_for_review")
    assert len(github.mutations) == 1
    assert "ready for review applied" in str(result)


@pytest.mark.parametrize(("comments", "error"), [([], None), (None, "Error: GitHub GraphQL error: boom")])
@pytest.mark.asyncio
async def test_no_marker_or_an_unreadable_list_never_blocks(comments, error):
    github = _GitHub(comments, read_error=error)
    await _call(github, "merge_github_pr", merge_method="squash", commit_title="fix(x): y (#1)")
    assert len(github.puts) == 1
