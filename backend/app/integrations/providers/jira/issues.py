"""Jira issues - JQL search, CRUD, transitions, assignment, history, watchers.

All write tools accept a ``custom_fields`` map ({fieldId: value}) so the agent
can set any custom field discovered via the fields module. Search and get
render populated custom fields back to the agent (get_jira_issue resolves their
human labels via ``expand=names``).

Uses the v3 enhanced search endpoint ``/rest/api/3/search/jql`` (cursor
pagination via ``nextPageToken``); the legacy ``/rest/api/3/search`` was removed
by Atlassian (HTTP 410) in 2025 and does not return a ``total`` - use
count_jira_issues for an exact/approximate count.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._common import (
    _SOURCE,
    api3,
    clamp,
    comment_body,
    format_issue,
    jira_request,
    text_to_adf,
)

# Default field set requested on search (keeps responses compact). Callers can
# override via the ``fields`` argument to add custom fields or request *all.
_DEFAULT_SEARCH_FIELDS = [
    "summary",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "reporter",
    "labels",
    "created",
    "updated",
    "components",
    "fixVersions",
]


def _coerce_str(name: str, value: object) -> tuple[str, ToolResult | None]:
    """Return ``(normalized, error)`` - non-string, non-None inputs error out.

    Model-generated tool calls occasionally send a number / list / dict for a
    string argument; surfacing a structured ``ToolResult`` lets the model retry
    instead of crashing on ``.strip()`` deep in the agent loop.
    """
    if value is None:
        return "", None
    if not isinstance(value, str):
        return "", ToolResult(
            tool_call_id="",
            content=(
                f"Invalid '{name}' argument: expected a string, got "
                f"{type(value).__name__}."
            ),
            is_error=True,
        )
    return value.strip(), None


def _coerce_fields(value: object) -> tuple[list[str] | None, ToolResult | None]:
    """Normalise the model-supplied ``fields`` arg to a ``list[str]`` (or None).

    Jira's ``/search/jql`` body expects ``fields`` as an array. Models sometimes
    send a single string or a comma-separated string instead; accept and split
    those leniently. Any other type (dict, int, ...) returns a structured
    ``ToolResult`` so the model can retry rather than Jira rejecting a malformed
    body with a confusing 400 - mirrors :func:`_coerce_str`.
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return (parts or None), None
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return (parts or None), None
    return None, ToolResult(
        tool_call_id="",
        content=(
            "Invalid 'fields' argument: expected a list of field ids/names (or a "
            f"comma-separated string), got {type(value).__name__}."
        ),
        is_error=True,
    )


