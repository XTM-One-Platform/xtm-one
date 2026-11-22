"""A branch another live run leases is not moved by this run's GitHub write tools.

A run that pushes a fix takes a lease on its branch in shared state
(``hold="take"`` on an entry naming the repository and the branch). The
platform honoured it only at dispatch: an ITEM naming the pull request stayed
undispatched (``shared_state_pull_requests.live_pull_request_lease``). A run
with no item binding (an event run: a red check, a review, a merge-queue
ejection) still pushed to the leased branch, and two runs wrote two fixes.

``build_tools`` now wraps the tools that move a branch with :func:`apply_branch_lease`
when the platform passes a ``check_branch_lease`` callback (``integration_tools``
binds it to the calling run and its person). Before the write, the callback
reads the live leases naming ``repo`` + ``branch``; one held by ANOTHER live
run answers its refusal text and nothing is written (not an error, as a
refused ``hold="take"`` is not). The run's own lease, an ended holder, a
release, or a failed read let the write go ahead as before.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

#: ``(repo, branch) -> refusal text or None``: the lease check of the calling run.
BranchLeaseCheck = Callable[[str, str], Awaitable[str | None]]

#: Tools that move one named branch, by the argument that names it.
BRANCH_WRITE_TOOLS: dict[str, str] = {
    "create_github_commit": "branch",
    "create_or_update_github_file": "branch",
    "delete_github_file": "branch",
    "merge_github_branch": "branch",
    "rebase_github_branch": "branch",
    "revert_github_commit": "branch",
}

#: ``manage_github_ref`` actions that move or delete the branch they name.
REF_BRANCH_ACTIONS = frozenset({"update_branch", "delete_branch"})


def branch_written(tool: str, kwargs: dict[str, Any]) -> str | None:
    """The branch the call to *tool* moves, ``None`` when it names none (the default branch). Pure."""
    if tool == "manage_github_ref":
        action = str(kwargs.get("action") or "").strip().lower()
        if action not in REF_BRANCH_ACTIONS:
            return None
        argument = "branch"
    else:
        argument = BRANCH_WRITE_TOOLS.get(tool)
        if argument is None:
            return None
    branch = kwargs.get(argument)
    return branch.strip() or None if isinstance(branch, str) else None


def _guarded(tool: str, handler: Any, check: BranchLeaseCheck) -> Any:
    @functools.wraps(handler)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        branch = branch_written(tool, kwargs)
        repo = kwargs.get("repo")
        if branch and isinstance(repo, str) and repo.strip():
            try:
                refusal = await check(repo.strip(), branch)
            except Exception as exc:  # a failed read never blocks a write
                logger.warning("Branch lease of %s:%s unreadable: %s", repo, branch, exc)
                refusal = None
            if refusal:
                logger.info("GitHub %s on %s:%s stood down: branch leased by a live run", tool, repo, branch)
                return refusal
        return await handler(*args, **kwargs)

    return guarded


def apply_branch_lease(
    definitions: list[ToolDefinition], executor: CallableToolExecutor, check: BranchLeaseCheck
) -> list[ToolDefinition]:
    """Guard every branch-moving tool registered on *executor* with *check*; the definitions are unchanged."""
    for definition in definitions:
        name = definition.name
        if name not in BRANCH_WRITE_TOOLS and name != "manage_github_ref":
            continue
        handler = executor.handler(name)
        if handler is not None:
            executor.register(name, _guarded(name, handler, check))
    return definitions
