"""Jira field discovery - the bridge between a field NAME and its custom ID.

These read-only tools let the agent answer "what is the field ID for the
'Github url' custom field?" (e.g. ``customfield_11226``) and "what custom
fields can I set when creating/editing this issue, and what value shape do they
expect?". Without them an agent can never reliably read or write a custom field,
because Jira keys every custom field by an opaque ``customfield_N`` id.

Endpoints:
* ``GET /rest/api/3/field/search`` - paginated catalogue of fields (system +
  custom), filterable by name/description ``query`` and ``type``.
* ``GET /rest/api/3/field`` - the full non-paginated field list (fallback).
* ``GET /rest/api/3/issue/createmeta/{projectIdOrKey}/issuetypes[/{id}]`` - the
  fields available on the create screen for a project + issue type, INCLUDING
  team-managed (next-gen) project custom fields that ``/field`` omits, with
  ``required`` flags and ``allowedValues``.
* ``GET /rest/api/3/issue/{issueIdOrKey}/editmeta`` - the fields editable on a
  specific issue, with allowed values and operations.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _SOURCE, api3, clamp, jira_request


def _format_field(f: dict[str, Any]) -> str:
    schema = f.get("schema") or {}
    field_type = schema.get("type", "?")
    items = schema.get("items")
    type_str = f"{field_type}[{items}]" if items else field_type
    kind = "custom" if f.get("custom") or schema.get("custom") else "system"
    fid = f.get("id") or f.get("key") or "?"
    line = f"  {f.get('name', '?')} - id: {fid} [{kind}, {type_str}]"
    clause_names = f.get("clauseNames")
    if clause_names:
        line += f"  (JQL: {', '.join(clause_names)})"
    return line


def _format_meta_field(f: dict[str, Any]) -> list[str]:
    """Render one createmeta/editmeta field with its allowed values."""
    schema = f.get("schema") or {}
    field_type = schema.get("type", "?")
    items = schema.get("items")
    type_str = f"{field_type}[{items}]" if items else field_type
    fid = f.get("fieldId") or f.get("key") or "?"
    required = " (required)" if f.get("required") else ""
    lines = [f"  {f.get('name', '?')} - id: {fid} [{type_str}]{required}"]
    allowed = f.get("allowedValues")
    if isinstance(allowed, list) and allowed:
        rendered = []
        for v in allowed[:15]:
            if isinstance(v, dict):
                rendered.append(
                    str(
                        v.get("name")
                        or v.get("value")
                        or v.get("id")
                        or v.get("key")
                        or v
                    )
                )
            else:
                rendered.append(str(v))
        more = "..." if len(allowed) > 15 else ""
        lines.append(f"      allowed values: {', '.join(rendered)}{more}")
    return lines


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    api = api3(base)

    async def list_jira_fields(
        query: str = "", field_type: str = "all", count: int = 50
    ) -> str:
        """List Jira fields (system + custom), resolving a name to its field ID.

        Use this to map a human field name (e.g. 'Github url', 'Story Points')
        to the ``customfield_N`` id needed to read it from an issue or set it
        on create/update.
        """
        params: dict[str, Any] = {
            "maxResults": clamp(count, 50),
            "startAt": 0,
            "expand": "key",
        }
        if query:
            params["query"] = query
        if field_type in ("custom", "system"):
            params["type"] = field_type
        resp, err = await jira_request(
            "get", f"{api}/field/search", "list fields", headers=headers, params=params
        )
        if err:
            return err
        data = resp.json()
        fields = data.get("values", [])
        if not fields:
            # /field/search only covers company-managed fields; fall back to the
            # full /field list (which the query filter must then apply locally).
            resp2, err2 = await jira_request(
                "get", f"{api}/field", "list all fields", headers=headers
            )
            if err2:
                return err2
            all_fields = resp2.json() or []
            if query:
                q = query.lower()
                all_fields = [
                    f
                    for f in all_fields
                    if q in (f.get("name", "").lower())
                    or q in (f.get("id", "").lower())
                ]
            if field_type == "custom":
                all_fields = [f for f in all_fields if f.get("custom")]
            elif field_type == "system":
                all_fields = [f for f in all_fields if not f.get("custom")]
            fields = all_fields[: clamp(count, 50)]
            if not fields:
                hint = f" matching '{query}'" if query else ""
                return (
                    f"No fields found{hint}. For team-managed (next-gen) projects, "
                    "custom fields may only appear via get_jira_create_fields "
                    "(by project + issue type) or get_jira_edit_fields (by issue)."
                )
        total = data.get("total", len(fields))
        header = f"Fields ({total} total, showing {len(fields)}):"
        body = [_format_field(f) for f in fields]
        tip = (
            "\nTip: use the 'id' (e.g. customfield_11226) with get_jira_issue / "
            "search_jira_issues 'fields', or in create_jira_issue / "
            "update_jira_issue 'custom_fields'."
        )
        return "\n".join([header, *body, tip])

    async def get_jira_create_fields(project_key: str, issue_type: str = "") -> str:
        """Discover the fields (incl. custom + required + allowed values) for creating an issue.

        Without ``issue_type`` this lists the project's issue types and their
        ids. With an ``issue_type`` id or name it returns every field on the
        create screen - including team-managed project custom fields that
        list_jira_fields cannot see - with required flags and allowed values.
        """
        if not project_key:
            return "project_key is required."
        # Resolve issue types for the project first.
        resp, err = await jira_request(
            "get",
            f"{api}/issue/createmeta/{project_key}/issuetypes",
            f"list create issue types for {project_key}",
            headers=headers,
            params={"maxResults": 100},
        )
        if err:
            return err
        data = resp.json()
        issue_types = data.get("values") or data.get("issueTypes") or []
        if not issue_types:
            return (
                f"No creatable issue types found for project {project_key} "
                "(check the project key and that you can create issues there)."
            )
        if not issue_type:
            lines = [f"Issue types for {project_key} (pass one to see its fields):"]
            for it in issue_types:
                subtask = " (sub-task)" if it.get("subtask") else ""
                lines.append(f"  {it.get('name', '?')} - id: {it.get('id', '?')}{subtask}")
            return "\n".join(lines)
        # Match the requested issue type by id or name.
        match = next(
            (
                it
                for it in issue_types
                if str(it.get("id")) == str(issue_type)
                or (it.get("name", "").lower() == issue_type.lower())
            ),
            None,
        )
        if not match:
            available = ", ".join(it.get("name", "?") for it in issue_types)
            return (
                f"Issue type '{issue_type}' not found in {project_key}. "
                f"Available: {available}"
            )
        type_id = match.get("id")
        resp2, err2 = await jira_request(
            "get",
            f"{api}/issue/createmeta/{project_key}/issuetypes/{type_id}",
            f"get create fields for {project_key}/{match.get('name')}",
            headers=headers,
            params={"maxResults": 200},
        )
        if err2:
            return err2
        fields = resp2.json().get("values") or resp2.json().get("fields") or []
        if not fields:
            return f"No create fields returned for {project_key}/{match.get('name')}."
        header = (
            f"Create fields for {project_key} / {match.get('name')} "
            f"({len(fields)} fields):"
        )
        body: list[str] = []
        for f in fields:
            body.extend(_format_meta_field(f))
        tip = (
            "\nTip: pass any custom field via create_jira_issue 'custom_fields' "
            "as {\"FIELD_ID\": VALUE}. For option/select fields use "
            "{\"value\": \"...\"} or {\"id\": \"...\"}; for user fields use "
            "{\"accountId\": \"...\"}; for multi-value fields use a list."
        )
        return "\n".join([header, *body, tip])

    async def get_jira_edit_fields(issue_key: str) -> str:
        """Discover the fields (incl. custom) editable on a specific issue.

        Returns each editable field with its id, type, allowed values and
        whether it is required - the reliable way to learn the custom field ids
        and value shapes needed by update_jira_issue 'custom_fields'.
        """
        if not issue_key:
            return "issue_key is required."
        resp, err = await jira_request(
            "get",
            f"{api}/issue/{issue_key}/editmeta",
            f"get edit fields for {issue_key}",
            headers=headers,
        )
        if err:
            return err
        fields = (resp.json() or {}).get("fields") or {}
        if not fields:
            return f"No editable fields returned for {issue_key}."
        header = f"Editable fields for {issue_key} ({len(fields)} fields):"
        body: list[str] = []
        for fid, meta in fields.items():
            meta = dict(meta)
            meta.setdefault("fieldId", fid)
            body.extend(_format_meta_field(meta))
        tip = (
            "\nTip: pass custom fields via update_jira_issue 'custom_fields' as "
            "{\"FIELD_ID\": VALUE} using the value shapes shown above."
        )
        return "\n".join([header, *body, tip])

    executor.register("list_jira_fields", list_jira_fields)
    executor.register("get_jira_create_fields", get_jira_create_fields)
    executor.register("get_jira_edit_fields", get_jira_edit_fields)

    return [
        ToolDefinition(
            name="list_jira_fields",
            description=(
                "List Jira fields (system + custom) to resolve a field NAME to its "
                "field ID. This is how you find the customfield_N id for a named "
                "custom field (e.g. 'Github url' -> customfield_11226) so you can "
                "read it from an issue or set it on create/update. Filter by 'query' "
                "(matches the field name) and 'field_type' (custom/system/all). For "
                "team-managed (next-gen) projects whose custom fields are not global, "
                "use get_jira_create_fields or get_jira_edit_fields instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Filter by field name/description (e.g. 'github', 'story points')",
                        "default": "",
                    },
                    "field_type": {
                        "type": "string",
                        "enum": ["all", "custom", "system"],
                        "description": "Restrict to custom or system fields",
                        "default": "all",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max fields to return (max 100)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_jira_create_fields",
            description=(
                "Discover the fields available when CREATING an issue in a project, "
                "including team-managed project custom fields that list_jira_fields "
                "cannot see, with their ids, required flags and allowed values. Call "
                "without issue_type to list the project's issue types + ids, then "
                "again with an issue type to get its create fields. Use the returned "
                "custom field ids + value shapes with create_jira_issue 'custom_fields'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_key": {
                        "type": "string",
                        "description": "Project key (e.g. 'ENG', 'PROJ')",
                    },
                    "issue_type": {
                        "type": "string",
                        "description": "Issue type name or id (omit to list available types)",
                        "default": "",
                    },
                },
                "required": ["project_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_jira_edit_fields",
            description=(
                "Discover the fields (including custom fields) that can be EDITED on a "
                "specific issue, with their ids, types, allowed values and required "
                "flags. Use this to learn the customfield_N id and value shape "
                "before calling update_jira_issue with 'custom_fields'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key (e.g. 'ENG-68')",
                    },
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
