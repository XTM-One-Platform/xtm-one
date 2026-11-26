"""merge_github_pr names the real cause of a refusal, read from GitHub.

A 405 on the merge endpoint is read back over GraphQL (draft, head, merge
state, review decision, merge queue of the base): a base protected by a merge
queue gets the PR enqueued (answered queued, with its position), a PR already
queued is said so, and every other cause is named with one fix, never the
blanket "update the branch". A head that moved names both SHAs and that the
green claim must be read again. The GitHub answers below are recorded shapes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_HEAD = "a" * 40
_NEW_HEAD = "c" * 40
_NOT_MERGEABLE = (405, {"message": "Pull Request is not mergeable"})
_RULES = (405, {"message": "Repository rule violations found\n\nChanges must be made through the merge queue"})


def _fn():
    ex = MagicMock()
    ex.register = MagicMock()
    build_tools("fake-token", ex)
    for call in ex.register.call_args_list:
        if call[0][0] == "merge_github_pr":
            return call[0][1]
    raise KeyError("merge_github_pr not registered")


def _resp(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _facts(
    *,
    queue: bool = False,
    in_queue: bool = False,
    state: str = "CLEAN",
    review: str | None = "APPROVED",
    draft: bool = False,
    head: str = _HEAD,
    bypass: bool | None = None,
) -> dict:
    entry = {"position": 3, "state": "QUEUED"} if in_queue else None
    pull: dict[str, Any] = {
        "id": "PR_node",
        "isDraft": draft,
        "headRefOid": head,
        "mergeStateStatus": state,
        "reviewDecision": review,
        "isInMergeQueue": in_queue,
        "mergeQueueEntry": entry,
    }
    if bypass is not None:
        pull["viewerCanMergeAsAdmin"] = bypass
    return {
        "data": {
            "repository": {
                "mergeQueue": {"id": "MQ_1"} if queue else None,
                "pullRequest": pull,
            }
        }
    }


class _GitHub:
    """A scripted GitHub: the merge PUT, the PR reads, the GraphQL read and enqueue."""

    def __init__(
        self,
        merge_answer: tuple[int, dict[str, Any]],
        facts: dict[str, Any] | None = None,
        enqueue: dict[str, Any] | None = None,
        heads: list[str] | None = None,
    ) -> None:
        self.merge_answer = merge_answer
        self.facts = facts
        self.enqueue = enqueue
        self.heads = list(heads or [])
        self.puts: list[dict] = []
        self.graphql: list[dict] = []
        self.reads = 0

    async def handle(self, client, method, url, provider, action, **kwargs):
        if method == "put" and url.endswith("/merge"):
            self.puts.append(dict(kwargs.get("json") or {}))
            status, body = self.merge_answer
            return _resp(body, status), f"Error: GitHub API error ({status}): {body.get('message', '')}"
        if method == "post" and url.endswith("/graphql"):
            body = kwargs.get("json") or {}
            self.graphql.append(body)
            if "enqueuePullRequest" in body.get("query", ""):
                return _resp(self.enqueue or {"errors": [{"message": "not allowed"}]}), None
            return _resp(self.facts or {}), None
        if method == "get" and url.endswith("/pulls/12"):
            self.reads += 1
            index = self.reads - 2
            head = self.heads[min(index, len(self.heads) - 1)] if self.heads and index >= 0 else _HEAD
            pr = {
                "title": "fix(x): y (#1)",
                "body": "Closes #1",
                "head": {"sha": head},
                "base": {"ref": "main"},
                "merged": False,
                "mergeable": True,
                "mergeable_state": "clean",
            }
            return _resp(pr), None
        if url.endswith("/commits") or "/commits?" in url:
            return _resp([]), None
        return _resp({}), None


async def _run(github: _GitHub) -> str:
    with patch("app.integrations.providers.github._common.safe_request", side_effect=github.handle):
        return await _fn()(repo="acme/app", number=12, merge_method="squash", commit_title="fix(x): y (#1)")


@pytest.mark.asyncio
async def test_a_base_protected_by_a_merge_queue_gets_the_pr_enqueued_pinned_to_the_head():
    enqueue = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 2, "state": "QUEUED"}}}}
    github = _GitHub(_RULES, facts=_facts(queue=True), enqueue=enqueue)

    result = await _run(github)

    assert result.startswith("PR #12 in acme/app queued")
    assert "position 2" in result
    assert "update_branch" not in result
    mutation = github.graphql[-1]
    assert "enqueuePullRequest" in mutation["query"]
    assert mutation["variables"] == {"id": "PR_node", "head": _HEAD}
    facts_read = next(g for g in github.graphql if "mergeQueue(branch" in g["query"])
    assert facts_read["variables"]["base"] == "main"
    assert len(github.puts) == 1


def _with_timeline(facts: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    facts["data"]["repository"]["pullRequest"]["timelineItems"] = {"nodes": nodes}
    return facts


_EJECTED = {"__typename": "RemovedFromMergeQueueEvent", "createdAt": "2026-11-24T19:03:14Z", "reason": "failed_checks"}


@pytest.mark.asyncio
async def test_a_head_ejected_for_failing_checks_is_not_enqueued_again() -> None:
    facts = _with_timeline(_facts(queue=True), [{"__typename": "PullRequestCommit"}, _EJECTED])
    github = _GitHub(_RULES, facts=facts, enqueue={"data": {"enqueuePullRequest": {"mergeQueueEntry": {}}}})

    result = await _run(github)

    assert result.startswith("Error: PR #12 in acme/app is NOT merged and was NOT added to the merge queue")
    assert "2026-11-24T19:03:14Z" in result
    assert _HEAD[:12] in result
    assert "merge_group" in result
    assert not any("enqueuePullRequest" in g["query"] for g in github.graphql)


@pytest.mark.asyncio
async def test_a_commit_after_the_ejection_lets_the_head_be_enqueued() -> None:
    facts = _with_timeline(_facts(queue=True), [_EJECTED, {"__typename": "PullRequestCommit"}])
    enqueue = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1, "state": "QUEUED"}}}}
    github = _GitHub(_RULES, facts=facts, enqueue=enqueue)

    result = await _run(github)

    assert result.startswith("PR #12 in acme/app queued")


@pytest.mark.asyncio
async def test_an_ejection_for_a_conflict_does_not_hold_the_enqueue() -> None:
    conflict = dict(_EJECTED, reason="merge_conflict")
    facts = _with_timeline(_facts(queue=True), [conflict])
    enqueue = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1, "state": "QUEUED"}}}}
    github = _GitHub(_RULES, facts=facts, enqueue=enqueue)

    result = await _run(github)

    assert result.startswith("PR #12 in acme/app queued")


@pytest.mark.asyncio
async def test_a_refused_enqueue_says_the_queue_and_the_reason():
    github = _GitHub(_RULES, facts=_facts(queue=True))

    result = await _run(github)

    assert result.startswith("Error: PR #12 in acme/app is NOT merged")
    assert "merge queue" in result
    assert "not allowed" in result


class _QueuedMeanwhile(_GitHub):
    """The first facts read sees no entry; another run queues it before the enqueue."""

    async def handle(self, client, method, url, provider, action, **kwargs):
        if method == "post" and url.endswith("/graphql"):
            query = (kwargs.get("json") or {}).get("query", "")
            if "enqueuePullRequest" not in query and any(
                "enqueuePullRequest" in g.get("query", "") for g in self.graphql
            ):
                self.graphql.append(kwargs.get("json") or {})
                return _resp(_facts(queue=True, in_queue=True)), None
        return await super().handle(client, method, url, provider, action, **kwargs)


@pytest.mark.asyncio
async def test_an_enqueue_refused_because_another_run_queued_it_is_said_queued() -> None:
    github = _QueuedMeanwhile(_RULES, facts=_facts(queue=True))

    result = await _run(github)

    assert not result.startswith("Error")
    assert "already in the merge queue of main" in result
    assert "position 3" in result
    assert "queued meanwhile" in result


_BASE_MODIFIED = (405, {"message": "Base branch was modified. Review and try the merge again."})


@pytest.mark.asyncio
async def test_a_base_modified_refusal_on_a_queue_protected_base_enqueues_at_once() -> None:
    """Main moving under a merge queue: enqueue the PR, no poll, no second PUT, no branch update."""
    enqueue = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 4, "state": "QUEUED"}}}}
    github = _GitHub(_BASE_MODIFIED, facts=_facts(queue=True, state="BEHIND"), enqueue=enqueue)

    with patch("app.integrations.providers.github.pulls.asyncio.sleep") as sleep:
        result = await _run(github)

    assert result.startswith("PR #12 in acme/app queued")
    assert "position 4" in result
    assert "update_branch" not in result
    assert len(github.puts) == 1
    sleep.assert_not_called()
    mutation = github.graphql[-1]
    assert mutation["variables"] == {"id": "PR_node", "head": _HEAD}


@pytest.mark.asyncio
async def test_a_base_modified_refusal_without_a_queue_keeps_the_wait_and_retry() -> None:
    github = _GitHub(_BASE_MODIFIED, facts=_facts(queue=False))

    with patch("app.integrations.providers.github.pulls.asyncio.sleep"):
        result = await _run(github)

    assert len(github.puts) == 2
    assert not any("enqueuePullRequest" in g["query"] for g in github.graphql)
    assert "queued" not in result


@pytest.mark.asyncio
async def test_a_pr_already_in_the_queue_is_said_queued_without_a_branch_update():
    github = _GitHub(_NOT_MERGEABLE, facts=_facts(queue=True, in_queue=True))

    result = await _run(github)

    assert "already in the merge queue of main" in result
    assert "position 3" in result
    assert "No branch update is needed" in result
    assert not any("enqueuePullRequest" in g["query"] for g in github.graphql)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_state", ["AWAITING_CHECKS", "QUEUED", "MERGEABLE"])
async def test_an_enqueue_github_accepts_is_a_success_whatever_the_review_decision(entry_state: str) -> None:
    """GitHub's verdict wins: review_required with an accepted entry is queued, never an error (#2324)."""
    enqueue = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 5, "state": entry_state}}}}
    github = _GitHub(_RULES, facts=_facts(queue=True, review="REVIEW_REQUIRED"), enqueue=enqueue)

    result = await _run(github)

    assert result.startswith("PR #12 in acme/app queued")
    assert "position 5" in result
    assert entry_state.lower() in result
    assert "will NOT merge" not in result
    assert "Error" not in result


