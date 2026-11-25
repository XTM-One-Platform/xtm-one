"""GitHub integration provider package.

Best-in-class agentic access to GitHub across its REST and GraphQL APIs, split
by domain (mirrors ``providers/drata`` and ``providers/opencti``):

* ``_common``   - shared headers, HTTP + GraphQL plumbing, repo validation, formatters.
* ``orgs``      - organizations, members (with roles), teams, team membership.
* ``enterprise``- enterprise accounts (GraphQL): member organizations, member roster, owners/billing managers.
* ``repos``     - repositories: list/get, collaborators, contributors, admin management.
* ``contents``  - file read/write/delete + repository tree.
* ``commits``   - commits: list/get, compare, commit comments.
* ``revert``    - one commit undoing another, whatever its size, by object id.
* ``branches``  - branches, tags, refs, branch protection (ruleset-aware).
* ``merge``     - a ref merged into a branch server-side, conflicting paths named.
* ``rebase``    - a branch's commits replayed onto a ref one by one, moved with a lease.
* ``rulesets``  - repository & organization Rulesets (modern branch/tag governance) + effective branch rules.
* ``issues``    - issues, comments, labels, milestones, sub-issues, locking, coding-agent assignment.
* ``pulls``     - pull requests: read/diff/files, create/update/review/merge, review comments, automerge.
* ``review_threads`` - pull request review threads: list, reply, resolve.
* ``pr_states`` - the merge state of up to 30 pull requests in one compact read.
* ``stale_prs`` - the open pull requests a push left behind or conflicting (a zero-token gate).
* ``actions``   - GitHub Actions: workflows, runs, jobs, logs, dispatch, artifacts, secrets, variables, environments.
* ``checks``    - check runs, combined commit status, create status.
* ``security``  - code scanning, Dependabot, secret scanning alerts + repository security advisory reads.
* ``releases``  - releases: list/get, create/update/delete/publish.
* ``projects``  - Projects v2 (GraphQL): the product backlog - list/get/items/add/draft/set-field/manage.
* ``search``    - code, repositories, users, commits search.
* ``users``     - whoami, user profiles, notifications.
* ``graphql``   - raw GraphQL escape hatch.
* ``pr_events`` - webhook deliveries as generic pull request events.
* ``event_enrichment`` - the open pull request of a CI or push event, for trigger filters.

Authentication: an OAuth user/installation token or a personal access token,
sent as ``Authorization: Bearer``. GitHub Apps use fine-grained repository
permissions (configured on the app), not OAuth scopes.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.event_enrichment import register_event_enricher
from app.integrations.full_ci_redispatch import register_full_ci_redispatcher
from app.integrations.pull_request_events import register_pull_request_event_parser
from app.integrations.reference_state import (
    register_branch_closings_reader,
    register_commit_containment_reader,
    register_pull_request_ci_reader,
    register_reference_state_reader,
    register_tag_reader,
    register_workflow_run_reader,
)
from app.integrations.run_payload_shapers import register_run_payload_shaper
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import (
    actions,
    branches,
    checks,
    commits,
    contents,
    enterprise,
    graphql,
    issues,
    merge,
    orgs,
    people,
    pr_states,
    projects,
    pulls,
    rebase,
    releases,
    repos,
    revert,
    review_threads,
    rulesets,
    search,
    security,
    stale_prs,
    users,
)
from ._common import _BASE_URL, GITHUB_API, _headers, gh_graphql
from .event_enrichment import GITHUB_EVENT_ENRICHER
from .full_ci_gate import redispatch as redispatch_full_ci
from .pr_events import parse_github_pull_request_event
from .reference_state import (
    read_branch_closings,
    read_commit_containment,
    read_pull_request_ci,
    read_reference_state,
    read_successful_workflow_run,
    read_tag_carrying,
)
from .run_payload import shape_github_run_payload

logger = logging.getLogger(__name__)

# The CI and state of the pull requests a work item waits on.
register_pull_request_event_parser("github", parse_github_pull_request_event)
# The pull request a CI or push event belongs to, for trigger filters.
register_event_enricher("github", GITHUB_EVENT_ENRICHER)
# A push as the run is shown it: no duplicated head commit, first-line messages.
register_run_payload_shaper("github", shape_github_run_payload)
# The state of a reference a blocked work item waits on, when no event came.
register_reference_state_reader("github", read_reference_state)
# The closed pull requests of a head branch, for a reference that follows its successor.
register_branch_closings_reader("github", read_branch_closings)
# Whether a build carries a merge commit, for an item blocked on a deployment.
register_commit_containment_reader("github", read_commit_containment)
# The first successful run of a workflow after a time, for an item blocked on a release run.
register_workflow_run_reader("github", read_successful_workflow_run)
# The newest tag carrying a commit, for an item blocked on a release tag (#2209).
register_tag_reader("github", read_tag_carrying)
# The head and CI verdict of a pull request a parked work item names, when no CI event came.
register_pull_request_ci_reader("github", read_pull_request_ci)
# The full CI run of a pull request parked on its base's red, run again at the base's green.
register_full_ci_redispatcher("github", redispatch_full_ci)

__all__ = ["GITHUB_API", "GitHubIntegration", "build_tools", "gh_graphql"]


class GitHubIntegration(BaseIntegration):
    provider_name = "github"
    display_name = "GitHub"
    icon = "GitPullRequest"
    description = (
        "Connect GitHub for repository management, issue and project (backlog) tracking, "
        "pull request reviews, CI/CD (GitHub Actions) monitoring and control, security "
        "alert triage, repository security advisory reads, Git operations (branches, files, "
        "tags, commits, automated PRs), Projects v2 boards, and GraphQL API access."
    )
    auth_types = ["oauth", "api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",  # noqa: S106
            # OAuth token exchange endpoint URL, not a secret.
            # GitHub App permissions are configured on the app itself,
            # not via OAuth scopes. Empty scopes = use app permissions.
            scopes=[],
        )

    async def validate_connection(self, token: str) -> bool:
        async with egress_client(timeout=15) as client:
            resp = await client.get(f"{_BASE_URL}/user", headers=_headers(token))
            return resp.status_code == 200

    async def get_user_info(self, token: str) -> dict:
        async with egress_client(timeout=15) as client:
            resp = await client.get(f"{_BASE_URL}/user", headers=_headers(token))
            resp.raise_for_status()
            data = resp.json()
            return {
                "login": data.get("login"),
                "name": data.get("name"),
                "avatar": data.get("avatar_url"),
            }


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Build every GitHub tool and register its callable on *executor*."""
    headers = _headers(token)

    # Optional loop-protection hook injected by ``integration_tools``: an async
    # ``(integration_id, repo, number) -> None`` recorder. We bind it to a
    # ``(repo, number) -> None`` callback passed to each domain's write tools so
    # the webhook receiver can drop the resulting self-triggered echo event for
    # assignments that opted in. See ``services/github_echo.py``.
    self_action_recorder = kwargs.get("self_action_recorder")

    async def mark_self_action(repo: str, number: int | str) -> None:
        if not self_action_recorder or not repo or number in (None, ""):
            return
        try:
            await self_action_recorder(repo, int(number))
        except Exception as exc:
            logger.debug("Failed to record GitHub self-action marker: %s", format_exception_message(exc))

    # The file tools store what they download as files of the run.
    persist_file_fn = kwargs.get("persist_file_fn")
    # Names a new pull request on the work item it is opened for.
    record_pull_request = kwargs.get("record_pull_request")
    # Refuses a pull request for a cancelled work item before it is created (#11554).
    check_work_item = kwargs.get("check_work_item")
    # Names the open pull request a commit was pushed to on the run's work item.
    record_push = kwargs.get("record_push")
    # Parks a PR whose full CI is red on its base's own failure (#12069).
    park_full_ci = kwargs.get("park_full_ci")
    tools: list[ToolDefinition] = []
    for module in (
        orgs,
        enterprise,
        repos,
        contents,
        commits,
        revert,
        branches,
        merge,
        rebase,
        rulesets,
        issues,
        pulls,
        pr_states,
        stale_prs,
        review_threads,
        actions,
        checks,
        security,
        releases,
        projects,
        search,
        users,
        people,
        graphql,
    ):
        if module is contents:
            tools += module.register(headers, executor, mark_self_action, persist_file_fn, record_push)
        elif module in (releases, actions):
            tools += module.register(headers, executor, mark_self_action, persist_file_fn)
        elif module is pulls:
            tools += module.register(
                headers, executor, mark_self_action, record_pull_request, check_work_item, park_full_ci
            )
        else:
            tools += module.register(headers, executor, mark_self_action)
    # A branch another live run leases is not moved by this run (gap
    # branch-lease-bypassed-by-concurrent-push): ``integration_tools`` passes
    # the lease check of the calling run (``None`` outside a run).
    check_branch_lease = kwargs.get("check_branch_lease")
    if check_branch_lease is not None:
        from .branch_lease import apply_branch_lease

        tools = apply_branch_lease(tools, executor, check_branch_lease)
    # A repository Scope(s) is a hard boundary: ``integration_tools``
    # passes the scoped ``owner/name`` set when the agent or the assignment
    # selected one (``None`` = no scope).
    repo_scope = kwargs.get("repo_scope")
    if repo_scope is not None:
        from .scope import apply_repo_scope

        tools = apply_repo_scope(tools, executor, frozenset(repo_scope))
    return tools
