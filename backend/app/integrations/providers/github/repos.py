"""GitHub repositories tools - list/get, collaborators, contributors, admin management."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _BASE_URL, _PROVIDER, _json_headers, login_error, parse_repo

_SOURCE = "integration:github"

_REPO_ONLY = {
    "type": "object",
    "properties": {"repo": {"type": "string", "description": "Repository in format owner/name"}},
    "required": ["repo"],
}


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub repository tools and register their callables."""

    async def list_github_repos(count: int = 30, sort: str = "updated", org: str = "") -> str:
        """List repositories. When org is specified, list that org's repos; otherwise list the user's."""
        if org and (e := login_error(org, "org")):
            return e
        url = f"{_BASE_URL}/orgs/{org}/repos" if org else f"{_BASE_URL}/user/repos"
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                "list repositories",
                headers=headers,
                params={"sort": sort, "per_page": max(1, min(count, 100)), "direction": "desc"},
            )
            if err:
                return err
            repos = resp.json()
            label = f"in {org}" if org else "for the authenticated user"

            def _fmt(r: dict) -> str:
                vis = "[private]" if r.get("private") else "[public]"
                lang = r.get("language") or "—"
                stars = r.get("stargazers_count", 0)
                desc = r.get("description") or "No description"
                return f"{r['full_name']} {vis} ★{stars} ({lang}) — {desc}"

            scope = _common.active_repo_scope()
            if scope is not None:
                # Under a repository Scope(s): the scoped repositories
                # only, and the ones this page did not return still named.
                kept = [
                    r for r in (repos if isinstance(repos, list) else [])
                    if isinstance(r, dict) and _common.in_repo_scope(r.get("full_name"))
                ]
                listed = {str(r["full_name"]).lower() for r in kept}
                lines = [
                    f"Repositories in this agent's or assignment's Scope(s) ({len(scope)}); "
                    "no other repository may be used:"
                ]
                lines += [_fmt(r) for r in kept]
                lines += [
                    f"{full_name} (in scope; not in this listing - use get_github_repository)"
                    for full_name in sorted(scope - listed)
                ]
                return "\n".join(lines)

            return _common.format_entity_list(
                repos, _fmt, noun="repository", empty_msg=f"No repositories found {label}."
            )

    async def get_github_repository(repo: str) -> str:
        """Get a repository's full profile: description, default branch, topics, languages, counts, license, visibility."""
        owner, name, err = parse_repo(repo)
        if err:
            return err
        async with egress_client(timeout=30) as client:
            resp, rerr = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}",
                _PROVIDER,
                f"get repository {repo}",
                headers=headers,
            )
            if rerr:
                return rerr
            r = resp.json()
            lang_resp, _ = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/languages",
                _PROVIDER,
                "fetch languages",
                headers=headers,
            )
            languages = list((lang_resp.json() if lang_resp else {}).keys())
            lines = [
                f"Repository: {r.get('full_name')} {'[private]' if r.get('private') else '[public]'}",
                f"Description: {r.get('description') or 'None'}",
                f"URL: {r.get('html_url', '')}",
                f"Default branch: {r.get('default_branch', '?')}",
                f"Primary language: {r.get('language') or 'N/A'}"
                + (f" | All: {', '.join(languages[:10])}" if languages else ""),
                f"Stars: {r.get('stargazers_count', 0)} | Forks: {r.get('forks_count', 0)} | "
                f"Watchers: {r.get('subscribers_count', r.get('watchers_count', 0))} | "
                f"Open issues+PRs: {r.get('open_issues_count', 0)}",
                f"License: {(r.get('license') or {}).get('spdx_id') or 'None'}",
                f"Topics: {', '.join(r.get('topics', [])) or 'None'}",
                f"Archived: {'yes' if r.get('archived') else 'no'} | "
                f"Fork: {'yes' if r.get('fork') else 'no'} | "
                f"Created: {(r.get('created_at') or '')[:10]} | Pushed: {(r.get('pushed_at') or '')[:10]}",
            ]
            if r.get("homepage"):
                lines.append(f"Homepage: {r['homepage']}")
            return "\n".join(lines)

    async def list_github_collaborators(repo: str, affiliation: str = "all", count: int = 50) -> str:
        """List a repository's collaborators with their permission level. Filter by affiliation (all, direct, outside)."""
        owner, name, err = parse_repo(repo)
        if err:
            return err
        async with egress_client(timeout=30) as client:
            resp, rerr = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/collaborators",
                _PROVIDER,
                f"list collaborators in {repo}",
                headers=headers,
                params={
                    "per_page": max(1, min(count, 100)),
                    "affiliation": affiliation if affiliation in ("all", "direct", "outside") else "all",
                },
            )
            if rerr:
                return rerr
            collabs = resp.json()

            def _fmt(c: dict) -> str:
                perms = c.get("permissions") or {}
                level = next((p for p in ("admin", "maintain", "push", "triage", "pull") if perms.get(p)), "read")
                return f"{c.get('login')} ({c.get('role_name', level)})"

            return _common.format_entity_list(
                collabs, _fmt, noun="collaborator", empty_msg=f"No collaborators found in {repo}."
            )

    async def list_github_contributors(repo: str, count: int = 30) -> str:
        """List a repository's top contributors by commit count."""
        owner, name, err = parse_repo(repo)
        if err:
            return err
        async with egress_client(timeout=30) as client:
            resp, rerr = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/contributors",
                _PROVIDER,
                f"list contributors in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if rerr:
                return rerr
            contributors = resp.json()
            return _common.format_entity_list(
                contributors,
                lambda c: f"{c.get('login')} — {c.get('contributions', 0)} commits",
                noun="contributor",
                empty_msg=f"No contributors found in {repo}.",
            )

    async def manage_github_repository(
        repo: str = "",
        action: str = "",
        org: str = "",
        name: str = "",
        description: str = "",
        private: bool | None = None,
        homepage: str = "",
        default_branch: str = "",
        topics: list[str] | None = None,
        username: str = "",
        permission: str = "push",
        new_owner: str = "",
        default_branch_only: bool = False,
    ) -> str:
        """Administer a repository (write / destructive).

        ``action`` is one of:
          - ``create`` — create a repo named ``name`` (under ``org``, else the
            authenticated user). Uses ``description``, ``private``, ``homepage``.
          - ``update`` — change settings on ``repo`` (``description``, ``private``,
            ``homepage``, ``default_branch``).
          - ``set_topics`` — REPLACE ``repo``'s topics with ``topics``.
          - ``add_collaborator`` / ``remove_collaborator`` — manage ``username``'s
            access to ``repo`` (``permission`` = pull|triage|push|maintain|admin).
          - ``archive`` / ``unarchive`` — set ``repo``'s archived state.
          - ``transfer`` — transfer ``repo`` to ``new_owner``.
          - ``fork`` — fork ``repo`` under ``org`` (else the authenticated
            user), optionally renamed ``name``; ``default_branch_only`` copies
            the default branch alone. GitHub creates the fork asynchronously.
          - ``enable_actions`` — turn GitHub Actions on for ``repo`` (all
            actions allowed); a new fork starts with Actions disabled.
          - ``delete`` — PERMANENTLY delete ``repo``. This cannot be undone.
        """
        action = (action or "").strip().lower()
        jheaders = _json_headers(headers)
        async with egress_client(timeout=30) as client:
            if action == "create":
                if not name.strip():
                    return "Error: 'name' is required to create a repository."
                payload: dict[str, Any] = {"name": name.strip()}
                if description:
                    payload["description"] = description
                if homepage:
                    payload["homepage"] = homepage
                if private is not None:
                    payload["private"] = bool(private)
                if org.strip():
                    if e := login_error(org, "org"):
                        return e
                    url = f"{_BASE_URL}/orgs/{org}/repos"
                else:
                    url = f"{_BASE_URL}/user/repos"
                resp, err = await _common.safe_request(
                    client, "post", url, _PROVIDER, "create repository", headers=jheaders, json=payload
                )
                if err:
                    return err
                r = resp.json()
                return f"Repository created: {r.get('full_name')} — {r.get('html_url', '')}"

            owner, rname, rerr = parse_repo(repo)
            if rerr:
                return rerr
            base = f"{_BASE_URL}/repos/{owner}/{rname}"

            if action == "update":
                payload = {}
                if description:
                    payload["description"] = description
                if homepage:
                    payload["homepage"] = homepage
                if private is not None:
                    payload["private"] = bool(private)
                if default_branch:
                    payload["default_branch"] = default_branch
                if not payload:
                    return "No fields to update. Provide description, private, homepage, or default_branch."
                resp, err = await _common.safe_request(
                    client, "patch", base, _PROVIDER, f"update {repo}", headers=jheaders, json=payload
                )
                if err:
                    return err
                return f"Updated repository {owner}/{rname}."
            if action == "set_topics":
                resp, err = await _common.safe_request(
                    client,
                    "put",
                    f"{base}/topics",
                    _PROVIDER,
                    f"set topics on {repo}",
                    headers=jheaders,
                    json={"names": [t.strip().lower() for t in (topics or []) if t.strip()]},
                )
                if err:
                    return err
                names = (resp.json() or {}).get("names", [])
                return f"Topics on {owner}/{rname}: {', '.join(names) or '(none)'}"
            if action in ("add_collaborator", "remove_collaborator"):
                if e := login_error(username, "username"):
                    return e
                url = f"{base}/collaborators/{username}"
                if action == "add_collaborator":
                    valid = {"pull", "triage", "push", "maintain", "admin"}
                    # Fall back to 'push' for an invalid value and report the
                    # permission actually sent to GitHub (not the raw input).
                    effective_permission = permission if permission in valid else "push"
                    resp, err = await _common.safe_request(
                        client,
                        "put",
                        url,
                        _PROVIDER,
                        f"add collaborator {username} to {repo}",
                        headers=jheaders,
                        json={"permission": effective_permission},
                    )
                    if err:
                        return err
                    return f"Invited {username} to {owner}/{rname} with '{effective_permission}' permission."
                resp, err = await _common.safe_request(
                    client, "delete", url, _PROVIDER, f"remove collaborator {username}", headers=headers
                )
                if err:
                    return err
                return f"Removed {username} from {owner}/{rname}."
            if action in ("archive", "unarchive"):
                resp, err = await _common.safe_request(
                    client,
                    "patch",
                    base,
                    _PROVIDER,
                    f"{action} {repo}",
                    headers=jheaders,
                    json={"archived": action == "archive"},
                )
                if err:
                    return err
                return f"{'Archived' if action == 'archive' else 'Unarchived'} repository {owner}/{rname}."
            if action == "transfer":
                if e := login_error(new_owner, "new_owner"):
                    return e
                resp, err = await _common.safe_request(
                    client,
                    "post",
                    f"{base}/transfer",
                    _PROVIDER,
                    f"transfer {repo}",
                    headers=jheaders,
                    json={"new_owner": new_owner},
                )
                if err:
                    return err
                return f"Transfer of {owner}/{rname} to {new_owner} initiated."
            if action == "fork":
                payload = {"default_branch_only": bool(default_branch_only)}
                if org.strip():
                    if e := login_error(org, "org"):
                        return e
                    payload["organization"] = org.strip()
                if name.strip():
                    payload["name"] = name.strip()
                resp, err = await _common.safe_request(
                    client, "post", f"{base}/forks", _PROVIDER, f"fork {repo}", headers=jheaders, json=payload
                )
                if err:
                    return err
                r = resp.json() or {}
                return (
                    f"Fork of {owner}/{rname}: {r.get('full_name', '')} — {r.get('html_url', '')} "
                    f"(default branch {r.get('default_branch', 'unknown')}). GitHub creates the fork "
                    "asynchronously: a read can answer 404 for a few seconds. GitHub Actions is "
                    "disabled on a new fork: action=enable_actions turns it on."
                )
            if action == "enable_actions":
                resp, err = await _common.safe_request(
                    client,
                    "put",
                    f"{base}/actions/permissions",
                    _PROVIDER,
                    f"enable Actions on {repo}",
                    headers=jheaders,
                    json={"enabled": True, "allowed_actions": "all"},
                )
                if err:
                    return err
                return (
                    f"GitHub Actions enabled on {owner}/{rname} (all actions allowed). A fork may still "
                    "hold workflows until its first push after this call."
                )
            if action == "delete":
                resp, err = await _common.safe_request(
                    client, "delete", base, _PROVIDER, f"delete {repo}", headers=headers
                )
                if err:
                    return err
                return f"Repository {owner}/{rname} permanently deleted."
            return (
                "Error: invalid action. Use create, update, set_topics, add_collaborator, "
                "remove_collaborator, archive, unarchive, transfer, fork, enable_actions, or delete."
            )

    executor.register("list_github_repos", list_github_repos)
    executor.register("get_github_repository", get_github_repository)
    executor.register("list_github_collaborators", list_github_collaborators)
    executor.register("list_github_contributors", list_github_contributors)
    executor.register("manage_github_repository", manage_github_repository)

    return [
        ToolDefinition(
            name="list_github_repos",
            description="List GitHub repositories. Specify org to list repos for a specific organization; omit to list the user's accessible repos.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of repos to return (max 100)", "default": 30},
                    "sort": {"type": "string", "enum": ["updated", "created", "pushed", "full_name"], "default": "updated"},
                    "org": {"type": "string", "description": "Organization login to list repos for (omit for user's repos)", "default": ""},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_repository",
            description="Get a repository's full profile: description, default branch, topics, languages, star/fork/issue counts, license, visibility, and archived/fork status.",
            input_schema=_REPO_ONLY,
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_collaborators",
            description="List a repository's collaborators with their permission level (admin/maintain/push/triage/pull). Filter by affiliation (all, direct, outside).",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "affiliation": {"type": "string", "enum": ["all", "direct", "outside"], "default": "all"},
                    "count": {"type": "integer", "description": "Max collaborators (max 100)", "default": 50},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_contributors",
            description="List a repository's top contributors ranked by number of commits.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "count": {"type": "integer", "description": "Max contributors (max 100)", "default": 30},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_github_repository",
            description=(
                "Administer a repository. action=create (new repo named 'name' under 'org' or the user), "
                "update (description/private/homepage/default_branch), set_topics (REPLACES topics), "
                "add_collaborator/remove_collaborator (manage 'username' access, permission=pull|triage|push|maintain|admin), "
                "archive/unarchive, transfer (to 'new_owner'), fork (under 'org' or the user, optional 'name', "
                "'default_branch_only'), enable_actions (turn GitHub Actions on, e.g. on a new fork), or delete. "
                "action=delete PERMANENTLY deletes the repository and cannot be undone. Requires repository admin permissions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository owner/name (required for all actions except create)", "default": ""},
                    "action": {
                        "type": "string",
                        "enum": [
                            "create", "update", "set_topics", "add_collaborator",
                            "remove_collaborator", "archive", "unarchive", "transfer", "fork",
                            "enable_actions", "delete",
                        ],
                    },
                    "org": {"type": "string", "description": "Org to create the repo or the fork under (create/fork; omit for the user)", "default": ""},
                    "name": {"type": "string", "description": "New repository name (create; optional new name of a fork)", "default": ""},
                    "default_branch_only": {"type": "boolean", "description": "Fork the default branch only (fork only)", "default": False},
                    "description": {"type": "string", "default": ""},
                    "private": {"type": "boolean", "description": "Visibility (create/update)"},
                    "homepage": {"type": "string", "default": ""},
                    "default_branch": {"type": "string", "description": "New default branch (update only)", "default": ""},
                    "topics": {"type": "array", "items": {"type": "string"}, "description": "Topics (set_topics only)"},
                    "username": {"type": "string", "description": "Collaborator login (add/remove_collaborator)", "default": ""},
                    "permission": {"type": "string", "enum": ["pull", "triage", "push", "maintain", "admin"], "default": "push"},
                    "new_owner": {"type": "string", "description": "Destination owner (transfer only)", "default": ""},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
