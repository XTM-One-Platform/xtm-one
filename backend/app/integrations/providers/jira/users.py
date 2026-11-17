"""Jira users and groups."""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _SOURCE, api3, clamp, jira_request


def _format_users(users: list[dict[str, Any]], header: str) -> str:
    lines = [header]
    for u in users:
        active = "active" if u.get("active") else "inactive"
        email = u.get("emailAddress", "")
        email_str = f" <{email}>" if email else ""
        lines.append(
            f"  {u.get('displayName', '?')}{email_str} [{active}] "
            f"accountId: {u.get('accountId', '?')}"
        )
    return "\n".join(lines)


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    api = api3(base)

    async def get_jira_myself() -> str:
        """Get the authenticated user's Jira profile (account ID, name, email, timezone)."""
        resp, err = await jira_request(
            "get", f"{api}/myself", "get current user profile", headers=headers
        )
        if err:
            return err
        me = resp.json()
        lines = [
            f"Authenticated as: {me.get('displayName', '?')}",
            f"  Account ID: {me.get('accountId', '?')}",
            f"  Email: {me.get('emailAddress', 'N/A')}",
        ]
        if me.get("timeZone"):
            lines.append(f"  Timezone: {me['timeZone']}")
        lines.append(
            "\nUse the Account ID for JQL (e.g. assignee = ACCOUNT_ID) or for "
            "assign_jira_issue / create_jira_issue."
        )
        return "\n".join(lines)

    async def search_jira_users(query: str, count: int = 20) -> str:
        """Search users by name or email. Returns account IDs for use in assignments/JQL."""
        resp, err = await jira_request(
            "get",
            f"{api}/user/search",
            f"search users '{query}'",
            headers=headers,
            params={"query": query, "maxResults": clamp(count, 20, 50)},
        )
        if err:
            return err
        users = resp.json() or []
        if not users:
            return f"No users found matching '{query}'."
        return _format_users(users, f"Users matching '{query}' ({len(users)}):")

    async def find_jira_assignable_users(
        project_key: str = "", issue_key: str = "", query: str = "", count: int = 20
    ) -> str:
        """Find users assignable to a project or a specific issue (the valid assignees).

        Provide project_key or issue_key (issue_key is more precise). ``query``
        optionally filters by name/email.
        """
        if not project_key and not issue_key:
            return "Provide project_key or issue_key."
        params: dict[str, Any] = {"maxResults": clamp(count, 20, 50)}
        if query:
            params["query"] = query
        if issue_key:
            params["issueKey"] = issue_key
        if project_key:
            params["project"] = project_key
        resp, err = await jira_request(
            "get",
            f"{api}/user/assignable/search",
            "find assignable users",
            headers=headers,
            params=params,
        )
        if err:
            return err
        users = resp.json() or []
        target = issue_key or project_key
        if not users:
            return f"No assignable users found for {target}."
        return _format_users(users, f"Assignable users for {target} ({len(users)}):")

    async def manage_jira_group(
        action: str,
        group_id: str = "",
        query: str = "",
        account_id: str = "",
        count: int = 50,
    ) -> str:
        """Find groups, list members, or add/remove a user (requires admin scope).

        Actions: 'list' (query to search group names), 'members' (group_id),
        'add_user' (group_id + account_id), 'remove_user' (group_id + account_id).
        """
        action = (action or "").strip().lower()
        if action == "list":
            resp, err = await jira_request(
                "get",
                f"{api}/groups/picker",
                "list groups",
                headers=headers,
                params={"query": query, "maxResults": clamp(count, 50)},
            )
            if err:
                return err
            groups = resp.json().get("groups", [])
            if not groups:
                return "No groups found."
            lines = [f"Groups ({len(groups)}):"]
            for g in groups:
                lines.append(f"  {g.get('name', '?')} (groupId: {g.get('groupId', '?')})")
            return "\n".join(lines)
        if action == "members":
            if not group_id:
                return "group_id is required for 'members'."
            resp, err = await jira_request(
                "get",
                f"{api}/group/member",
                f"list members of group {group_id}",
                headers=headers,
                params={"groupId": group_id, "maxResults": clamp(count, 50)},
            )
            if err:
                return err
            members = resp.json().get("values", [])
            if not members:
                return f"Group {group_id} has no members."
            return _format_users(members, f"Members of group {group_id} ({len(members)}):")
        if action == "add_user":
            if not (group_id and account_id):
                return "group_id and account_id are required for 'add_user'."
            _, err = await jira_request(
                "post",
                f"{api}/group/user",
                f"add user to group {group_id}",
                headers=headers,
                params={"groupId": group_id},
                json={"accountId": account_id},
            )
            if err:
                return err
            return f"Added user {account_id} to group {group_id}."
        if action == "remove_user":
            if not (group_id and account_id):
                return "group_id and account_id are required for 'remove_user'."
            _, err = await jira_request(
                "delete",
                f"{api}/group/user",
                f"remove user from group {group_id}",
                headers=headers,
                params={"groupId": group_id, "accountId": account_id},
            )
            if err:
                return err
            return f"Removed user {account_id} from group {group_id}."
        return f"Unknown action '{action}'. Valid: list, members, add_user, remove_user."

    executor.register("get_jira_myself", get_jira_myself)
    executor.register("search_jira_users", search_jira_users)
    executor.register("find_jira_assignable_users", find_jira_assignable_users)
    executor.register("manage_jira_group", manage_jira_group)

    return [
        ToolDefinition(
            name="get_jira_myself",
            description=(
                "Get the authenticated Jira user's profile - account ID, display name, "
                "email and timezone. Call this to discover your own account ID for JQL "
                "(assignee = ACCOUNT_ID) or for create/assign operations."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_jira_users",
            description="Search for users by name or email. Returns display name, email, active status and account ID (needed for assignments).",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name or email to search"},
                    "count": {"type": "integer", "description": "Max results (max 50)", "default": 20},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="find_jira_assignable_users",
            description=(
                "Find the users that can be assigned to a project or a specific issue "
                "(the valid assignees - narrower than search_jira_users). Provide "
                "project_key or issue_key (issue_key is most precise)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string", "description": "Project key", "default": ""},
                    "issue_key": {"type": "string", "description": "Issue key (most precise)", "default": ""},
                    "query": {"type": "string", "description": "Filter by name/email", "default": ""},
                    "count": {"type": "integer", "description": "Max results (max 50)", "default": 20},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_jira_group",
            description=(
                "Find groups, list members, or add/remove a user from a group (group "
                "membership changes require admin scope). Actions: 'list' (query), "
                "'members' (group_id), 'add_user'/'remove_user' (group_id + account_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "members", "add_user", "remove_user"]},
                    "group_id": {"type": "string", "description": "Group id (members/add/remove)", "default": ""},
                    "query": {"type": "string", "description": "Group name search (list)", "default": ""},
                    "account_id": {"type": "string", "description": "User account id (add/remove)", "default": ""},
                    "count": {"type": "integer", "description": "Max results (max 100)", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
