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
  update is needed, also when another run queued it between the read and
  the enqueue (GitHub then refuses the mutation; #2018);
- refuses to enqueue a head the merge queue ejected for failing checks with
  no commit since: the queue would run the same tree and fail it again,
  locking the branch for the fix meanwhile (#2010);
- says a queue entry will NOT merge when its required review is missing
  (the queue applies no review bypass) or GitHub reads it UNMERGEABLE,
  with the cause, instead of "the queue merges it" (#409);
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
    " mergeQueueEntry { position state }"
    " timelineItems(last: 10, itemTypes: [PULL_REQUEST_COMMIT, HEAD_REF_FORCE_PUSHED_EVENT,"
    " REMOVED_FROM_MERGE_QUEUE_EVENT]) {"
    " nodes { __typename ... on RemovedFromMergeQueueEvent { createdAt reason } } } } } }"
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
    #: When the queue last ejected this head for failing checks with no commit
    #: or force-push since ('' otherwise).
    ejected_unchanged_at: str = ""


def _ejected_unchanged_at(pr: dict[str, Any]) -> str:
    """The time of a failed-checks ejection that is the PR's latest head event, else ''.

    The timeline holds commits, force-pushes and queue removals; when its last
    node is a removal for failed checks, the head the queue failed is still the
    head, and enqueueing it again re-runs the same failing tree.
    """
    timeline = pr.get("timelineItems")
    nodes = timeline.get("nodes") if isinstance(timeline, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return ""
    last = nodes[-1]
    if not isinstance(last, dict) or last.get("__typename") != "RemovedFromMergeQueueEvent":
        return ""
    if str(last.get("reason") or "").lower() != "failed_checks":
        return ""
    return str(last.get("createdAt") or "unknown time")


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
        ejected_unchanged_at=_ejected_unchanged_at(pr),
    )


def _position(position: int | None) -> str:
    return f"position {position}" if position is not None else "position not returned"


def _stuck_entry(queue_state: str, review_decision: str) -> str:
    """Why a queue entry will not merge, or '' when nothing says so.

    A queue does not apply a review bypass: an entry whose required review is
    missing reads UNMERGEABLE and stays in the queue without merging (#409).
    """
    review = review_decision.upper()
    if review in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
        decision = review.lower().replace("_", " ")
        return (
            f"a required review is missing (review decision: {decision}) and the merge queue does not "
            f"bypass it. Get the approval; the queue then merges it"
        )
    if queue_state.upper() == "UNMERGEABLE":
        return (
            "GitHub reads the entry UNMERGEABLE (a rule of the base is not satisfied). Read the rules "
            "with `get_github_branch_rules` and the checks with `get_github_ci_status` (pr_number)"
        )
    return ""


def _queued_line(lead: str, number: int, repo: str, stuck: str, said: str) -> str:
    """The answer for a queue entry that will not merge: the cause, then where it sits."""
    return (
        f"Error: PR #{number} in {repo} is in the merge queue but will NOT merge as it stands: "
        f"{stuck}. {lead} No branch update helps.{said}"
    )


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
        stuck = _stuck_entry(facts.queue_state, facts.review_decision)
        where = (
            f"It is in the merge queue of {base} "
            f"({_position(facts.queue_position)}{', ' + facts.queue_state.lower() if facts.queue_state else ''})."
        )
        if stuck:
            return _queued_line(where, number, repo, stuck, said)
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
    if facts.base_has_queue and facts.ejected_unchanged_at:
        return (
            f"Error: PR #{number} in {repo} is NOT merged and was NOT added to the merge queue: the queue "
            f"ejected head {facts.head[:12]} for failing checks at {facts.ejected_unchanged_at} and no commit "
            f"was pushed since, so it would fail the same way and lock the branch meanwhile. Read the failed "
            f"merge_group run (`list_github_workflow_runs` event='merge_group'), push the fix (or merge the base "
            f"with `manage_github_pr` action='update_branch' when a sibling of its group caused it), then merge "
            f"again on the new head.{said}"
        )
    if facts.base_has_queue:
        variables: dict[str, Any] = {"id": facts.pr_id}
        if expected_head:
            variables["head"] = expected_head
        data, err = await _common.gh_graphql(
            client, headers, _ENQUEUE_MUTATION, variables, action=f"add PR #{number} in {repo} to the merge queue"
        )
        if err:
            # Another run may have queued it between the read and the mutation:
            # GitHub then refuses the enqueue, yet the PR is queued (#2018).
            again = await read_facts(client, headers, owner, name, number, base)
            if again is not None and again.in_queue:
                return (
                    f"PR #{number} in {repo} is already in the merge queue of {base} "
                    f"({_position(again.queue_position)}"
                    f"{', ' + again.queue_state.lower() if again.queue_state else ''}): it was queued meanwhile, "
                    f"so the enqueue was refused. Nothing else was done: the queue merges it. No branch update "
                    f"is needed."
                )
            return (
                f"Error: PR #{number} in {repo} is NOT merged: {base} is protected by a merge queue, so a "
                f"direct merge is refused, and adding it to the queue failed: {err.removeprefix('Error: ')}"
            )
        entry = ((data or {}).get("enqueuePullRequest") or {}).get("mergeQueueEntry") or {}
        position = entry.get("position") if isinstance(entry, dict) else None
        state = str(entry.get("state") or "").lower() if isinstance(entry, dict) else ""
        stuck = _stuck_entry(state, facts.review_decision)
        if stuck:
            where = (
                f"It was added to the merge queue of {base} "
                f"({_position(position if isinstance(position, int) else None)}{', ' + state if state else ''}) "
                f"pinned to head {expected_head[:12] or facts.head[:12]}."
            )
            return _queued_line(where, number, repo, stuck, said)
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
