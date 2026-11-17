"""GitHub Enterprise tools (GraphQL) - the enterprise-account governance surface.

GitHub Enterprise Cloud groups multiple organizations under a single
**enterprise account**. The enterprise's organization roster, its member roster,
and who its owners / billing managers are live in the GraphQL API's
``enterprise(slug:)`` field - they are NOT exposed by the REST API. These tools
wrap those queries so an agent can answer governance / audit questions such as
"which organizations and members belong to the enterprise, and who are its
owners" without hand-writing GraphQL (the gap an RBAC audit hits when it tries a
raw ``membersWithRole`` / enterprise query and gets *Resource not accessible by
integration*).

Access notes:
  - Enterprise data generally requires a Personal Access Token (classic) with
    the ``read:enterprise`` (or ``admin:enterprise``) scope. GitHub **App**
    installation tokens are scoped to organizations/repositories and cannot read
    an enterprise account, so these tools degrade with a clear remediation hint.
  - Enterprise *owners / billing managers* (``ownerInfo``) are only visible to a
    caller who is themselves an enterprise owner.

Docs: https://docs.github.com/en/graphql/reference/objects#enterprise
"""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _coerce_str

_SOURCE = "integration:github"

#: Concrete remediation appended (only on permission errors) to enterprise reads.
_ENTERPRISE_HINT = (
    "use a Personal Access Token (classic) with the 'read:enterprise' (or "
    "'admin:enterprise') scope - GitHub App installation tokens cannot read an "
    "enterprise account. Listing enterprise owners/billing managers additionally "
    "requires you to be an enterprise owner."
)


def _slug_error(slug: Any) -> str | None:
    """Return a friendly error when *slug* is empty.

    The slug is sent as a GraphQL variable (never interpolated into a URL path),
    so there is no path-injection surface to guard - only emptiness is rejected.
    """
    if not _coerce_str(slug).strip():
        return (
            "Error: 'slug' is required (the enterprise slug from the URL, e.g. "
            "'my-enterprise' in https://github.com/enterprises/my-enterprise)."
        )
    return None


