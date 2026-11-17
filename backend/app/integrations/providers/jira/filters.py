"""Jira saved filters (saved JQL) and dashboards."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _SOURCE, api3, clamp, jira_request, truncate


def _format_filter(f: dict[str, Any]) -> list[str]:
    owner = (f.get("owner") or {}).get("displayName", "?")
    lines = [f"  {f.get('name', '?')} (id: {f.get('id', '?')}, owner: {owner})"]
    if f.get("jql"):
        lines.append(f"    JQL: {truncate(f['jql'], 300)}")
    return lines


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    api = api3(base)

    async def list_jira_filters(query: str = "", count: int = 30) -> str:
        """List saved filters. With a ``query`` searches by name; otherwise returns your favourites."""
        if query:
            resp, err = await jira_request(
                "get",
                f"{api}/filter/search",
                f"search filters '{query}'",
                headers=headers,
                params={
                    "filterName": query,
                    "maxResults": clamp(count, 30, 50),
                    "expand": "jql,owner",
                },
            )
            if err:
                return err
            filters = resp.json().get("values", [])
            label = f"Filters matching '{query}'"
        else:
            resp, err = await jira_request(
                "get",
                f"{api}/filter/favourite",
                "list favourite filters",
                headers=headers,
                params={"expand": "jql,owner"},
            )
            if err:
                return err
            filters = resp.json() or []
            label = "Favourite filters"
        if not filters:
            return f"{label}: none found."
        lines = [f"{label} ({len(filters)}):"]
        for f in filters:
            lines.extend(_format_filter(f))
        return "\n".join(lines)

    async def manage_jira_filter(
        action: str,
        filter_id: str = "",
        name: str = "",
        jql: str = "",
        description: str = "",
        favourite: bool = False,
    ) -> str:
        """Get, create, update or delete a saved filter (saved JQL).

        Actions: 'get' (filter_id), 'create' (name + jql), 'update' (filter_id +
        fields), 'delete' (filter_id).
        """
        action = (action or "").strip().lower()
        if action == "get":
            if not filter_id:
                return "filter_id is required for 'get'."
            resp, err = await jira_request(
                "get",
                f"{api}/filter/{filter_id}",
                f"get filter {filter_id}",
                headers=headers,
                params={"expand": "jql,owner,description"},
            )
            if err:
                return err
            f = resp.json()
            lines = [f"Filter '{f.get('name', '?')}' (id: {f.get('id', '?')})"]
            if f.get("description"):
                lines.append(f"  Description: {truncate(f['description'])}")
            lines.append(f"  JQL: {f.get('jql', '?')}")
            lines.append(f"  Owner: {(f.get('owner') or {}).get('displayName', '?')}")
            return "\n".join(lines)
        if action == "create":
            if not (name and jql):
                return "name and jql are required for 'create'."
            payload: dict[str, Any] = {"name": name, "jql": jql, "favourite": favourite}
            if description:
                payload["description"] = description
            resp, err = await jira_request(
                "post", f"{api}/filter", "create filter", headers=headers, json=payload
            )
            if err:
                return err
            f = resp.json()
            return f"Created filter '{f.get('name')}' (id: {f.get('id')})."
        if action == "update":
            if not filter_id:
                return "filter_id is required for 'update'."
            payload = {}
            if name:
                payload["name"] = name
            if jql:
                payload["jql"] = jql
            if description:
                payload["description"] = description
            if not payload:
                return "Provide name, jql or description to update."
            _, err = await jira_request(
                "put",
                f"{api}/filter/{filter_id}",
                f"update filter {filter_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Filter {filter_id} updated."
        if action == "delete":
            if not filter_id:
                return "filter_id is required for 'delete'."
            _, err = await jira_request(
                "delete",
                f"{api}/filter/{filter_id}",
                f"delete filter {filter_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Filter {filter_id} deleted."
        return f"Unknown action '{action}'. Valid: get, create, update, delete."

    async def list_jira_dashboards(count: int = 30) -> str:
        """List dashboards visible to the user (name, id, owner)."""
        resp, err = await jira_request(
            "get",
            f"{api}/dashboard",
            "list dashboards",
            headers=headers,
            params={"maxResults": clamp(count, 30, 50)},
        )
        if err:
            return err
        dashboards = resp.json().get("dashboards", [])
        if not dashboards:
            return "No dashboards found."
        lines = [f"Dashboards ({len(dashboards)}):"]
        for d in dashboards:
            owner = (d.get("owner") or {}).get("displayName", "?")
            lines.append(f"  {d.get('name', '?')} (id: {d.get('id', '?')}, owner: {owner})")
        return "\n".join(lines)

    executor.register("list_jira_filters", list_jira_filters)
    executor.register("manage_jira_filter", manage_jira_filter)
    executor.register("list_jira_dashboards", list_jira_dashboards)

    return [
        ToolDefinition(
            name="list_jira_filters",
            description="List saved filters (saved JQL). With a 'query' searches filter names; without it returns your favourite filters (name, id, JQL, owner).",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Filter name search (omit for favourites)", "default": ""},
                    "count": {"type": "integer", "description": "Max filters (max 50)", "default": 30},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_jira_filter",
            description=(
                "Get, create, update or delete a saved filter (saved JQL). Actions: "
                "'get' (filter_id), 'create' (name + jql [+ description/favourite]), "
                "'update' (filter_id + fields), 'delete' (filter_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "create", "update", "delete"]},
                    "filter_id": {"type": "string", "description": "Filter id (get/update/delete)", "default": ""},
                    "name": {"type": "string", "description": "Filter name (create/update)", "default": ""},
                    "jql": {"type": "string", "description": "JQL query (create/update)", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "favourite": {"type": "boolean", "description": "Mark as favourite (create)", "default": False},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_jira_dashboards",
            description="List dashboards visible to the user (name, id, owner).",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Max dashboards (max 50)", "default": 30},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
