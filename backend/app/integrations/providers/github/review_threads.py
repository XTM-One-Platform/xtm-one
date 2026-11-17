"""Pull request review threads: list, answer, resolve.

A pull request merges once every review thread is answered and resolved
(a code review bot's included), but the REST review comments carry neither a
thread id nor a resolution state. These two tools wrap the GraphQL surface
an agent babysitting its pull requests needs:

* ``list_github_pr_review_threads`` - one read: each thread's id, path and
  line, outdated flag, its first comment and its last reply's author, with
  the counts of unresolved and resolved threads. GitHub pages review threads
  by 100: the tool follows the cursor through every page before it states a
  count, so the counts cover the whole pull request. When paging stops early
  (the page cap, or a later page that fails) the answer says how many
  threads were read of how many, and never claims there is no unresolved
  thread.
* ``manage_github_pr_review_thread`` - ``reply``, ``resolve``, ``unresolve``
  or ``reply_and_resolve`` on one thread, its state read back.

A reply posted while the account holds a pending (unsubmitted) review on the
pull request joins that review: GitHub answers it with the state ``PENDING``
and nobody else sees it. The reply reads the comment's state back and
a pending reply is an error naming the review; the review is never submitted
by the tool, since that publishes all of it. The listing names any pending
review of the account.

Replies to one pull request are posted one after another. An agent
answering several threads issues the calls in one tool round, which the loop
runs concurrently; GitHub then attached concurrent replies to pending reviews
of the account (4 of 4 in parallel came back ``PENDING``, 4 of 4 one at a
time came back submitted). A per pull request lock in the process serializes
the reply mutations; resolving and other pull requests are not held.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _coerce_str, parse_repo

_SOURCE = "integration:github"

#: Threads read per page (GitHub pages review threads by 100 at most).
_PAGE_SIZE = 100
#: Pages read before the listing says it is partial (5,000 threads).
_MAX_PAGES = 50
#: Characters of a comment shown in a listing.
_PREVIEW_CHARS = 240
_STATES = ("unresolved", "resolved", "all")
_ACTIONS = ("reply", "resolve", "unresolve", "reply_and_resolve")

_LIST_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state
      pending: reviews(states: PENDING, first: 5) {
        nodes { id author { login } comments { totalCount } }
      }
      reviewThreads(first: $first, after: $after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          originalLine
          resolvedBy { login }
          first: comments(first: 1) { totalCount nodes { author { login } body url } }
          last: comments(last: 1) { nodes { author { login } } }
        }
      }
    }
  }
}
"""

_REPLY = """
mutation($thread: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $thread, body: $body}) {
    comment { url state pullRequestReview { id state comments { totalCount } } }
  }
}
"""

_RESOLVE = """
mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) { thread { id isResolved } }
}
"""

_UNRESOLVE = """
mutation($thread: ID!) {
  unresolveReviewThread(input: {threadId: $thread}) { thread { id isResolved } }
}
"""


