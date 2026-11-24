"""GitHub pull requests tools - list/read/diff/files, create/update/review/merge,
reviewers, review comments, commits, and consolidated PR management."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.http import TRANSIENT_RETRY_STATUSES
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult, workspace_ingest_requested

from . import (
    _common,
    attachments,
    body_edits,
    closing_refs,
    full_ci_gate,
    merge_refusals,
    pr_files,
    pr_inherit,
    pr_overlap,
    pr_title,
    review_loop,
)
from ._common import (
    _BASE_URL,
    _ITEM_PREVIEW_CHARS,
    _MAX_DIFF_CHARS,
    _PROVIDER,
    _body_preview,
    _coerce_int,
    _coerce_str,
    _json_headers,
    _label_str,
    _login,
    body_section,
    one_line,
    parse_repo,
    window_args_error,
)
from .checks import _CI_MAX_PAGES, _CI_PAGE_SIZE, ci_verdict, classify_ci, is_conflict, running_note, running_workflows
from .contents import _json_object
from .landed import pull_key, pull_probe
from .merge import conflict_answer, outcome_line
from .merge_resolution import _already_merged, _branch_tip, drop_stopped_alignment, ref_moved_and_undone

_SOURCE = "integration:github"
_MERGE_METHODS = {"MERGE", "SQUASH", "REBASE"}

#: GitHub's 422 on update-branch when the PR branch already holds its base
#: ("There are no new commits on the base branch."): a no-op, never an error.
_UP_TO_DATE_RE = re.compile(r"no new commits on the base branch|already up[ -]to[ -]date", re.IGNORECASE)

#: GitHub's 422 on create PR when an open pull request already joins the head
#: and base ("A pull request already exists for owner:branch.").
_ALREADY_EXISTS_RE = re.compile(r"a pull request already exists", re.IGNORECASE)

#: Statuses a review request is retried on: the gateway set, plus the 404 GitHub
#: answers for a pull request created a moment before (seen on xtm-one #809, the
#: same call passing seconds later). The request is idempotent, so a retry can
#: never add a second request; the budget below bounds a pull request that is
#: really missing.
_REVIEW_REQUEST_RETRY_STATUSES = TRANSIENT_RETRY_STATUSES | {404}
_REVIEW_REQUEST_RETRY_SECONDS = 15.0

#: Pauses (seconds) before each new merge attempt after an answer that leaves
#: the merge state unknown (gateway 5xx, transport error, "already in
#: progress"). Two retries, so at most three PUTs and about 20 s of waiting.
_MERGE_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0)

#: Pauses (seconds) between the PR reads after a "Base branch was modified"
#: refusal: the base moved seconds before the merge and GitHub recomputes the
#: PR's mergeability (``mergeable`` null, ``mergeable_state`` "unknown")
#: before it accepts a merge again. About 30 s in all, then one retry.
_BASE_MODIFIED_POLL_DELAYS: tuple[float, ...] = (3.0, 5.0, 7.0, 15.0)

#: Merge states after which the merge is retried at once: GitHub has finished
#: computing and the PR is not behind its base nor conflicting. "blocked" is
#: a review or status requirement the caller may bypass; GitHub refuses again
#: when it cannot, so the retry never forces anything. "unstable" (a failing
#: check on the new merge ref) is left to the caller.
_BASE_MODIFIED_RETRY_STATES = frozenset({"clean", "blocked", "has_hooks"})

#: Pause (seconds) before the one retry of a label write that GitHub answers
#: 404 right after the pull request was created (its issue not yet readable).
_LABEL_RETRY_DELAY = 2.0

#: The words :func:`app.integrations.http.format_http_error` writes for a 404.
_NOT_FOUND = "resource not found"

#: Merge states that a branch update fixes.
_BASE_MODIFIED_UPDATE_STATES = frozenset({"behind", "dirty"})


def _is_base_modified(resp: Any) -> bool:
    """True when a merge PUT was refused because the base branch moved meanwhile."""
    if resp is None or getattr(resp, "status_code", 0) != 405:
        return False
    try:
        message = str((resp.json() or {}).get("message") or "")
    except Exception:
        message = ""
    return "base branch was modified" in message.lower()


def _merge_outcome_unknown(resp: Any, err: str) -> bool:
    """True when a failed merge PUT may still have been applied by GitHub.

    A gateway 502/503/504, a transport error with no response, or a refusal
    saying a merge is already in progress: GitHub can land the merge after
    answering any of them, so the caller reads the PR back before retrying.
    A plain 405 / 409 / 422 refusal is a definite "not merged".
    """
    if resp is None:
        return True
    status = getattr(resp, "status_code", 0)
    if status in TRANSIENT_RETRY_STATUSES:
        return True
    if status not in (405, 409):
        return False
    try:
        body = resp.text or ""
    except Exception:
        body = ""
    return "in progress" in f"{err or ''} {body}".lower()


#: Pauses (seconds) before each new draft-toggle mutation after an answer of
#: unknown outcome. Two retries, so at most three mutations and about 20 s.
_DRAFT_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0)

#: Lower-cased fragments of a GraphQL helper error that leave a mutation's
#: outcome unknown: a gateway 5xx, a timeout, a transport failure, an empty or
#: non-JSON body, or GitHub's internal "Something went wrong" GraphQL error.
_DRAFT_UNKNOWN_MARKERS: tuple[str, ...] = (
    "server error (5",
    "timed out",
    "could not connect",
    "request failed while trying",
    "unexpected, empty, or non-json response",
    "contained no data",
    "something went wrong",
)


def _draft_outcome_unknown(err: str) -> bool:
    """True when a draft-toggle mutation error may hide an applied change, or none.

    A permission, validation or not-found error is a definite refusal (False):
    it is answered as it is and never retried.
    """
    low = (err or "").lower()
    return any(marker in low for marker in _DRAFT_UNKNOWN_MARKERS)


async def _pr_draft(
    client: httpx.AsyncClient, owner: str, name: str, number: int, headers: dict[str, str]
) -> bool | None:
    """The PR's ``draft`` flag read back over REST, None when it cannot be read."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}",
        _PROVIDER,
        f"read back PR #{number}",
        headers=headers,
    )
    if err or resp is None:
        return None
    try:
        draft = (resp.json() or {}).get("draft")
    except ValueError:
        return None
    return draft if isinstance(draft, bool) else None


def _merge_error_brief(resp: Any, err: str) -> str:
    """A short label of a failed merge answer for the tool result."""
    if resp is None:
        return "no HTTP response (" + (err or "transport error")[:120] + ")"
    return f"HTTP {getattr(resp, 'status_code', '?')}"

#: The review thread counts a PR read shows: totalCount and each
#: thread's resolution, by pages of 100, nothing else.
_THREADS_COUNT_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) { totalCount pageInfo { hasNextPage endCursor } nodes { isResolved } }
    }
  }
}
"""


#: The pending review requests of a PR, users and bots: the REST answer of a
#: request leaves the Copilot bot out of requested_reviewers.
_REVIEW_REQUESTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewRequests(first: 100) {
        nodes { requestedReviewer { __typename ... on User { login } ... on Bot { login } ... on Mannequin { login } } }
      }
    }
  }
}
"""


def _requested_logins(data: dict[str, Any]) -> list[str]:
    """The logins of the pending review requests in a _REVIEW_REQUESTS_QUERY answer."""
    pull = (data.get("repository") or {}).get("pullRequest") or {}
    nodes = (pull.get("reviewRequests") or {}).get("nodes") or []
    logins = []
    for node in nodes:
        reviewer = (node or {}).get("requestedReviewer") or {}
        if isinstance(reviewer, dict) and reviewer.get("login"):
            logins.append(str(reviewer["login"]))
    return logins


#: Pages of 100 review threads a PR read counts before it says the count is partial.
_THREADS_MAX_PAGES = 5

#: list_github_prs with an author or head prefix filter: the pulls API filters
#: neither, so pages of this size are read and filtered, at most this many.
_LIST_FILTER_PAGE_SIZE = 100
_LIST_FILTER_MAX_PAGES = 3


async def _no_extras() -> tuple[None, None]:
    """A closed or merged PR has no CI or thread line worth two more calls."""
    return None, None


