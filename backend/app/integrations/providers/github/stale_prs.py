"""The open pull requests a push to their base left behind or conflicting.

A listener on a push to the default branch brings the open pull requests it
tracks up to date. Most pushes leave none of them behind or conflicting, and
each such run still cost a full model run to find that out (#10622).

``list_github_prs_needing_update`` answers the question in one read-only
call, with no model: the open pull requests of a repository on a base branch
(optionally of one author, ``@me`` for the integration's account) whose merge
state is behind or dirty, or whose mergeability conflicts. Its result carries
``ToolResult.structured`` (a list, empty when none), so an assignment's
``activation_check`` gates a push listener on it::

    {"tool": "list_github_prs_needing_update",
     "arguments": {"repo": "{{payload.repository.full_name}}", "author": "@me"},
     "condition": "non_empty"}

A listener that never acts on draft pull requests, or on some branches (a
person's parked experiments), passes ``exclude_drafts`` and
``exclude_head_prefix`` so the gate tests the same set as its prompt; without
them a draft left conflicting for weeks wakes the listener on every push::

    {"tool": "list_github_prs_needing_update",
     "arguments": {"repo": "{{payload.repository.full_name}}", "author": "@me",
                   "exclude_drafts": true, "exclude_head_prefix": "feat/innovation-"},
     "condition": "non_empty"}

A base that requires a status check without "require branches to be up to
date" never makes a pull request behind: one whose head never got a required
context (the workflow was added to the base after the push) reads blocked and
waits forever. Such a pull request, its head commit older than
``MISSING_CONTEXT_AGE``, is listed with the reason
``missing_required_contexts`` and the ``missing`` names; merging the base in
runs the new workflow. The required contexts are read once per base, and only
when a pull request is a candidate.

Right after a push GitHub recomputes each open pull request's mergeability
lazily (``UNKNOWN`` for a few seconds). Those pull requests are read again
after the ``pr_states`` pauses; one still unknown after them is answered as
needing an update, so a gate built on this tool never misses a wake.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from . import _common, checks, pr_states
from ._common import login_error, one_line, parse_repo

_SOURCE = "integration:github"

#: Open pull requests one call reads (GitHub search pages by 100).
MAX_PULLS = 100
#: Merge states that need the base merged in.
_STALE_STATES = frozenset({"BEHIND", "DIRTY"})
_TITLE_CHARS = 80
#: A head commit younger than this may still be starting its workflows: never missing.
MISSING_CONTEXT_AGE = timedelta(minutes=30)
#: Contexts read per head; a head with more is never judged missing (the rest is unread).
_MAX_CONTEXTS = 100

_PR_FIELDS = (
    "number title state isDraft mergeable mergeStateStatus headRefName baseRefName "
    "commits(last: 1) { nodes { commit { committedDate statusCheckRollup { "
    f"contexts(first: {_MAX_CONTEXTS}) {{ pageInfo {{ hasNextPage }} "
    "nodes { __typename ... on CheckRun { name } ... on StatusContext { context } } } } } } }"
)

_SEARCH_QUERY = (
    "query($q: String!) {\n"
    f"  search(query: $q, type: ISSUE, first: {MAX_PULLS}) {{\n"
    "    issueCount\n"
    f"    nodes {{ ... on PullRequest {{ {_PR_FIELDS} }} }}\n"
    "  }\n"
    "}\n"
)

_NUMBERS_QUERY_FIELDS = _PR_FIELDS


def search_terms(owner: str, name: str, base: str, author: str, exclude_drafts: bool = False) -> str:
    """The GitHub search qualifiers of the open pull requests to read."""
    terms = [f"repo:{owner}/{name}", "is:pr", "is:open"]
    if base:
        terms.append(f"base:{base}")
    if author:
        terms.append(f"author:{author}")
    if exclude_drafts:
        # Filtered by GitHub so the drafts do not use up the 100 pull requests one call reads.
        terms.append("draft:false")
    return " ".join(terms)


def as_bool(value: Any) -> bool | None:
    """A boolean argument, also as the text an activation_check template renders; ``None`` when invalid."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in {"", "false", "0", "no"}:
        return False
    if text in {"true", "1", "yes"}:
        return True
    return None


