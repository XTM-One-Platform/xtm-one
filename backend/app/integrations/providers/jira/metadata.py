"""Jira reference metadata - issue types, priorities, statuses, resolutions,
link types and labels.

Consolidated behind a single ``list_jira_metadata(kind=...)`` tool so these
small read-only lookups do not each consume a slot in the agent's tool list.
"""

from __future__ import annotations

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _SOURCE, api3, clamp, jira_request

_KINDS = ("issue_types", "priorities", "statuses", "resolutions", "link_types", "labels")


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    api = api3(base)

    async def list_jira_metadata(kind: str, count: int = 100) -> str:
        """List Jira reference data of one ``kind`` (see the enum in the schema)."""
        kind = (kind or "").strip().lower()
        if kind == "issue_types":
            resp, err = await jira_request(
                "get", f"{api}/issuetype", "list issue types", headers=headers
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return "No issue types found."
            lines = [f"Issue types ({len(items)}):"]
            for it in items:
                subtask = " (sub-task)" if it.get("subtask") else ""
                lines.append(f"  {it.get('name', '?')} - id: {it.get('id', '?')}{subtask}")
            return "\n".join(lines)
        if kind == "priorities":
            resp, err = await jira_request(
                "get", f"{api}/priority", "list priorities", headers=headers
            )
            if err:
                return err
            items = resp.json() or []
            return "Priorities:\n" + "\n".join(
                f"  {p.get('name', '?')} (id: {p.get('id', '?')})" for p in items
            )
        if kind == "statuses":
            resp, err = await jira_request(
                "get", f"{api}/status", "list statuses", headers=headers
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return "No statuses found."
            lines = [f"Statuses ({len(items)}):"]
            for s in items:
                category = (s.get("statusCategory") or {}).get("name", "?")
                lines.append(f"  {s.get('name', '?')} [{category}] (id: {s.get('id', '?')})")
            return "\n".join(lines)
        if kind == "resolutions":
            resp, err = await jira_request(
                "get", f"{api}/resolution", "list resolutions", headers=headers
            )
            if err:
                return err
            items = resp.json() or []
            return "Resolutions:\n" + "\n".join(
                f"  {r.get('name', '?')} (id: {r.get('id', '?')})" for r in items
            )
        if kind == "link_types":
            resp, err = await jira_request(
                "get", f"{api}/issueLinkType", "list issue link types", headers=headers
            )
            if err:
                return err
            items = resp.json().get("issueLinkTypes", [])
            if not items:
                return "No issue link types found."
            lines = [f"Issue link types ({len(items)}):"]
            for lt in items:
                lines.append(
                    f"  {lt.get('name', '?')}: inward='{lt.get('inward', '?')}', "
                    f"outward='{lt.get('outward', '?')}'"
                )
            return "\n".join(lines)
        if kind == "labels":
            resp, err = await jira_request(
                "get",
                f"{api}/label",
                "list labels",
                headers=headers,
                params={"maxResults": clamp(count, 100, 1000)},
            )
            if err:
                return err
            data = resp.json()
            values = data.get("values", [])
            if not values:
                return "No labels found."
            return (
                f"Labels ({data.get('total', len(values))} total, showing {len(values)}):\n  "
                + ", ".join(values)
            )
        return f"Unknown kind '{kind}'. Valid: {', '.join(_KINDS)}."

    executor.register("list_jira_metadata", list_jira_metadata)

    return [
        ToolDefinition(
            name="list_jira_metadata",
            description=(
                "List Jira reference data by 'kind': 'issue_types', 'priorities', "
                "'statuses', 'resolutions', 'link_types' (with inward/outward names "
                "for link_jira_issues), or 'labels'. Use this to discover the valid "
                "values + ids for issue creation, transitions and linking."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(_KINDS)},
                    "count": {"type": "integer", "description": "Max items for 'labels' (max 1000)", "default": 100},
                },
                "required": ["kind"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