def _apply_custom_fields(
    fields: dict[str, Any], custom_fields: dict[str, Any] | None
) -> None:
    """Merge a {fieldId: value} map into a create/update ``fields`` payload."""
    if isinstance(custom_fields, dict):
        for fid, value in custom_fields.items():
            if value is not None:
                fields[str(fid)] = value


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    api = api3(base)

    async def search_jira_issues(
        jql: object = None,
        query: object = None,
        count: int = 20,
        next_page_token: str = "",
        fields: object = None,
    ) -> str | ToolResult:
        """Search issues with JQL via /search/jql (cursor pagination).

        ``jql`` is canonical; ``query`` is an accepted alias. Pass ``fields`` to
        request specific fields - including custom field ids (e.g.
        ['summary','status','customfield_11226']) or the special tokens '*all' /
        '*navigable' - so custom field values come back in the results.
        """
        jql_value, jql_err = _coerce_str("jql", jql)
        if jql_err is not None:
            return jql_err
        query_value, query_err = _coerce_str("query", query)
        if query_err is not None:
            return query_err
        if jql_value and query_value and jql_value != query_value:
            return ToolResult(
                tool_call_id="",
                content=(
                    "Conflicting JQL inputs: provide either 'jql' or 'query', "
                    "or use the same value for both."
                ),
                is_error=True,
            )
        effective_jql = jql_value or query_value
        if not effective_jql:
            return ToolResult(
                tool_call_id="",
                content="Missing JQL query. Provide 'jql' (preferred) or 'query'.",
                is_error=True,
            )
        fields_value, fields_err = _coerce_fields(fields)
        if fields_err is not None:
            return fields_err
        requested_fields = fields_value or list(_DEFAULT_SEARCH_FIELDS)
        body: dict[str, Any] = {
            "jql": effective_jql,
            "maxResults": clamp(count, 20, 50),
            "fields": requested_fields,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        resp, err = await jira_request(
            "post",
            f"{api}/search/jql",
            f"search issues with JQL: {effective_jql}",
            headers=headers,
            json=body,
        )
        if err:
            return err
        data = resp.json()
        issues = data.get("issues", [])
        if not issues:
            return f"No issues found for JQL: {effective_jql}"
        lines = [f"Found {len(issues)} issue(s):"]
        for issue in issues:
            lines.append(format_issue(issue))
        token = data.get("nextPageToken")
        if token:
            lines.append(
                f'\n[More results available - pass next_page_token="{token}"]'
            )
        return "\n\n".join(lines)

    async def count_jira_issues(jql: str) -> str:
        """Return the approximate number of issues matching a JQL query.

        The enhanced /search/jql endpoint does not return a total, so use this
        (/search/approximate-count) when you need a count rather than the rows.
        """
        if not jql or not jql.strip():
            return "Missing JQL query."
        resp, err = await jira_request(
            "post",
            f"{api}/search/approximate-count",
            f"count issues for JQL: {jql}",
            headers=headers,
            json={"jql": jql.strip()},
        )
        if err:
            return err
        count = resp.json().get("count")
        return f"Approximately {count} issue(s) match: {jql.strip()}"

    async def get_jira_issue(issue_key: str) -> str:
        """Get full details of an issue: all fields (incl. custom), comments, transitions.

        Custom field values are rendered with their human labels (resolved via
        expand=names).
        """
        resp, err = await jira_request(
            "get",
            f"{api}/issue/{issue_key}",
            f"get issue {issue_key}",
            headers=headers,
            params={"expand": "names,renderedFields,transitions"},
        )
        if err:
            return err
        issue = resp.json()
        result = format_issue(issue, detail=True)

        comments_resp, comments_err = await jira_request(
            "get",
            f"{api}/issue/{issue_key}/comment",
            f"get comments for {issue_key}",
            headers=headers,
            params={"maxResults": 30, "orderBy": "-created"},
        )
        if not comments_err:
            cdata = comments_resp.json()
            comments = cdata.get("comments", [])
            if comments:
                result += f"\n\nComments ({cdata.get('total', len(comments))}):"
                for c in comments:
                    author = (c.get("author") or {}).get("displayName", "?")
                    created = c.get("created", "?")
                    text = comment_body(c, issue_key)
                    result += (
                        f"\n\n--- {author} ({created}) [id: {c.get('id', '?')}] ---\n{text}"
                    )
            else:
                result += "\n\nNo comments."

        transitions = issue.get("transitions", [])
        if transitions:
            names = [f"{t['name']} (id: {t['id']})" for t in transitions]
            result += f"\n\nAvailable transitions: {', '.join(names)}"
        return result

    async def create_jira_issue(
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str = "",
        priority: str = "",
        assignee_account_id: str = "",
        labels: list[str] | None = None,
        components: list[str] | None = None,
        parent_key: str = "",
        due_date: str = "",
        custom_fields: dict[str, Any] | None = None,
    ) -> str:
        """Create a Jira issue.

        ``custom_fields`` is a {fieldId: value} map (discover ids + value shapes
        with get_jira_create_fields / list_jira_fields). issue_type examples:
        Task, Bug, Story, Epic, Sub-task.
        """
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = text_to_adf(description)
        if priority:
            fields["priority"] = {"name": priority}
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
        if labels:
            fields["labels"] = labels
        if components:
            fields["components"] = [{"name": c} for c in components]
        if parent_key:
            fields["parent"] = {"key": parent_key}
        if due_date:
            fields["duedate"] = due_date
        _apply_custom_fields(fields, custom_fields)
        resp, err = await jira_request(
            "post",
            f"{api}/issue",
            f"create issue in {project_key}",
            headers=headers,
            json={"fields": fields},
        )
        if err:
            return err
        issue = resp.json()
        return f"Issue created: {issue.get('key', '?')} (id: {issue.get('id', '?')})"

    async def update_jira_issue(
        issue_key: str,
        summary: str = "",
        description: str = "",
        priority: str = "",
        assignee_account_id: str = "",
        labels: list[str] | None = None,
        components: list[str] | None = None,
        resolution: str = "",
        due_date: str = "",
        custom_fields: dict[str, Any] | None = None,
    ) -> str:
        """Update fields on an issue. Only provided fields change.

        ``custom_fields`` is a {fieldId: value} map (discover ids + value shapes
        with get_jira_edit_fields / list_jira_fields).
        """
        fields: dict[str, Any] = {}
        if summary:
            fields["summary"] = summary
        if description:
            fields["description"] = text_to_adf(description)
        if priority:
            fields["priority"] = {"name": priority}
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
        if labels is not None:
            fields["labels"] = labels
        if components is not None:
            fields["components"] = [{"name": c} for c in components]
        if resolution:
            fields["resolution"] = {"name": resolution}
        if due_date:
            fields["duedate"] = due_date
        _apply_custom_fields(fields, custom_fields)
        if not fields:
            return "No fields to update."
        _, err = await jira_request(
            "put",
            f"{api}/issue/{issue_key}",
            f"update issue {issue_key}",
            headers=headers,
            json={"fields": fields},
        )
        if err:
            return err
        return f"Issue {issue_key} updated successfully."

    async def get_jira_transitions(issue_key: str) -> str:
        """Get available status transitions for an issue (ids + required fields)."""
        resp, err = await jira_request(
            "get",
            f"{api}/issue/{issue_key}/transitions",
            f"get transitions for {issue_key}",
            headers=headers,
            params={"expand": "transitions.fields"},
        )
        if err:
            return err
        transitions = resp.json().get("transitions", [])
        if not transitions:
            return f"No transitions available for {issue_key}."
        lines = [f"Available transitions for {issue_key}:"]
        for t in transitions:
            to_status = (t.get("to") or {}).get("name", "?")
            line = f"  {t['name']} (id: {t['id']}) -> {to_status}"
            required = [
                fmeta.get("name", fkey)
                for fkey, fmeta in (t.get("fields") or {}).items()
                if fmeta.get("required")
            ]
            if required:
                line += f"  [required: {', '.join(required)}]"
            lines.append(line)
        return "\n".join(lines)

    async def transition_jira_issue(
        issue_key: str,
        transition_id: str,
        comment: str = "",
        resolution: str = "",
    ) -> str:
        """Transition an issue to a new status (see get_jira_transitions for ids)."""
        payload: dict[str, Any] = {"transition": {"id": transition_id}}
        if resolution:
            payload["fields"] = {"resolution": {"name": resolution}}
        if comment:
            payload["update"] = {
                "comment": [{"add": {"body": text_to_adf(comment)}}]
            }
        _, err = await jira_request(
            "post",
            f"{api}/issue/{issue_key}/transitions",
            f"transition issue {issue_key}",
            headers=headers,
            json=payload,
        )
        if err:
            return err
        return f"Issue {issue_key} transitioned (transition {transition_id})."

    async def assign_jira_issue(issue_key: str, account_id: str) -> str:
        """Assign an issue to a user by account ID. Use '-1' to unassign."""
        body = {"accountId": None if account_id == "-1" else account_id}
        _, err = await jira_request(
            "put",
            f"{api}/issue/{issue_key}/assignee",
            f"assign issue {issue_key}",
            headers=headers,
            json=body,
        )
        if err:
            return err
        target = "Unassigned" if account_id == "-1" else account_id
        return f"Issue {issue_key} assigned to {target}."

    async def get_jira_issue_changelog(issue_key: str, count: int = 30) -> str:
        """Get the change history (who changed what field, when) for an issue."""
        resp, err = await jira_request(
            "get",
            f"{api}/issue/{issue_key}/changelog",
            f"get changelog for {issue_key}",
            headers=headers,
            params={"maxResults": clamp(count, 30)},
        )
        if err:
            return err
        entries = resp.json().get("values", [])
        if not entries:
            return f"No change history for {issue_key}."
        lines = [f"Change history for {issue_key} ({len(entries)} entries):"]
        for entry in entries:
            author = (entry.get("author") or {}).get("displayName", "?")
            created = entry.get("created", "?")
            lines.append(f"\n--- {author} ({created}) ---")
            for item in entry.get("items", []):
                field = item.get("field", "?")
                frm = item.get("fromString") or "-"
                to = item.get("toString") or "-"
                lines.append(f"  {field}: {frm} -> {to}")
        return "\n".join(lines)

    async def delete_jira_issue(issue_key: str, delete_subtasks: bool = True) -> str:
        """Delete an issue permanently. Optionally delete its sub-tasks too. Cannot be undone."""
        _, err = await jira_request(
            "delete",
            f"{api}/issue/{issue_key}",
            f"delete issue {issue_key}",
            headers=headers,
            params={"deleteSubtasks": "true" if delete_subtasks else "false"},
        )
        if err:
            return err
        return f"Issue {issue_key} deleted."

    async def manage_jira_issue_watchers(
        action: str,
        issue_key: str,
        account_id: str = "",
        notify_text: str = "",
        notify_subject: str = "",
    ) -> str:
        """Manage issue watchers / votes / notifications (long-tail issue ops).

        Actions: 'list_watchers', 'add_watcher', 'remove_watcher' (account_id),
        'add_vote', 'remove_vote', 'notify' (notify_text + optional notify_subject).
        """
        action = (action or "").strip().lower()
        if not issue_key:
            return "issue_key is required."
        if action == "list_watchers":
            resp, err = await jira_request(
                "get",
                f"{api}/issue/{issue_key}/watchers",
                f"list watchers for {issue_key}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            watchers = data.get("watchers", [])
            if not watchers:
                return f"{issue_key} has no watchers."
            names = [
                f"{w.get('displayName', '?')} ({w.get('accountId', '?')})"
                for w in watchers
            ]
            return f"Watchers of {issue_key} ({len(names)}): " + ", ".join(names)
        if action == "add_watcher":
            if not account_id:
                return "account_id is required to add a watcher."
            _, err = await jira_request(
                "post",
                f"{api}/issue/{issue_key}/watchers",
                f"add watcher to {issue_key}",
                headers=headers,
                json=account_id,
            )
            if err:
                return err
            return f"Added watcher {account_id} to {issue_key}."
        if action == "remove_watcher":
            if not account_id:
                return "account_id is required to remove a watcher."
            _, err = await jira_request(
                "delete",
                f"{api}/issue/{issue_key}/watchers",
                f"remove watcher from {issue_key}",
                headers=headers,
                params={"accountId": account_id},
            )
            if err:
                return err
            return f"Removed watcher {account_id} from {issue_key}."
        if action == "add_vote":
            _, err = await jira_request(
                "post",
                f"{api}/issue/{issue_key}/votes",
                f"vote for {issue_key}",
                headers=headers,
            )
            if err:
                return err
            return f"Voted for {issue_key}."
        if action == "remove_vote":
            _, err = await jira_request(
                "delete",
                f"{api}/issue/{issue_key}/votes",
                f"remove vote from {issue_key}",
                headers=headers,
            )
            if err:
                return err
            return f"Removed vote from {issue_key}."
        if action == "notify":
            if not notify_text:
                return "notify_text is required for action 'notify'."
            payload: dict[str, Any] = {
                "textBody": notify_text,
                "htmlBody": notify_text,
                "subject": notify_subject or None,
                "to": {"watchers": True, "voters": True, "assignee": True},
            }
            _, err = await jira_request(
                "post",
                f"{api}/issue/{issue_key}/notify",
                f"notify watchers of {issue_key}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Notification sent for {issue_key}."
        return (
            f"Unknown action '{action}'. Valid: list_watchers, add_watcher, "
            "remove_watcher, add_vote, remove_vote, notify."
        )

    executor.register("search_jira_issues", search_jira_issues)
    executor.register("count_jira_issues", count_jira_issues)
    executor.register("get_jira_issue", get_jira_issue)
    executor.register("create_jira_issue", create_jira_issue)
    executor.register("update_jira_issue", update_jira_issue)
    executor.register("get_jira_transitions", get_jira_transitions)
    executor.register("transition_jira_issue", transition_jira_issue)
    executor.register("assign_jira_issue", assign_jira_issue)
    executor.register("get_jira_issue_changelog", get_jira_issue_changelog)
    executor.register("delete_jira_issue", delete_jira_issue)
    executor.register("manage_jira_issue_watchers", manage_jira_issue_watchers)

    _custom_fields_schema = {
        "type": "object",
        "description": (
            "Map of {customfield_id: value} for custom fields. Discover ids + "
            "value shapes via get_jira_create_fields / get_jira_edit_fields / "
            "list_jira_fields. Text fields take a string; option/select fields "
            "take {\"value\": \"...\"} or {\"id\": \"...\"}; user fields take "
            "{\"accountId\": \"...\"}; multi-value fields take a list."
        ),
        "additionalProperties": True,
    }

    return [
        ToolDefinition(
            name="search_jira_issues",
            description=(
                "Search Jira issues using JQL (Jira Query Language) - the most powerful "
                "way to find issues. Examples: 'project = PROJ AND status = \"In "
                "Progress\"', 'assignee = currentUser() AND resolution = Unresolved "
                "ORDER BY priority DESC', 'sprint in openSprints()'. Pass 'fields' to "
                "include custom field values in the results, e.g. "
                "['summary','status','customfield_11226'] (resolve a field name to its "
                "id with list_jira_fields), or ['*all'] for everything. Cursor "
                "pagination via next_page_token. For a count, use count_jira_issues."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "jql": {"type": "string", "description": "JQL query string (jql or query)"},
                    "query": {"type": "string", "description": "Alias for jql"},
                    "count": {
                        "type": "integer",
                        "description": "Max results per page (max 50)",
                        "default": 20,
                    },
                    "next_page_token": {
                        "type": "string",
                        "description": "Pagination token from a previous response",
                        "default": "",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Fields to return (include custom field ids like "
                            "customfield_11226, or '*all' / '*navigable')"
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="count_jira_issues",
            description=(
                "Return the approximate number of issues matching a JQL query "
                "(the enhanced search endpoint does not return a total). Use for "
                "'how many issues...' questions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "jql": {"type": "string", "description": "JQL query string"},
                },
                "required": ["jql"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_jira_issue",
            description=(
                "Get full details of an issue including description, comments, "
                "available transitions, and ALL custom field values (rendered with "
                "their human labels). Use search_jira_issues to find issue keys first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_jira_issue",
            description=(
                "Create a new Jira issue. Supports project, summary, type, description, "
                "priority, assignee, labels, components, parent (sub-tasks), due_date, "
                "and arbitrary custom fields via 'custom_fields'. Use "
                "get_jira_create_fields to discover required fields, custom field ids "
                "and allowed values for the project + issue type."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string", "description": "Project key (e.g. 'PROJ')"},
                    "summary": {"type": "string", "description": "Issue title/summary"},
                    "issue_type": {
                        "type": "string",
                        "description": "Issue type (Task, Bug, Story, Epic, Sub-task)",
                        "default": "Task",
                    },
                    "description": {"type": "string", "description": "Description (plain text)", "default": ""},
                    "priority": {"type": "string", "description": "Priority name", "default": ""},
                    "assignee_account_id": {"type": "string", "description": "Assignee account ID", "default": ""},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels"},
                    "components": {"type": "array", "items": {"type": "string"}, "description": "Component names"},
                    "parent_key": {"type": "string", "description": "Parent issue key for sub-tasks", "default": ""},
                    "due_date": {"type": "string", "description": "Due date (YYYY-MM-DD)", "default": ""},
                    "custom_fields": _custom_fields_schema,
                },
                "required": ["project_key", "summary"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_jira_issue",
            description=(
                "Update fields on an existing issue (only provided fields change). "
                "Supports summary, description, priority, assignee, labels, components, "
                "resolution, due_date, and arbitrary custom fields via 'custom_fields'. "
                "Use get_jira_edit_fields to discover editable custom field ids + value "
                "shapes. Note: resolution may need to be set via transition instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "summary": {"type": "string", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "priority": {"type": "string", "default": ""},
                    "assignee_account_id": {"type": "string", "default": ""},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Replace all labels"},
                    "components": {"type": "array", "items": {"type": "string"}, "description": "Replace all components"},
                    "resolution": {"type": "string", "default": ""},
                    "due_date": {"type": "string", "description": "Due date (YYYY-MM-DD)", "default": ""},
                    "custom_fields": _custom_fields_schema,
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_jira_transitions",
            description=(
                "Get available status transitions for an issue (ids, target status, "
                "required fields). Use before transition_jira_issue."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="transition_jira_issue",
            description=(
                "Transition an issue to a new status (e.g. To Do -> In Progress -> "
                "Done). Use get_jira_transitions for valid ids and required fields; "
                "pass a resolution when the transition requires one. Optionally add a "
                "comment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "transition_id": {"type": "string", "description": "Transition ID from get_jira_transitions"},
                    "resolution": {"type": "string", "description": "Resolution to set during transition", "default": ""},
                    "comment": {"type": "string", "description": "Optional comment", "default": ""},
                },
                "required": ["issue_key", "transition_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="assign_jira_issue",
            description=(
                "Assign an issue to a user by account ID (use get_jira_myself for your "
                "own, find_jira_assignable_users to find valid assignees). Set "
                "account_id to '-1' to unassign."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "account_id": {"type": "string", "description": "Account ID (or '-1' to unassign)"},
                },
                "required": ["issue_key", "account_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_jira_issue_changelog",
            description=(
                "Get the change history of an issue - who changed which field from "
                "what to what, and when. Useful for auditing status/assignee/field "
                "changes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "count": {"type": "integer", "description": "Max history entries (max 100)", "default": 30},
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="delete_jira_issue",
            description="Delete an issue permanently (optionally its sub-tasks too). This cannot be undone.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "delete_subtasks": {"type": "boolean", "description": "Also delete sub-tasks", "default": True},
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="manage_jira_issue_watchers",
            description=(
                "Manage an issue's watchers, votes and notifications. Actions: "
                "'list_watchers', 'add_watcher'/'remove_watcher' (account_id), "
                "'add_vote'/'remove_vote', 'notify' (send an email to watchers/voters/"
                "assignee with notify_text)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_watchers",
                            "add_watcher",
                            "remove_watcher",
                            "add_vote",
                            "remove_vote",
                            "notify",
                        ],
                    },
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "account_id": {"type": "string", "description": "Account ID (add/remove watcher)", "default": ""},
                    "notify_text": {"type": "string", "description": "Notification body (notify)", "default": ""},
                    "notify_subject": {"type": "string", "description": "Notification subject (notify)", "default": ""},
                },
                "required": ["action", "issue_key"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
