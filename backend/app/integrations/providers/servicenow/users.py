"""ServiceNow users & groups (``sys_user``, ``sys_user_group``, ``sys_user_grmember``).

These tools resolve people and groups to the sys_ids that the incident / change /
problem / task tools need for their reference fields (assigned_to,
assignment_group, caller_id).
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _SOURCE,
    build_encoded_query,
    disp,
    get_record,
    list_table,
)

_USER_TABLE = "sys_user"
_GROUP_TABLE = "sys_user_group"
_MEMBER_TABLE = "sys_user_grmember"


def _format_user(rec: dict[str, Any]) -> str:
    active = disp(rec.get("active"))
    return (
        f"  {disp(rec.get('name')) or '?'} ({disp(rec.get('user_name'))})\n"
        f"    Email: {disp(rec.get('email')) or 'n/a'} | "
        f"Title: {disp(rec.get('title')) or 'n/a'} | "
        f"Dept: {disp(rec.get('department')) or 'n/a'}"
        + (" | INACTIVE" if active == "false" else "")
        + f"\n    sys_id: {disp(rec.get('sys_id'))}"
    )


def _format_group(rec: dict[str, Any]) -> str:
    return (
        f"  {disp(rec.get('name')) or '?'}\n"
        f"    Manager: {disp(rec.get('manager')) or 'n/a'} | "
        f"Email: {disp(rec.get('email')) or 'n/a'}\n"
        f"    {disp(rec.get('description')) or ''}".rstrip()
        + f"\n    sys_id: {disp(rec.get('sys_id'))}"
    )


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def search_servicenow_users(
        text: str = "", active_only: bool = True, limit: int = 20, offset: int = 0
    ) -> str:
        """Search users by name, user_name, or email; returns their sys_ids.

        Use this to resolve a person to the sys_id needed for assigned_to /
        caller_id on incidents, changes, problems, and tasks. ``active_only``
        (default true) hides deactivated accounts.
        """
        clauses = []
        if text and text.strip():
            t = text.strip()
            clauses.append(f"nameLIKE{t}^ORuser_nameLIKE{t}^ORemailLIKE{t}")
        if active_only:
            clauses.append("active=true")
        query = "^".join(clauses)
        return await list_table(
            base,
            headers,
            _USER_TABLE,
            "search users",
            _format_user,
            noun="user",
            query=query,
            limit=limit,
            offset=offset,
            fields="name,user_name,email,title,department,active,sys_id",
        )

    async def get_servicenow_user(user: str) -> str:
        """Get a user by sys_id or user_name (login)."""
        return await get_record(
            base,
            headers,
            _USER_TABLE,
            user,
            f"get user {user}",
            lambda rec: _format_user(rec).lstrip(),
            number_field="user_name",
            fields="name,user_name,email,title,department,phone,active,sys_id",
        )

    async def search_servicenow_groups(
        text: str = "", active_only: bool = True, limit: int = 20, offset: int = 0
    ) -> str:
        """Search assignment groups by name; returns their sys_ids.

        Use this to resolve a group to the sys_id needed for assignment_group on
        incidents, changes, problems, and tasks.
        """
        clauses = []
        if text and text.strip():
            clauses.append(f"nameLIKE{text.strip()}")
        if active_only:
            clauses.append("active=true")
        query = "^".join(clauses)
        return await list_table(
            base,
            headers,
            _GROUP_TABLE,
            "search groups",
            _format_group,
            noun="group",
            query=query,
            limit=limit,
            offset=offset,
            fields="name,description,email,manager,active,sys_id",
        )

    async def list_servicenow_group_members(
        group: str, limit: int = 50, offset: int = 0
    ) -> str:
        """List the members of an assignment group (by group sys_id).

        Resolve the group sys_id first with search_servicenow_groups.
        """
        group = (group or "").strip()
        if not group:
            return "Error: a group sys_id is required (use search_servicenow_groups)."
        query = build_encoded_query({"group": group})

        def _fmt(rec: dict[str, Any]) -> str:
            return f"  {disp(rec.get('user')) or '?'} (member sys_id: {disp(rec.get('sys_id'))})"

        return await list_table(
            base,
            headers,
            _MEMBER_TABLE,
            f"list members of group {group}",
            _fmt,
            noun="member",
            query=query,
            limit=limit,
            offset=offset,
            fields="user,group,sys_id",
        )

    executor.register("search_servicenow_users", search_servicenow_users)
    executor.register("get_servicenow_user", get_servicenow_user)
    executor.register("search_servicenow_groups", search_servicenow_groups)
    executor.register("list_servicenow_group_members", list_servicenow_group_members)

    _limit = {"type": "integer", "default": 20}
    _offset = {"type": "integer", "default": 0}
    return [
        ToolDefinition(
            name="search_servicenow_users",
            description=(
                "Search ServiceNow users by name, user_name, or email and return "
                "their sys_ids - use this to resolve a person for assigned_to / "
                "caller_id on incidents, changes, problems, and tasks."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Name, login, or email"},
                    "active_only": {"type": "boolean", "default": True},
                    "limit": _limit,
                    "offset": _offset,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_servicenow_user",
            description="Get one ServiceNow user by sys_id or user_name (login).",
            input_schema={
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "sys_id or user_name"}
                },
                "required": ["user"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_servicenow_groups",
            description=(
                "Search ServiceNow assignment groups by name and return their "
                "sys_ids - use this to resolve a group for assignment_group."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Group name (contains)"},
                    "active_only": {"type": "boolean", "default": True},
                    "limit": _limit,
                    "offset": _offset,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_servicenow_group_members",
            description=(
                "List the members of an assignment group by its sys_id (resolve the "
                "group first with search_servicenow_groups)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group": {"type": "string", "description": "Group sys_id"},
                    "limit": {"type": "integer", "default": 50},
                    "offset": _offset,
                },
                "required": ["group"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
