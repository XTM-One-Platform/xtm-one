"""Jira issue comments and worklogs."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _MAX_FULL_COMMENT_CHARS,
    _SOURCE,
    api3,
    body_text,
    clamp,
    comment_body,
    jira_request,
    text_to_adf,
    truncate,
    truncate_body,
)


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    api = api3(base)

    async def list_jira_comments(issue_key: str, count: int = 30) -> str:
        """List comments on an issue (most recent first)."""
        resp, err = await jira_request(
            "get",
            f"{api}/issue/{issue_key}/comment",
            f"list comments for {issue_key}",
            headers=headers,
            params={"maxResults": clamp(count, 30), "orderBy": "-created"},
        )
        if err:
            return err
        data = resp.json()
        comments = data.get("comments", [])
        if not comments:
            return f"No comments on {issue_key}."
        lines = [f"Comments on {issue_key} ({data.get('total', len(comments))}):"]
        for c in comments:
            author = (c.get("author") or {}).get("displayName", "?")
            created = c.get("created", "?")
            text = comment_body(c, issue_key)
            lines.append(f"\n--- {author} ({created}) [id: {c.get('id', '?')}] ---\n{text}")
        return "\n".join(lines)

    async def get_jira_comment(issue_key: str, comment_id: str) -> str:
        """Read one comment in full (list tools cap each comment body)."""
        if not (issue_key and comment_id):
            return "Error: issue_key and comment_id are required."
        resp, err = await jira_request(
            "get",
            f"{api}/issue/{issue_key}/comment/{comment_id}",
            f"get comment {comment_id} on {issue_key}",
            headers=headers,
        )
        if err:
            return err
        c = resp.json()
        author = (c.get("author") or {}).get("displayName", "?")
        created = c.get("created", "?")
        text = truncate_body(body_text(c.get("body")), _MAX_FULL_COMMENT_CHARS)
        return (
            f"Comment {c.get('id', comment_id)} on {issue_key} by {author} "
            f"({created}):\n{text}"
        )

    async def add_jira_comment(
        issue_key: str, body: str, is_internal: bool = False
    ) -> str:
        """Add a comment to an issue. Set is_internal=True for an internal (JSM) note."""
        payload: dict[str, Any] = {"body": text_to_adf(body)}
        if is_internal:
            payload["properties"] = [
                {"key": "sd.public.comment", "value": {"internal": True}}
            ]
        resp, err = await jira_request(
            "post",
            f"{api}/issue/{issue_key}/comment",
            f"add comment to {issue_key}",
            headers=headers,
            json=payload,
        )
        if err:
            return err
        return f"Comment added to {issue_key} (id: {resp.json().get('id', '?')})."

    async def update_jira_comment(issue_key: str, comment_id: str, body: str) -> str:
        """Edit an existing comment on an issue."""
        _, err = await jira_request(
            "put",
            f"{api}/issue/{issue_key}/comment/{comment_id}",
            f"update comment {comment_id} on {issue_key}",
            headers=headers,
            json={"body": text_to_adf(body)},
        )
        if err:
            return err
        return f"Comment {comment_id} on {issue_key} updated."

    async def delete_jira_comment(issue_key: str, comment_id: str) -> str:
        """Delete a comment from an issue. Cannot be undone."""
        _, err = await jira_request(
            "delete",
            f"{api}/issue/{issue_key}/comment/{comment_id}",
            f"delete comment {comment_id} on {issue_key}",
            headers=headers,
        )
        if err:
            return err
        return f"Comment {comment_id} deleted from {issue_key}."

    async def list_jira_worklogs(issue_key: str, count: int = 50) -> str:
        """List worklog entries (logged time) on an issue."""
        resp, err = await jira_request(
            "get",
            f"{api}/issue/{issue_key}/worklog",
            f"list worklogs for {issue_key}",
            headers=headers,
            params={"maxResults": clamp(count, 50)},
        )
        if err:
            return err
        data = resp.json()
        worklogs = data.get("worklogs", [])
        if not worklogs:
            return f"No worklogs on {issue_key}."
        lines = [f"Worklogs on {issue_key} ({data.get('total', len(worklogs))}):"]
        for w in worklogs:
            author = (w.get("author") or {}).get("displayName", "?")
            spent = w.get("timeSpent", "?")
            started = (w.get("started") or "?")[:10]
            lines.append(
                f"  {spent} by {author} on {started} (id: {w.get('id', '?')})"
            )
            note = truncate(body_text(w.get("comment")), 200)
            if note:
                lines.append(f"    {note}")
        return "\n".join(lines)

    async def add_jira_worklog(
        issue_key: str, time_spent: str, comment: str = "", started: str = ""
    ) -> str:
        """Log work on an issue. time_spent like '1h 30m', '2d', '45m'.

        started is ISO 8601 (e.g. '2025-01-15T09:00:00.000+0000'); defaults to now.
        """
        payload: dict[str, Any] = {"timeSpent": time_spent}
        if comment:
            payload["comment"] = text_to_adf(comment)
        if started:
            payload["started"] = started
        resp, err = await jira_request(
            "post",
            f"{api}/issue/{issue_key}/worklog",
            f"add worklog to {issue_key}",
            headers=headers,
            json=payload,
        )
        if err:
            return err
        return f"Worklog added to {issue_key}: {time_spent} (id: {resp.json().get('id', '?')})."

    async def manage_jira_worklog(
        action: str,
        issue_key: str,
        worklog_id: str,
        time_spent: str = "",
        comment: str = "",
        started: str = "",
    ) -> str:
        """Update or delete an existing worklog entry.

        Actions: 'update' (time_spent / comment / started) or 'delete'.
        """
        action = (action or "").strip().lower()
        if not (issue_key and worklog_id):
            return "issue_key and worklog_id are required."
        if action == "update":
            payload: dict[str, Any] = {}
            if time_spent:
                payload["timeSpent"] = time_spent
            if comment:
                payload["comment"] = text_to_adf(comment)
            if started:
                payload["started"] = started
            if not payload:
                return "Provide time_spent, comment, or started to update."
            _, err = await jira_request(
                "put",
                f"{api}/issue/{issue_key}/worklog/{worklog_id}",
                f"update worklog {worklog_id} on {issue_key}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Worklog {worklog_id} on {issue_key} updated."
        if action == "delete":
            _, err = await jira_request(
                "delete",
                f"{api}/issue/{issue_key}/worklog/{worklog_id}",
                f"delete worklog {worklog_id} on {issue_key}",
                headers=headers,
            )
            if err:
                return err
            return f"Worklog {worklog_id} deleted from {issue_key}."
        return f"Unknown action '{action}'. Valid: update, delete."

    executor.register("list_jira_comments", list_jira_comments)
    executor.register("get_jira_comment", get_jira_comment)
    executor.register("add_jira_comment", add_jira_comment)
    executor.register("update_jira_comment", update_jira_comment)
    executor.register("delete_jira_comment", delete_jira_comment)
    executor.register("list_jira_worklogs", list_jira_worklogs)
    executor.register("add_jira_worklog", add_jira_worklog)
    executor.register("manage_jira_worklog", manage_jira_worklog)

    return [
        ToolDefinition(
            name="list_jira_comments",
            description="List comments on an issue (most recent first), with author, date and comment id.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "count": {"type": "integer", "description": "Max comments (max 100)", "default": 30},
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_jira_comment",
            description=(
                "Read ONE issue comment in full. The list tools cap each comment "
                "body and say so with a '[truncated: ...]' marker naming the "
                "comment id - use this tool with that id to read the rest."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "comment_id": {"type": "string", "description": "Comment id (from list_jira_comments / get_jira_issue)"},
                },
                "required": ["issue_key", "comment_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_jira_comment",
            description="Add a comment to an issue. Set is_internal=True for an internal note (Jira Service Management only).",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "body": {
                        "type": "string",
                        "description": "Comment text; [~accountid:ID] mentions a user (ids: search_jira_users)",
                    },
                    "is_internal": {"type": "boolean", "description": "Internal note (JSM only)", "default": False},
                },
                "required": ["issue_key", "body"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_jira_comment",
            description="Edit an existing comment on an issue (replaces its body). Use list_jira_comments for comment ids.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "comment_id": {"type": "string", "description": "Comment id"},
                    "body": {"type": "string", "description": "New comment text; [~accountid:ID] mentions a user"},
                },
                "required": ["issue_key", "comment_id", "body"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_jira_comment",
            description="Delete a comment from an issue permanently. Cannot be undone.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "comment_id": {"type": "string", "description": "Comment id"},
                },
                "required": ["issue_key", "comment_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_jira_worklogs",
            description="List worklog entries (logged time) on an issue, with author, time spent, date and worklog id.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "count": {"type": "integer", "description": "Max worklogs (max 100)", "default": 50},
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_jira_worklog",
            description="Log work on an issue. Time format: '1h 30m', '2d', '45m'. Optionally set a start time (ISO 8601).",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "time_spent": {"type": "string", "description": "Time spent (e.g. '1h 30m', '2d')"},
                    "comment": {"type": "string", "description": "Worklog comment", "default": ""},
                    "started": {"type": "string", "description": "Start time ISO 8601 (default now)", "default": ""},
                },
                "required": ["issue_key", "time_spent"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_jira_worklog",
            description="Update or delete an existing worklog entry. Actions: 'update' (time_spent/comment/started) or 'delete'. Use list_jira_worklogs for ids.",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["update", "delete"]},
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "worklog_id": {"type": "string", "description": "Worklog id"},
                    "time_spent": {"type": "string", "description": "New time spent (update)", "default": ""},
                    "comment": {"type": "string", "description": "New comment (update)", "default": ""},
                    "started": {"type": "string", "description": "New start time ISO 8601 (update)", "default": ""},
                },
                "required": ["action", "issue_key", "worklog_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
