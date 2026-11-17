"""Jira projects, versions and components."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _SOURCE, api3, clamp, jira_request, truncate


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    api = api3(base)

    async def _resolve_project_id(project_key: str) -> tuple[str, str | None]:
        resp, err = await jira_request(
            "get",
            f"{api}/project/{project_key}",
            f"resolve project id for {project_key}",
            headers=headers,
        )
        if err:
            return "", err
        return str(resp.json().get("id", "")), None

    async def list_jira_projects(count: int = 50, query: str = "") -> str:
        """List projects accessible to the user (key, name, type, lead)."""
        params: dict[str, Any] = {"maxResults": clamp(count, 50), "orderBy": "name"}
        if query:
            params["query"] = query
        resp, err = await jira_request(
            "get", f"{api}/project/search", "list projects", headers=headers, params=params
        )
        if err:
            return err
        data = resp.json()
        projects = data.get("values", [])
        if not projects:
            return "No projects found."
        lines = [f"Projects ({data.get('total', len(projects))} total, showing {len(projects)}):"]
        for p in projects:
            ptype = (p.get("projectTypeKey") or "").replace("_", " ")
            lead = (p.get("lead") or {}).get("displayName", "?")
            lines.append(
                f"  {p['key']} - {p['name']} [{ptype}] ({p.get('style', 'classic')}) lead: {lead}"
            )
        return "\n".join(lines)

    async def get_jira_project(project_key: str) -> str:
        """Get details for a project: description, issue types, components, lead, id."""
        resp, err = await jira_request(
            "get",
            f"{api}/project/{project_key}",
            f"get project {project_key}",
            headers=headers,
            params={"expand": "description,lead,issueTypes"},
        )
        if err:
            return err
        p = resp.json()
        lines = [
            f"Project: {p['key']} - {p['name']} (id: {p.get('id', '?')})",
            f"  Type: {p.get('projectTypeKey', '?')} | Style: {p.get('style', '?')}",
            f"  Lead: {(p.get('lead') or {}).get('displayName', '?')}",
        ]
        if p.get("description"):
            lines.append(f"  Description: {truncate(p['description'])}")
        issue_types = p.get("issueTypes", [])
        if issue_types:
            lines.append(f"  Issue Types: {', '.join(it['name'] for it in issue_types)}")
        components = p.get("components", [])
        if components:
            lines.append(f"  Components: {', '.join(c['name'] for c in components)}")
        return "\n".join(lines)

    async def list_jira_project_versions(project_key: str) -> str:
        """List the versions (fixVersions) of a project with release status and dates."""
        resp, err = await jira_request(
            "get",
            f"{api}/project/{project_key}/versions",
            f"list versions for {project_key}",
            headers=headers,
        )
        if err:
            return err
        versions = resp.json() or []
        if not versions:
            return f"No versions in project {project_key}."
        lines = [f"Versions in {project_key} ({len(versions)}):"]
        for v in versions:
            state = "released" if v.get("released") else ("archived" if v.get("archived") else "unreleased")
            release_date = v.get("releaseDate") or ""
            date_str = f", release {release_date}" if release_date else ""
            lines.append(f"  {v.get('name', '?')} [{state}{date_str}] (id: {v.get('id', '?')})")
        return "\n".join(lines)

    async def list_jira_project_components(project_key: str) -> str:
        """List the components of a project with lead and id."""
        resp, err = await jira_request(
            "get",
            f"{api}/project/{project_key}/components",
            f"list components for {project_key}",
            headers=headers,
        )
        if err:
            return err
        components = resp.json() or []
        if not components:
            return f"No components in project {project_key}."
        lines = [f"Components in {project_key} ({len(components)}):"]
        for c in components:
            lead = (c.get("lead") or {}).get("displayName", "")
            lead_str = f", lead: {lead}" if lead else ""
            lines.append(f"  {c.get('name', '?')} (id: {c.get('id', '?')}{lead_str})")
            if c.get("description"):
                lines.append(f"    {truncate(c['description'], 200)}")
        return "\n".join(lines)

    async def manage_jira_version(
        action: str,
        project_key: str = "",
        version_id: str = "",
        name: str = "",
        description: str = "",
        release_date: str = "",
        start_date: str = "",
    ) -> str:
        """Create, update, release or delete a project version.

        Actions: 'create' (project_key + name), 'update' (version_id + fields),
        'release' (version_id [+ release_date]), 'delete' (version_id).
        """
        action = (action or "").strip().lower()
        if action == "create":
            if not (project_key and name):
                return "project_key and name are required for 'create'."
            project_id, err = await _resolve_project_id(project_key)
            if err:
                return err
            payload: dict[str, Any] = {"name": name, "projectId": int(project_id)}
            if description:
                payload["description"] = description
            if release_date:
                payload["releaseDate"] = release_date
            if start_date:
                payload["startDate"] = start_date
            resp, err = await jira_request(
                "post", f"{api}/version", "create version", headers=headers, json=payload
            )
            if err:
                return err
            v = resp.json()
            return f"Created version '{v.get('name')}' (id: {v.get('id')}) in {project_key}."
        if action in ("update", "release"):
            if not version_id:
                return f"version_id is required for '{action}'."
            payload = {}
            if name:
                payload["name"] = name
            if description:
                payload["description"] = description
            if release_date:
                payload["releaseDate"] = release_date
            if start_date:
                payload["startDate"] = start_date
            if action == "release":
                payload["released"] = True
            if not payload:
                return "Provide at least one field to update."
            _, err = await jira_request(
                "put",
                f"{api}/version/{version_id}",
                f"{action} version {version_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Version {version_id} {'released' if action == 'release' else 'updated'}."
        if action == "delete":
            if not version_id:
                return "version_id is required for 'delete'."
            _, err = await jira_request(
                "delete",
                f"{api}/version/{version_id}",
                f"delete version {version_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Version {version_id} deleted."
        return f"Unknown action '{action}'. Valid: create, update, release, delete."

    async def manage_jira_component(
        action: str,
        project_key: str = "",
        component_id: str = "",
        name: str = "",
        description: str = "",
        lead_account_id: str = "",
    ) -> str:
        """Create, update or delete a project component.

        Actions: 'create' (project_key + name), 'update' (component_id + fields),
        'delete' (component_id).
        """
        action = (action or "").strip().lower()
        if action == "create":
            if not (project_key and name):
                return "project_key and name are required for 'create'."
            payload: dict[str, Any] = {"name": name, "project": project_key}
            if description:
                payload["description"] = description
            if lead_account_id:
                payload["leadAccountId"] = lead_account_id
            resp, err = await jira_request(
                "post", f"{api}/component", "create component", headers=headers, json=payload
            )
            if err:
                return err
            c = resp.json()
            return f"Created component '{c.get('name')}' (id: {c.get('id')}) in {project_key}."
        if action == "update":
            if not component_id:
                return "component_id is required for 'update'."
            payload = {}
            if name:
                payload["name"] = name
            if description:
                payload["description"] = description
            if lead_account_id:
                payload["leadAccountId"] = lead_account_id
            if not payload:
                return "Provide at least one field to update."
            _, err = await jira_request(
                "put",
                f"{api}/component/{component_id}",
                f"update component {component_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Component {component_id} updated."
        if action == "delete":
            if not component_id:
                return "component_id is required for 'delete'."
            _, err = await jira_request(
                "delete",
                f"{api}/component/{component_id}",
                f"delete component {component_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Component {component_id} deleted."
        return f"Unknown action '{action}'. Valid: create, update, delete."

    executor.register("list_jira_projects", list_jira_projects)
    executor.register("get_jira_project", get_jira_project)
    executor.register("list_jira_project_versions", list_jira_project_versions)
    executor.register("list_jira_project_components", list_jira_project_components)
    executor.register("manage_jira_version", manage_jira_version)
    executor.register("manage_jira_component", manage_jira_component)

    return [
        ToolDefinition(
            name="list_jira_projects",
            description="List Jira projects accessible to the user (key, name, type, style, lead). Optionally search by name.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Max projects (max 100)", "default": 50},
                    "query": {"type": "string", "description": "Search projects by name", "default": ""},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_jira_project",
            description="Get details for a project: numeric id, description, issue types, components and lead.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string", "description": "Project key (e.g. 'PROJ')"},
                },
                "required": ["project_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_jira_project_versions",
            description="List a project's versions (fixVersions) with release status, dates and ids.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string", "description": "Project key (e.g. 'PROJ')"},
                },
                "required": ["project_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_jira_project_components",
            description="List a project's components with lead, description and ids.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string", "description": "Project key (e.g. 'PROJ')"},
                },
                "required": ["project_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_jira_version",
            description=(
                "Create, update, release or delete a project version (used in "
                "fixVersions). Actions: 'create' (project_key + name [+ description/"
                "release_date/start_date]), 'update' (version_id + fields), 'release' "
                "(version_id [+ release_date]), 'delete' (version_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "update", "release", "delete"]},
                    "project_key": {"type": "string", "description": "Project key (create)", "default": ""},
                    "version_id": {"type": "string", "description": "Version id (update/release/delete)", "default": ""},
                    "name": {"type": "string", "description": "Version name", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "release_date": {"type": "string", "description": "Release date (YYYY-MM-DD)", "default": ""},
                    "start_date": {"type": "string", "description": "Start date (YYYY-MM-DD)", "default": ""},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_jira_component",
            description=(
                "Create, update or delete a project component. Actions: 'create' "
                "(project_key + name [+ description/lead_account_id]), 'update' "
                "(component_id + fields), 'delete' (component_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "update", "delete"]},
                    "project_key": {"type": "string", "description": "Project key (create)", "default": ""},
                    "component_id": {"type": "string", "description": "Component id (update/delete)", "default": ""},
                    "name": {"type": "string", "description": "Component name", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "lead_account_id": {"type": "string", "description": "Component lead account id", "default": ""},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
