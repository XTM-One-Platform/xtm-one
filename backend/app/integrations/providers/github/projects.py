"""GitHub Projects v2 tools (GraphQL) - the product backlog surface.

Projects v2 (custom fields like Status, Priority, Iteration, ...) is NOT
available via the REST API; it lives entirely in the GraphQL API. These tools
wrap the documented Projects v2 queries/mutations so an agent can read the
backlog and its statuses, add items, and move cards between columns without
hand-writing GraphQL.

Docs: https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _BASE_URL, _PROVIDER, _coerce_int, login_error, parse_repo

_SOURCE = "integration:github"

_FIELDS_FRAGMENT = """
fields(first: 50) {
  nodes {
    ... on ProjectV2FieldCommon { id name dataType }
    ... on ProjectV2SingleSelectField { options { id name } }
    ... on ProjectV2IterationField {
      configuration { iterations { id title } completedIterations { id title } }
    }
  }
}
"""


async def _resolve_project_id(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    project_id: str,
    login: str,
    project_number: int,
) -> tuple[str | None, str | None]:
    """Resolve a project node id from an explicit id, or from (login, number).

    Tries the organization first, then falls back to a user-owned project.
    """
    if project_id.strip():
        return project_id.strip(), None
    if e := login_error(login, "login"):
        return None, f"{e} (or pass project_id directly)."
    num = _coerce_int(project_number)
    if not num:
        return None, "Error: provide either project_id, or login + project_number (the number in the project URL)."
    # A combined organization{...} user{...} query returns a top-level NOT_FOUND
    # error for whichever owner type the login is NOT, so query each type
    # separately and tolerate only that benign "could not resolve to an
    # Organization/User" mismatch before falling back.
    errors: list[str] = []
    for owner_key in ("organization", "user"):
        query = f"query($login:String!,$num:Int!){{{owner_key}(login:$login){{projectV2(number:$num){{id}}}}}}"
        data, err = await _common.gh_graphql(
            client, headers, query, {"login": login, "num": num}, action=f"resolve project #{num} for {login}"
        )
        if err:
            errors.append(err)
            continue
        proj = ((data.get(owner_key) or {}).get("projectV2")) or {}
        if proj.get("id"):
            return proj["id"], None
    # Surface a genuine error (permission denied, rate limit, ...) instead of
    # masking it with the generic message; only the wrong-owner-type "Could not
    # resolve to an Organization/User" mismatch is expected and tolerated here.
    real = [e for e in errors if "could not resolve to" not in e.lower()]
    if real:
        return None, real[0]
    return None, f"Error: No project #{num} found for '{login}' (as an org or user). Check the login and project number."


_ITEM_REPOSITORY_QUERY = (
    "query($i:ID!){node(id:$i){... on ProjectV2Item{content{__typename "
    "... on Issue{repository{nameWithOwner}} ... on PullRequest{repository{nameWithOwner}}}}}}"
)


async def _item_scope_error(
    client: httpx.AsyncClient, headers: dict[str, str], item_id: str
) -> str | None:
    """Under a repository Scope(s), why project item *item_id* may not change.

    A card is an issue or pull request of SOME repository - or a draft, which
    belongs to none - and its opaque ``PVTI_`` id only says which once it is
    resolved. Anything that does not resolve to a draft or to a scoped
    repository is refused.
    """
    scope = _common.active_repo_scope()
    if scope is None:
        return None
    data, err = await _common.gh_graphql(
        client, headers, _ITEM_REPOSITORY_QUERY, {"i": item_id}, action="resolve project item"
    )
    if err:
        return err
    content = ((data.get("node") or {}).get("content")) or {}
    kind = content.get("__typename")
    repo = (content.get("repository") or {}).get("nameWithOwner") or ""
    if kind == "DraftIssue" or (kind in ("Issue", "PullRequest") and _common.in_repo_scope(repo)):
        return None
    where = f"repository '{repo}'" if repo else "a repository that could not be resolved"
    return (
        f"Error: project item {item_id} belongs to {where}, outside the Scope(s) set "
        f"for this agent or assignment (allowed repositories: {', '.join(sorted(scope)) or 'none'}). "
        "Do not retry with another item of that repository."
    )


async def _fetch_fields(
    client: httpx.AsyncClient, headers: dict[str, str], project_id: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch a project's fields with their ids, types, options and iterations."""
    query = "query($id:ID!){node(id:$id){... on ProjectV2 {" + _FIELDS_FRAGMENT + "}}}"
    data, err = await _common.gh_graphql(client, headers, query, {"id": project_id}, action="fetch project fields")
    if err:
        return [], err
    nodes = (((data.get("node") or {}).get("fields")) or {}).get("nodes") or []
    return [n for n in nodes if n], None


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub Projects v2 tools and register their callables."""

    async def list_github_projects(login: str = "", repo: str = "", count: int = 20) -> str:
        """List Projects v2 for an org/user login, or for a repository (repo='owner/name')."""
        count = max(1, min(count, 50))
        async with egress_client(timeout=30) as client:
            if repo.strip():
                owner, name, rerr = parse_repo(repo)
                if rerr:
                    return rerr
                query = (
                    "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){"
                    "projectsV2(first:$n){nodes{id number title url closed shortDescription items{totalCount}}}}}"
                )
                data, err = await _common.gh_graphql(
                    client, headers, query, {"o": owner, "r": name, "n": count}, action=f"list projects for {repo}"
                )
                if err:
                    return err
                nodes = (((data.get("repository") or {}).get("projectsV2")) or {}).get("nodes") or []
                scope = repo
            else:
                if e := login_error(login, "login"):
                    return e
                # Query org then user separately (a combined query errors on the
                # owner type the login is not); break as soon as one resolves.
                nodes = []
                last_err: str | None = None
                for owner_key in ("organization", "user"):
                    query = (
                        f"query($login:String!,$n:Int!){{{owner_key}(login:$login){{"
                        "projectsV2(first:$n){nodes{id number title url closed shortDescription items{totalCount}}}}}"
                    )
                    data, err = await _common.gh_graphql(
                        client, headers, query, {"login": login, "n": count}, action=f"list projects for {login}"
                    )
                    if err:
                        last_err = err
                        continue
                    owner = data.get(owner_key)
                    if owner is not None:
                        nodes = ((owner.get("projectsV2")) or {}).get("nodes") or []
                        last_err = None
                        break
                if last_err and not nodes:
                    return last_err
                scope = login
            if not nodes:
                return f"No Projects v2 found for {scope}."
            lines = [f"Projects v2 for {scope} ({len(nodes)}):"]
            for p in nodes:
                state = "closed" if p.get("closed") else "open"
                desc = (p.get("shortDescription") or "").strip()
                desc_str = f" — {desc}" if desc else ""
                lines.append(
                    f"  #{p.get('number')} {p.get('title')} [{state}] "
                    f"({(p.get('items') or {}).get('totalCount', 0)} items) — {p.get('url', '')}{desc_str}"
                    f"\n      id: {p.get('id')}"
                )
            lines.append("\nUse the project number with 'login', or the id, in the other project tools.")
            return "\n".join(lines)

    async def get_github_project(project_id: str = "", login: str = "", project_number: int = 0) -> str:
        """Get a project's fields with their options/iterations (needed to set Status, Priority, etc.)."""
        async with egress_client(timeout=30) as client:
            pid, err = await _resolve_project_id(client, headers, project_id, login, project_number)
            if err:
                return err
            query = (
                "query($id:ID!){node(id:$id){... on ProjectV2 {"
                "title url number shortDescription closed items{totalCount}" + _FIELDS_FRAGMENT + "}}}"
            )
            data, err = await _common.gh_graphql(client, headers, query, {"id": pid}, action="get project")
            if err:
                return err
            node = data.get("node") or {}
            if not node:
                return "Project not found or not accessible."
            fields = ((node.get("fields")) or {}).get("nodes") or []
            lines = [
                f"Project #{node.get('number')}: {node.get('title')} [{'closed' if node.get('closed') else 'open'}]",
                f"URL: {node.get('url', '')}",
                f"Items: {(node.get('items') or {}).get('totalCount', 0)}",
                f"id: {pid}",
            ]
            if node.get("shortDescription"):
                lines.append(f"Description: {node['shortDescription']}")
            lines.append(f"\nFields ({len(fields)}):")
            for f in fields:
                if not f:
                    continue
                dtype = f.get("dataType", "?")
                line = f"  {f.get('name')} [{dtype}]"
                if f.get("options"):
                    line += " — options: " + ", ".join(o.get("name", "?") for o in f["options"])
                cfg = f.get("configuration") or {}
                iters = (cfg.get("iterations") or []) + (cfg.get("completedIterations") or [])
                if iters:
                    line += " — iterations: " + ", ".join(i.get("title", "?") for i in iters[:10])
                lines.append(line)
            return "\n".join(lines)

    async def list_github_project_items(
        project_id: str = "", login: str = "", project_number: int = 0, count: int = 50, after: str = ""
    ) -> str:
        """List a project's items (the backlog) with their content and ALL custom field values (Status, Priority, ...)."""
        count = max(1, min(count, 100))
        async with egress_client(timeout=30) as client:
            pid, err = await _resolve_project_id(client, headers, project_id, login, project_number)
            if err:
                return err
            query = (
                "query($id:ID!,$n:Int!,$after:String){node(id:$id){... on ProjectV2 {"
                "items(first:$n, after:$after){pageInfo{hasNextPage endCursor} nodes{"
                "id isArchived "
                # 50 mirrors the fields(first: 50) cap in _FIELDS_FRAGMENT, so an
                # item's field values are not silently dropped below the number of
                # fields the project can define.
                "fieldValues(first:50){nodes{"
                "... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2FieldCommon{name}}}"
                "... on ProjectV2ItemFieldNumberValue{number field{... on ProjectV2FieldCommon{name}}}"
                "... on ProjectV2ItemFieldDateValue{date field{... on ProjectV2FieldCommon{name}}}"
                "... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2FieldCommon{name}}}"
                "... on ProjectV2ItemFieldIterationValue{title field{... on ProjectV2FieldCommon{name}}}}}"
                "content{"
                "... on DraftIssue{title}"
                "... on Issue{number title state url repository{nameWithOwner}}"
                "... on PullRequest{number title state url repository{nameWithOwner}}}}}}}}"
            )
            variables: dict[str, Any] = {"id": pid, "n": count, "after": after or None}
            data, err = await _common.gh_graphql(client, headers, query, variables, action="list project items")
            if err:
                return err
            items_conn = ((data.get("node") or {}).get("items")) or {}
            nodes = items_conn.get("nodes") or []
            if not nodes:
                return "This project has no items."
            # Under a repository Scope(s) a card of another repository
            # - or one whose repository is not readable - is not shown; drafts
            # belong to no repository and are.
            scoped = _common.active_repo_scope() is not None
            lines: list[str] = []
            for it in nodes:
                content = it.get("content") or {}
                if content.get("number") is not None:
                    repo_name = (content.get("repository") or {}).get("nameWithOwner", "")
                    if scoped and not _common.in_repo_scope(repo_name):
                        continue
                    head = f"[{repo_name}#{content['number']}] {content.get('title')} ({content.get('state', '?')})"
                elif content.get("title"):
                    head = f"(draft) {content['title']}"
                elif scoped:
                    continue
                else:
                    head = "(item not accessible)"
                fvals = []
                for fv in ((it.get("fieldValues") or {}).get("nodes") or []):
                    fname = (fv.get("field") or {}).get("name")
                    if not fname or fname == "Title":
                        continue
                    val = fv.get("name") or fv.get("text") or fv.get("title") or fv.get("date")
                    if val is None and "number" in fv:
                        val = fv["number"]
                    if val is not None:
                        fvals.append(f"{fname}: {val}")
                archived = " [archived]" if it.get("isArchived") else ""
                line = f"  - {head}{archived}"
                if fvals:
                    line += "\n      " + " | ".join(fvals)
                line += f"\n      item_id: {it.get('id')}"
                lines.append(line)
            page = items_conn.get("pageInfo") or {}
            more = page.get("hasNextPage") and page.get("endCursor")
            if not lines and not more:
                return "This project has no items in this Scope(s)."
            header = f"Project items in this Scope(s) ({len(lines)}):" if scoped else f"Project items ({len(lines)}):"
            lines.insert(0, header)
            if more:
                lines.append(_common.format_pagination_hint(page["endCursor"], "after"))
            return "\n".join(lines)

    async def add_github_project_item(
        repo: str, content_number: int, project_id: str = "", login: str = "", project_number: int = 0
    ) -> str:
        """Add an existing issue or pull request (repo + number) to a project."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            pid, err = await _resolve_project_id(client, headers, project_id, login, project_number)
            if err:
                return err
            # Resolve the issue/PR node id (issues endpoint also returns PRs).
            issue_resp, ierr = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/issues/{content_number}",
                _PROVIDER,
                f"resolve {repo}#{content_number}",
                headers=headers,
            )
            if ierr:
                return ierr
            content_node = (issue_resp.json() or {}).get("node_id")
            if not content_node:
                return f"Could not resolve node id for {repo}#{content_number}."
            query = (
                "mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}"
            )
            data, gerr = await _common.gh_graphql(
                client, headers, query, {"p": pid, "c": content_node}, action="add project item"
            )
            if gerr:
                return gerr
            item_id = (((data.get("addProjectV2ItemById") or {}).get("item")) or {}).get("id")
            return f"Added {repo}#{content_number} to the project (item_id: {item_id})."

    async def create_github_project_draft(
        title: str, body: str = "", project_id: str = "", login: str = "", project_number: int = 0
    ) -> str:
        """Create a draft issue directly in a project (a backlog card not yet a repo issue)."""
        if not title.strip():
            return "Error: 'title' is required."
        async with egress_client(timeout=30) as client:
            pid, err = await _resolve_project_id(client, headers, project_id, login, project_number)
            if err:
                return err
            query = (
                "mutation($p:ID!,$t:String!,$b:String!){"
                "addProjectV2DraftIssue(input:{projectId:$p,title:$t,body:$b}){projectItem{id}}}"
            )
            data, gerr = await _common.gh_graphql(
                client, headers, query, {"p": pid, "t": title, "b": body}, action="create project draft"
            )
            if gerr:
                return gerr
            item_id = (((data.get("addProjectV2DraftIssue") or {}).get("projectItem")) or {}).get("id")
            return f"Created draft issue '{title}' in the project (item_id: {item_id})."

    async def set_github_project_field(
        item_id: str,
        field_name: str,
        value: str,
        project_id: str = "",
        login: str = "",
        project_number: int = 0,
    ) -> str:
        """Set a project item's custom field by name (e.g. Status='In Progress', Priority='P1', a date or number, or an iteration title)."""
        if not item_id.strip():
            return "Error: 'item_id' is required (the PVTI_... id from list_github_project_items)."
        if not field_name.strip():
            return "Error: 'field_name' is required."
        async with egress_client(timeout=30) as client:
            if scope_err := await _item_scope_error(client, headers, item_id.strip()):
                return scope_err
            pid, err = await _resolve_project_id(client, headers, project_id, login, project_number)
            if err:
                return err
            fields, ferr = await _fetch_fields(client, headers, pid)
            if ferr:
                return ferr
            field = next((f for f in fields if (f.get("name") or "").lower() == field_name.strip().lower()), None)
            if not field:
                names = ", ".join(f.get("name", "?") for f in fields)
                return f"Error: no field named '{field_name}'. Available fields: {names}."
            dtype = (field.get("dataType") or "").upper()
            value_obj: dict[str, Any]
            if dtype == "SINGLE_SELECT":
                opt = next((o for o in (field.get("options") or []) if (o.get("name") or "").lower() == value.strip().lower()), None)
                if not opt:
                    opts = ", ".join(o.get("name", "?") for o in (field.get("options") or []))
                    return f"Error: '{value}' is not a valid option for '{field_name}'. Options: {opts}."
                value_obj = {"singleSelectOptionId": opt["id"]}
            elif dtype == "ITERATION":
                cfg = field.get("configuration") or {}
                iters = (cfg.get("iterations") or []) + (cfg.get("completedIterations") or [])
                it = next((i for i in iters if (i.get("title") or "").lower() == value.strip().lower()), None)
                if not it:
                    titles = ", ".join(i.get("title", "?") for i in iters)
                    return f"Error: '{value}' is not a valid iteration for '{field_name}'. Iterations: {titles}."
                value_obj = {"iterationId": it["id"]}
            elif dtype == "NUMBER":
                try:
                    value_obj = {"number": float(value)}
                except (TypeError, ValueError):
                    return f"Error: '{value}' is not a valid number for field '{field_name}'."
            elif dtype == "DATE":
                value_obj = {"date": value.strip()}
            elif dtype in ("TEXT", "TITLE"):
                value_obj = {"text": value}
            else:
                return (
                    f"Error: field '{field_name}' (type {dtype or 'unknown'}) cannot be set with this tool. "
                    "Assignees, Labels, Milestone and Repository are properties of the issue/PR - set them with "
                    "update_github_issue / add_github_labels instead."
                )
            query = (
                "mutation($p:ID!,$i:ID!,$f:ID!,$v:ProjectV2FieldValue!){"
                "updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:$v})"
                "{projectV2Item{id}}}"
            )
            _data, gerr = await _common.gh_graphql(
                client,
                headers,
                query,
                {"p": pid, "i": item_id.strip(), "f": field["id"], "v": value_obj},
                action=f"set field {field_name}",
            )
            if gerr:
                return gerr
            return f"Set '{field['name']}' = '{value}' on project item {item_id}."

    async def manage_github_project_item(
        item_id: str, action: str, project_id: str = "", login: str = "", project_number: int = 0
    ) -> str:
        """Archive, unarchive, or delete a project item (write / destructive).

        ``action``: ``archive`` | ``unarchive`` | ``delete``. Delete removes the
        card from the project (it does not delete the underlying issue/PR).
        """
        if not item_id.strip():
            return "Error: 'item_id' is required."
        action = (action or "").strip().lower()
        async with egress_client(timeout=30) as client:
            if scope_err := await _item_scope_error(client, headers, item_id.strip()):
                return scope_err
            pid, err = await _resolve_project_id(client, headers, project_id, login, project_number)
            if err:
                return err
            iid = item_id.strip()
            if action == "delete":
                query = "mutation($p:ID!,$i:ID!){deleteProjectV2Item(input:{projectId:$p,itemId:$i}){deletedItemId}}"
            elif action == "archive":
                query = "mutation($p:ID!,$i:ID!){archiveProjectV2Item(input:{projectId:$p,itemId:$i}){item{id}}}"
            elif action == "unarchive":
                query = "mutation($p:ID!,$i:ID!){unarchiveProjectV2Item(input:{projectId:$p,itemId:$i}){item{id}}}"
            else:
                return "Error: invalid action. Use archive, unarchive, or delete."
            _data, gerr = await _common.gh_graphql(
                client, headers, query, {"p": pid, "i": iid}, action=f"{action} project item"
            )
            if gerr:
                return gerr
            return f"Project item {iid} {action}d."

    executor.register("list_github_projects", list_github_projects)
    executor.register("get_github_project", get_github_project)
    executor.register("list_github_project_items", list_github_project_items)
    executor.register("add_github_project_item", add_github_project_item)
    executor.register("create_github_project_draft", create_github_project_draft)
    executor.register("set_github_project_field", set_github_project_field)
    executor.register("manage_github_project_item", manage_github_project_item)

    _target = {
        "project_id": {"type": "string", "description": "Project node id (PVT_...); alternative to login+project_number", "default": ""},
        "login": {"type": "string", "description": "Org or user login that owns the project", "default": ""},
        "project_number": {"type": "integer", "description": "Project number from the project URL", "default": 0},
    }

    return [
        ToolDefinition(
            name="list_github_projects",
            description=(
                "List GitHub Projects v2 (the new project boards that hold the product backlog) for an org/user login, "
                "or for a repository (repo='owner/name'). Returns each project's number, title, open/closed state, item "
                "count, URL, and node id. Projects v2 data is GraphQL-only - this is how you discover the backlog board."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "login": {"type": "string", "description": "Org or user login (omit if using repo)", "default": ""},
                    "repo": {"type": "string", "description": "Repository owner/name to list repo-linked projects (optional)", "default": ""},
                    "count": {"type": "integer", "description": "Max projects (max 50)", "default": 20},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_project",
            description=(
                "Get a Project v2's custom fields with their options and iterations - the Status/Priority/etc. columns "
                "and their allowed values. Call this before set_github_project_field so you know the valid field names "
                "and option labels. Identify the project by project_id, or by login + project_number."
            ),
            input_schema={"type": "object", "properties": dict(_target), "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_project_items",
            description=(
                "List a Project v2's items - the backlog cards - with their linked issue/PR (or draft) and ALL custom "
                "field values (Status, Priority, Iteration, etc.). This is how you read 'where the backlog is and what "
                "status each item has'. Returns each item's item_id (needed to update fields) and paginates via 'after'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_target,
                    "count": {"type": "integer", "description": "Max items per page (max 100)", "default": 50},
                    "after": {"type": "string", "description": "Pagination cursor from a previous call (optional)", "default": ""},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_github_project_item",
            description="Add an existing issue or pull request (repo + number) to a Project v2 board. Use set_github_project_field afterwards to set its Status/Priority.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository owner/name of the issue/PR"},
                    "content_number": {"type": "integer", "description": "Issue or pull request number to add"},
                    **_target,
                },
                "required": ["repo", "content_number"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="create_github_project_draft",
            description="Create a draft issue directly on a Project v2 board (a backlog card that is not yet a repository issue).",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Draft issue title"},
                    "body": {"type": "string", "description": "Draft issue body (Markdown)", "default": ""},
                    **_target,
                },
                "required": ["title"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="set_github_project_field",
            description=(
                "Set a project item's custom field by NAME (e.g. move a card by setting Status='In Progress', or set "
                "Priority='P1', a date, a number, or an iteration by its title). Pass the item_id from "
                "list_github_project_items and the human-readable value - single-select options and iterations are "
                "resolved by name automatically. Note: Assignees/Labels/Milestone are issue properties - set them with "
                "update_github_issue / add_github_labels instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "Project item id (PVTI_...) from list_github_project_items"},
                    "field_name": {"type": "string", "description": "Field name (e.g. 'Status', 'Priority', 'Iteration')"},
                    "value": {"type": "string", "description": "The value: option label, iteration title, number, date (YYYY-MM-DD), or text"},
                    **_target,
                },
                "required": ["item_id", "field_name", "value"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_github_project_item",
            description=(
                "Archive, unarchive, or delete a Project v2 item (card). action=archive|unarchive|delete. Delete removes "
                "the card from the board but does NOT delete the underlying issue/PR."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "Project item id (PVTI_...)"},
                    "action": {"type": "string", "enum": ["archive", "unarchive", "delete"]},
                    **_target,
                },
                "required": ["item_id", "action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