@pytest.mark.asyncio
async def test_an_enqueued_entry_github_reads_unmergeable_is_said_not_to_merge_with_the_cause() -> None:
    enqueue = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 5, "state": "UNMERGEABLE"}}}}
    github = _GitHub(_RULES, facts=_facts(queue=True, review="REVIEW_REQUIRED"), enqueue=enqueue)

    result = await _run(github)

    assert result.startswith("Error: PR #12 in acme/app is in the merge queue but will NOT merge")
    assert "UNMERGEABLE" in result
    assert "position 5" in result
    assert "the queue merges it" not in result


@pytest.mark.asyncio
async def test_a_pr_already_queued_with_review_required_is_said_queued() -> None:
    facts = _facts(queue=True, in_queue=True, review="REVIEW_REQUIRED")
    facts["data"]["repository"]["pullRequest"]["mergeQueueEntry"]["state"] = "AWAITING_CHECKS"
    github = _GitHub(_NOT_MERGEABLE, facts=facts)

    result = await _run(github)

    assert "already in the merge queue of main" in result
    assert "awaiting_checks" in result
    assert "will NOT merge" not in result


@pytest.mark.asyncio
async def test_the_facts_read_asks_whether_the_viewer_can_bypass() -> None:
    github = _GitHub(_NOT_MERGEABLE, facts=_facts(state="BLOCKED", review="REVIEW_REQUIRED"))

    await _run(github)

    facts_read = next(g for g in github.graphql if "mergeQueue(branch" in g["query"])
    assert "viewerCanMergeAsAdmin" in facts_read["query"]


