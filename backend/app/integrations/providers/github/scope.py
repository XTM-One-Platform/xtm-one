"""Hard repository boundary for the GitHub tools.

The Scope(s) an operator selects on an agent or an assignment
(``integration_element_bindings["github:repository"]``) used to be prompt text
only: the agent kept every GitHub tool and, with a personal access token,
reached every repository of the token holder. When a repository scope is set,
``build_tools`` now passes the tools through :func:`apply_repo_scope`:

* a call naming a repository outside the scope (the ``repo`` argument every
  per-repository tool takes, ``owner/name``) is refused with a message naming
  the allowed repositories - an explicit refusal, not an empty result, so the
  model does not retry with other arguments;
* every request of a scoped call goes through the request guard of
  ``_common.safe_request``: a ``..`` segment is refused (httpx would collapse
  it into another repository's path - scoped or not), and so is any REST path
  that addresses a repository outside the scope, whichever argument it came
  from;
* the listings whose results span repositories keep only the scoped ones:
  ``list_github_repos``, the code / commit / issue searches, a team's
  repositories, sub-issues and Projects v2 items (a project item is resolved
  to its repository before it is changed);
* the tools that reach repositories without naming one are withdrawn: the raw
  GraphQL passthrough, repository search and the notification inbox (which
  spans every repository of the account);
* the free-text search tools lose any ``repo:`` / ``org:`` / ``user:`` /
  ``owner:`` qualifier their query carries, wherever it sits (the issue
  search's ``state`` / ``labels`` / ``assignee`` filters are written into the
  query too), and are pinned to the scoped repositories - the pin comes first
  so the query cannot negate it;
* the actions that move work outside the scope are refused: creating or
  transferring a repository, changing a team's membership (it opens or closes
  every repository of the team) and a cross-repository ref (``owner:branch``).

Organization, enterprise, team, project and user metadata stay available: they
are not repository content, and a call among them that names a repository
(``manage_github_team_membership``, ``add_github_project_item``) goes through
the same ``repo`` check.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import re
from collections.abc import Mapping
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._common import _REPO_SCOPE, parse_repo

logger = logging.getLogger(__name__)

#: Element-binding key of a GitHub repository scope (``integration_elements``).
GITHUB_REPOSITORY_SCOPE_KEY = "github:repository"

#: Tools that reach repositories without naming one, withdrawn under a scope.
DROPPED_WHEN_SCOPED = frozenset(
    {
        "github_graphql_query",
        "query_github_graphql",
        "search_github_repositories",
        "list_github_notifications",
        "manage_github_notification",
        # Organization-wide searches and Copilot reports (people.py).
        "get_github_engineering_metrics",
        "get_github_ai_usage",
    }
)

#: Free-text search tools whose query is pinned to the scoped repositories.
SCOPED_QUERY_TOOLS = frozenset({"search_github_code", "search_github_commits", "search_github_issues"})

#: The free-text arguments each of them writes into its search query.
_QUERY_ARGUMENTS = {"search_github_issues": ("query", "state", "labels", "assignee")}

#: Arguments naming a git ref that GitHub also reads as ``owner:branch`` - a
#: branch of ANOTHER repository of the network (a fork).
_REF_ARGUMENTS = {
    "compare_github_commits": ("base", "head"),
    "create_github_pr": ("base", "head"),
}

# A search qualifier that widens a query beyond the repositories named in it,
# wherever it sits: at the start of a token, inside a group, quoted, any case.
# Negated forms (``-repo:x``) only narrow, but are dropped too: the scope is
# the whole allow-list and nothing outside it can be reached anyway.
_WIDENING_QUALIFIER_RE = re.compile(r'(?i)(?<![\w.])-?(?:repo|org|user|owner):(?:"[^"]*"?|[^\s()"]+)')


def repo_scope_from_bindings(
    element_scope: Mapping[str, Any] | None,
) -> frozenset[str] | None:
    """The scoped repositories (lower-cased ``owner/name``), or ``None``.

    *element_scope* is a run's EFFECTIVE scope (``services.element_scope``):
    ``None`` means no repository scope was set. A scope with no usable entry
    is an EMPTY set, not ``None``: that includes the empty list a narrowing
    produces (an assignment refining to repositories its agent no longer
    offers, a delegated run whose parent allowed none), so a boundary that
    allows nothing never turns into no boundary at all.
    """
    if not isinstance(element_scope, Mapping):
        return None
    items = element_scope.get(GITHUB_REPOSITORY_SCOPE_KEY)
    if items is None:
        return None
    if not isinstance(items, list):
        return frozenset()
    names: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        metadata = item.get("metadata") or {}
        raw = (metadata.get("full_name") if isinstance(metadata, Mapping) else None) or item.get("id") or ""
        owner, name, err = parse_repo(raw)
        if not err:
            names.add(f"{owner}/{name}".lower())
    return frozenset(names)


def _allowed_text(scope: frozenset[str]) -> str:
    return ", ".join(sorted(scope)) if scope else "none (no valid repository is selected)"


def _outside(what: str, scope: frozenset[str]) -> str:
    return (
        f"Refused: {what} is outside the Scope(s) set for this agent or "
        f"assignment, which is limited to: {_allowed_text(scope)}."
    )


def _refusal(tool: str, kwargs: dict[str, Any], scope: frozenset[str]) -> str | None:
    """Why this call is refused under *scope*, or ``None`` when it may run."""
    action = str(kwargs.get("action") or "").strip().lower()
    if tool == "manage_github_repository" and action == "create":
        return _outside("creating a repository", scope)
    if tool == "manage_github_repository" and action == "transfer":
        return _outside("transferring a repository to another owner", scope)
    if tool == "manage_github_repository" and action == "fork":
        return _outside("forking a repository (it creates a repository outside the scope)", scope)
    if tool == "manage_github_team_membership" and action in ("add_member", "remove_member"):
        return _outside(
            "changing team membership (it opens or closes every repository of the team)",
            scope,
        )
    for argument in _REF_ARGUMENTS.get(tool, ()):
        ref = kwargs.get(argument)
        if isinstance(ref, str) and ":" in ref:
            return _outside(f"'{ref.strip()}' (a branch of another repository, '{argument}')", scope)
    repo = kwargs.get("repo")
    if repo is not None and not isinstance(repo, str):
        return "Error: 'repo' must be a repository in 'owner/name' format (e.g. 'octocat/Hello-World')."
    if isinstance(repo, str) and repo.strip():
        owner, name, err = parse_repo(repo)
        if err:
            return err
        if f"{owner}/{name}".lower() not in scope:
            return (
                f"Refused: repository '{repo.strip()}' is outside the Scope(s) "
                "set for this agent or assignment. Allowed repositories: "
                f"{_allowed_text(scope)}. Work only on those - do not retry "
                "with another repository."
            )
        return None
    if tool in SCOPED_QUERY_TOOLS and not scope:
        return (
            "Refused: this agent or assignment is limited to a GitHub Scope(s) "
            "with no valid repository selected, so there is nothing to search."
        )
    return None


def _scope_query(tool: str, kwargs: dict[str, Any], scope: frozenset[str]) -> dict[str, Any]:
    """Pin a free-text search to the scoped repositories."""
    kwargs = dict(kwargs)
    for argument in _QUERY_ARGUMENTS.get(tool, ("query",)):
        value = kwargs.get(argument)
        if isinstance(value, str) and value:
            cleaned = " ".join(_WIDENING_QUALIFIER_RE.sub(" ", value).split())
            if cleaned != value.strip():
                logger.info("GitHub Scope(s): dropped repository qualifiers from the %s of a %s call", argument, tool)
            kwargs[argument] = cleaned
    repo = kwargs.get("repo")
    if not (isinstance(repo, str) and repo.strip()):
        if len(scope) == 1:
            kwargs["repo"] = next(iter(scope))
        elif tool != "search_github_issues":
            # First, so an operator in the query (``fix NOT``) cannot negate it.
            qualifiers = " ".join(f"repo:{r}" for r in sorted(scope))
            kwargs["query"] = f"{qualifiers} {kwargs.get('query') or ''}".strip()
    return kwargs


def _guarded(tool: str, handler: Any, scope: frozenset[str]) -> Any:
    @functools.wraps(handler)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        refusal = _refusal(tool, kwargs, scope)
        if refusal:
            logger.info("GitHub Scope(s): refused a %s call", tool)
            return ToolResult(tool_call_id="", content=refusal, is_error=True)
        if tool in SCOPED_QUERY_TOOLS:
            kwargs = _scope_query(tool, kwargs, scope)
        if tool == "list_github_repos":
            if not scope:
                return (
                    "No repository is available: this agent or assignment is limited "
                    "to a GitHub Scope(s) with no valid repository selected."
                )
            # As many repositories as one page holds, so the scoped ones show.
            kwargs["count"] = 100
        # The request guard and the cross-repository listings read the scope of
        # the call in progress; set only while this call runs.
        token = _REPO_SCOPE.set(scope)
        try:
            return await handler(*args, **kwargs)
        finally:
            _REPO_SCOPE.reset(token)

    return guarded


def apply_repo_scope(
    definitions: list[ToolDefinition],
    executor: CallableToolExecutor,
    scope: frozenset[str],
) -> list[ToolDefinition]:
    """Enforce *scope* on the GitHub tools just registered on *executor*."""
    kept: list[ToolDefinition] = []
    note = f" Limited to this Scope(s): {_allowed_text(scope)}."
    refused_tools = 0
    for definition in definitions:
        name = definition.name
        handler = executor.handler(name)
        if name in DROPPED_WHEN_SCOPED:
            executor.unregister(name)
            refused_tools += 1
            continue
        if handler is None:
            kept.append(definition)
            continue
        executor.register(name, _guarded(name, handler, scope))
        if name in SCOPED_QUERY_TOOLS or name == "list_github_repos":
            definition = dataclasses.replace(definition, description=definition.description + note)
        kept.append(definition)
    logger.info(
        "GitHub Scope(s) enforced on %d repositories: %d tool(s) withdrawn, %d tool(s) guarded",
        len(scope),
        refused_tools,
        len(kept),
    )
    return kept