class _ReplyLock:
    """One pull request's reply lock and the number of callers using it."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


#: Reply locks by pull request, dropped once their last caller leaves.
_REPLY_LOCKS: dict[tuple[str, str, int], _ReplyLock] = {}


@asynccontextmanager
async def _one_reply_at_a_time(owner: str, name: str, number: int) -> AsyncIterator[None]:
    """Hold the reply lock of one pull request (case-insensitive repository)."""
    key = (owner.lower(), name.lower(), int(number))
    entry = _REPLY_LOCKS.get(key)
    if entry is None:
        entry = _REPLY_LOCKS[key] = _ReplyLock()
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0 and _REPLY_LOCKS.get(key) is entry:
            del _REPLY_LOCKS[key]


def _preview(text: Any) -> str:
    flat = " ".join(_coerce_str(text).split())
    return flat if len(flat) <= _PREVIEW_CHARS else flat[: _PREVIEW_CHARS - 1] + "…"


def _login(node: Any) -> str:
    author = node.get("author") if isinstance(node, dict) else None
    return _coerce_str(author.get("login")) if isinstance(author, dict) else "ghost"


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the review thread tools and register their callables."""

    async def list_github_pr_review_threads(repo: str, number: int, state: str = "unresolved") -> str:
        """The review threads of a pull request, unresolved ones by default."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        state = (_coerce_str(state).strip().lower() or "unresolved")
        if state not in _STATES:
            return f"Error: 'state' must be one of {', '.join(_STATES)}."
        pull: dict[str, Any] | None = None
        nodes: list[dict[str, Any]] = []
        total = 0
        stopped = ""
        after: str | None = None
        async with egress_client(timeout=30) as client:
            for page in range(_MAX_PAGES):
                data, err = await _common.gh_graphql(
                    client,
                    headers,
                    _LIST_QUERY,
                    {"owner": owner, "name": name, "number": int(number), "first": _PAGE_SIZE, "after": after},
                    action=f"read the review threads of {repo}#{number}",
                )
                if err and pull is None:
                    return err
                if err:
                    stopped = f"page {page + 1} failed: {_preview(err)}"
                    break
                found = ((data or {}).get("repository") or {}).get("pullRequest")
                if not isinstance(found, dict):
                    if pull is None:
                        return f"Error: pull request #{number} does not exist in {repo}."
                    stopped = f"page {page + 1} came back empty"
                    break
                pull = pull or found
                threads = found.get("reviewThreads") or {}
                nodes.extend(n for n in threads.get("nodes") or [] if isinstance(n, dict))
                total = max(total, int(threads.get("totalCount") or 0))
                info = threads.get("pageInfo") or {}
                after = _coerce_str(info.get("endCursor")) or None
                if not info.get("hasNextPage"):
                    break
                if after is None:
                    stopped = f"page {page + 1} has more threads but no cursor"
                    break
            else:
                stopped = f"the cap of {_MAX_PAGES} pages of {_PAGE_SIZE} was reached"
        if pull is None:
            return f"Error: pull request #{number} does not exist in {repo}."
        total = max(total, len(nodes))
        missing = total - len(nodes)
        if missing and not stopped:
            stopped = "GitHub ended the pages early"
        unresolved = sum(1 for n in nodes if not n.get("isResolved"))
        resolved = len(nodes) - unresolved
        shown = [
            n for n in nodes
            if state == "all" or (state == "resolved") == bool(n.get("isResolved"))
        ]
        head = f"Review threads of {repo}#{number} ({pull.get('state', '')}): "
        if stopped:
            lines = [
                head + f"PARTIAL, {len(nodes)} of {total} threads read: "
                f"{unresolved} unresolved, {resolved} resolved among them."
            ]
        else:
            lines = [head + f"{unresolved} unresolved, {resolved} resolved."]
        for review in (pull.get("pending") or {}).get("nodes") or []:
            if not isinstance(review, dict):
                continue
            count = int(((review.get("comments") or {}).get("totalCount")) or 0)
            lines.append(
                f"Pending review {review.get('id')} of {_login(review)} holds {count} unpublished comment(s): "
                "nobody else sees them until it is submitted."
            )
        if stopped:
            unread = f"{missing} thread(s) were not read" if missing else "more threads may exist"
            lines.append(
                f"Paging stopped early ({stopped}): {unread}, so the counts cover the threads read only "
                "and unresolved threads may remain. Do not merge on this answer."
            )
        if not shown:
            if stopped:
                lines.append(
                    f"No {state} thread among the {len(nodes)} read." if state != "all" else "No review thread read."
                )
            else:
                lines.append(f"No {state} thread." if state != "all" else "No review thread.")
            return "\n".join(lines)
        for node in shown:
            first_conn = node.get("first") or {}
            first = next(iter(first_conn.get("nodes") or []), {}) or {}
            last = next(iter((node.get("last") or {}).get("nodes") or []), {}) or {}
            line = node.get("line") or node.get("originalLine")
            where = f"{node.get('path', '')}:{line}" if line else _coerce_str(node.get("path"))
            flags = []
            if node.get("isResolved"):
                flags.append(f"resolved by {_coerce_str((node.get('resolvedBy') or {}).get('login')) or 'unknown'}")
            if node.get("isOutdated"):
                flags.append("outdated")
            count = int(first_conn.get("totalCount") or 0)
            lines.append(
                f"- {node.get('id')} {where}" + (f" ({', '.join(flags)})" if flags else "")
                + f": {_login(first)}: {_preview(first.get('body'))}"
                + (f" [{count} comments, last by {_login(last)}]" if count > 1 else "")
            )
        return "\n".join(lines)

    async def manage_github_pr_review_thread(
        repo: str, number: int, thread_id: str, action: str, body: str = ""
    ) -> str:
        """Reply to, resolve or unresolve one review thread of a pull request."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        action = _coerce_str(action).strip().lower()
        if action not in _ACTIONS:
            return f"Error: 'action' must be one of {', '.join(_ACTIONS)}."
        thread = _coerce_str(thread_id).strip()
        if not thread:
            return "Error: 'thread_id' is required (the id list_github_pr_review_threads gives, PRRT_...)."
        text = _coerce_str(body).strip()
        if action in ("reply", "reply_and_resolve") and not text:
            return f"Error: 'body' is required for {action}: the answer posted in the thread."
        done: list[str] = []
        async with egress_client(timeout=30) as client:
            if action in ("reply", "reply_and_resolve"):
                async with _one_reply_at_a_time(owner, name, number):
                    data, err = await _common.gh_graphql(
                        client, headers, _REPLY, {"thread": thread, "body": text},
                        action=f"reply to review thread {thread}",
                    )
                if err:
                    return err
                comment = ((data or {}).get("addPullRequestReviewThreadReply") or {}).get("comment") or {}
                done.append(f"replied ({comment.get('url', 'no link returned')})")
                if _coerce_str(comment.get("state")).upper() == "PENDING":
                    review = comment.get("pullRequestReview") or {}
                    review_id = _coerce_str(review.get("id"))
                    if not review_id:
                        return (
                            f"Error: the reply to review thread {thread} joined a pending review of this "
                            "account and GitHub did not name it: the reply is not published."
                        )
                    # Submitting would publish the whole review (other drafts, its body), and
                    # nothing proves atomically that it holds this reply alone: never submit.
                    drafts = int(((review.get("comments") or {}).get("totalCount")) or 0)
                    return (
                        f"Error: the reply to review thread {thread} joined the pending review {review_id} of "
                        f"this account ({drafts} unpublished comment(s) in it, this reply included): nobody "
                        "else sees it and nothing was resolved. The review was not submitted, since that "
                        "would publish all of it: submit or discard it on GitHub deliberately, then check "
                        "the thread again."
                    )
            if action in ("resolve", "unresolve", "reply_and_resolve"):
                mutation, key = (_UNRESOLVE, "unresolveReviewThread") if action == "unresolve" else (_RESOLVE, "resolveReviewThread")
                data, err = await _common.gh_graphql(
                    client, headers, mutation, {"thread": thread},
                    action=f"{'unresolve' if action == 'unresolve' else 'resolve'} review thread {thread}",
                )
                if err:
                    prefix = f"Thread {thread} {done[0]}, but " if done else ""
                    return f"{prefix}{err}" if prefix else err
                state = ((data or {}).get(key) or {}).get("thread") or {}
                done.append("resolved" if state.get("isResolved") else "unresolved")
        await mark_self_action(repo, number)
        return f"Review thread {thread} of {repo}#{number}: {', then '.join(done)}."

    executor.register("list_github_pr_review_threads", list_github_pr_review_threads)
    executor.register("manage_github_pr_review_thread", manage_github_pr_review_thread)

    return [
        ToolDefinition(
            name="list_github_pr_review_threads",
            description=(
                "List a pull request's review threads (the conversations a merge waits on, a code review bot's "
                "included) with their thread id, file and line, outdated flag, first comment and last replier, "
                "and the counts of unresolved and resolved threads. state=unresolved (default), resolved or all. "
                "A pending (unsubmitted) review of the account is named: its comments are unseen until submitted. "
                "Answer and resolve them with manage_github_pr_review_thread."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "number": {"type": "integer", "description": "Pull request number"},
                    "state": {"type": "string", "enum": list(_STATES), "default": "unresolved"},
                },
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_github_pr_review_thread",
            description=(
                "Act on one pull request review thread by its id (from list_github_pr_review_threads): "
                "action=reply posts 'body' in the thread, resolve or unresolve changes its state, "
                "reply_and_resolve answers then resolves it (the usual step before a merge). A reply that "
                "joins a pending review of the account is reported as unpublished (the review is never "
                "submitted by the tool) and nothing is resolved. Several replies to one pull request in "
                "one round are posted one after another."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "number": {"type": "integer", "description": "Pull request number"},
                    "thread_id": {"type": "string", "description": "Review thread id (PRRT_...)"},
                    "action": {"type": "string", "enum": list(_ACTIONS)},
                    "body": {"type": "string", "description": "The reply (reply, reply_and_resolve)", "default": ""},
                },
                "required": ["repo", "number", "thread_id", "action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
