"""GitHub search tools - code, repositories, users, commits."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _BASE_URL, _PROVIDER

_SOURCE = "integration:github"


def _in_scope(items: Any, repo_of: Callable[[dict], Any]) -> tuple[list[dict], int]:
    """The results of the repository Scope(s) in progress, and their count.

    A pinned search can still be widened on GitHub's side (a boolean operator
    in the query), so the results are filtered on their own repository, and
    GitHub's total - over the widened search - is replaced by what is shown.
    """
    kept = [
        item
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and _common.in_repo_scope(repo_of(item))
    ]
    return kept, len(kept)


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub search tools and register their callables."""

    async def search_github_code(query: str, repo: str = "", count: int = 15) -> str:
        """Search code across GitHub repositories. Optionally scope to a specific repo."""
        async with egress_client(timeout=30) as client:
            q = f"repo:{repo} {query}" if repo else query
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/search/code",
                _PROVIDER,
                "search code",
                headers=headers,
                params={"q": q, "per_page": max(1, min(count, 30))},
            )
            if err:
                return err
            data = resp.json()
            items = data.get("items", [])
            total = data.get("total_count", 0)
            if _common.active_repo_scope() is not None:
                items, total = _in_scope(items, lambda i: (i.get("repository") or {}).get("full_name"))
            if not items:
                return "No code matches found."
            lines = [f"Found {total} results (showing {len(items)}):"]
            for item in items:
                repo_name = item.get("repository", {}).get("full_name", "")
                lines.append(f"  {repo_name}/{item['path']} (score: {item.get('score', 0):.1f})")
            return "\n".join(lines)

    async def search_github_repositories(query: str, sort: str = "best-match", count: int = 20) -> str:
        """Search public + accessible repositories with GitHub's repo search qualifiers (e.g. 'language:python stars:>100')."""
        params: dict[str, Any] = {"q": query, "per_page": max(1, min(count, 50))}
        if sort in ("stars", "forks", "updated", "help-wanted-issues"):
            params["sort"] = sort
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client, "get", f"{_BASE_URL}/search/repositories", _PROVIDER, "search repositories", headers=headers, params=params
            )
            if err:
                return err
            data = resp.json()
            items = data.get("items", [])
            if not items:
                return "No repositories matched."
            lines = [f"Found {data.get('total_count', 0)} repositories (showing {len(items)}):"]
            for r in items:
                lines.append(
                    f"  {r.get('full_name')} ★{r.get('stargazers_count', 0)} ({r.get('language') or '—'}) "
                    f"— {r.get('description') or 'No description'}"
                )
            return "\n".join(lines)

    async def search_github_users(query: str, user_type: str = "", count: int = 20) -> str:
        """Search GitHub users and organizations (e.g. 'location:Paris', 'type:org'). user_type filters user|org."""
        q = query
        if user_type in ("user", "org"):
            q += f" type:{user_type}"
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client, "get", f"{_BASE_URL}/search/users", _PROVIDER, "search users", headers=headers, params={"q": q, "per_page": max(1, min(count, 50))}
            )
            if err:
                return err
            data = resp.json()
            items = data.get("items", [])
            if not items:
                return "No users matched."
            lines = [f"Found {data.get('total_count', 0)} users (showing {len(items)}):"]
            for u in items:
                lines.append(f"  {u.get('login')} ({u.get('type', 'User')}) — {u.get('html_url', '')}")
            return "\n".join(lines)

    async def search_github_commits(query: str, repo: str = "", count: int = 20) -> str:
        """Search commits by message/author across GitHub. Optionally scope to a repo (e.g. 'fix author:octocat')."""
        async with egress_client(timeout=30) as client:
            q = f"repo:{repo} {query}" if repo else query
            resp, err = await _common.safe_request(
                client, "get", f"{_BASE_URL}/search/commits", _PROVIDER, "search commits", headers=headers, params={"q": q, "per_page": max(1, min(count, 50))}
            )
            if err:
                return err
            data = resp.json()
            items = data.get("items", [])
            total = data.get("total_count", 0)
            if _common.active_repo_scope() is not None:
                items, total = _in_scope(items, lambda c: (c.get("repository") or {}).get("full_name"))
            if not items:
                return "No commits matched."
            lines = [f"Found {total} commits (showing {len(items)}):"]
            for c in items:
                commit = c.get("commit") or {}
                msg = (commit.get("message") or "").splitlines()[0] if commit.get("message") else "(no message)"
                repo_name = (c.get("repository") or {}).get("full_name", "")
                lines.append(f"  {(c.get('sha') or '')[:8]} {msg} — {repo_name}")
            return "\n".join(lines)

    executor.register("search_github_code", search_github_code)
    executor.register("search_github_repositories", search_github_repositories)
    executor.register("search_github_users", search_github_users)
    executor.register("search_github_commits", search_github_commits)

    return [
        ToolDefinition(
            name="search_github_code",
            description="Search code across GitHub repositories by keyword. Optionally scope to a specific repo. Returns matching file paths with relevance scores.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords (e.g. 'TODO fixme', 'class AuthService')"},
                    "repo": {"type": "string", "description": "Scope search to a specific repo (owner/name). Leave empty to search all accessible repos.", "default": ""},
                    "count": {"type": "integer", "description": "Number of results (max 30)", "default": 15},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_github_repositories",
            description="Search repositories with GitHub's qualifiers (e.g. 'topic:cti language:python stars:>100', 'org:OpenCTI-Platform'). Sort by stars/forks/updated.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Repository search query with qualifiers"},
                    "sort": {"type": "string", "enum": ["best-match", "stars", "forks", "updated", "help-wanted-issues"], "default": "best-match"},
                    "count": {"type": "integer", "description": "Number of results (max 50)", "default": 20},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_github_users",
            description="Search GitHub users and organizations (e.g. 'fullname:John location:Paris'). Use user_type=user|org to filter. Useful to resolve a person to their login.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "User search query"},
                    "user_type": {"type": "string", "enum": ["", "user", "org"], "default": ""},
                    "count": {"type": "integer", "description": "Number of results (max 50)", "default": 20},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_github_commits",
            description="Search commits by message or author across GitHub. Optionally scope to a repo (e.g. 'fix race condition author:octocat'). Returns short SHA, summary, and repo.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Commit search query"},
                    "repo": {"type": "string", "description": "Scope to a repo owner/name (optional)", "default": ""},
                    "count": {"type": "integer", "description": "Number of results (max 50)", "default": 20},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
