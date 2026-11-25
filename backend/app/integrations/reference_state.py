# SPDX-License-Identifier: Apache-2.0

"""The state of a pull request or issue, read when its events never came.

A work item blocked on code-host references (``blocked_on_refs``) is released
by the deliveries the platform receives (``pull_request_events``). A
repository whose webhook is not installed sends none, so the scheduler reads
the references of an item blocked for a while itself
(``services/work_item_blocked_refs.poll_blocked_on_refs``). Each provider
registers a reader that answers the closing as the same
:class:`~app.integrations.pull_request_events.PullRequestEvent` a delivery
would have produced (``closed`` with ``merged``, or ``issue_closed``), and
``None`` while the reference is open or when it cannot be read: the generic
layer never reads a vendor API.

Light on purpose (no model, no service import): provider packages register
their reader when they are imported, like ``pull_request_events``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.integrations.pull_request_events import PullRequestEvent

#: ``(repo, kind, number, token)`` -> the closing event, ``None`` while open or unreadable.
#: *kind* is ``pull_request`` or ``issue``; *repo* is ``owner/name``, lowercased.
ReferenceStateReader = Callable[[str, str, int, str], Awaitable[PullRequestEvent | None]]

_READERS: dict[str, ReferenceStateReader] = {}


def register_reference_state_reader(provider: str, reader: ReferenceStateReader) -> None:
    """Register *provider*'s reader. Idempotent (last wins)."""
    _READERS[provider] = reader


def reference_state_readers() -> dict[str, ReferenceStateReader]:
    """Every registered reader, by provider."""
    return dict(_READERS)


#: ``(repo, head_branch, token)`` -> the closings of the pull requests whose head is
#: *head_branch* (merged or not), oldest number first; ``[]`` when none or unreadable.
#: A work item blocked on a pull request a workflow closes and opens again under a
#: new number on the same branch (a changesets release pull request).
BranchClosingsReader = Callable[[str, str, str], Awaitable[list[PullRequestEvent]]]

_BRANCH_CLOSINGS_READERS: dict[str, BranchClosingsReader] = {}


def register_branch_closings_reader(provider: str, reader: BranchClosingsReader) -> None:
    """Register *provider*'s head branch closings reader. Idempotent (last wins)."""
    _BRANCH_CLOSINGS_READERS[provider] = reader


def branch_closings_readers() -> dict[str, BranchClosingsReader]:
    """Every registered head branch closings reader, by provider."""
    return dict(_BRANCH_CLOSINGS_READERS)


#: ``(repo, commit, build_commit, token)`` -> whether *build_commit* contains *commit*
#: (it is that commit or a descendant of it), ``None`` when it cannot be read.
CommitContainmentReader = Callable[[str, str, str, str], Awaitable[bool | None]]

_CONTAINMENT_READERS: dict[str, CommitContainmentReader] = {}


def register_commit_containment_reader(provider: str, reader: CommitContainmentReader) -> None:
    """Register *provider*'s commit containment reader (a work item blocked on a deployment). Idempotent."""
    _CONTAINMENT_READERS[provider] = reader


def commit_containment_readers() -> dict[str, CommitContainmentReader]:
    """Every registered commit containment reader, by provider."""
    return dict(_CONTAINMENT_READERS)


#: ``(repo, workflow, after, branch, title_contains, token)`` -> the first successful run of
#: *workflow* in *repo* created at or after *after* (ISO 8601 UTC), on *branch* when given,
#: whose display title holds *title_contains* when given (#34), as
#: ``{"id", "url", "created_at", "updated_at", "head_sha"}``; ``None`` when there is
#: none yet or it cannot be read. A work item blocked on a ``workflow_run`` reference
#: (a release run that deploys a change merged in another repository).
WorkflowRunReader = Callable[[str, str, str, str | None, str | None, str], Awaitable[dict[str, object] | None]]

_WORKFLOW_RUN_READERS: dict[str, WorkflowRunReader] = {}


def register_workflow_run_reader(provider: str, reader: WorkflowRunReader) -> None:
    """Register *provider*'s successful workflow run reader. Idempotent (last wins)."""
    _WORKFLOW_RUN_READERS[provider] = reader


def workflow_run_readers() -> dict[str, WorkflowRunReader]:
    """Every registered workflow run reader, by provider."""
    return dict(_WORKFLOW_RUN_READERS)


#: ``(repo, pattern, carries_sha, token)`` -> the newest tag of *repo* whose name matches the
#: glob *pattern* and whose commit is *carries_sha* or a descendant of it, as
#: ``{"tag", "commit"}``; ``None`` when there is none yet or it cannot be read. A work
#: item blocked on a ``tag`` reference (a release cut from a merge, #2209).
TagReader = Callable[[str, str, str, str], Awaitable[dict[str, str] | None]]

_TAG_READERS: dict[str, TagReader] = {}


def register_tag_reader(provider: str, reader: TagReader) -> None:
    """Register *provider*'s tag reader. Idempotent (last wins)."""
    _TAG_READERS[provider] = reader


def tag_readers() -> dict[str, TagReader]:
    """Every registered tag reader, by provider."""
    return dict(_TAG_READERS)


#: ``(repo, number, token)`` -> the current head of an OPEN pull request and the
#: verdict of its CI, as ``{"head_sha", "head_branch", "draft", "conclusion",
#: "run_url"}`` with ``conclusion`` ``success`` / ``failure`` once every check of
#: the head completed (or one failed), ``None`` while one runs or none reported;
#: ``None`` for a closed pull request or when it cannot be read. A work item
#: parked on a pull request whose CI delivery never came
#: (``services/work_item_pull_requests.poll_pull_request_ci``).
PullRequestCiReader = Callable[[str, int, str], Awaitable[dict | None]]

_PULL_REQUEST_CI_READERS: dict[str, PullRequestCiReader] = {}


def register_pull_request_ci_reader(provider: str, reader: PullRequestCiReader) -> None:
    """Register *provider*'s pull request CI reader. Idempotent (last wins)."""
    _PULL_REQUEST_CI_READERS[provider] = reader


def pull_request_ci_readers() -> dict[str, PullRequestCiReader]:
    """Every registered pull request CI reader, by provider."""
    return dict(_PULL_REQUEST_CI_READERS)