def _not_found(slug: str) -> str:
    return (
        f"Enterprise '{slug}' not found or not accessible. Check the slug, and "
        f"note that to read enterprise data you must {_ENTERPRISE_HINT}"
    )


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub Enterprise tools and register their callables."""

    async def get_github_enterprise(slug: str) -> str:
        """Get an enterprise account's profile: name, URL, and org/member counts."""
        if e := _slug_error(slug):
            return e
        slug = slug.strip()
        query = (
            "query($slug:String!){enterprise(slug:$slug){"
            "name slug description url location websiteUrl viewerIsAdmin "
            "organizations{totalCount} members{totalCount}}}"
        )
        async with egress_client(timeout=30) as client:
            data, err = await _common.gh_graphql(
                client, headers, query, {"slug": slug}, action=f"get enterprise {slug}"
            )
            if err:
                return _common.with_permission_hint(err, _ENTERPRISE_HINT)
            ent = data.get("enterprise")
            if not ent:
                return _not_found(slug)
            orgs = (ent.get("organizations") or {}).get("totalCount", "?")
            members = (ent.get("members") or {}).get("totalCount", "?")
            lines = [
                f"Enterprise: {ent.get('name')} (slug: {ent.get('slug')})",
                f"Description: {ent.get('description') or 'None'}",
                f"URL: {ent.get('url', '')}",
                f"Organizations: {orgs} | Members: {members}",
                f"You are an enterprise owner: {'yes' if ent.get('viewerIsAdmin') else 'no'}",
            ]
            if ent.get("location"):
                lines.append(f"Location: {ent['location']}")
            if ent.get("websiteUrl"):
                lines.append(f"Website: {ent['websiteUrl']}")
            lines.append(
                "\nUse list_github_enterprise_organizations, list_github_enterprise_members, "
                "and list_github_enterprise_owners to enumerate the roster and roles."
            )
            return "\n".join(lines)

    async def list_github_enterprise_organizations(slug: str, count: int = 50, after: str = "") -> str:
        """List the organizations that belong to an enterprise account."""
        if e := _slug_error(slug):
            return e
        slug = slug.strip()
        n = max(1, min(count, 100))
        query = (
            "query($slug:String!,$n:Int!,$after:String){enterprise(slug:$slug){"
            "organizations(first:$n, after:$after){totalCount "
            "pageInfo{hasNextPage endCursor} nodes{login name description}}}}"
        )
        async with egress_client(timeout=30) as client:
            data, err = await _common.gh_graphql(
                client, headers, query, {"slug": slug, "n": n, "after": after or None},
                action=f"list organizations of enterprise {slug}",
            )
            if err:
                return _common.with_permission_hint(err, _ENTERPRISE_HINT)
            ent = data.get("enterprise")
            if not ent:
                return _not_found(slug)
            conn = ent.get("organizations") or {}
            nodes = [o for o in (conn.get("nodes") or []) if isinstance(o, dict)]
            if not nodes:
                return f"No organizations found in enterprise '{slug}'."
            total = conn.get("totalCount")
            lines = [f"Organizations in enterprise '{slug}'" + (f" ({total} total)" if total is not None else "") + ":"]
            for o in nodes:
                desc = (o.get("description") or "").strip().replace("\n", " ")
                desc_str = f" — {desc[:100]}" if desc else ""
                lines.append(f"  - {o.get('login')} ({o.get('name') or 'no display name'}){desc_str}")
            page = conn.get("pageInfo") or {}
            if page.get("hasNextPage") and page.get("endCursor"):
                lines.append(_common.format_pagination_hint(page["endCursor"], "after"))
            return "\n".join(lines)

    async def list_github_enterprise_members(slug: str, count: int = 50, after: str = "", query: str = "") -> str:
        """List the members of an enterprise account (its full people roster).

        Optionally narrow the roster with ``query`` (matches login / name /
        email). Enterprise *owners* and *billing managers* are listed with their
        role by list_github_enterprise_owners.
        """
        if e := _slug_error(slug):
            return e
        slug = slug.strip()
        n = max(1, min(count, 100))
        gql = (
            "query($slug:String!,$n:Int!,$after:String,$q:String){enterprise(slug:$slug){"
            "members(first:$n, after:$after, query:$q){totalCount "
            "pageInfo{hasNextPage endCursor} nodes{"
            "__typename ... on EnterpriseUserAccount{login name} ... on User{login name}}}}}"
        )
        async with egress_client(timeout=30) as client:
            data, err = await _common.gh_graphql(
                client, headers, gql,
                {"slug": slug, "n": n, "after": after or None, "q": query.strip() or None},
                action=f"list members of enterprise {slug}",
            )
            if err:
                return _common.with_permission_hint(err, _ENTERPRISE_HINT)
            ent = data.get("enterprise")
            if not ent:
                return _not_found(slug)
            conn = ent.get("members") or {}
            nodes = [m for m in (conn.get("nodes") or []) if isinstance(m, dict)]
            if not nodes:
                return f"No members found in enterprise '{slug}'."
            total = conn.get("totalCount")
            lines = [f"Members of enterprise '{slug}'" + (f" ({total} total)" if total is not None else "") + ":"]
            for m in nodes:
                login = m.get("login", "?")
                name = m.get("name")
                lines.append(f"  - {login} ({name})" if name else f"  - {login}")
            page = conn.get("pageInfo") or {}
            if page.get("hasNextPage") and page.get("endCursor"):
                lines.append(_common.format_pagination_hint(page["endCursor"], "after"))
            return "\n".join(lines)

    async def list_github_enterprise_owners(slug: str, count: int = 50, after: str = "") -> str:
        """List an enterprise's administrators WITH their role (owner / billing manager).

        Requires the caller to be an enterprise owner; otherwise GitHub denies
        access and the tool reports the permission needed.
        """
        if e := _slug_error(slug):
            return e
        slug = slug.strip()
        n = max(1, min(count, 100))
        query = (
            "query($slug:String!,$n:Int!,$after:String){enterprise(slug:$slug){ownerInfo{"
            "admins(first:$n, after:$after){totalCount "
            "pageInfo{hasNextPage endCursor} edges{role node{login name}}}}}}"
        )
        async with egress_client(timeout=30) as client:
            data, err = await _common.gh_graphql(
                client, headers, query, {"slug": slug, "n": n, "after": after or None},
                action=f"list owners of enterprise {slug}",
            )
            if err:
                return _common.with_permission_hint(err, _ENTERPRISE_HINT)
            ent = data.get("enterprise")
            if not ent:
                return _not_found(slug)
            owner_info = ent.get("ownerInfo")
            if not owner_info:
                return (
                    f"Enterprise owners are not visible for '{slug}'. To list them you must "
                    f"{_ENTERPRISE_HINT}"
                )
            conn = owner_info.get("admins") or {}
            edges = [e for e in (conn.get("edges") or []) if isinstance(e, dict)]
            if not edges:
                return f"No administrators found in enterprise '{slug}'."
            total = conn.get("totalCount")
            lines = [f"Administrators of enterprise '{slug}'" + (f" ({total} total)" if total is not None else "") + ":"]
            for edge in edges:
                role = (edge.get("role") or "").upper()
                label = "billing manager" if role == "BILLING_MANAGER" else "owner" if role == "OWNER" else (role.lower() or "admin")
                node = edge.get("node") or {}
                login = node.get("login", "?")
                name = node.get("name")
                disp = f"{login} ({name})" if name else login
                lines.append(f"  - {disp} — {label}")
            page = conn.get("pageInfo") or {}
            if page.get("hasNextPage") and page.get("endCursor"):
                lines.append(_common.format_pagination_hint(page["endCursor"], "after"))
            return "\n".join(lines)

    executor.register("get_github_enterprise", get_github_enterprise)
    executor.register("list_github_enterprise_organizations", list_github_enterprise_organizations)
    executor.register("list_github_enterprise_members", list_github_enterprise_members)
    executor.register("list_github_enterprise_owners", list_github_enterprise_owners)

    _slug = {"slug": {"type": "string", "description": "Enterprise slug (from the enterprise URL)"}}
    _paged_slug = {
        **_slug,
        "count": {"type": "integer", "description": "Max results per page (max 100)", "default": 50},
        "after": {"type": "string", "description": "Pagination cursor from a previous call", "default": ""},
    }

    return [
        ToolDefinition(
            name="get_github_enterprise",
            description=(
                "Get a GitHub Enterprise account's profile: name, URL, and total organization + member counts. "
                "Start here for an enterprise governance/RBAC review, then enumerate the roster with the other "
                "enterprise tools. Enterprise data is GraphQL-only and needs a PAT with the read:enterprise scope."
            ),
            input_schema={"type": "object", "properties": dict(_slug), "required": ["slug"]},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_enterprise_organizations",
            description=(
                "List the organizations that belong to a GitHub Enterprise account (login, name, description). "
                "Use this to map the enterprise's org structure before drilling into per-org members/teams."
            ),
            input_schema={"type": "object", "properties": dict(_paged_slug), "required": ["slug"]},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_enterprise_members",
            description=(
                "List the members of a GitHub Enterprise account - the full people roster across all its "
                "organizations. Optionally filter with 'query' (login/name/email). Pair with "
                "list_github_enterprise_owners to see who holds the owner / billing-manager roles."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_paged_slug,
                    "query": {"type": "string", "description": "Filter by login, name, or email (optional)", "default": ""},
                },
                "required": ["slug"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_enterprise_owners",
            description=(
                "List a GitHub Enterprise account's administrators WITH their role (owner or billing manager) - "
                "the enterprise-level RBAC roster. Requires you to be an enterprise owner; otherwise it reports "
                "the access needed."
            ),
            input_schema={"type": "object", "properties": dict(_paged_slug), "required": ["slug"]},
            source=_SOURCE,
            priority=0,
        ),
    ]
