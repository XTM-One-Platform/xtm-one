"""request_github_reviewers confirms a bot reviewer the REST answer leaves out.

GitHub takes a Copilot review request (``copilot-pull-request-reviewer[bot]``)
but answers it with an empty ``requested_reviewers``: the tool then reads the
pull request's review requests over GraphQL before it calls a reviewer
unconfirmed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"


def _tool(name: str):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}[name]


async def _no_self_action(*args, **kwargs):
    return None


@pytest.fixture(autouse=True)
def _quiet_self_action():
    with patch("app.integrations.providers.github.pulls.mark_self_action", new=_no_self_action, create=True):
        yield


class _GitHub:
    """Answers the REST request and the GraphQL read, and records the calls."""

    def __init__(self, rest_logins: list[str], graphql_reviewers: list[dict[str, Any]] | None):
        self.rest_logins = rest_logins
        self.graphql_reviewers = graphql_reviewers
        self.calls: list[tuple[str, str, Any]] = []
        self.kwargs: list[dict[str, Any]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        self.kwargs.append(kwargs)
        if url.endswith("/graphql"):
            if self.graphql_reviewers is None:
                return httpx.Response(502, json={}), "Error: GitHub server error (502)."
            nodes = [{"requestedReviewer": r} for r in self.graphql_reviewers]
            payload = {"data": {"repository": {"pullRequest": {"reviewRequests": {"nodes": nodes}}}}}
            return httpx.Response(200, json=payload), None
        reviewers = [{"login": login} for login in self.rest_logins]
        return httpx.Response(201, json={"requested_reviewers": reviewers}), None


@pytest.mark.asyncio
async def test_copilot_confirmed_from_the_review_requests_when_the_rest_answer_omits_it():
    gh = _GitHub([], [{"__typename": "Bot", "login": "copilot-pull-request-reviewer"}])
    with patch(_SAFE_REQUEST, new=gh):
        out = await _tool("request_github_reviewers")(repo="o/r", number=7, reviewers=["copilot"])
    assert out == "Reviewers requested on PR #7: copilot-pull-request-reviewer[bot]"
    assert gh.calls[0][2] == {"reviewers": ["copilot-pull-request-reviewer[bot]"]}
    assert gh.calls[1][1].endswith("/graphql")


@pytest.mark.asyncio
async def test_rest_answer_naming_copilot_needs_no_graphql_read():
    gh = _GitHub(["Copilot"], None)
    with patch(_SAFE_REQUEST, new=gh):
        out = await _tool("request_github_reviewers")(repo="o/r", number=7, reviewers=["copilot"])
    assert out.startswith("Reviewers requested on PR #7")
    assert len(gh.calls) == 1


@pytest.mark.asyncio
async def test_login_case_does_not_make_a_reviewer_unconfirmed():
    gh = _GitHub(["octocat"], None)
    with patch(_SAFE_REQUEST, new=gh):
        out = await _tool("request_github_reviewers")(repo="o/r", number=7, reviewers=["OctoCat"])
    assert out == "Reviewers requested on PR #7: OctoCat"
    assert len(gh.calls) == 1


@pytest.mark.asyncio
async def test_a_reviewer_missing_from_both_reads_is_still_reported():
    gh = _GitHub([], [{"__typename": "User", "login": "alice"}])
    with patch(_SAFE_REQUEST, new=gh):
        out = await _tool("request_github_reviewers")(repo="o/r", number=7, reviewers=["alice", "bob"])
    assert out.startswith("Warning: reviewers requested on PR #7 but these were NOT confirmed by GitHub: bob.")
    assert "Confirmed reviewers: alice" in out


@pytest.mark.asyncio
async def test_a_failed_graphql_read_keeps_the_rest_verdict():
    gh = _GitHub([], None)
    with patch(_SAFE_REQUEST, new=gh):
        out = await _tool("request_github_reviewers")(repo="o/r", number=7, reviewers=["copilot"])
    assert "NOT confirmed by GitHub: copilot-pull-request-reviewer[bot]" in out
    assert "Confirmed reviewers: none" in out


@pytest.mark.asyncio
async def test_the_review_request_is_retried_on_the_404_of_a_just_created_pull_request():
    gh = _GitHub(["octocat"], None)
    with patch(_SAFE_REQUEST, new=gh):
        await _tool("request_github_reviewers")(repo="o/r", number=7, reviewers=["octocat"])
    sent = gh.kwargs[0]
    assert 404 in sent["retry_statuses"]
    assert {502, 503, 504} <= set(sent["retry_statuses"])
    assert 0 < sent["max_total_seconds"] <= 30


@pytest.mark.asyncio
async def test_safe_request_retries_a_listed_404_then_answers_the_success(monkeypatch):
    from app.integrations import http as integration_http

    monkeypatch.setattr(integration_http.asyncio, "sleep", _no_self_action)
    answers = iter([httpx.Response(404, json={"message": "Not Found"}), httpx.Response(201, json={})])
    transport = httpx.MockTransport(lambda request: next(answers))
    async with httpx.AsyncClient(transport=transport) as client:
        resp, err = await integration_http.safe_request(
            client,
            "post",
            "https://api.github.com/repos/o/r/pulls/7/requested_reviewers",
            "github",
            "request reviewers",
            json={"reviewers": ["octocat"]},
            retry_statuses=frozenset({404}),
            max_total_seconds=15.0,
        )
    assert err is None
    assert resp.status_code == 201
