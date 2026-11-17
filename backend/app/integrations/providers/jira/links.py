"""Jira issue links and remote (web) links."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _SOURCE, api3, jira_request, text_to_adf


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    api = api3(base)

    async def link_jira_issues(
        inward_issue_key: str,
        outward_issue_key: str,
        link_type: str = "Relates",
        comment: str = "",
    ) -> str:
        """Link two issues. Common types: Blocks, Clones, Duplicate, Relates, Causes.

        For 'Blocks': inward_issue 'is blocked by', outward_issue 'blocks'. Use
        list_jira_metadata(kind='link_types') for the exact inward/outward names.
        """
        payload: dict[str, Any] = {
            "type": {"name": link_type},
            "inwardIssue": {"key": inward_issue_key},
            "outwardIssue": {"key": outward_issue_key},
        }
        if comment:
            payload["comment"] = {"body": text_to_adf(comment)}
        _, err = await jira_request(
            "post",
            f"{api}/issueLink",
            f"link {inward_issue_key} -> {outward_issue_key}",
            headers=headers,
            json=payload,
        )
        if err:
            return err
        return f"Issues linked: {inward_issue_key} [{link_type}] {outward_issue_key}"

    async def list_jira_issue_links(issue_key: str) -> str:
        """List the issue links on an issue (type, direction, target, link id)."""
        resp, err = await jira_request(
            "get",
            f"{api}/issue/{issue_key}",
            f"list links for {issue_key}",
            headers=headers,
            params={"fields": "issuelinks"},
        )
        if err:
            return err
        links = (resp.json().get("fields") or {}).get("issuelinks", [])
        if not links:
            return f"No links on {issue_key}."
        lines = [f"Links on {issue_key} ({len(links)}):"]
        for link in links:
            if link.get("inwardIssue"):
                target = link["inwardIssue"]
                direction = (link.get("type") or {}).get("inward", "relates to")
            elif link.get("outwardIssue"):
                target = link["outwardIssue"]
                direction = (link.get("type") or {}).get("outward", "relates to")
            else:
                continue
            target_key = target.get("key", "?")
            tfields = target.get("fields") or {}
            target_status = (tfields.get("status") or {}).get("name", "?")
            target_summary = tfields.get("summary", "?")
            lines.append(
                f"  [link id {link.get('id', '?')}] {direction} {target_key} "
                f"[{target_status}] - {target_summary}"
            )
        return "\n".join(lines)

    async def delete_jira_issue_link(link_id: str) -> str:
        """Delete an issue link by its id (from list_jira_issue_links). Cannot be undone."""
        _, err = await jira_request(
            "delete",
            f"{api}/issueLink/{link_id}",
            f"delete issue link {link_id}",
            headers=headers,
        )
        if err:
            return err
        return f"Issue link {link_id} deleted."

    async def manage_jira_remote_links(
        action: str,
        issue_key: str,
        url: str = "",
        title: str = "",
        link_id: str = "",
    ) -> str:
        """Manage an issue's remote (web) links to external resources.

        Actions: 'list' (issue_key), 'create' (issue_key + url + title),
        'delete' (issue_key + link_id).
        """
        action = (action or "").strip().lower()
        if not issue_key:
            return "issue_key is required."
        if action == "list":
            resp, err = await jira_request(
                "get",
                f"{api}/issue/{issue_key}/remotelink",
                f"list remote links for {issue_key}",
                headers=headers,
            )
            if err:
                return err
            links = resp.json() or []
            if not links:
                return f"No remote links on {issue_key}."
            lines = [f"Remote links on {issue_key} ({len(links)}):"]
            for link in links:
                obj = link.get("object") or {}
                lines.append(
                    f"  [id {link.get('id', '?')}] {obj.get('title', '?')} - {obj.get('url', '?')}"
                )
            return "\n".join(lines)
        if action == "create":
            if not (url and title):
                return "url and title are required for 'create'."
            _, err = await jira_request(
                "post",
                f"{api}/issue/{issue_key}/remotelink",
                f"add remote link to {issue_key}",
                headers=headers,
                json={"object": {"url": url, "title": title}},
            )
            if err:
                return err
            return f"Remote link '{title}' added to {issue_key}."
        if action == "delete":
            if not link_id:
                return "link_id is required for 'delete'."
            _, err = await jira_request(
                "delete",
                f"{api}/issue/{issue_key}/remotelink/{link_id}",
                f"delete remote link {link_id} from {issue_key}",
                headers=headers,
            )
            if err:
                return err
            return f"Remote link {link_id} deleted from {issue_key}."
        return f"Unknown action '{action}'. Valid: list, create, delete."

    executor.register("link_jira_issues", link_jira_issues)
    executor.register("list_jira_issue_links", list_jira_issue_links)
    executor.register("delete_jira_issue_link", delete_jira_issue_link)
    executor.register("manage_jira_remote_links", manage_jira_remote_links)

    return [
        ToolDefinition(
            name="link_jira_issues",
            description=(
                "Link two issues. Common types: Blocks, Clones, Duplicate, Relates, "
                "Causes. Example: link_jira_issues('PROJ-1', 'PROJ-2', 'Blocks') means "
                "PROJ-1 blocks PROJ-2. Use list_jira_metadata(kind='link_types') for "
                "the available types."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "inward_issue_key": {"type": "string", "description": "Inward issue key"},
                    "outward_issue_key": {"type": "string", "description": "Outward issue key"},
                    "link_type": {"type": "string", "description": "Link type name", "default": "Relates"},
                    "comment": {"type": "string", "description": "Optional comment on the link", "default": ""},
                },
                "required": ["inward_issue_key", "outward_issue_key"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_jira_issue_links",
            description="List the issue links on an issue - link id, type, direction, target key, status and summary.",
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
            name="delete_jira_issue_link",
            description="Delete an issue link by its id (from list_jira_issue_links). Cannot be undone.",
            input_schema={
                "type": "object",
                "properties": {
                    "link_id": {"type": "string", "description": "Issue link id"},
                },
                "required": ["link_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="manage_jira_remote_links",
            description=(
                "Manage an issue's remote (web) links to external resources (e.g. a "
                "GitHub PR, a Confluence page). Actions: 'list' (issue_key), 'create' "
                "(issue_key + url + title), 'delete' (issue_key + link_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "delete"]},
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'PROJ-123')"},
                    "url": {"type": "string", "description": "Link URL (create)", "default": ""},
                    "title": {"type": "string", "description": "Link title (create)", "default": ""},
                    "link_id": {"type": "string", "description": "Remote link id (delete)", "default": ""},
                },
                "required": ["action", "issue_key"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