@pytest.mark.asyncio
async def test_a_queued_entry_read_unmergeable_names_the_rules() -> None:
    facts = _facts(queue=True, in_queue=True)
    facts["data"]["repository"]["pullRequest"]["mergeQueueEntry"]["state"] = "UNMERGEABLE"
    github = _GitHub(_NOT_MERGEABLE, facts=facts)

    result = await _run(github)

    assert "will NOT merge" in result
    assert "UNMERGEABLE" in result
    assert "get_github_branch_rules" in result
    assert "Nothing else was done: the queue merges it" not in result


@pytest.mark.asyncio
async def test_a_405_on_a_clean_pr_names_the_protection_rules_not_update_branch():
    github = _GitHub(_NOT_MERGEABLE, facts=_facts(state="CLEAN"))

    result = await _run(github)

    assert "reads clean" in result
    assert "get_github_branch_rules" in result
    assert "update_branch" not in result
    assert "Pull Request is not mergeable" in result


@pytest.mark.asyncio
async def test_a_405_blocked_by_a_required_review_names_the_review():
    github = _GitHub(_NOT_MERGEABLE, facts=_facts(state="BLOCKED", review="REVIEW_REQUIRED", bypass=False))

    result = await _run(github)

    assert "required review is missing" in result
    assert "review required" in result
    assert "GitHub says this account cannot bypass it" in result
    assert "update_branch" not in result


