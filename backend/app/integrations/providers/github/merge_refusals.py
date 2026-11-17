"""Why ``merge_github_pr`` was refused, read from GitHub, with the one fix.

GitHub's merge endpoint answers a 405 with a short message ("Pull Request is
not mergeable", "Repository rule violations found") that does not say which
rule refused it, and a 409 "Head branch was modified" that does not name the
new head. The caller then guessed: a branch update on a clean PR, or on a PR
already in a merge queue, costs a CI round and gains nothing.

On such a refusal the tool reads the pull request and its base once over
GraphQL (draft, head, merge state, review decision, merge queue) and:

- enqueues the PR when its base is protected by a merge queue (GitHub's
  ``enqueuePullRequest``, pinned to the head read before the merge), and
  answers "queued" with the position;
- says the PR is already queued, with its position, and that no branch
  update is needed;
- otherwise names the cause (draft, behind, conflict, required review,
  failing or pending check, a protection rule on a clean PR) with one fix.

A head that moved names the expected and the current SHA, and that the
green claim taken on the old head must be read again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import _common

_FACTS_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $base: String!) {"
    " repository(owner: $owner, name: $name) {"
    " mergeQueue(branch: $base) { id }"
    " pullRequest(number: $number) {"
    " id isDraft headRefOid mergeStateStatus reviewDecision isInMergeQueue"
    " mergeQueueEntry { position state } } } }"
)

_ENQUEUE_MUTATION = (
    "mutation($id: ID!, $head: GitObjectID) {"
    " enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) {"
    " mergeQueueEntry { position state } } }"
)


@dataclass(frozen=True)
class MergeFacts:
    """What GitHub says of a pull request and its base right after a refused merge."""

    pr_id: str
    is_draft: bool
    head: str
    merge_state: str
    review_decision: str
    base_has_queue: bool
    in_queue: bool
    queue_position: int | None
    queue_state: str


async def read_facts(
    client: Any, headers: dict[str, str], owner: str, name: str, number: int, base: str
) -> MergeFacts | None:
    """One GraphQL read of the PR and its base; None when the read fails."""
    if not base:
        return None
    data, err = await _common.gh_graphql(
        client,
        headers,
        _FACTS_QUERY,
        {"owner": owner, "name": name, "number": int(number), "base": base},
        action=f"read why PR #{number} in {owner}/{name} was not merged",
    )
    if err or not isinstance(data, dict):
        return None
    repo = data.get("repository")
    if not isinstance(repo, dict):
        return None
    pr = repo.get("pullRequest")
    if not isinstance(pr, dict) or not pr.get("id"):
        return None
    raw_entry = pr.get("mergeQueueEntry")
    entry: dict[str, Any] = raw_entry if isinstance(raw_entry, dict) else {}
    position = entry.get("position")
    return MergeFacts(
        pr_id=str(pr.get("id")),
        is_draft=bool(pr.get("isDraft")),
        head=str(pr.get("headRefOid") or ""),
        merge_state=str(pr.get("mergeStateStatus") or "UNKNOWN").upper(),
        review_decision=str(pr.get("reviewDecision") or "").upper(),
        base_has_queue=isinstance(repo.get("mergeQueue"), dict),
        in_queue=bool(pr.get("isInMergeQueue")) or bool(entry),
        queue_position=position if isinstance(position, int) else None,
        queue_state=str(entry.get("state") or ""),
    )


def _position(position: int | None) -> str:
    return f"position {position}" if position is not None else "position not returned"


def head_moved_line(repo: str, number: int, expected: str, current: str) -> str:
    """The refusal of a merge whose head moved: both SHAs and the claim to re-read."""
    return (
        f"Error: PR #{number} in {repo} is NOT merged: its head moved from {expected[:12] or 'unknown'} "
        f"(read before the merge) to {current[:12] or 'unknown'}. Nothing else was tried. A green "
        f"claim or a review taken on {expected[:12] or 'the old head'} no longer holds: read the new "
        f"head's checks with `get_github_pr` and take the claim again on {current[:12] or 'the new head'}."
    )


async def refusal_answer(
    client: Any,
    headers: dict[str, str],
    owner: str,
    name: str,
    number: int,
    base: str,
    expected_head: str,
    github_msg: str,
) -> str | None:
    """The answer to a 405 merge refusal, or None to keep the generic one.

    Enqueues the PR when its base has a merge queue; otherwise names the
    cause GitHub reports and the one fix.
    """
    repo = f"{owner}/{name}"
    facts = await read_facts(client, headers, owner, name, number, base)
    if facts is None:
        return None
    said = f" GitHub answered: {github_msg}" if github_msg else ""
    if expected_head and facts.head and facts.head != expected_head:
        return head_moved_line(repo, number, expected_head, facts.head) + said
    if facts.in_queue:
        return (
            f"PR #{number} in {repo} is already in the merge queue of {base} "
            f"({_position(facts.queue_position)}{', ' + facts.queue_state.lower() if facts.queue_state else ''}). "
            f"Nothing else was done: the queue merges it. No branch update is needed."
        )
    if facts.is_draft:
        return (
            f"Error: Cannot merge PR #{number} in {repo}: it is a draft. Call `manage_github_pr` with "
            f"action='ready_for_review', then merge again.{said}"
        )
    if facts.base_has_queue:
        variables: dict[str, Any] = {"id": facts.pr_id}
        if expected_head:
            variables["head"] = expected_head
        data, err = await _common.gh_graphql(
            client, headers, _ENQUEUE_MUTATION, variables, action=f"add PR #{number} in {repo} to the merge queue"
        )
        if err:
            return (
                f"Error: PR #{number} in {repo} is NOT merged: {base} is protected by a merge queue, so a "
                f"direct merge is refused, and adding it to the queue failed: {err.removeprefix('Error: ')}"
            )
        entry = ((data or {}).get("enqueuePullRequest") or {}).get("mergeQueueEntry") or {}
        position = entry.get("position") if isinstance(entry, dict) else None
        state = str(entry.get("state") or "").lower() if isinstance(entry, dict) else ""
        return (
            f"PR #{number} in {repo} queued: {base} is protected by a merge queue, so the PR was added to it "
            f"({_position(position if isinstance(position, int) else None)}{', ' + state if state else ''}) "
            f"pinned to head {expected_head[:12] or facts.head[:12]}. The queue merges it with the method its "
            f"settings name. No branch update is needed; read it back later with `get_github_pr`."
        )
    return _cause_line(repo, number, facts, said)


def _cause_line(repo: str, number: int, facts: MergeFacts, said: str) -> str | None:
    """The cause of a 405 from the merge state and review decision, with one fix."""
    head = facts.head[:12]
    state = facts.merge_state
    lead = f"Error: Cannot merge PR #{number} in {repo}"
    if state == "BEHIND":
        return (
            f"{lead}: it is behind its base and the base requires an up-to-date branch. Call "
            f"`manage_github_pr` with action='update_branch', wait for CI on the new head, then merge again.{said}"
        )
    if state == "DIRTY":
        return (
            f"{lead}: it conflicts with its base. Call `manage_github_pr` with action='update_branch' "
            f"(it answers the conflicting hunks), resolve them, then merge again.{said}"
        )
    if state == "BLOCKED" and facts.review_decision in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
        decision = facts.review_decision.lower().replace("_", " ")
        return (
            f"{lead}: a required review is missing (review decision: {decision}) and this account "
            f"cannot bypass it. No branch update helps: get the approval, then merge again.{said}"
        )
    if state == "BLOCKED":
        return (
            f"{lead}: a required status check or rule on its base is not satisfied on head {head}. "
            f"Read the required checks with `get_github_ci_status` (pr_number) and the rules with "
            f"`get_github_branch_rules`; merge again once they pass. No branch update is needed.{said}"
        )
    if state == "UNSTABLE":
        return (
            f"{lead}: a check is failing on head {head}. Read it with `get_github_ci_status` (pr_number), "
            f"fix it, then merge again. No branch update is needed.{said}"
        )
    if state in ("CLEAN", "HAS_HOOKS"):
        return (
            f"{lead}: the PR reads clean on head {head}, so a protection rule or ruleset of its base "
            f"refused the merge for this account (a restricted merge method, signed commits, a push "
            f"restriction). Read them with `get_github_branch_rules` on the base; no branch update helps.{said}"
        )
    return None
