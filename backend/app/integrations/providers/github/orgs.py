"""GitHub organizations & teams tools - orgs, members, teams, team membership."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _BASE_URL, _PROVIDER, _coerce_str, _json_headers, login_error, team_slug_error

_SOURCE = "integration:github"

#: Concrete remediation appended (only on permission errors) to org member /
#: team reads so a "Resource not accessible by integration" failure tells the
#: user exactly which access to grant.
_ORG_MEMBERS_HINT = (
    "grant the GitHub App the 'Organization -> Members: Read' permission (and make "
    "sure it is installed on the organization), or use a Personal Access Token with "
    "the 'read:org' scope."
)


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub organization & team tools and register their callables."""

    async def list_github_organizations() -> str:
        """List organizations the authenticated user/app has access to."""
        async with egress_client(timeout=30) as client:
            resp, _ = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/user/installations",
                _PROVIDER,
                "list installations",
                headers=headers,
                params={"per_page": "100"},
            )
            if resp is not None:
                installations = resp.json().get("installations", [])
                if installations:
                    lines = []
                    for inst in installations:
                        acct = inst.get("account", {})
                        login = acct.get("login", "")
                        acct_type = acct.get("type", "")
                        lines.append(f"{login} [{acct_type}] (installation #{inst.get('id')})")
                    return "Organizations/accounts with GitHub App installed:\n" + "\n".join(lines)
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/user/orgs",
                _PROVIDER,
                "list organizations",
                headers=headers,
                params={"per_page": "100"},
            )
            if err:
                return err
            orgs = resp.json()
            if not orgs:
                return "No organizations found."
            lines = [f"{o['login']} — {o.get('description') or 'No description'}" for o in orgs]
            return "Organizations:\n" + "\n".join(lines)

    async def get_github_organization(org: str) -> str:
        """Get an organization's profile: plan, member/repo counts, and metadata."""
        if e := login_error(org, "org"):
            return e
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/orgs/{org}",
                _PROVIDER,
                f"get organization {org}",
                headers=headers,
            )
            if err:
                return err
            o = resp.json()
            lines = [
                f"Organization: {o.get('login')} ({o.get('name') or 'no display name'})",
                f"Description: {o.get('description') or 'None'}",
                f"URL: {o.get('html_url', '')}",
                f"Public repos: {o.get('public_repos', 0)} | Private repos: {o.get('total_private_repos', 'N/A')}",
                f"Followers: {o.get('followers', 0)} | Following: {o.get('following', 0)}",
            ]
            if o.get("plan"):
                plan = o["plan"]
                lines.append(f"Plan: {plan.get('name')} ({plan.get('filled_seats', '?')}/{plan.get('seats', '?')} seats)")
            if o.get("company"):
                lines.append(f"Company: {o['company']}")
            if o.get("location"):
                lines.append(f"Location: {o['location']}")
            if o.get("blog"):
                lines.append(f"Website: {o['blog']}")
            return "\n".join(lines)

    async def list_github_org_members(org: str, role: str = "all", count: int = 50, after: str = "") -> str:
        """List members of an organization WITH each member's role (admin/member).

        Filter by role (all, admin, member). For ``all`` the roles are read from
        the GraphQL ``membersWithRole`` connection so every member is annotated
        with their organization role in one call; ``admin`` / ``member`` use the
        server-side REST filter. If member roles are not accessible the tool
        degrades to the plain roster and reports the permission needed.
        """
        if e := login_error(org, "org"):
            return e
        n = max(1, min(count, 100))
        role_norm = (role or "all").strip().lower()
        # 'after' is a GraphQL cursor that only applies to the role='all' path;
        # the REST role filter below doesn't use it. Reject it rather than
        # silently ignoring it so callers don't think pagination is happening.
        if _coerce_str(after).strip() and role_norm in ("admin", "member"):
            return (
                "Error: the 'after' cursor only applies to role='all'. For "
                "role='admin'/'member', raise 'count' (max 100) instead."
            )
        async with egress_client(timeout=30) as client:
            # admin / member: the REST role filter is server-side and complete,
            # and we already know the role of every returned member.
            if role_norm in ("admin", "member"):
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/orgs/{org}/members",
                    _PROVIDER,
                    f"list {role_norm}s of {org}",
                    headers=headers,
                    params={"per_page": n, "role": role_norm},
                )
                if err:
                    return _common.with_permission_hint(err, _ORG_MEMBERS_HINT)
                return _common.format_entity_list(
                    resp.json(),
                    lambda m: f"{m.get('login')} ({m.get('type', 'User')}) — {role_norm}",
                    noun=f"{role_norm} member",
                    empty_msg=f"No {role_norm}s found in {org}.",
                )

            # all: annotate every member with their role via GraphQL.
            query = (
                "query($login:String!,$n:Int!,$after:String){organization(login:$login){"
                "membersWithRole(first:$n, after:$after){totalCount "
                "pageInfo{hasNextPage endCursor} edges{role node{login name}}}}}"
            )
            data, err = await _common.gh_graphql(
                client,
                headers,
                query,
                {"login": org, "n": n, "after": after or None},
                action=f"list members with role of {org}",
            )
            if not err:
                conn = ((data.get("organization") or {}).get("membersWithRole")) or {}
                edges = [e for e in (conn.get("edges") or []) if isinstance(e, dict)]
                if not edges:
                    return f"No members found in {org}."
                total = conn.get("totalCount")
                header = f"Members of {org}" + (f" ({total} total)" if total is not None else "") + ":"
                lines = [header]
                for edge in edges:
                    grole = "admin" if (edge.get("role") or "").upper() == "ADMIN" else "member"
                    node = edge.get("node") or {}
                    login = node.get("login", "?")
                    name = node.get("name")
                    disp = f"{login} ({name})" if name else login
                    lines.append(f"  - {disp} — {grole}")
                page = conn.get("pageInfo") or {}
                if page.get("hasNextPage") and page.get("endCursor"):
                    lines.append(_common.format_pagination_hint(page["endCursor"], "after"))
                return "\n".join(lines)

            # GraphQL not accessible: fall back to the plain roster (no roles).
            resp, rerr = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/orgs/{org}/members",
                _PROVIDER,
                f"list members of {org}",
                headers=headers,
                params={"per_page": n},
            )
            if rerr:
                return _common.with_permission_hint(rerr, _ORG_MEMBERS_HINT)
            roster = _common.format_entity_list(
                resp.json(),
                lambda m: f"{m.get('login')} ({m.get('type', 'User')}) — {m.get('html_url', '')}",
                noun="member",
                empty_msg=f"No members found in {org}.",
            )
            return (
                f"{roster}\n\nNote: member roles could not be retrieved — "
                f"{_common.with_permission_hint(err, _ORG_MEMBERS_HINT)}"
            )

    async def get_github_org_membership(org: str, username: str) -> str:
        """Get a single user's organization role (admin/member) and membership state."""
        if e := login_error(org, "org"):
            return e
        if e := login_error(username, "username"):
            return e
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/orgs/{org}/memberships/{username}",
                _PROVIDER,
                f"get membership of {username} in {org}",
                headers=headers,
                expected_statuses={404},
            )
            if err:
                # GitHub returns 404 when the user is not a member of the org —
                # that is a valid, expected outcome (not an error), so short-circuit
                # to a clear "not a member" answer instead of surfacing a tool
                # failure that the LLM cannot reason past.
                if resp is not None and resp.status_code == 404:
                    return f"{username} is not a member of {org}."
                return _common.with_permission_hint(err, _ORG_MEMBERS_HINT)
            m = resp.json()
            user = m.get("user") or {}
            return "\n".join(
                [
                    f"Membership of {user.get('login', username)} in {org}:",
                    f"Role: {m.get('role', '?')} (admin = organization owner, member = regular member)",
                    f"State: {m.get('state', '?')} (active or pending invitation)",
                ]
            )

    async def list_github_teams(org: str, count: int = 50) -> str:
        """List teams in an organization with slug, privacy, and description."""
        if e := login_error(org, "org"):
            return e
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/orgs/{org}/teams",
                _PROVIDER,
                f"list teams in {org}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            teams = resp.json()

            def _fmt(t: dict) -> str:
                desc = (t.get("description") or "").strip().replace("\n", " ")
                desc_str = f" — {desc[:100]}" if desc else ""
                return f"{t.get('name')} (slug: {t.get('slug')}) [{t.get('privacy', '?')}]{desc_str}"

            return _common.format_entity_list(
                teams, _fmt, noun="team", empty_msg=f"No teams found in {org}."
            )

    async def get_github_team(org: str, team_slug: str) -> str:
        """Get a team's details plus its members and repositories."""
        if e := login_error(org, "org"):
            return e
        if e := team_slug_error(team_slug):
            return e
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/orgs/{org}/teams/{team_slug}",
                _PROVIDER,
                f"get team {team_slug} in {org}",
                headers=headers,
            )
            if err:
                return _common.with_permission_hint(err, _ORG_MEMBERS_HINT)
            t = resp.json()
            members_resp, _ = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/orgs/{org}/teams/{team_slug}/members",
                _PROVIDER,
                "fetch team members",
                headers=headers,
                params={"per_page": 100},
            )
            # A second pass with role=maintainer lets us label each member's team
            # role (maintainer vs plain member) - the REST team-members payload
            # itself does not carry the role.
            maint_resp, _ = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/orgs/{org}/teams/{team_slug}/members",
                _PROVIDER,
                "fetch team maintainers",
                headers=headers,
                params={"per_page": 100, "role": "maintainer"},
            )
            repos_resp, _ = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/orgs/{org}/teams/{team_slug}/repos",
                _PROVIDER,
                "fetch team repos",
                headers=headers,
                params={"per_page": 100},
            )
            members = members_resp.json() if members_resp else []
            maintainers = {
                m.get("login") for m in (maint_resp.json() if maint_resp else []) if isinstance(m, dict)
            }
            repos = repos_resp.json() if repos_resp else []
            repos_count = t.get("repos_count", len(repos))
            if _common.active_repo_scope() is not None:
                # Under a repository Scope(s) the team's other
                # repositories are neither named nor counted.
                repos = [
                    r for r in (repos if isinstance(repos, list) else [])
                    if isinstance(r, dict) and _common.in_repo_scope(r.get("full_name"))
                ]
                repos_count = f"{len(repos)} in this Scope(s)"
            lines = [
                f"Team: {t.get('name')} (slug: {t.get('slug')})",
                f"Privacy: {t.get('privacy', '?')} | Members: {t.get('members_count', len(members))} | Repos: {repos_count}",
                f"Description: {t.get('description') or 'None'}",
            ]
            if members:
                member_strs = [
                    f"{login} (maintainer)" if (login := m.get("login", "?")) in maintainers else login
                    for m in members[:50]
                ]
                lines.append("\nMembers: " + ", ".join(member_strs))
            if repos:
                lines.append("Repositories: " + ", ".join(r.get("full_name", "?") for r in repos[:50]))
            return "\n".join(lines)

    async def manage_github_team_membership(
        org: str,
        team_slug: str,
        action: str,
        username: str = "",
        repo: str = "",
        role: str = "member",
        permission: str = "push",
    ) -> str:
        """Manage a team's membership and repository access (write).

        ``action`` is one of:
          - ``add_member`` / ``remove_member`` — add or remove ``username``
            (``role`` = member | maintainer when adding).
          - ``add_repo`` / ``remove_repo`` — grant or revoke a team's access to
            ``repo`` (owner/name); ``permission`` = pull | triage | push |
            maintain | admin when granting.
        """
        if e := login_error(org, "org"):
            return e
        if e := team_slug_error(team_slug):
            return e
        action = (action or "").strip().lower()
        jheaders = _json_headers(headers)
        async with egress_client(timeout=30) as client:
            if action in ("add_member", "remove_member"):
                if e := login_error(username, "username"):
                    return e
                url = f"{_BASE_URL}/orgs/{org}/teams/{team_slug}/memberships/{username}"
                if action == "add_member":
                    body = {"role": role if role in ("member", "maintainer") else "member"}
                    resp, err = await _common.safe_request(
                        client, "put", url, _PROVIDER, f"add {username} to {team_slug}", headers=jheaders, json=body
                    )
                    if err:
                        return err
                    state = (resp.json() or {}).get("state", "active")
                    return f"Added {username} to team {team_slug} as {body['role']} (state: {state})."
                resp, err = await _common.safe_request(
                    client, "delete", url, _PROVIDER, f"remove {username} from {team_slug}", headers=headers
                )
                if err:
                    return err
                return f"Removed {username} from team {team_slug}."
            if action in ("add_repo", "remove_repo"):
                owner, name, rerr = _common.parse_repo(repo)
                if rerr:
                    return rerr
                url = f"{_BASE_URL}/orgs/{org}/teams/{team_slug}/repos/{owner}/{name}"
                if action == "add_repo":
                    valid = {"pull", "triage", "push", "maintain", "admin"}
                    body = {"permission": permission if permission in valid else "push"}
                    resp, err = await _common.safe_request(
                        client, "put", url, _PROVIDER, f"grant {team_slug} access to {repo}", headers=jheaders, json=body
                    )
                    if err:
                        return err
                    return f"Granted team {team_slug} '{body['permission']}' access to {owner}/{name}."
                resp, err = await _common.safe_request(
                    client, "delete", url, _PROVIDER, f"revoke {team_slug} access to {repo}", headers=headers
                )
                if err:
                    return err
                return f"Revoked team {team_slug} access to {owner}/{name}."
            return "Error: invalid action. Use add_member, remove_member, add_repo, or remove_repo."

    executor.register("list_github_organizations", list_github_organizations)
    executor.register("get_github_organization", get_github_organization)
    executor.register("list_github_org_members", list_github_org_members)
    executor.register("get_github_org_membership", get_github_org_membership)
    executor.register("list_github_teams", list_github_teams)
    executor.register("get_github_team", get_github_team)
    executor.register("manage_github_team_membership", manage_github_team_membership)

    _org = {
        "type": "object",
        "properties": {"org": {"type": "string", "description": "Organization login"}},
        "required": ["org"],
    }

    return [
        ToolDefinition(
            name="list_github_organizations",
            description="List GitHub organizations and accounts the app/user has access to. Use this to discover available orgs before listing repos or working with them.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_organization",
            description="Get an organization's profile: plan and seat usage, public/private repo counts, follower counts, and metadata.",
            input_schema=_org,
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_org_members",
            description=(
                "List members of an organization WITH each member's role (admin = organization owner, "
                "member = regular member). Filter by role (all, admin, member). This is the org-membership "
                "RBAC roster - use it to answer 'who belongs to the org and who are the owners/admins'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login"},
                    "role": {"type": "string", "enum": ["all", "admin", "member"], "default": "all"},
                    "count": {"type": "integer", "description": "Max members per page (max 100)", "default": 50},
                    "after": {"type": "string", "description": "Pagination cursor from a previous call (role=all only)", "default": ""},
                },
                "required": ["org"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_org_membership",
            description=(
                "Get one user's organization membership: their role (admin = owner, member) and state "
                "(active or pending invitation). Use for a targeted RBAC check on a single member."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login"},
                    "username": {"type": "string", "description": "GitHub login of the member to check"},
                },
                "required": ["org", "username"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_teams",
            description="List teams in an organization with their slug (needed for other team tools), privacy, and description.",
            input_schema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login"},
                    "count": {"type": "integer", "description": "Max teams (max 100)", "default": 50},
                },
                "required": ["org"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_team",
            description="Get a team's details plus its members (maintainers are labelled) and the repositories it can access. Use the team slug from list_github_teams.",
            input_schema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login"},
                    "team_slug": {"type": "string", "description": "Team slug (from list_github_teams)"},
                },
                "required": ["org", "team_slug"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_github_team_membership",
            description=(
                "Manage a team's membership and repository access. action=add_member/remove_member "
                "manages a user's team membership (role=member|maintainer); action=add_repo/remove_repo "
                "grants or revokes a team's access to a repository (permission=pull|triage|push|maintain|admin). "
                "Requires organization admin permissions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login"},
                    "team_slug": {"type": "string", "description": "Team slug"},
                    "action": {
                        "type": "string",
                        "enum": ["add_member", "remove_member", "add_repo", "remove_repo"],
                    },
                    "username": {"type": "string", "description": "User login (for add_member/remove_member)", "default": ""},
                    "repo": {"type": "string", "description": "Repository owner/name (for add_repo/remove_repo)", "default": ""},
                    "role": {"type": "string", "enum": ["member", "maintainer"], "default": "member"},
                    "permission": {
                        "type": "string",
                        "enum": ["pull", "triage", "push", "maintain", "admin"],
                        "default": "push",
                    },
                },
                "required": ["org", "team_slug", "action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
