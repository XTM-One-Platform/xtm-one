"""manage_github_pr ready_for_review / convert_to_draft survive a transient GitHub answer.

The draft toggle is a GraphQL mutation, which the shared helper never retries
(it may have been applied). After an answer of unknown outcome (GraphQL
"Something went wrong", a gateway 5xx, a timeout, an empty body) the tool reads
the PR's ``draft`` flag back: already in the target state is answered applied
with no second mutation; otherwise the mutation is sent again, at most three
times. A definite refusal (permission, not found) is never retried.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.pulls import _draft_outcome_unknown

_TRANSIENT = "Error: GitHub GraphQL error: Something went wrong while executing your query. Please try again."


def _fn():
    ex = MagicMock()
    ex.register = MagicMock()
    build_tools("fake-token", ex)
    for call in ex.register.call_args_list:
        if call[0][0] == "manage_github_pr":
            return call[0][1]
    raise KeyError("manage_github_pr not registered")


class _GitHub:
    """Scripted GitHub: mutation answers in order, the draft flag of each PR read after the first."""

    def __init__(self, answers: list[str | None], drafts: list[bool | None]):
        self.answers = list(answers)
        self.drafts = list(drafts)
        self.mutations = 0
        self.reads = 0

    async def graphql(self, client, headers, query, variables=None, **kwargs):
        if not query.startswith("mutation"):  # the review-loop verdict read: no marker
            return {"repository": {"pullRequest": {"comments": {"nodes": []}}}}, None
        self.mutations += 1
        err = self.answers.pop(0)
        if err:
            return None, err
        return {"pr": {"pullRequest": {"isDraft": "Draft" in query}}}, None

    async def request(self, client, method, url, provider, action, **kwargs):
        assert method == "get" and url.endswith("/pulls/12")
        self.reads += 1
        if self.reads == 1:  # node id resolution
            return httpx.Response(200, json={"node_id": "PR_kwDOabc"}), None
        draft = self.drafts.pop(0) if self.drafts else None
        if draft is None:
            return None, "Error: GitHub server error (502) while trying to read back PR #12."
        return httpx.Response(200, json={"draft": draft}), None


async def _run(github: _GitHub, action: str) -> str:
    with (
        patch("app.integrations.providers.github._common.gh_graphql", side_effect=github.graphql),
        patch("app.integrations.providers.github._common.safe_request", side_effect=github.request),
        patch("app.integrations.providers.github.pulls._DRAFT_RETRY_DELAYS", (0.0, 0.0)),
    ):
        return await _fn()(repo="acme/app", number=12, action=action)


@pytest.mark.asyncio
async def test_a_transient_error_then_success_answers_ready():
    github = _GitHub([_TRANSIENT, None], drafts=[True])

    result = await _run(github, "ready_for_review")

    assert github.mutations == 2
    assert result == "PR #12: ready for review applied (attempt 2)."


@pytest.mark.asyncio
async def test_a_transient_error_whose_change_landed_is_answered_without_a_second_mutation():
    github = _GitHub([_TRANSIENT], drafts=[False])

    result = await _run(github, "ready_for_review")

    assert github.mutations == 1
    assert "ready for review applied" in result
    assert "read-back shows the PR ready for review" in result
    assert not result.startswith("Error")


@pytest.mark.asyncio
async def test_convert_to_draft_reads_back_draft_true_as_applied():
    github = _GitHub(["Error: GitHub server error (502) while trying to convert_to_draft PR #12."], drafts=[True])

    result = await _run(github, "convert_to_draft")

    assert github.mutations == 1
    assert "read-back shows the PR draft" in result


@pytest.mark.asyncio
async def test_three_transient_errors_give_up_with_the_state_named():
    github = _GitHub([_TRANSIENT, _TRANSIENT, _TRANSIENT], drafts=[True, True, None])

    result = await _run(github, "ready_for_review")

    assert github.mutations == 3
    assert result.startswith("Error")
    assert "NOT applied after 3 attempts" in result
    assert "last read-back shows the PR not read" in result


@pytest.mark.asyncio
async def test_a_definite_refusal_is_never_retried_nor_read_back():
    refusal = "Error: GitHub GraphQL error: Resource not accessible by integration"
    github = _GitHub([refusal], drafts=[])

    result = await _run(github, "ready_for_review")

    assert github.mutations == 1
    assert github.reads == 1  # the node id resolution only
    assert result == refusal


@pytest.mark.parametrize(
    ("err", "unknown"),
    [
        (_TRANSIENT, True),
        ("Error: GitHub server error (503) while trying to x. The service may be temporarily unavailable.", True),
        ("Error: GitHub request timed out while trying to x.", True),
        ("Error: GitHub returned an unexpected, empty, or non-JSON response while trying to x.", True),
        ("Error: GitHub GraphQL error: Could not resolve to a node with the global id of 'x'", False),
        ("Error: GitHub rejected the request to x — validation error.", False),
        ("Error: GitHub rate limit exceeded while trying to x.", False),
    ],
)
def test_outcome_classification(err: str, unknown: bool):
    assert _draft_outcome_unknown(err) is unknown
