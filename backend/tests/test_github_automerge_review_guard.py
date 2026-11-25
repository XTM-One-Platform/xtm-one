"""Auto-merge is never enabled on a pull request that still waits for its review.

GitHub auto-merge (and the merge queue it feeds) lands a pull request the
moment its checks pass; with an admin bypass it even skips a required review.
``manage_github_pr`` action=enable_automerge therefore reads ``reviewDecision``
first and refuses REVIEW_REQUIRED and CHANGES_REQUESTED (or an unreadable
decision); APPROVED or no review rule (``None``) lets it through. The raw
``github_graphql_query`` refuses the merge mutations outright, so the guard
cannot be bypassed by hand.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.graphql import guarded_merge_refusal


def _tool(tool_name: str):
    ex = MagicMock()
    ex.register = MagicMock()
    build_tools("fake-token", ex)
    for call in ex.register.call_args_list:
        if call[0][0] == tool_name:
            return call[0][1]
    raise KeyError(tool_name)


class _GitHub:
    """Scripted GitHub: no review-loop marker, a scripted review decision, every mutation recorded."""

    def __init__(self, decision: str | None, read_error: str | None = None, bypass: bool | None = None):
        self.decision = decision
        self.read_error = read_error
        self.bypass = bypass
        self.mutations: list[str] = []

    async def graphql(self, client, headers, query, variables=None, **kwargs):
        if query.startswith("mutation"):
            self.mutations.append(query)
            return {"enablePullRequestAutoMerge": {"pullRequest": {"number": 12}}}, None
        if "reviewDecision" in query:
            if self.read_error:
                return None, self.read_error
            pull: dict[str, object] = {"reviewDecision": self.decision}
            if self.bypass is not None:
                pull["viewerCanMergeAsAdmin"] = self.bypass
            return {"repository": {"pullRequest": pull}}, None
        return {"repository": {"pullRequest": {"comments": {"nodes": []}}}}, None

    async def request(self, client, method, url, provider, action, **kwargs):
        if method == "get" and url.endswith("/pulls/12"):
            return httpx.Response(200, json={"node_id": "PR_x"}), None
        return httpx.Response(200, json={}), None


async def _enable(github: _GitHub):
    with (
        patch("app.integrations.providers.github._common.gh_graphql", side_effect=github.graphql),
        patch("app.integrations.providers.github._common.safe_request", side_effect=github.request),
    ):
        return await _tool("manage_github_pr")(repo="acme/app", number=12, action="enable_automerge")


@pytest.mark.parametrize("decision", ["REVIEW_REQUIRED", "CHANGES_REQUESTED"])
@pytest.mark.parametrize("bypass", [False, None])
@pytest.mark.asyncio
async def test_automerge_is_refused_while_the_review_is_owed_and_the_viewer_cannot_bypass(
    decision: str, bypass: bool | None
) -> None:
    github = _GitHub(decision, bypass=bypass)
    result = str(await _enable(github))
    assert github.mutations == []
    assert "auto-merge not enabled on PR #12, nothing was written" in result
    assert decision in result


@pytest.mark.parametrize("decision", ["REVIEW_REQUIRED", "CHANGES_REQUESTED"])
@pytest.mark.asyncio
async def test_automerge_lets_github_decide_when_the_viewer_can_bypass(decision: str) -> None:
    """viewerCanMergeAsAdmin true: the review decision alone never refuses (#2324)."""
    github = _GitHub(decision, bypass=True)
    result = str(await _enable(github))
    assert len(github.mutations) == 1
    assert "Auto-merge (squash) enabled on PR #12" in result


@pytest.mark.asyncio
async def test_an_unreadable_decision_is_refused():
    github = _GitHub(None, read_error="Error: GitHub GraphQL error: boom")
    result = str(await _enable(github))
    assert github.mutations == []
    assert "could not be read" in result


@pytest.mark.parametrize("decision", ["APPROVED", None])
@pytest.mark.asyncio
async def test_automerge_goes_through_once_approved_or_without_a_review_rule(decision):
    github = _GitHub(decision)
    result = str(await _enable(github))
    assert len(github.mutations) == 1
    assert "enablePullRequestAutoMerge" in github.mutations[0]
    assert "Auto-merge (squash) enabled on PR #12" in result


@pytest.mark.parametrize(
    "document",
    [
        "mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id}){clientMutationId}}",
        'mutation { a: enqueuePullRequest(input: {pullRequestId: "x"}) { clientMutationId } }',
        'mutation M { mergePullRequest(input: {pullRequestId: "x"}) { clientMutationId } }',
    ],
)
def test_the_raw_tool_refuses_merge_mutations(document):
    refusal = guarded_merge_refusal(document)
    assert refusal is not None
    assert "nothing was written" in refusal


@pytest.mark.parametrize(
    "document",
    [
        'query { repository(owner: "a", name: "b") { pullRequest(number: 1) { autoMergeRequest { enabledAt } } } }',
        'mutation { addComment(input: {subjectId: "x", body: "enablePullRequestAutoMerge"}) { clientMutationId } }',
        'mutation { addLabelsToLabelable(input: {labelableId: "x", labelIds: []}) { clientMutationId } }',
    ],
)
def test_queries_strings_and_other_mutations_pass(document):
    assert guarded_merge_refusal(document) is None


@pytest.mark.asyncio
async def test_github_graphql_query_sends_nothing_for_a_guarded_mutation():
    sent: list[str] = []

    async def graphql(client, headers, query, variables=None, **kwargs):
        sent.append(query)
        return {}, None

    with patch("app.integrations.providers.github._common.gh_graphql", side_effect=graphql):
        result = await _tool("github_graphql_query")(
            query="mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id}){clientMutationId}}",
            variables='{"id": "PR_x"}',
        )
    assert sent == []
    assert "mutation not sent" in result