def head_prefixes(value: Any) -> list[str]:
    """The head branch prefixes to skip: one string, a comma-separated string or a list."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [prefix for item in items if (prefix := str(item or "").strip())]


def excluded(node: dict[str, Any], exclude_drafts: bool, prefixes: list[str]) -> bool:
    """Whether the caller asked to leave this pull request out."""
    if exclude_drafts and node.get("isDraft"):
        return True
    head = str(node.get("headRefName") or "")
    return any(head.startswith(prefix) for prefix in prefixes)


def needs_update(node: dict[str, Any]) -> str | None:
    """Why an open pull request needs its base merged in, or ``None``."""
    if str(node.get("state") or "").upper() != "OPEN":
        return None
    merge_state = str(node.get("mergeStateStatus") or "").upper()
    mergeable = str(node.get("mergeable") or "").upper()
    if checks.is_conflict(mergeable, merge_state):
        return "conflicting"
    if merge_state in _STALE_STATES:
        return merge_state.lower()
    if pr_states.is_computing(node):
        return "unknown"
    return None


def _head_commit(node: dict[str, Any]) -> dict[str, Any]:
    commits = ((node.get("commits") or {}).get("nodes")) or []
    first = commits[-1] if commits and isinstance(commits[-1], dict) else {}
    return first.get("commit") or {}


def head_age(node: dict[str, Any], now: datetime) -> timedelta | None:
    """How old the head commit is, or ``None`` when GitHub did not say."""
    raw = str(_head_commit(node).get("committedDate") or "")
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return now - when


def reported_contexts(node: dict[str, Any]) -> frozenset[str] | None:
    """The check run names and status contexts of the head, ``None`` when not all were read."""
    contexts = (_head_commit(node).get("statusCheckRollup") or {}).get("contexts") or {}
    if (contexts.get("pageInfo") or {}).get("hasNextPage"):
        return None
    names: set[str] = set()
    for item in contexts.get("nodes") or []:
        if isinstance(item, dict) and (label := item.get("name") or item.get("context")):
            names.add(str(label))
    return frozenset(names)


def missing_required(node: dict[str, Any], required: frozenset[str], now: datetime) -> list[str]:
    """The required contexts an old enough head never reported (pending ones count as reported)."""
    if not required:
        return []
    age = head_age(node, now)
    if age is None or age < MISSING_CONTEXT_AGE:
        return []
    reported = reported_contexts(node)
    if reported is None:
        return []
    return sorted(required - reported)


def _entry(node: dict[str, Any], reason: str, missing: list[str] | None = None) -> dict[str, Any]:
    entry = {
        "number": node.get("number"),
        "head": str(node.get("headRefName") or ""),
        "base": str(node.get("baseRefName") or ""),
        "reason": reason,
        "draft": bool(node.get("isDraft")),
        "title": one_line(node.get("title"), _TITLE_CHARS),
    }
    if missing:
        entry["missing"] = missing
    return entry


def _numbers_query(numbers: list[int]) -> str:
    fields = "\n".join(f"    pr{n}: pullRequest(number: {n}) {{ {_NUMBERS_QUERY_FIELDS} }}" for n in numbers)
    return (
        "query($owner: String!, $name: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{fields}\n"
        "  }\n"
        "}\n"
    )


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the stale pull request tool and register its callable."""

    async def list_github_prs_needing_update(
        repo: str,
        base: str = "",
        author: str = "",
        exclude_drafts: Any = False,
        exclude_head_prefix: Any = None,
    ) -> str | ToolResult:
        """The open pull requests of *repo* behind or conflicting with their base."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        base = str(base or "").strip()
        author = str(author or "").strip()
        if author and author != "@me" and (aerr := login_error(author, "author")):
            return aerr
        skip_drafts = as_bool(exclude_drafts)
        if skip_drafts is None:
            return f"Error: exclude_drafts must be true or false, not {one_line(exclude_drafts, 40)!r}"
        prefixes = head_prefixes(exclude_head_prefix)
        query = search_terms(owner, name, base, author, skip_drafts)
        async with egress_client(timeout=30) as client:
            data, err = await _common.gh_graphql(
                client, headers, _SEARCH_QUERY, {"q": query}, action=f"list the open pull requests of {repo}"
            )
            if err:
                return err
            search = (data or {}).get("search") or {}
            nodes = {
                int(node["number"]): node
                for node in search.get("nodes") or []
                if isinstance(node, dict) and isinstance(node.get("number"), int)
            }
            left_out = [n for n, node in nodes.items() if excluded(node, skip_drafts, prefixes)]
            for number in left_out:
                del nodes[number]
            for pause in pr_states._UNKNOWN_BACKOFF:
                computing = sorted(n for n, node in nodes.items() if pr_states.is_computing(node))
                if not computing:
                    break
                await asyncio.sleep(pause)
                fresh, ferr = await _common.gh_graphql(
                    client, headers, _numbers_query(computing), {"owner": owner, "name": name},
                    action=f"read the merge state of {len(computing)} pull request(s) of {repo}",
                )
                if ferr:
                    # A failed re-read keeps the first answer (unknown counts as needing an update).
                    break
                repository = (fresh or {}).get("repository") or {}
                for number in computing:
                    node = repository.get(f"pr{number}")
                    if isinstance(node, dict):
                        nodes[number] = node
            stale = [_entry(node, reason) for node in nodes.values() if (reason := needs_update(node))]
            listed = {entry["number"] for entry in stale}
            now = datetime.now(UTC)
            candidates = [
                node
                for number, node in nodes.items()
                if number not in listed
                and str(node.get("state") or "").upper() == "OPEN"
                and (age := head_age(node, now)) is not None
                and age >= MISSING_CONTEXT_AGE
                and reported_contexts(node) is not None
            ]
            required_by_base: dict[str, frozenset[str]] = {}
            for node in candidates:
                node_base = str(node.get("baseRefName") or "")
                if not node_base:
                    continue
                if node_base not in required_by_base:
                    required_by_base[node_base] = await checks.read_required_contexts(
                        client, headers, owner, name, node_base
                    )
                if missing := missing_required(node, required_by_base[node_base], now):
                    stale.append(_entry(node, "missing_required_contexts", missing))
        stale.sort(key=lambda entry: entry["number"])
        total = int(search.get("issueCount") or len(nodes))
        scanned = len(nodes) + len(left_out)
        read = f"{scanned} open pull request(s) read" + (f" of {total}" if total > scanned else "")
        if left_out:
            read += f", {len(left_out)} left out by the exclusions"
        if not stale:
            content = f"No open pull request of {owner}/{name} needs an update ({query}; {read})."
        else:
            lines = [f"{len(stale)} open pull request(s) of {owner}/{name} need an update ({query}; {read}):"]
            lines += [
                f"#{e['number']} | {e['reason']}"
                + (f" ({', '.join(e['missing'])})" if e.get("missing") else "")
                + f"{' | draft' if e['draft'] else ''} | {e['head']} -> {e['base']} | {e['title']}"
                for e in stale
            ]
            content = "\n".join(lines)
        return ToolResult(tool_call_id="", content=content, structured=stale)

    executor.register("list_github_prs_needing_update", list_github_prs_needing_update)

    return [
        ToolDefinition(
            name="list_github_prs_needing_update",
            # State other actors move (CI, reviews, other runs): a re-read later is a new question.
            live_read=True,
            description=(
                "List the open pull requests of a repository whose base moved under them: merge state behind or "
                "dirty, or conflicting (one GitHub is still computing counts too), or an old head missing a "
                "required check of the base. Empty when none. Filter by base "
                "branch and author ('@me': the integration's account); exclude_drafts and exclude_head_prefix leave out "
                "the pull requests the caller never acts on. Read-only and structured: an activation_check with "
                "condition non_empty stands a push listener down at zero tokens when nothing needs an update."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "base": {"type": "string", "description": "Base branch (optional; default every base)"},
                    "author": {"type": "string", "description": "Author login or '@me' (optional)"},
                    "exclude_drafts": {
                        "type": "boolean",
                        "description": "Leave out draft pull requests (default false)",
                    },
                    "exclude_head_prefix": {
                        "type": "string",
                        "description": (
                            "Leave out pull requests whose head branch starts with this prefix "
                            "(several comma-separated; a JSON list is accepted too; optional)"
                        ),
                    },
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