async def _pr_node_id(
    client: httpx.AsyncClient, owner: str, name: str, number: int, headers: dict[str, str]
) -> tuple[str | None, str | None]:
    """Resolve a PR number to its GraphQL node id (needed for draft/automerge mutations)."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}",
        _PROVIDER,
        f"resolve PR #{number}",
        headers=headers,
    )
    if err:
        return None, err
    node_id = (resp.json() or {}).get("node_id")
    if not node_id:
        return None, f"Error: Could not resolve node id for PR #{number}."
    return node_id, None


#: Review decisions under which GitHub auto-merge is refused: the pull request still waits for a
#: reviewer, and an auto-merge enabled now lets GitHub (or its merge queue) land it the moment the
#: checks pass, with no tool reading the review again (an admin bypass even skips the rule).
_AUTOMERGE_BLOCKING_DECISIONS = frozenset({"REVIEW_REQUIRED", "CHANGES_REQUESTED"})

_REVIEW_DECISION_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){reviewDecision}}}"
)


async def _automerge_review_refusal(
    client: httpx.AsyncClient, owner: str, name: str, number: int, headers: dict[str, str]
) -> str | None:
    """The refusal when PR *number* still waits for an approving review, else ``None``.

    ``reviewDecision`` is ``None`` when the base requires no review: auto-merge then
    stays allowed. An unreadable decision is refused too: the doubt never enables a merge.
    """
    data, err = await _common.gh_graphql(
        client,
        headers,
        _REVIEW_DECISION_QUERY,
        {"owner": owner, "name": name, "number": number},
        action=f"read the review decision of PR #{number} before enabling auto-merge",
    )
    if err or not isinstance(data, dict):
        return (
            f"Error: auto-merge not enabled on PR #{number}, nothing was written: its review decision "
            f"could not be read ({err or 'no data'})."
        )
    pull = (data.get("repository") or {}).get("pullRequest") or {}
    decision = pull.get("reviewDecision") if isinstance(pull, dict) else None
    if decision not in _AUTOMERGE_BLOCKING_DECISIONS:
        return None
    return (
        f"Error: auto-merge not enabled on PR #{number}, nothing was written: its review decision is "
        f"{decision}. Auto-merge would let GitHub merge it as soon as the checks pass, before the review "
        "the repository asks for (an admin bypass skips the review rule). Wait for an approving review, "
        "then merge with merge_github_pr."
    )


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
    record_pull_request: Any = None,
    check_work_item: Any = None,
    park_full_ci: Any = None,
) -> list[ToolDefinition]:
    """Build the GitHub pull request tools and register their callables.

    *record_pull_request* is the platform's ``(work_item_id, repo, number,
    head_branch, head_sha, url) -> str`` that names a new pull request on the
    work item it is opened for; ``None`` outside an agent's run, and
    then ``work_item_id`` is not offered. *check_work_item* ``(work_item_id) ->
    str | None`` answers why no pull request may be opened for that item (it
    is cancelled, #11554); ``create_github_pr`` refuses with it before any write.
    *park_full_ci* ``(repo, base, workflow, number, head_sha) -> bool`` parks a
    pull request whose full CI is red on its base's own failure, run again at
    the base's green (#12069); ``None`` outside a person's run.
    """

    async def list_github_prs(
        repo: str,
        state: str = "open",
        sort: str = "updated",
        base: str = "",
        count: int = 20,
        author: str = "",
        head_prefix: str = "",
        include_preview: bool = True,
    ) -> str:
        """List pull requests in a repository with filters and body preview.

        The pulls API filters neither by author nor by head branch prefix:
        with *author* or *head_prefix* the pages are read (at most
        ``_LIST_FILTER_MAX_PAGES`` of ``_LIST_FILTER_PAGE_SIZE``) and filtered
        before formatting, so only the matching rows reach the context.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        want = max(1, min(count, 30))
        author_key = author.strip().lstrip("@").lower()
        prefix = head_prefix.strip()
        filtered = bool(author_key or prefix)
        per_page = _LIST_FILTER_PAGE_SIZE if filtered else want
        max_pages = _LIST_FILTER_MAX_PAGES if filtered else 1
        params: dict[str, Any] = {"state": state, "per_page": per_page, "sort": sort, "direction": "desc"}
        if base:
            params["base"] = base
        matches: list[dict[str, Any]] = []
        scanned = 0
        exhausted = False
        async with egress_client(timeout=30) as client:
            for page in range(1, max_pages + 1):
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/pulls",
                    _PROVIDER,
                    f"list PRs in {repo}",
                    headers=headers,
                    params={**params, "page": page},
                )
                if err:
                    return err
                batch = resp.json() or []
                scanned += len(batch)
                for pr in batch:
                    login = str((pr.get("user") or {}).get("login") or "").lower()
                    head_ref = str((pr.get("head") or {}).get("ref") or "")
                    if author_key and login != author_key:
                        continue
                    if prefix and not head_ref.startswith(prefix):
                        continue
                    matches.append(pr)
                if len(batch) < per_page:
                    exhausted = True
                    break
                if len(matches) >= want:
                    break
        matches = matches[:want]
        filters = "".join(
            f" {label} {value}" for label, value in (("by", author_key), ("with head prefix", prefix)) if value
        )
        if not matches:
            if filtered and not exhausted:
                return (
                    f"No {state} pull requests{filters} among the {scanned} most recent (sort={sort}); "
                    "older ones were not read."
                )
            return f"No {state} pull requests{filters} found."
        lines = []
        for pr in matches:
            draft = " [DRAFT]" if pr.get("draft") else ""
            merged = " [MERGED]" if pr.get("merged_at") else ""
            reviewers = ", ".join(r["login"] for r in pr.get("requested_reviewers", []))
            reviewer_str = f" reviewers: {reviewers}" if reviewers else ""
            entry = (
                f"#{pr['number']} {pr['title']} [{pr['state']}]{draft}{merged}"
                f"{_label_str(pr.get('labels', []))} by {pr['user']['login']}"
                f" ({pr['head']['ref']} → {pr['base']['ref']}){reviewer_str}"
            )
            if include_preview:
                entry += _body_preview(pr.get("body"))
            lines.append(entry)
        text = ("\n\n" if include_preview else "\n").join(lines)
        if filtered and not exhausted and len(matches) < want:
            text += f"\n\n(Scanned the {scanned} most recent {state} pull requests (sort={sort}); older ones were not read.)"
        return text

    async def _pr_state_extras(
        client: httpx.AsyncClient, owner: str, name: str, number: int, head_sha: str, conflict: bool = False
    ) -> tuple[str | None, str | None]:
        """The checks summary of the head and the review thread counts, each ``None`` when unreadable.

        *conflict*: the PR conflicts with its base, so the verdict is ``conflict``
        (checks.ci_verdict, the verdict of get_github_ci_status and list_github_pr_states).
        """

        async def checks() -> str | None:
            if not head_sha:
                return None

            async def all_runs() -> tuple[list[dict[str, Any]], int, bool] | None:
                # Every page, as get_github_ci_status reads them.
                runs: list[dict[str, Any]] = []
                total = 0
                for page in range(1, _CI_MAX_PAGES + 1):
                    resp, err = await _common.safe_request(
                        client, "get", f"{_BASE_URL}/repos/{owner}/{name}/commits/{head_sha}/check-runs", _PROVIDER,
                        f"get check runs for PR #{number}", headers=headers,
                        params={"per_page": _CI_PAGE_SIZE, "page": page},
                    )
                    if err or resp is None:
                        return None
                    data = resp.json() or {}
                    batch = data.get("check_runs") or []
                    runs.extend(batch)
                    total = _coerce_int(data.get("total_count")) or 0
                    if len(batch) < _CI_PAGE_SIZE or len(runs) >= total:
                        return runs, total, False
                return runs, total, True

            async def all_statuses() -> tuple[list[dict[str, Any]], bool] | None:
                # The combined status pages too: a failing
                # context past the first 100 must not read as green.
                statuses: list[dict[str, Any]] = []
                for page in range(1, _CI_MAX_PAGES + 1):
                    resp, err = await _common.safe_request(
                        client, "get", f"{_BASE_URL}/repos/{owner}/{name}/commits/{head_sha}/status", _PROVIDER,
                        f"get commit status for PR #{number}", headers=headers,
                        params={"per_page": _CI_PAGE_SIZE, "page": page},
                    )
                    if err or resp is None:
                        return None
                    data = resp.json() or {}
                    batch = data.get("statuses") or []
                    statuses.extend(batch)
                    count = _coerce_int(data.get("total_count")) or 0
                    if len(batch) < _CI_PAGE_SIZE or len(statuses) >= count:
                        return statuses, False
                return statuses, True

            got, got_statuses = await asyncio.gather(all_runs(), all_statuses())
            if got is None or got_statuses is None:
                return None
            runs, total, truncated = got
            statuses, statuses_truncated = got_statuses
            # One status per context, the first seen (GitHub lists the newest
            # first): a rerun context is not counted twice.
            latest_statuses: dict[str, dict[str, Any]] = {}
            for status in statuses:
                latest_statuses.setdefault(str(status.get("context") or "?"), status)
            buckets = classify_ci(runs, list(latest_statuses.values()))
            failed, pending = buckets["failed"], buckets["pending"]
            # Every reported check passed: a queued workflow run may not have reported yet.
            running = None if failed or pending else await running_workflows(client, headers, owner, name, head_sha)
            verdict = ci_verdict({k: len(v) for k, v in buckets.items()}, conflict=conflict, running=running)
            line = (
                f"Checks on head: {verdict}, {len(buckets['passed'])} passed, {len(failed)} failed, "
                f"{len(pending)} pending, {len(buckets['skipped'])} skipped"
            )
            if failed:
                line += "; failed: " + ", ".join(e["name"] for e in failed[:10])
                if len(failed) > 10:
                    line += f" (+{len(failed) - 10} more)"
            if running and not conflict:
                line += "; " + running_note(running)
            cap = _CI_MAX_PAGES * _CI_PAGE_SIZE
            if truncated:
                line += f" (first {len(runs)} of {total} check runs; get_github_ci_status uses the same {cap:,}-run cap)"
            if statuses_truncated:
                line += f" (first {len(statuses)} commit statuses only)"
            return line

        async def threads() -> str | None:
            # Every page of threads, at most _THREADS_MAX_PAGES; a
            # longer list is labelled partial rather than counted short.
            unresolved = seen = total = 0
            after: str | None = None
            for _ in range(_THREADS_MAX_PAGES):
                data, err = await _common.gh_graphql(
                    client, headers, _THREADS_COUNT_QUERY,
                    {"owner": owner, "name": name, "number": number, "after": after},
                    action=f"count review threads of PR #{number}",
                )
                if err or not data:
                    return None
                conn = ((data.get("repository") or {}).get("pullRequest") or {}).get("reviewThreads") or {}
                nodes = conn.get("nodes") or []
                seen += len(nodes)
                total = _coerce_int(conn.get("totalCount")) or seen
                unresolved += sum(1 for n in nodes if isinstance(n, dict) and not n.get("isResolved"))
                info = conn.get("pageInfo") or {}
                after = info.get("endCursor")
                if not info.get("hasNextPage") or not after:
                    break
            more = f" (partial: {seen} of {total} threads read)" if total > seen else ""
            return f"Review threads: {unresolved} unresolved of {total}{more}"

        return await asyncio.gather(checks(), threads())

    async def get_github_pr(
        repo: str, number: int, include_body: bool = False, offset: int = 0, max_chars: int = 0
    ) -> str | ToolResult:
        """Read a pull request's state (head, mergeable, checks, open threads), its body head and reviews."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        start, size, werr = window_args_error(offset, max_chars)
        if werr:
            return werr
        include = include_body is True or str(include_body).lower() == "true"
        prefix = f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}"
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client, "get", prefix, _PROVIDER, f"read PR #{number} in {repo}", headers=headers,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    # Return a "not found" message (no leading "Error" so the agent
                    # loop does not log it at WARNING level) with explicit guidance to
                    # use list_github_prs instead of guessing/incrementing PR numbers.
                    return (
                        f"PR #{number} not found in {repo}. "
                        "Do not guess or increment PR numbers — use list_github_prs "
                        "to discover real, existing PR numbers for this repository."
                    )
                return err
            pr = resp.json()
            reviews_coro = _common.safe_request(
                client, "get", f"{prefix}/reviews", _PROVIDER, f"fetch reviews for PR #{number}", headers=headers, params={"per_page": 50}
            )
            files_coro = _common.safe_request(
                client, "get", f"{prefix}/files", _PROVIDER, f"fetch files for PR #{number}", headers=headers, params={"per_page": 100}
            )
            head_sha = str((pr.get("head") or {}).get("sha") or "")
            open_pr = pr.get("state") == "open"
            extras_coro = (
                _pr_state_extras(
                    client, owner, name, number, head_sha, is_conflict(pr.get("mergeable"), pr.get("mergeable_state"))
                )
                if open_pr
                else _no_extras()
            )
            (reviews_resp, _), (files_resp, _), (checks_line, threads_line) = await asyncio.gather(
                reviews_coro, files_coro, extras_coro
            )
            reviews = reviews_resp.json() if reviews_resp else []
            files = files_resp.json() if files_resp else []
            lines = [
                f"PR #{number}: {pr['title']}",
                f"State: {pr['state']}",
                f"Author: {pr['user']['login']}",
                f"Created: {pr['created_at']}",
                f"Updated: {pr['updated_at']}",
                f"Base: {pr['base']['ref']} ← Head: {pr['head']['ref']}" + (f" @ {head_sha[:12]}" if head_sha else ""),
                f"URL: {pr['html_url']}",
            ]
            if pr.get("draft"):
                lines.append("Status: DRAFT")
            elif pr.get("merged_at"):
                lines.append(f"Merged: {pr['merged_at']} by {(pr.get('merged_by') or {}).get('login', '?')}")
            if pr.get("assignees"):
                lines.append(f"Assignees: {', '.join(a['login'] for a in pr['assignees'])}")
            if pr.get("requested_reviewers"):
                lines.append(f"Requested reviewers: {', '.join(r['login'] for r in pr['requested_reviewers'])}")
            if pr.get("labels"):
                lines.append(f"Labels: {', '.join(lb['name'] for lb in pr['labels'])}")
            if pr.get("milestone"):
                lines.append(f"Milestone: {pr['milestone']['title']}")
            mergeable = pr.get("mergeable")
            if mergeable is not None:
                lines.append(f"Mergeable: {'yes' if mergeable else 'no'}")
                if pr.get("mergeable_state"):
                    lines.append(f"Mergeable state: {pr['mergeable_state']}")
            if checks_line:
                lines.append(checks_line)
            if threads_line:
                lines.append(threads_line)
            lines.append(
                f"Changes: +{pr.get('additions', 0)} −{pr.get('deletions', 0)} in {pr.get('changed_files', 0)} files"
            )
            lines.append(body_section(pr.get("body"), include=include, offset=start, size=size, what="pull request"))
            if files:
                lines.append(f"\nChanged files ({len(files)}):")
                for f in files[:50]:
                    status_icon = {"added": "+", "removed": "−", "modified": "~", "renamed": "→"}.get(f.get("status", ""), "?")
                    lines.append(f"  {status_icon} {f['filename']} (+{f.get('additions', 0)} −{f.get('deletions', 0)})")
            if reviews:
                lines.append(f"\nReviews ({len(reviews)}):")
                for r in reviews:
                    state_str = r.get("state", "COMMENTED")
                    who = f"{_login(r.get('user'))} ({state_str}) at {r.get('submitted_at', '?')}"
                    if include:
                        lines.append(f"\n--- {who} ---")
                        if r.get("body"):
                            lines.append(r["body"])
                    else:
                        preview = one_line(r.get("body"))
                        lines.append(f"  - {who}" + (f": {preview}" if preview else ""))
                if not include and any(len(r.get("body") or "") > _ITEM_PREVIEW_CHARS for r in reviews):
                    lines.append("  (review bodies cut; include_body=true shows them whole)")
            else:
                lines.append("\nNo reviews yet.")
            text = "\n".join(lines)
            computing = ("mergeable" in pr and pr["mergeable"] is None) or str(
                pr.get("mergeable_state") or ""
            ).lower() == "unknown"
            if open_pr and computing:
                # GitHub is still computing the mergeability: a transient answer,
                # never replayed from the agent loop's cache.
                return ToolResult(tool_call_id="", content=text, transient=True)
            return text

    async def get_github_pr_diff(repo: str, number: int) -> str:
        """Get the unified diff of a pull request for code review.

        Returns the actual code changes in unified diff format.
        Large diffs are truncated to stay within context limits.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        diff_headers = {**headers, "Accept": "application/vnd.github.v3.diff"}
        async with egress_client(timeout=60) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}",
                _PROVIDER,
                f"get diff for PR #{number} in {repo}",
                headers=diff_headers,
            )
            if err:
                return err
            diff = resp.text
            if not diff:
                return f"PR #{number} has no changes."
            if workspace_ingest_requested():
                # Bound for the sandbox workspace: the whole diff, uncut.
                return diff
            if len(diff) > _MAX_DIFF_CHARS:
                return (
                    f"PR #{number} diff (truncated to {_MAX_DIFF_CHARS:,} chars — full diff is {len(diff):,} chars):\n\n"
                    + diff[:_MAX_DIFF_CHARS]
                    + f"\n\n... [truncated — {len(diff) - _MAX_DIFF_CHARS:,} more chars; pass to_workspace_path to "
                    "get the whole diff in the sandbox workspace]"
                )
            return f"PR #{number} diff:\n\n{diff}"

    async def get_github_pr_files(
        repo: str,
        number: int,
        path: str = "",
        pattern: str = "",
        include_patch: bool = False,
        offset: int = 0,
        max_chars: int = 0,
    ) -> str:
        """The files a pull request changes: one line each by default, hunks or patches on request.

        ``path`` narrows the files (glob, directory or file); ``pattern`` answers
        only the hunks whose lines match a regular expression; ``include_patch``
        reads the patches of the selected files by window (``offset``,
        ``max_chars``). Every page of the files endpoint is read.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        regex: re.Pattern[str] | None = None
        if pattern:
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                return f"Error: 'pattern' is not a valid regular expression: {exc}"
        start, size, werr = window_args_error(offset, max_chars)
        if werr:
            return werr
        files: list[dict[str, Any]] = []
        async with egress_client(timeout=30) as client:
            for page in range(1, pr_files.MAX_PAGES + 1):
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}/files",
                    _PROVIDER,
                    f"get files for PR #{number} in {repo}",
                    headers=headers,
                    params={"per_page": pr_files.PAGE_SIZE, "page": page},
                )
                if err:
                    if not files:
                        return err
                    break
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                files.extend(f for f in batch if isinstance(f, dict))
                if len(batch) < pr_files.PAGE_SIZE:
                    break
        if not files:
            return f"PR #{number} has no changed files."
        selected = pr_files.select(files, path)
        if regex is None and not include_patch:
            return pr_files.compact_list(number, files, selected, path)
        head = pr_files.compact_list(number, files, selected, path).split("\n\n", 1)[0]
        if regex is not None:
            body, files_hit, hunks_hit = pr_files.pattern_body(selected, regex)
            head += f"\nPattern {pattern!r}: {hunks_hit} matching hunks in {files_hit} of {len(selected)} files"
            if not body:
                return head
        else:
            body = pr_files.patch_body(selected)
        if start and start >= len(body):
            return f"{head}\nError: offset {start:,} is past the end of the answer ({len(body):,} characters)."
        text, footer = pr_files.window(body, start, size)
        return f"{head}\n{footer}\n\n{text}\n{footer}"

    async def list_github_pr_commits(repo: str, number: int, count: int = 50) -> str:
        """List the commits that make up a pull request."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}/commits",
                _PROVIDER,
                f"list commits for PR #{number} in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            commits = resp.json()

            def _fmt(c: dict) -> str:
                msg = ((c.get("commit") or {}).get("message") or "").splitlines()[0] or "(no message)"
                return f"{(c.get('sha') or '')[:8]} {msg}"

            return _common.format_entity_list(
                commits, _fmt, noun="commit", empty_msg=f"PR #{number} has no commits."
            )

    async def list_github_pr_review_comments(repo: str, number: int, count: int = 50) -> str:
        """List inline review comments (diff comments) on a pull request."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}/comments",
                _PROVIDER,
                f"list review comments for PR #{number} in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            comments = resp.json()
            if not comments:
                return f"PR #{number} has no inline review comments."
            lines = [f"Inline review comments on PR #{number} ({len(comments)}):"]
            for c in comments:
                loc = f"{c.get('path', '?')}:{c.get('line') or c.get('original_line') or '?'}"
                body = (c.get("body") or "").strip().replace("\n", " ")
                lines.append(f"  [{_common._login(c.get('user'))}] {loc} — {body[:200]}")
            return "\n".join(lines)

    async def _assign_pull_request(client: Any, owner: str, name: str, number: int, wanted: list[str]) -> str:
        """Add *wanted* as assignees of pull request *number*; one line naming the outcome.

        GitHub silently drops a login it cannot assign, so the read-back is the
        assignee list of the answer, and every wanted login missing from it is named.
        """
        resp, err = await _common.safe_request(
            client,
            "post",
            f"{_BASE_URL}/repos/{owner}/{name}/issues/{number}/assignees",
            _PROVIDER,
            f"assign PR #{number} in {owner}/{name}",
            headers=_json_headers(headers),
            json={"assignees": wanted},
        )
        if err:
            return f"Not assigned ({', '.join(wanted)}): {err}. The pull request was created; assign it with update_github_issue."
        data = resp.json() if resp is not None else {}
        got = [a.get("login", "") for a in (data.get("assignees") or []) if isinstance(a, dict)]
        missing = [w for w in wanted if w.lower() not in {g.lower() for g in got}]
        line = f"Assignees: {', '.join(got) or 'none'}"
        if missing:
            line += f" (not assigned: {', '.join(missing)}; GitHub drops a login without access to the repository)"
        return line

    async def _unknown_labels(client: Any, owner: str, name: str, wanted: list[str]) -> list[str]:
        """The labels of *wanted* the repository does not define (a 404 on the label read).

        Any other answer (a permission error, a gateway error) never blocks: the
        label is then left to the write after creation.
        """
        unknown: list[str] = []
        for label in wanted:
            _resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/labels/{quote(label, safe='')}",
                _PROVIDER,
                f"read label '{label}' in {owner}/{name}",
                headers=headers,
            )
            if err and _NOT_FOUND in err:
                unknown.append(label)
        return unknown

    async def _label_pull_request(client: Any, owner: str, name: str, number: int, wanted: list[str]) -> str:
        """Add *wanted* as labels of pull request *number*; one line naming the labels read back.

        A 404 right after creation (the pull request's issue not yet readable)
        is retried once; a failure never undoes the pull request.
        """
        url = f"{_BASE_URL}/repos/{owner}/{name}/issues/{number}/labels"
        resp, err = None, None
        for attempt in range(2):
            resp, err = await _common.safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                f"label PR #{number} in {owner}/{name}",
                headers=_json_headers(headers),
                json={"labels": wanted},
            )
            if not (err and _NOT_FOUND in err and attempt == 0):
                break
            await asyncio.sleep(_LABEL_RETRY_DELAY)
        if err:
            return f"Not labelled ({', '.join(wanted)}): {err}. The pull request was created; label it with add_github_labels."
        data = resp.json() if resp is not None else []
        got = [lb.get("name", "") for lb in (data if isinstance(data, list) else []) if isinstance(lb, dict)]
        return f"Labels: {', '.join(got) or 'none'}"

    async def _open_pull_for(client: Any, owner: str, name: str, head: str, base: str) -> dict[str, Any] | None:
        """The open pull request of *head* into *base*, or ``None`` when the read shows none or fails."""
        # A fork head is written "user:branch"; the list filters on it as given.
        qualified = head if ":" in head else f"{owner}:{head}"
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/pulls",
            _PROVIDER,
            f"read the open pull request of {head} in {owner}/{name}",
            headers=headers,
            params={"state": "open", "head": qualified, "base": base, "per_page": 5},
        )
        if err or resp is None:
            return None
        pulls = resp.json()
        found = pulls[0] if isinstance(pulls, list) and pulls and isinstance(pulls[0], dict) else None
        return found if found and found.get("number") and found.get("html_url") else None

    async def create_github_pr(
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
        work_item_id: str = "",
        assignees: list[str] | None = None,
        labels: list[str] | None = None,
        milestone: str | int | None = None,
        attachment_file_ids: list[str] | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Create a new pull request, named on *work_item_id* when given.

        *attachment_file_ids* are committed to the assets branch under
        ``xtm-assets/pr-<head>/`` (:mod:`.attachments`), never to *head*, and
        the body gains one line per file, after every refusal check passed.

        The title is checked first against the rule the repository declares on
        *base* (:mod:`.pr_title`): a title that breaks it is refused
        with the rule named and nothing is written. A closing keyword that
        points at a pull request (:mod:`.closing_refs`) is refused too.
        *assignees* are added through the Issues API right after creation; the
        result names the logins GitHub read back, and a failure to assign is
        reported without undoing the pull request. *labels* must exist on the
        repository (an unknown one refuses the call before any write) and are
        written first after creation, so a workflow reading the labels on
        ``opened`` sees them. The issues of the same repository the title or
        body closes give the pull request their type labels (``bug``,
        ``feature``) and, when no *milestone* is given, the milestone of the
        first one that has one (:mod:`.pr_inherit`); an explicit *milestone*
        (number or exact title) the repository does not define refuses the
        call, and a milestone is never created. Another open pull request of
        the same author that shares a file or an issue is named in a warning
        (:mod:`.pr_overlap`), never a refusal.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if work_item_id.strip() and check_work_item is not None:
            stop = await check_work_item(work_item_id.strip())
            if stop:
                return f"Error: no pull request opened, nothing was written: {stop}"
        wanted_labels = list(dict.fromkeys(lb.strip() for lb in (labels or []) if lb and lb.strip()))
        async with egress_client(timeout=60 if attachment_file_ids else 30) as client:
            refusal, title_note = await pr_title.checked_title(client, owner, name, base, title, headers)
            if refusal:
                return refusal
            refusal = await closing_refs.pr_closing_refusal(
                client, f"{owner}/{name}", {"title": title, "body": body}, headers, "create PR"
            )
            if refusal:
                return refusal
            if wanted_labels:
                unknown = await _unknown_labels(client, owner, name, wanted_labels)
                if unknown:
                    return (
                        f"Refused: label(s) not defined in {owner}/{name}: {', '.join(unknown)}. "
                        "Nothing was written; list them with list_github_labels and call again."
                    )
            milestone_number: int | None = None
            milestone_title = ""
            milestone_note = ""
            if milestone is not None and str(milestone).strip():
                milestone_number, milestone_title, refusal = await pr_inherit.resolve_milestone(
                    client, owner, name, milestone, headers
                )
                if refusal:
                    return refusal
            inherited = await pr_inherit.from_closing_issues(client, owner, name, title, body, headers)
            for label in inherited.labels:
                if label not in wanted_labels:
                    wanted_labels.append(label)
            if milestone_number is None and inherited.milestone_number is not None:
                milestone_number, milestone_title = inherited.milestone_number, inherited.milestone_title
                milestone_note = f" (from closing issue #{inherited.source_issue})"
            elif milestone_number is None and inherited.closing_issues:
                closing = ", ".join(f"#{n}" for n in inherited.closing_issues)
                milestone_note = f"No milestone: closing issue {closing} has none"
            lines, aerr = await attachments.attach_files(
                client, headers, repo, 0, attachment_file_ids, file_context, folder=f"pr-{head}"
            )
            if aerr:
                return aerr
            body = attachments.compose_body(body, lines)
            payload: dict[str, Any] = {"title": title, "head": head, "base": base}
            if body:
                payload["body"] = body
            if draft:
                payload["draft"] = True
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{owner}/{name}/pulls",
                _PROVIDER,
                f"create PR in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            already = False
            if err:
                pr = await _open_pull_for(client, owner, name, head, base) if _ALREADY_EXISTS_RE.search(err) else None
                if pr is None:
                    return err
                already = True
            else:
                pr = resp.json()
                await mark_self_action(repo, pr.get("number"))
            if already:
                # A repeat (a resumed turn, a retry) of a creation that landed: the open
                # pull request is the answer, nothing is labelled or assigned again.
                pr_head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
                created = (
                    f"PR #{pr['number']} already open, nothing created: {pr['html_url']} "
                    f"(head {head} at {str(pr_head.get('sha') or '')[:12]}, base {base})"
                )
                if not work_item_id or record_pull_request is None:
                    return created
                bound = await record_pull_request(
                    work_item_id.strip(),
                    f"{owner}/{name}",
                    int(pr["number"]),
                    pr_head.get("ref") or head,
                    pr_head.get("sha"),
                    pr.get("html_url"),
                    bool(pr.get("draft")),
                )
                return f"{created}\n{bound}"
            created = f"PR #{pr['number']} created: {pr['html_url']}"
            if lines:
                created = f"{created}\n{len(lines)} attachment(s) stored on branch {attachments.ASSETS_BRANCH}"
            if title_note:
                created = f"{created}\n{title_note}"
            if wanted_labels:
                labelled = await _label_pull_request(client, owner, name, int(pr["number"]), wanted_labels)
                created = f"{created}\n{labelled}"
            if milestone_number is not None:
                placed = await pr_inherit.set_milestone(
                    client,
                    owner,
                    name,
                    int(pr["number"]),
                    milestone_number,
                    milestone_title,
                    headers,
                    _LABEL_RETRY_DELAY,
                )
                created = f"{created}\n{placed}{milestone_note if placed.startswith('Milestone:') else ''}"
            elif milestone_note:
                created = f"{created}\n{milestone_note}"
            wanted = [a.strip() for a in (assignees or []) if a and a.strip()]
            if wanted:
                created = f"{created}\n{await _assign_pull_request(client, owner, name, int(pr['number']), wanted)}"
            try:
                overlap = await pr_overlap.overlap_warning(client, f"{owner}/{name}", pr, headers)
            except Exception:  # best effort: an overlap read never fails the creation
                overlap = None
            if overlap:
                created = f"{created}\n{overlap}"
            # The full CI the merge is gated on starts now, next to the pull_request
            # checks the opening triggers, not at the first merge call (#12067).
            started = await full_ci_gate.start_on_push(client, owner, name, pr, headers)
            if started:
                created = f"{created}\n{started}"
            if not work_item_id or record_pull_request is None:
                return created
            pr_head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
            bound = await record_pull_request(
                work_item_id.strip(),
                f"{owner}/{name}",
                int(pr["number"]),
                pr_head.get("ref") or head,
                pr_head.get("sha"),
                pr.get("html_url"),
                bool(pr.get("draft")),
            )
            return f"{created}\n{bound}"

    async def _current_body(client: Any, owner: str, name: str, number: int) -> tuple[str, str | None]:
        """The body GitHub serves for pull request *number*, before attachments are appended to it."""
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}",
            _PROVIDER,
            f"read PR #{number} in {owner}/{name}",
            headers=headers,
        )
        if err:
            return "", f"{err}\nNothing was written."
        data = resp.json() if resp is not None else {}
        return (str(data.get("body") or "") if isinstance(data, dict) else ""), None

    async def update_github_pr(
        repo: str,
        number: int,
        title: str = "",
        body: str = "",
        state: str = "",
        base: str = "",
        edits: list[dict[str, Any]] | None = None,
        expect_updated_at: str = "",
        attachment_file_ids: list[str] | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Update a pull request's title, body, base branch, or state (open/closed). Only provided fields change.

        ``edits`` change the body in place (:mod:`.body_edits`) instead of a
        whole ``body``; ``expect_updated_at`` refuses a write over a newer edit.
        ``attachment_file_ids`` are committed to the assets branch under
        ``xtm-assets/<number>/`` (:mod:`.attachments`), never to the pull
        request's branch, and one line per file ends the new body (the current
        body when neither ``body`` nor ``edits`` is given).

        A new title is checked against the repository's declared rule,
        read on *base* when given and on the default branch otherwise. A closing
        keyword that points at a pull request is refused.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if edits and body:
            return "Error: give 'body' or 'edits', not both. Nothing was written."
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if body:
            payload["body"] = body
        if state in ("open", "closed"):
            payload["state"] = state
        if base:
            payload["base"] = base
        if not payload and not edits and not attachment_file_ids:
            return "No fields to update. Provide title, body, edits, attachment_file_ids, state, or base."
        async with egress_client(timeout=60 if attachment_file_ids else 30) as client:
            title_note = None
            if title:
                refusal, title_note = await pr_title.checked_title(client, owner, name, base, title, headers)
                if refusal:
                    return refusal
            diff = ""
            if edits or expect_updated_at:
                new_body, diff, eerr = await body_edits.edited_body(
                    client,
                    f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}",
                    headers,
                    edits,
                    expect_updated_at,
                    f"PR #{number} in {repo}",
                )
                if eerr:
                    return eerr
                if edits:
                    body = new_body
                    payload["body"] = new_body
            if title or body:
                refusal = await closing_refs.pr_closing_refusal(
                    client, f"{owner}/{name}", {"title": title, "body": body}, headers, f"update PR #{number}"
                )
                if refusal:
                    return refusal
            stored = ""
            if attachment_file_ids:
                if not body:
                    current, cerr = await _current_body(client, owner, name, number)
                    if cerr:
                        return cerr
                    body = current
                lines, aerr = await attachments.attach_files(
                    client, headers, repo, number, attachment_file_ids, file_context
                )
                if aerr:
                    return aerr
                body = attachments.compose_body(body, lines)
                payload["body"] = body
                stored = f"\n{len(lines)} attachment(s) stored on branch {attachments.ASSETS_BRANCH}"
            resp, err = await _common.safe_request(
                client,
                "patch",
                f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}",
                _PROVIDER,
                f"update PR #{number} in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            if err:
                return err
            await mark_self_action(repo, number)
            updated = f"PR #{number} updated [{(resp.json() or {}).get('state', '?')}] in {owner}/{name}.{stored}"
            if title_note:
                updated = f"{updated}\n{title_note}"
            return f"{updated}\nBody diff:\n{diff}" if diff else updated

    async def create_github_pr_review(
        repo: str,
        pr_number: int,
        event: str,
        body: str = "",
        attachment_file_ids: list[str] | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Submit a PR review: APPROVE, REQUEST_CHANGES, or COMMENT, with files of the run attached."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        event_upper = event.upper()
        if event_upper not in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
            return f"Invalid event: {event}. Must be 'APPROVE', 'REQUEST_CHANGES', or 'COMMENT'."
        async with egress_client(timeout=60 if attachment_file_ids else 30) as client:
            lines, aerr = await attachments.attach_files(
                client, headers, repo, pr_number, attachment_file_ids, file_context
            )
            if aerr:
                return aerr
            body = attachments.compose_body(body, lines)
            payload: dict[str, Any] = {"event": event_upper}
            if body:
                payload["body"] = body
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{owner}/{name}/pulls/{pr_number}/reviews",
                _PROVIDER,
                f"submit review on PR #{pr_number} in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            if err:
                return err
            review = resp.json()
            await mark_self_action(repo, pr_number)
            return f"Review submitted ({event_upper}): {review.get('html_url', '')}"

    async def create_github_pr_review_comment(
        repo: str,
        pr_number: int,
        body: str,
        path: str,
        line: int,
        side: str = "RIGHT",
        commit_id: str = "",
        start_line: int = 0,
    ) -> str:
        """Add an inline review comment on a specific file and line of a pull request's diff."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            head_sha = commit_id.strip()
            if not head_sha:
                pr_resp, perr = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/pulls/{pr_number}",
                    _PROVIDER,
                    f"resolve head SHA for PR #{pr_number}",
                    headers=headers,
                )
                if perr:
                    return perr
                head_sha = ((pr_resp.json() or {}).get("head") or {}).get("sha", "")
                if not head_sha:
                    return f"Could not resolve the head commit SHA for PR #{pr_number}."
            payload: dict[str, Any] = {
                "body": body,
                "commit_id": head_sha,
                "path": path,
                "line": line,
                "side": side if side in ("LEFT", "RIGHT") else "RIGHT",
            }
            if start_line:
                payload["start_line"] = start_line
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{owner}/{name}/pulls/{pr_number}/comments",
                _PROVIDER,
                f"add review comment to PR #{pr_number} in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            if err:
                return err
            await mark_self_action(repo, pr_number)
            return f"Inline review comment added on {path}:{line}: {(resp.json() or {}).get('html_url', '')}"

    async def merge_github_pr(
        repo: str, number: int, merge_method: str = "merge", commit_title: str = "", commit_message: str = ""
    ) -> str:
        """Merge a pull request. merge_method can be 'merge', 'squash', or 'rebase'.

        Refused with nothing merged when the PR's title or body, or the commit
        title or message given, carries a closing keyword that points at a pull
        request: GitHub would close that other PR on this merge. The
        messages of the PR's own commits are checked too whenever they reach
        the base branch: a squash without *commit_message* (GitHub builds the
        default message from them), a merge commit or a rebase.

        A merged answer names the merge commit SHA, its signature verdict
        (read from the commit) and its date, so no read-back is needed.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if merge_method not in ("merge", "squash", "rebase"):
            return f"Invalid merge_method: {merge_method}. Must be 'merge', 'squash', or 'rebase'."
        async with egress_client(timeout=30) as client:
            texts = {"commit_title": commit_title, "commit_message": commit_message}
            pr_resp, pr_err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}",
                _PROVIDER,
                f"read PR #{number} in {repo} before the merge",
                headers=headers,
            )
            if not pr_err and pr_resp is not None:
                try:
                    pr_data = pr_resp.json()
                except Exception:
                    pr_data = None
                if isinstance(pr_data, dict):
                    texts = {
                        "pull request title": pr_data.get("title") or "",
                        "pull request body": pr_data.get("body") or "",
                        **texts,
                    }
            if merge_method != "squash" or not commit_message:
                texts.update(await closing_refs.pr_commit_messages(client, f"{owner}/{name}", number, headers))
            refusal = await closing_refs.pr_closing_refusal(
                client, f"{owner}/{name}", texts, headers, f"merge PR #{number}"
            )
            if refusal:
                return refusal
            refusal, loop_note = await review_loop.review_loop_check(client, owner, name, number, headers, "merged")
            if refusal:
                return refusal
            # A repository whose PR CI skips its suites requires their full run
            # green on the exact head first ([merge] full_ci_workflows, #11985).
            gate_note = ""
            gate_sha = ""
            if not pr_err and pr_resp is not None and isinstance(pr_data, dict):
                refusal, gate_note = await full_ci_gate.gate(
                    client, owner, name, number, pr_data, headers, park=park_full_ci
                )
                if refusal:
                    return refusal
                if gate_note.startswith(" Full CI on head"):
                    gate_sha = str((pr_data.get("head") or {}).get("sha") or "")
            payload: dict[str, Any] = {"merge_method": merge_method}
            if gate_sha:
                # The run was green on this SHA: a push in between gets GitHub's 409.
                payload["sha"] = gate_sha
            if commit_title:
                payload["commit_title"] = commit_title
            if commit_message:
                payload["commit_message"] = commit_message
            head_sha = ""
            base_ref = ""
            if not pr_err and pr_resp is not None and isinstance(pr_data, dict):
                head_sha = str((pr_data.get("head") or {}).get("sha") or "")
                base_ref = str((pr_data.get("base") or {}).get("ref") or "")

            async def _put_merge() -> tuple[Any, str | None]:
                return await _common.safe_request(
                    client,
                    "put",
                    f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}/merge",
                    _PROVIDER,
                    f"merge PR #{number} in {repo}",
                    headers=_json_headers(headers),
                    json=payload,
                    # Empty set: opt into receiving the failed response (so the
                    # 405 branch below can read the body) WITHOUT classifying 405
                    # as "expected" — a merge failure is a genuine error that must
                    # stay visible in the warning log, unlike the benign probe
                    # misses expected_statuses was designed to silence.
                    expected_statuses=set(),
                )

            async def _merge_state() -> tuple[str, str, str]:
                """(merge commit SHA or '', mergeable_state, head SHA) of the PR read back.

                The merge state is 'unknown' while GitHub computes it or when the
                read fails.
                """
                back, back_err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}",
                    _PROVIDER,
                    f"read PR #{number} in {repo} back after a merge error",
                    headers=headers,
                )
                if back_err or back is None:
                    return "", "unknown", ""
                try:
                    back_data = back.json()
                except Exception:
                    return "", "unknown", ""
                if not isinstance(back_data, dict):
                    return "", "unknown", ""
                now_head = str((back_data.get("head") or {}).get("sha") or "")
                if back_data.get("merged"):
                    return str(back_data.get("merge_commit_sha") or "unknown"), "merged", now_head
                state = str(back_data.get("mergeable_state") or "unknown").lower()
                if back_data.get("mergeable") is None:
                    state = "unknown"
                return "", state, now_head

            async def _merged_sha() -> str:
                """The merge commit SHA when the PR reads back merged, else ''."""
                return (await _merge_state())[0]

            async def _receipt(sha: str) -> str:
                """The commit receipt, then the full CI runs the merge was gated on."""
                return await _commit_receipt(sha) + gate_note + loop_note

            async def _commit_receipt(sha: str) -> str:
                """' Merge commit <sha>, signed ...' read from the commit itself.

                One read of the commit gives the signature verdict and the date,
                so the caller needs no read-back. A failed read is said as such.
                """
                if not sha or sha == "unknown":
                    return " Merge commit SHA not returned by GitHub: read it with `get_github_pr`."
                commit, commit_err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/commits/{sha}",
                    _PROVIDER,
                    f"read merge commit {sha[:12]} of PR #{number} in {repo}",
                    headers=headers,
                )
                data: Any = None
                if not commit_err and commit is not None:
                    try:
                        data = commit.json()
                    except Exception:
                        data = None
                inner = (data or {}).get("commit") if isinstance(data, dict) else None
                verification = (inner or {}).get("verification") if isinstance(inner, dict) else None
                if not isinstance(verification, dict) or "verified" not in verification:
                    return f" Merge commit {sha}; signature not read (commit read failed)."
                reason = str(verification.get("reason") or "")
                if verification.get("verified"):
                    verdict = f"signed ({reason})"
                else:
                    verdict = f"NOT verified ({reason or 'unsigned'})"
                when = str(((inner or {}).get("committer") or {}).get("date") or "")
                return f" Merge commit {sha}, {verdict}" + (f", committed {when}" if when else "") + "."

            resp, err = await _put_merge()
            attempts = 1
            # A gateway error or "merge already in progress" leaves the merge in
            # an unknown state: GitHub may have applied it. Read the PR back
            # before any new attempt, and pin each retry to the head read before
            # the first one so a push in between is never merged unseen.
            while err and _merge_outcome_unknown(resp, err):
                landed = await _merged_sha()
                if landed:
                    await mark_self_action(repo, number)
                    return (
                        f"PR #{number} merged ({merge_method}): merge commit {landed[:12]}. GitHub "
                        f"answered an error on attempt {attempts} ({_merge_error_brief(resp, err)}), "
                        f"but the read-back shows the PR merged." + await _receipt(landed)
                    )
                if attempts > len(_MERGE_RETRY_DELAYS):
                    return (
                        f"Error: PR #{number} in {repo} is NOT merged after {attempts} attempt(s): "
                        f"GitHub's last answer was {_merge_error_brief(resp, err)}, and the PR still "
                        f"reads open and unmerged. Nothing else was tried. Read it again later with "
                        f"`get_github_pr` and call `merge_github_pr` once more if it is still open and green."
                    )
                await asyncio.sleep(_MERGE_RETRY_DELAYS[attempts - 1])
                if head_sha:
                    payload["sha"] = head_sha
                resp, err = await _put_merge()
                attempts += 1
            if err and _is_base_modified(resp):
                # The base moved seconds before the PUT. GitHub recomputes the
                # PR's mergeability; a clean PR merges once it has, without a
                # branch update and the CI round that would cost.
                state = "unknown"
                for delay in _BASE_MODIFIED_POLL_DELAYS:
                    await asyncio.sleep(delay)
                    landed, state, now_head = await _merge_state()
                    if landed:
                        await mark_self_action(repo, number)
                        return (
                            f"PR #{number} merged ({merge_method}): merge commit {landed[:12]}. "
                            f"GitHub first answered 'Base branch was modified'; the read-back shows the PR merged."
                            + await _receipt(landed)
                        )
                    if head_sha and now_head and now_head != head_sha:
                        return merge_refusals.head_moved_line(repo, number, head_sha, now_head) + (
                            " GitHub answered: Base branch was modified."
                        )
                    if state != "unknown":
                        break
                if state in _BASE_MODIFIED_RETRY_STATES:
                    if head_sha:
                        payload["sha"] = head_sha
                    resp, err = await _put_merge()
                    attempts += 1
                    if not err:
                        data = resp.json()
                        await mark_self_action(repo, number)
                        return (
                            f"PR #{number} merged ({merge_method}) (attempt {attempts}, after GitHub "
                            f"recomputed mergeability once the base moved): "
                            f"{str(data.get('message') or 'Success').rstrip('.')}."
                            + await _receipt(str(data.get("sha") or ""))
                        )
                elif state in _BASE_MODIFIED_UPDATE_STATES:
                    return (
                        f"Error: Cannot merge PR #{number} in {repo}: the base branch moved and the PR now "
                        f"reads '{state}'. Call `manage_github_pr` with action='update_branch' (a 'dirty' "
                        f"PR answers the conflict analysis), wait for CI on the new head, then merge again."
                    )
                else:
                    return (
                        f"Error: PR #{number} in {repo} is NOT merged: GitHub answered 'Base branch was "
                        f"modified' and the PR's merge state reads '{state}' after about "
                        f"{int(sum(_BASE_MODIFIED_POLL_DELAYS))} s. Mergeability is still recomputing or "
                        f"a check is failing: read it with `get_github_pr` and call `merge_github_pr` again "
                        f"when it reads clean. No branch update is needed for a clean PR."
                    )
            if err:
                if resp is not None and resp.status_code == 409:
                    # "Head branch was modified": name the head now, never a bare refusal.
                    landed, _state, now_head = await _merge_state()
                    if landed:
                        await mark_self_action(repo, number)
                        return (
                            f"PR #{number} merged ({merge_method}): merge commit {landed[:12]}. GitHub answered "
                            f"409, but the read-back shows the PR merged." + await _receipt(landed)
                        )
                    if now_head and now_head != head_sha:
                        return merge_refusals.head_moved_line(repo, number, head_sha, now_head)
                    return err
                if resp is not None and resp.status_code == 405:
                    try:
                        github_msg = ((resp.json() or {}).get("message", "") or "").strip()
                    except Exception:
                        github_msg = ""
                    # GitHub's message may or may not end with punctuation
                    # ("Base branch was modified. Review and try the merge
                    # again." vs "Pull Request is not mergeable") — normalise
                    # so the concatenated guidance never produces a double
                    # period or a missing sentence break.
                    if github_msg and github_msg[-1] not in ".!?":
                        github_msg += "."
                    if github_msg and "conflict" in github_msg.lower():
                        return (
                            f"Error: Cannot merge PR #{number} in {repo} — {github_msg} "
                            f"The PR has unresolved merge conflicts that must be fixed in the "
                            f"branch before this PR can be merged. Ask the PR author to resolve "
                            f"the conflicts and push an updated commit."
                        )
                    # The real cause, read from GitHub: a merge queue (enqueued
                    # there), a draft, a missing review, a check, a rule.
                    answer = await merge_refusals.refusal_answer(
                        client, headers, owner, name, number, base_ref, head_sha, github_msg
                    )
                    if answer:
                        if answer.startswith(f"PR #{number} in {repo} queued"):
                            await mark_self_action(repo, number)
                        return answer
                    return (
                        f"Error: Cannot merge PR #{number} in {repo} — "
                        f"{github_msg or 'the PR is not in a mergeable state (base branch was modified).'} "
                        f"Re-fetch the PR state with `get_github_pr` to confirm it is still open, "
                        f"then call `manage_github_pr` with action='update_branch' to sync the PR "
                        f"branch with the latest base, wait for CI to pass, and retry the merge. "
                        f"Do NOT retry the merge immediately on the same stale state."
                    )
                return err
            data = resp.json()
            await mark_self_action(repo, number)
            retried = f" (attempt {attempts})" if attempts > 1 else ""
            message = str(data.get("message") or "Success").rstrip(".")
            receipt = await _receipt(str(data.get("sha") or ""))
            return f"PR #{number} merged ({merge_method}){retried}: {message}.{receipt}"

    def _reviewer_key(login: str) -> str:
        """One key per reviewer: logins are case-insensitive, and GitHub names the Copilot
        reviewer 'Copilot' in the REST answer while the request takes its bot account."""
        key = login.lower().removesuffix("[bot]")
        return "copilot" if key in {"copilot", "copilot-pull-request-reviewer"} else key

    async def request_github_reviewers(repo: str, number: int, reviewers: list[str]) -> str:
        """Request reviewers on a pull request by GitHub username.

        The alias "copilot" is automatically resolved to the GitHub
        Copilot code-review bot account.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        copilot_bot = "copilot-pull-request-reviewer[bot]"
        copilot_aliases = {"copilot", "github-copilot", "copilot-review"}
        resolved = [copilot_bot if r.lower() in copilot_aliases else r for r in reviewers]
        async with egress_client(timeout=30) as client:
            # A review request is idempotent (requesting a reviewer twice keeps one request),
            # so a 404 is retried: GitHub answers it for a pull request created a moment
            # before, until its replicas see it. The budget bounds a genuinely missing one.
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}/requested_reviewers",
                _PROVIDER,
                f"request reviewers on PR #{number} in {repo}",
                headers=_json_headers(headers),
                json={"reviewers": resolved},
                retry_statuses=_REVIEW_REQUEST_RETRY_STATUSES,
                max_total_seconds=_REVIEW_REQUEST_RETRY_SECONDS,
            )
            if err:
                return err
            actual = {r["login"] for r in (resp.json() if resp else {}).get("requested_reviewers", [])}
            await mark_self_action(repo, number)
            confirmed = {_reviewer_key(login) for login in actual}
            missing = [r for r in resolved if _reviewer_key(r) not in confirmed]
            if missing:
                # The REST answer leaves a bot reviewer (Copilot) out of requested_reviewers
                # even when GitHub took the request: read the pull request's own record.
                data, gerr = await _common.gh_graphql(
                    client,
                    headers,
                    _REVIEW_REQUESTS_QUERY,
                    {"owner": owner, "name": name, "number": number},
                    action=f"read the review requests of PR #{number}",
                )
                if not gerr and data:
                    confirmed |= {_reviewer_key(login) for login in _requested_logins(data)}
                    actual = actual | {r for r in missing if _reviewer_key(r) in confirmed}
                    missing = [r for r in resolved if _reviewer_key(r) not in confirmed]
            if missing:
                return (
                    f"Warning: reviewers requested on PR #{number} but these were NOT confirmed by GitHub: "
                    f"{', '.join(missing)}. Confirmed reviewers: {', '.join(actual) or 'none'}"
                )
            return f"Reviewers requested on PR #{number}: {', '.join(resolved)}"

    async def _update_branch_up_to_date(client: Any, owner: str, name: str, number: int) -> ToolResult:
        """GitHub's 422 'no new commits on the base branch': the branch already holds its base, a no-op."""
        repo = f"{owner}/{name}"
        pr_url = f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}"
        pr_resp, pr_err = await _common.safe_request(
            client, "get", pr_url, _PROVIDER, f"read PR #{number} in {repo}", headers=headers
        )
        pr = _json_object(pr_resp) if not pr_err else {}
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
        head_sha = _coerce_str(head.get("sha")) or "unread"
        base_ref = _coerce_str(base.get("ref")) or "its base"
        base_sha = _coerce_str(base.get("sha"))
        at = f" at {base_sha[:8]}" if base_sha else ""
        return ToolResult(
            tool_call_id="",
            content=(
                f"No change: the branch of PR #{number} in {repo} is already up to date with "
                f"'{base_ref}'{at}; head {head_sha}. Nothing was merged."
            ),
            no_op=True,
        )

    async def _drop_stopped_alignment(client: Any, owner: str, name: str, number: int) -> tuple[str, str | None]:
        """Move the PR branch off the take-the-ref commits a stopped line merge left: ``(note, refusal)``.

        A call stopped between the alignment commits and the merge (a rollout) leaves the conflicting paths
        holding the base's version. GitHub's update-branch then merges cleanly over them and the PR's own side
        of those paths is lost without a word (xtm-one #1254: 24 i18n keys). merge_github_branch drops them
        first; update_branch does the same before asking GitHub, so a repeat resolves both sides again.
        """
        repo = f"{owner}/{name}"
        repo_url = f"{_BASE_URL}/repos/{owner}/{name}"
        pr_resp, pr_err = await _common.safe_request(
            client, "get", f"{repo_url}/pulls/{number}", _PROVIDER, f"read PR #{number} in {repo}", headers=headers
        )
        if pr_err:
            return "", None
        pr = _json_object(pr_resp)
        head = pr.get("head") if isinstance(pr, dict) else None
        head_repo = head.get("repo") if isinstance(head, dict) else None
        branch = _coerce_str(head.get("ref")) if isinstance(head, dict) else ""
        owner_repo = _coerce_str(head_repo.get("full_name")) if isinstance(head_repo, dict) else ""
        if not branch or owner_repo != repo:
            return "", None
        return await drop_stopped_alignment(client, headers, repo, repo_url, branch)

    async def _update_branch(client: Any, owner: str, name: str, number: int) -> str | ToolResult:
        """GitHub's update-branch on PR #*number*, or the line merge of :func:`_update_branch_conflict` on a 422."""
        repo = f"{owner}/{name}"
        resp, err = await _common.safe_request(
            client,
            "put",
            f"{_BASE_URL}/repos/{owner}/{name}/pulls/{number}/update-branch",
            _PROVIDER,
            f"update branch for PR #{number}",
            headers=_json_headers(headers),
            json={},
            expected_statuses={422},
        )
        if resp is not None and resp.status_code == 422:
            return await _update_branch_conflict(client, owner, name, number, resp, err or "")
        if err:
            return err
        await mark_self_action(repo, number)
        return f"PR #{number} branch update queued: {(resp.json() or {}).get('message', 'Success')}"

    async def _update_branch_conflict(
        client: Any, owner: str, name: str, number: int, resp: httpx.Response, err: str
    ) -> str | ToolResult:
        """A refused update_branch: on a conflict, the line merge landed, or its hunks and the merge_github_branch call."""
        repo = f"{owner}/{name}"
        message = _coerce_str(_json_object(resp).get("message"))
        if _UP_TO_DATE_RE.search(message):
            return await _update_branch_up_to_date(client, owner, name, number)
        if "conflict" not in message.lower():
            return err or f"Error: GitHub refused to update the branch of PR #{number} in {repo}: {message}"
        repo_url = f"{_BASE_URL}/repos/{owner}/{name}"
        pr_resp, pr_err = await _common.safe_request(
            client, "get", f"{repo_url}/pulls/{number}", _PROVIDER, f"read PR #{number} in {repo}", headers=headers
        )
        pr = _json_object(pr_resp) if not pr_err else {}
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
        head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
        branch = _coerce_str(head.get("ref"))
        base_ref = _coerce_str(base.get("ref"))
        lead = (
            f"Error: the branch of PR #{number} in {repo} cannot take '{base_ref or 'its base'}': "
            "GitHub reports a merge conflict."
        )
        if not branch or not base_ref or _coerce_str(head_repo.get("full_name")) != repo:
            return (
                f"{lead} The PR's branch is not in {repo} (a fork) or could not be read, "
                "so the conflict is resolved there."
            )
        text = f"Merge {base_ref} into {branch}"
        before = await _branch_tip(client, headers, repo, repo_url, branch)
        answer = await conflict_answer(client, headers, repo, repo_url, branch, base_ref, text, {})
        if ref_moved_and_undone(answer):
            # The base moved between the line merge and the merge itself, and nothing stayed on the branch: the
            # line merge is run once more against the base's new commit instead of handing the agent a refusal.
            answer = await conflict_answer(client, headers, repo, repo_url, branch, base_ref, text, {})
        if answer.startswith("Merged"):
            # Every path merged line by line without an overlap: the branch took its base in signed commits.
            await mark_self_action(repo, number)
            return f"GitHub refused to update the branch of PR #{number} on a conflict; merged line by line instead. {answer}"
        if answer.startswith("No change"):
            # The same merge landed while this call ran (a replayed or concurrent call): a success, not a refusal.
            return ToolResult(
                tool_call_id="",
                content=f"The branch of PR #{number} in {repo} already holds '{base_ref}'. {answer}",
                no_op=True,
            )
        # Read from the branch itself: an error above can follow commits that landed.
        outcome = await outcome_line(client, headers, repo, repo_url, branch, before, answer)
        if "landed true" in outcome:
            await mark_self_action(repo, number)
            base_tip = await _branch_tip(client, headers, repo, repo_url, base_ref)
            if base_tip and await _already_merged(client, headers, repo, repo_url, branch, base_tip):
                # The merge landed although its answer was an error (a lost answer, a replayed call): the
                # branch holds the base, so the outcome leads, never "Error" over work that was written.
                return (
                    f"Merged (landed): the branch of PR #{number} in {repo} now holds '{base_ref}' "
                    f"({base_tip[:12]}) although the merge call answered an error.\n{outcome}\n{answer}"
                )
        return f"{lead}\n{answer}\n{outcome}"

    async def _toggle_draft(
        client: httpx.AsyncClient, owner: str, name: str, number: int, node_id: str | None, action: str
    ) -> str:
        """Mark a PR ready for review or convert it to draft, safe on a transient GitHub answer.

        A mutation answer of unknown outcome (``_draft_outcome_unknown``) is
        followed by a read of the PR's ``draft`` flag: already in the target
        state means applied, nothing sent again; otherwise the mutation is
        sent again after each pause of ``_DRAFT_RETRY_DELAYS``. A definite
        refusal is answered as it is.
        """
        repo = f"{owner}/{name}"
        verb = action.replace("_", " ")
        want_draft = action == "convert_to_draft"
        mut_name = "markPullRequestReadyForReview" if action == "ready_for_review" else "convertPullRequestToDraft"
        query = (
            f"mutation($id:ID!){{{mut_name}(input:{{pullRequestId:$id}})"
            "{pullRequest{number isDraft}}}"
        )
        attempts = 0
        while True:
            attempts += 1
            _data, gerr = await _common.gh_graphql(
                client, headers, query, {"id": node_id}, action=f"{action} PR #{number}"
            )
            if not gerr:
                await mark_self_action(repo, number)
                suffix = f" (attempt {attempts})" if attempts > 1 else ""
                return f"PR #{number}: {verb} applied{suffix}."
            if not _draft_outcome_unknown(gerr):
                return gerr
            draft = await _pr_draft(client, owner, name, number, headers)
            if draft is want_draft:
                await mark_self_action(repo, number)
                state = "draft" if want_draft else "ready for review"
                return (
                    f"PR #{number}: {verb} applied (GitHub answered a transient error on attempt {attempts}; "
                    f"the read-back shows the PR {state})."
                )
            if attempts > len(_DRAFT_RETRY_DELAYS):
                seen = "not read" if draft is None else ("draft" if draft else "ready for review")
                return (
                    f"{gerr}\nPR #{number}: {verb} NOT applied after {attempts} attempts; "
                    f"the last read-back shows the PR {seen}."
                )
            await asyncio.sleep(_DRAFT_RETRY_DELAYS[attempts - 1])

    async def manage_github_pr(repo: str, number: int, action: str, merge_method: str = "squash") -> str | ToolResult:
        """Manage a pull request's draft/auto-merge/branch state (write).

        ``action`` is one of:
          - ``ready_for_review`` / ``convert_to_draft`` — toggle draft state.
          - ``update_branch`` — merge the base branch into the PR branch.
          - ``enable_automerge`` — auto-merge once requirements pass
            (``merge_method`` = merge | squash | rebase).
          - ``disable_automerge`` — cancel a pending auto-merge.
          - ``dequeue`` — take the PR out of its base's merge queue (nothing
            written when it is not queued).
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        action = (action or "").strip().lower()
        async with egress_client(timeout=30) as client:
            if action == "update_branch":
                dropped, refusal = await _drop_stopped_alignment(client, owner, name, number)
                if refusal:
                    return refusal
                answer = await _update_branch(client, owner, name, number)
                if not dropped:
                    return answer
                if isinstance(answer, ToolResult):
                    answer.content = f"{answer.content}\n{dropped}"
                    return answer
                return f"{answer}\n{dropped}"
            if action in ("ready_for_review", "enable_automerge"):
                verb = "marked ready" if action == "ready_for_review" else "set to auto-merge"
                refusal = await review_loop.review_loop_refusal(client, owner, name, number, headers, verb)
                if refusal:
                    return refusal
            if action == "enable_automerge":
                refusal = await _automerge_review_refusal(client, owner, name, number, headers)
                if refusal:
                    return refusal
            node_id, e = await _pr_node_id(client, owner, name, number, headers)
            if e:
                return e
            if action in ("ready_for_review", "convert_to_draft"):
                return await _toggle_draft(client, owner, name, number, node_id, action)
            if action == "enable_automerge":
                method = merge_method.upper()
                if method not in _MERGE_METHODS:
                    method = "SQUASH"
                query = (
                    "mutation($id:ID!,$m:PullRequestMergeMethod!){"
                    "enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:$m})"
                    "{pullRequest{number autoMergeRequest{enabledAt}}}}"
                )
                _data, gerr = await _common.gh_graphql(
                    client, headers, query, {"id": node_id, "m": method}, action=f"enable auto-merge on PR #{number}"
                )
                if gerr:
                    return gerr
                await mark_self_action(repo, number)
                return f"Auto-merge ({method.lower()}) enabled on PR #{number}. It will merge once all requirements pass."
            if action == "disable_automerge":
                query = (
                    "mutation($id:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$id})"
                    "{pullRequest{number}}}"
                )
                _data, gerr = await _common.gh_graphql(
                    client, headers, query, {"id": node_id}, action=f"disable auto-merge on PR #{number}"
                )
                if gerr:
                    return gerr
                await mark_self_action(repo, number)
                return f"Auto-merge disabled on PR #{number}."
            if action == "dequeue":
                return await _dequeue(client, repo, number, node_id)
            return (
                "Error: invalid action. Use ready_for_review, convert_to_draft, update_branch, "
                "enable_automerge, disable_automerge, or dequeue."
            )

    async def _dequeue(client: Any, repo: str, number: int, node_id: str | None) -> str:
        """Take a pull request out of its merge queue, or say it is not queued."""
        read = "query($id:ID!){node(id:$id){... on PullRequest{isInMergeQueue mergeQueueEntry{position state}}}}"
        data, gerr = await _common.gh_graphql(
            client, headers, read, {"id": node_id}, action=f"read the merge queue entry of PR #{number}"
        )
        if gerr:
            return gerr
        node = (data or {}).get("node") or {}
        entry = node.get("mergeQueueEntry") if isinstance(node, dict) else None
        if not isinstance(entry, dict) and not (isinstance(node, dict) and node.get("isInMergeQueue")):
            return f"PR #{number} is not in a merge queue: nothing to do, nothing written."
        position = entry.get("position") if isinstance(entry, dict) else None
        mutation = "mutation($id:ID!){dequeuePullRequest(input:{id:$id}){mergeQueueEntry{state}}}"
        _data, gerr = await _common.gh_graphql(
            client, headers, mutation, {"id": node_id}, action=f"dequeue PR #{number}"
        )
        if gerr:
            return gerr
        await mark_self_action(repo, number)
        where = f" (it was at position {position})" if isinstance(position, int) else ""
        return f"PR #{number} removed from the merge queue{where}. Push, then merge_github_pr enqueues it again."

    executor.register("list_github_prs", list_github_prs)
    executor.register("get_github_pr", get_github_pr)
    executor.register("get_github_pr_diff", get_github_pr_diff)
    executor.register("get_github_pr_files", get_github_pr_files)
    executor.register("list_github_pr_commits", list_github_pr_commits)
    executor.register("list_github_pr_review_comments", list_github_pr_review_comments)
    executor.register("create_github_pr", create_github_pr)
    executor.register("update_github_pr", update_github_pr)
    executor.register("create_github_pr_review", create_github_pr_review)
    executor.register("create_github_pr_review_comment", create_github_pr_review_comment)
    executor.register("merge_github_pr", merge_github_pr)
    executor.register("request_github_reviewers", request_github_reviewers)
    executor.register("manage_github_pr", manage_github_pr)

    _repo = {"type": "string", "description": "Repository in format owner/name"}

    return [
        ToolDefinition(
            name="list_github_prs",
            description=(
                "List pull requests in a repository with body preview. Filter by state, sort order, base branch, "
                "author (login) and head branch prefix; the author and prefix filters read up to 300 of the most "
                "recent pull requests and return only the matching rows. include_preview=false drops the body "
                "previews (one line per PR). Shows title, author, labels, reviewers, and branch info. Only use "
                "get_github_pr when you need the full body, reviews, or detailed changes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "sort": {"type": "string", "enum": ["created", "updated", "popularity", "long-running"], "default": "updated"},
                    "base": {"type": "string", "description": "Filter by base branch (optional)", "default": ""},
                    "count": {"type": "integer", "description": "Number of PRs (max 30)", "default": 20},
                    "author": {
                        "type": "string",
                        "description": "Only PRs opened by this GitHub login (optional)",
                        "default": "",
                    },
                    "head_prefix": {
                        "type": "string",
                        "description": "Only PRs whose head branch starts with this prefix, e.g. 'fix/' (optional)",
                        "default": "",
                    },
                    "include_preview": {
                        "type": "boolean",
                        "description": "Include each PR's body preview (default true; false: one line per PR)",
                        "default": True,
                    },
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_pr",
            # State other actors move (CI, reviews, other runs): a re-read later is a new question.
            live_read=True,
            description=(
                "Read a pull request's state: branches and head SHA, draft/merged, mergeable state, and on an open PR "
                "the checks summary of the head (passed/failed/pending, failing names) and the unresolved review "
                "thread count; then the first 1,500 characters of the body, one line per review and the changed "
                "files. include_body=true reads the body by window (offset, max_chars) and the reviews whole. Use "
                "get_github_pr_diff or get_github_pr_files for the actual code changes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Pull request number"},
                    "include_body": {
                        "type": "boolean",
                        "description": "Read the body by window and the reviews whole (default false: the body's head)",
                        "default": False,
                    },
                    "offset": {"type": "integer", "description": "With include_body: body character to start at (default 0)", "default": 0},
                    "max_chars": {
                        "type": "integer",
                        "description": "With include_body: body characters to return (default 20,000, at most 60,000)",
                        "default": 0,
                    },
                },
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_pr_diff",
            description=(
                "Get the unified diff of a pull request — the actual code changes in diff format. Essential for code "
                "review. Large diffs are truncated; with to_workspace_path the whole diff lands in the sandbox "
                "workspace instead. get_github_pr_files lists the files and finds hunks by pattern."
            ),
            input_schema={
                "type": "object",
                "properties": {"repo": _repo, "number": {"type": "integer", "description": "Pull request number"}},
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=0,
            workspace_ingest=True,
        ),
        ToolDefinition(
            name="get_github_pr_files",
            description=(
                "The files a pull request changes, one line each (status, path, +/-), no patch. path narrows them, "
                "pattern answers only the matching hunks, include_patch reads the patches by window."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Pull request number"},
                    "path": {
                        "type": "string",
                        "description": "Glob ({a,b} groups expand), directory or file the files must match",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression: only the hunks whose lines match",
                    },
                    "include_patch": {"type": "boolean", "description": "The patches of the selected files"},
                    "offset": {"type": "integer", "description": "Character offset of the patch or hunk window"},
                    "max_chars": {"type": "integer", "description": "Window size (default 20,000, at most 60,000)"},
                },
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_pr_commits",
            description="List the commits that make up a pull request (short SHA + summary).",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Pull request number"},
                    "count": {"type": "integer", "description": "Max commits (max 100)", "default": 50},
                },
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_pr_review_comments",
            description="List inline review comments (diff comments anchored to file/line) on a pull request, with author and location.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Pull request number"},
                    "count": {"type": "integer", "description": "Max comments (max 100)", "default": 50},
                },
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_github_pr",
            description=(
                "Create a new pull request from head branch to base branch. A title that breaks the title rule "
                "the repository declares ([pull_request] in .xtm-one/checks.toml) is refused, the rule named, nothing written."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "title": {"type": "string", "description": "PR title, in the repository's title rule when it declares one"},
                    "head": {"type": "string", "description": "Source branch name (e.g. 'feature/my-feature')"},
                    "base": {"type": "string", "description": "Target branch name (e.g. 'main')"},
                    "body": {"type": "string", "description": "PR description in Markdown (optional)", "default": ""},
                    "draft": {"type": "boolean", "description": "Create as draft PR", "default": False},
                    "assignees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "GitHub logins to assign in the same call (optional); the result names who was assigned",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Existing labels set right after creation, before CI reads them (optional); unknown ones refuse the call",
                    },
                    "milestone": {
                        "type": "string",
                        "description": (
                            "Existing milestone, number or exact title (optional; never created, unknown refuses). "
                            "Default: the milestone of the issue the PR closes; its bug/feature label is copied too"
                        ),
                    },
                    "attachment_file_ids": attachments.SCHEMA,
                    **(
                        {
                            "work_item_id": {
                                "type": "string",
                                "description": (
                                    "Work item this PR is opened for (optional): the item names the PR, "
                                    "and is dispatched to its owner again when the PR's CI completes; "
                                    "items blocked on it wait for its deployment where observed"
                                ),
                                "default": "",
                            }
                        }
                        if record_pull_request is not None
                        else {}
                    ),
                },
                "required": ["repo", "title", "head", "base"],
            },
            source=_SOURCE,
            priority=1,
            # A resumed turn's repeat of a pull request that landed (its head branch) is answered with it.
            landed_probe=pull_probe(headers),
            landed_key=pull_key,
        ),
        ToolDefinition(
            name="update_github_pr",
            description="Update a pull request's title, body, base branch, or state (open/closed). Only provided fields change. Use state='closed' to close without merging.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Pull request number"},
                    "title": {"type": "string", "description": "New title (optional)", "default": ""},
                    "body": {"type": "string", "description": "New body in Markdown (optional)", "default": ""},
                    "state": {"type": "string", "enum": ["open", "closed"], "description": "New state (optional)"},
                    "base": {"type": "string", "description": "New base branch (optional)", "default": ""},
                    "edits": body_edits.EDITS_SCHEMA,
                    "expect_updated_at": body_edits.EXPECT_UPDATED_AT_SCHEMA,
                    "attachment_file_ids": attachments.SCHEMA,
                },
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="create_github_pr_review",
            description="Submit a PR review: APPROVE, REQUEST_CHANGES, or COMMENT with optional body.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "pr_number": {"type": "integer", "description": "Pull request number"},
                    "event": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"], "description": "Review action"},
                    "body": {"type": "string", "description": "Review comment (required for REQUEST_CHANGES)", "default": ""},
                    "attachment_file_ids": attachments.SCHEMA,
                },
                "required": ["repo", "pr_number", "event"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="create_github_pr_review_comment",
            description=(
                "Add an inline review comment on a specific file and line of a pull request's diff. 'line' is the line "
                "number in the file; side=RIGHT (added/context) or LEFT (deleted). Set start_line for a multi-line "
                "comment. commit_id defaults to the PR head SHA."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "pr_number": {"type": "integer", "description": "Pull request number"},
                    "body": {"type": "string", "description": "Comment text (Markdown)"},
                    "path": {"type": "string", "description": "File path the comment applies to"},
                    "line": {"type": "integer", "description": "Line number in the file"},
                    "side": {"type": "string", "enum": ["LEFT", "RIGHT"], "default": "RIGHT"},
                    "commit_id": {"type": "string", "description": "Commit SHA (defaults to the PR head)", "default": ""},
                    "start_line": {"type": "integer", "description": "Start line for a multi-line comment (optional)", "default": 0},
                },
                "required": ["repo", "pr_number", "body", "path", "line"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="merge_github_pr",
            description=(
                "Merge a pull request using merge commit, squash, or rebase strategy. On a base protected by a "
                "merge queue the PR is added to the queue (answered queued, with its position). A repository "
                "declaring [merge] full_ci_workflows merges only once that full run is green on the head "
                "(dispatched when absent)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Pull request number"},
                    "merge_method": {"type": "string", "enum": ["merge", "squash", "rebase"], "description": "Merge strategy", "default": "merge"},
                    "commit_title": {"type": "string", "description": "Custom merge commit title (optional)", "default": ""},
                    "commit_message": {"type": "string", "description": "Custom merge commit message (optional)", "default": ""},
                },
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="request_github_reviewers",
            description=(
                "Request specific GitHub users as reviewers on a pull request. To request GitHub Copilot code review, "
                'pass "copilot" as a reviewer name — it is automatically resolved to the correct bot account.'
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Pull request number"},
                    "reviewers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'GitHub usernames to request as reviewers. Use "copilot" for GitHub Copilot code review.',
                    },
                },
                "required": ["repo", "number", "reviewers"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_github_pr",
            description=(
                "Manage a pull request's draft / auto-merge / branch state. action=ready_for_review or convert_to_draft "
                "toggles draft; update_branch merges the base branch into the PR branch (refused on a conflict, it merges "
                "line by line on the server and lands when no hunk overlaps, else names the hunks and the "
                "merge_github_branch call that resolves them); enable_automerge auto-merges "
                "once all requirements pass (merge_method=merge|squash|rebase; refused while the PR waits for an "
                "approving review); disable_automerge cancels it; "
                "dequeue takes it out of its merge queue (nothing written when it is not queued)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Pull request number"},
                    "action": {
                        "type": "string",
                        "enum": [
                            "ready_for_review",
                            "convert_to_draft",
                            "update_branch",
                            "enable_automerge",
                            "disable_automerge",
                            "dequeue",
                        ],
                    },
                    "merge_method": {"type": "string", "enum": ["merge", "squash", "rebase"], "default": "squash"},
                },
                "required": ["repo", "number", "action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