@pytest.mark.asyncio
async def test_a_405_blocked_with_a_viewer_who_can_bypass_never_blames_the_review() -> None:
    github = _GitHub(_NOT_MERGEABLE, facts=_facts(state="BLOCKED", review="REVIEW_REQUIRED", bypass=True))

    result = await _run(github)

    assert "cannot bypass" not in result
    assert "required review is missing" not in result
    assert "required status check or rule" in result


@pytest.mark.asyncio
async def test_a_405_blocked_without_the_bypass_flag_does_not_claim_it_cannot_bypass() -> None:
    github = _GitHub(_NOT_MERGEABLE, facts=_facts(state="BLOCKED", review="REVIEW_REQUIRED"))

    result = await _run(github)

    assert "required review is missing" in result
    assert "cannot bypass" not in result
    assert "did not say whether this account can bypass it" in result


@pytest.mark.asyncio
async def test_a_405_blocked_by_a_check_names_the_checks():
    github = _GitHub(_NOT_MERGEABLE, facts=_facts(state="BLOCKED", review="APPROVED"))

    result = await _run(github)

    assert "required status check" in result
    assert "get_github_ci_status" in result


@pytest.mark.asyncio
async def test_a_405_on_a_draft_points_at_ready_for_review():
    github = _GitHub(_NOT_MERGEABLE, facts=_facts(draft=True))

    result = await _run(github)

    assert "it is a draft" in result
    assert "ready_for_review" in result


@pytest.mark.asyncio
async def test_a_405_behind_still_points_at_update_branch():
    github = _GitHub(_NOT_MERGEABLE, facts=_facts(state="BEHIND"))

    result = await _run(github)

    assert "behind its base" in result
    assert "update_branch" in result


@pytest.mark.asyncio
async def test_a_405_whose_head_moved_names_both_shas_and_the_claim():
    github = _GitHub(_NOT_MERGEABLE, facts=_facts(head=_NEW_HEAD))

    result = await _run(github)

    assert _HEAD[:12] in result
    assert _NEW_HEAD[:12] in result
    assert "claim" in result


@pytest.mark.asyncio
async def test_a_409_head_modified_names_both_shas_and_the_claim():
    github = _GitHub((409, {"message": "Head branch was modified. Review and try the merge again."}), heads=[_NEW_HEAD])

    result = await _run(github)

    assert result.startswith("Error: PR #12 in acme/app is NOT merged: its head moved")
    assert _HEAD[:12] in result
    assert _NEW_HEAD[:12] in result
    assert "take the claim again" in result


@pytest.mark.asyncio
async def test_an_unreadable_cause_keeps_the_generic_answer():
    github = _GitHub(_NOT_MERGEABLE, facts=None)

    result = await _run(github)

    assert result.startswith("Error: Cannot merge PR #12")
    assert "update_branch" in result
