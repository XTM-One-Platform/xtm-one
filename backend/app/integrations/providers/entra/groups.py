"""Microsoft Entra ID - groups, membership, and ownership."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    GRAPH_BASE,
    EntraClient,
    build_search_params,
    fmt_list,
    safe_json,
)

_GROUP_SELECT = (
    "id,displayName,description,mail,mailNickname,securityEnabled,mailEnabled,"
    "groupTypes,visibility,membershipRule,membershipRuleProcessingState"
)


def _group_kind(g: dict[str, Any]) -> str:
    types = g.get("groupTypes", []) or []
    if "Unified" in types:
        return "Microsoft 365"
    if g.get("securityEnabled") and g.get("mailEnabled"):
        return "mail-enabled security"
    if g.get("securityEnabled"):
        return "security"
    if g.get("mailEnabled"):
        return "distribution"
    return "group"


def _fmt_group(g: dict[str, Any]) -> str:
    dynamic = (
        "dynamic"
        if "DynamicMembership" in (g.get("groupTypes") or [])
        else "assigned"
    )
    return (
        f"- {g.get('displayName', 'Unnamed')} ({_group_kind(g)}, {dynamic})\n"
        f"  id: {g.get('id', '?')} | mail: {g.get('mail') or '-'}"
    )


def _fmt_member(m: dict[str, Any]) -> str:
    kind = (m.get("@odata.type") or "").split(".")[-1] or "object"
    label = m.get("displayName") or m.get("userPrincipalName") or "Unknown"
    upn = m.get("userPrincipalName")
    return f"- {label}{f' <{upn}>' if upn else ''} ({kind}) | id: {m.get('id', '?')}"


def register(
    client: EntraClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    src = "integration:entra"

    async def list_entra_groups(
        search: str = "",
        odata_filter: str = "",
        top: int = 25,
        skiptoken: str = "",
    ) -> str:
        params, extra = build_search_params(
            search=search,
            odata_filter=odata_filter,
            select=_GROUP_SELECT,
            skiptoken=skiptoken,
        )
        items, err, next_tok = await client.paged(
            "/groups",
            "list groups",
            top=min(top, 100),
            params=params,
            extra_headers=extra,
        )
        if err:
            return err
        return fmt_list(items, _fmt_group, noun="group", skiptoken=next_tok)

    async def get_entra_group(group_id: str) -> str:
        data, err = await client.get_json(
            f"/groups/{quote(group_id, safe='')}",
            "get group",
            params={"$select": _GROUP_SELECT},
        )
        if err:
            return err
        g = data or {}
        lines = [_fmt_group(g)]
        if g.get("description"):
            lines.append(f"  description: {g['description']}")
        if g.get("visibility"):
            lines.append(f"  visibility: {g['visibility']}")
        if g.get("membershipRule"):
            lines.append(
                f"  dynamic rule ({g.get('membershipRuleProcessingState', '?')}): "
                f"{g['membershipRule']}"
            )
        return "\n".join(lines)

    async def create_entra_group(
        display_name: str,
        mail_nickname: str,
        description: str = "",
        security_enabled: bool = True,
        mail_enabled: bool = False,
        unified: bool = False,
        visibility: str = "",
        membership_rule: str = "",
    ) -> str:
        group_types: list[str] = []
        if unified:
            group_types.append("Unified")
        if membership_rule:
            group_types.append("DynamicMembership")
        payload: dict[str, Any] = {
            "displayName": display_name,
            "mailNickname": mail_nickname,
            "securityEnabled": security_enabled,
            "mailEnabled": mail_enabled or unified,
            "groupTypes": group_types,
        }
        if description:
            payload["description"] = description
        if visibility:
            payload["visibility"] = visibility
        if membership_rule:
            payload["membershipRule"] = membership_rule
            payload["membershipRuleProcessingState"] = "On"
        resp, err = await client.request(
            "post", "/groups", "create group", json=payload
        )
        if err:
            return err
        g = safe_json(resp)
        return (
            f"Created {_group_kind(g)} group '{g.get('displayName', display_name)}' "
            f"(id: {g.get('id', '?')})."
        )

    async def update_entra_group(
        group_id: str,
        display_name: str = "",
        description: str = "",
        visibility: str = "",
        membership_rule: str = "",
        properties: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = dict(properties or {})
        for key, val in (
            ("displayName", display_name),
            ("description", description),
            ("visibility", visibility),
            ("membershipRule", membership_rule),
        ):
            if val:
                payload[key] = val
        if not payload:
            return "Error: No fields provided to update."
        _, err = await client.request(
            "patch",
            f"/groups/{quote(group_id, safe='')}",
            "update group",
            json=payload,
        )
        if err:
            return err
        return f"Updated group {group_id} - {', '.join(payload.keys())}."

    async def delete_entra_group(group_id: str) -> str:
        _, err = await client.request(
            "delete", f"/groups/{quote(group_id, safe='')}", "delete group"
        )
        if err:
            return err
        return (
            f"Deleted group {group_id}. Microsoft 365 groups are recoverable from "
            f"the recycle bin for 30 days; security groups are deleted permanently."
        )

    async def list_entra_group_members(
        group_id: str, transitive: bool = False, top: int = 50, skiptoken: str = ""
    ) -> str:
        rel = "transitiveMembers" if transitive else "members"
        items, err, next_tok = await client.paged(
            f"/groups/{quote(group_id, safe='')}/{rel}",
            "list group members",
            top=min(top, 100),
            params={"$skiptoken": skiptoken} if skiptoken else None,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_member,
            noun="member",
            skiptoken=next_tok,
            empty_msg="Group has no members.",
        )

    async def add_entra_group_member(group_id: str, member_id: str) -> str:
        _, err = await client.request(
            "post",
            f"/groups/{quote(group_id, safe='')}/members/$ref",
            "add group member",
            json={
                "@odata.id": f"{GRAPH_BASE}/directoryObjects/{quote(member_id, safe='')}"
            },
        )
        if err:
            return err
        return f"Added {member_id} to group {group_id}."

    async def remove_entra_group_member(group_id: str, member_id: str) -> str:
        _, err = await client.request(
            "delete",
            f"/groups/{quote(group_id, safe='')}/members/"
            f"{quote(member_id, safe='')}/$ref",
            "remove group member",
        )
        if err:
            return err
        return f"Removed {member_id} from group {group_id}."

    async def manage_entra_group(
        action: str,
        group_id: str = "",
        principal_id: str = "",
        top: int = 50,
        skiptoken: str = "",
    ) -> str:
        page = min(top, 100)
        page_params = {"$skiptoken": skiptoken} if skiptoken else None
        if action == "list_owners":
            items, err, next_tok = await client.paged(
                f"/groups/{quote(group_id, safe='')}/owners",
                "list group owners",
                top=page,
                params=page_params,
            )
            if err:
                return err
            return fmt_list(
                items,
                _fmt_member,
                noun="owner",
                skiptoken=next_tok,
                empty_msg="Group has no owners.",
            )
        if action == "add_owner":
            if not principal_id.strip():
                return "Error: principal_id (a user object id) is required for add_owner."
            _, err = await client.request(
                "post",
                f"/groups/{quote(group_id, safe='')}/owners/$ref",
                "add group owner",
                json={
                    "@odata.id": f"{GRAPH_BASE}/users/{quote(principal_id, safe='')}"
                },
            )
            return err or f"Added owner {principal_id} to group {group_id}."
        if action == "remove_owner":
            if not principal_id.strip():
                return "Error: principal_id (a user object id) is required for remove_owner."
            _, err = await client.request(
                "delete",
                f"/groups/{quote(group_id, safe='')}/owners/"
                f"{quote(principal_id, safe='')}/$ref",
                "remove group owner",
            )
            return err or f"Removed owner {principal_id} from group {group_id}."
        if action == "list_app_role_assignments":
            items, err, next_tok = await client.paged(
                f"/groups/{quote(group_id, safe='')}/appRoleAssignments",
                "list group app role assignments",
                top=page,
                params=page_params,
            )
            if err:
                return err

            def _fmt_ara(a: dict[str, Any]) -> str:
                return (
                    f"- {a.get('resourceDisplayName', '?')} | id: {a.get('id', '?')}"
                )

            return fmt_list(
                items, _fmt_ara, noun="app role assignment", skiptoken=next_tok
            )
        return (
            "Error: unknown action. Valid actions: list_owners, add_owner, "
            "remove_owner, list_app_role_assignments."
        )

    handlers = {
        "list_entra_groups": list_entra_groups,
        "get_entra_group": get_entra_group,
        "create_entra_group": create_entra_group,
        "update_entra_group": update_entra_group,
        "delete_entra_group": delete_entra_group,
        "list_entra_group_members": list_entra_group_members,
        "add_entra_group_member": add_entra_group_member,
        "remove_entra_group_member": remove_entra_group_member,
        "manage_entra_group": manage_entra_group,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    group_ref = {"type": "string", "description": "Group object id (GUID)."}
    return [
        ToolDefinition(
            name="list_entra_groups",
            description=(
                "List or search Entra ID groups (security, Microsoft 365, "
                "distribution, dynamic). Use 'search' for free-text or 'odata_filter' "
                "for precise queries. Returns each group's id, type, dynamic/assigned "
                "membership, and mail."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Free-text search on displayName, mail, description."},
                    "odata_filter": {
                        "type": "string",
                        "description": "OData $filter, e.g. \"securityEnabled eq true\".",
                    },
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_entra_group",
            description="Get a single Entra ID group's details, including dynamic membership rule if any.",
            input_schema={
                "type": "object",
                "properties": {"group_id": group_ref},
                "required": ["group_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="create_entra_group",
            description=(
                "Create an Entra ID group. Defaults to an assigned-membership "
                "security group. Set unified=true for a Microsoft 365 group, or pass "
                "membership_rule to create a dynamic group (e.g. "
                "\"(user.department eq \\\"Sales\\\")\")."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "mail_nickname": {"type": "string", "description": "Mail alias (no spaces)."},
                    "description": {"type": "string"},
                    "security_enabled": {"type": "boolean", "default": True},
                    "mail_enabled": {"type": "boolean", "default": False},
                    "unified": {
                        "type": "boolean",
                        "description": "Create a Microsoft 365 group (default false).",
                        "default": False,
                    },
                    "visibility": {
                        "type": "string",
                        "description": "M365 group visibility: Private, Public, or HiddenMembership.",
                    },
                    "membership_rule": {
                        "type": "string",
                        "description": "Dynamic membership rule; sets the group to dynamic when provided.",
                    },
                },
                "required": ["display_name", "mail_nickname"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_entra_group",
            description="Update a group's attributes (display name, description, visibility, dynamic rule, or arbitrary properties).",
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": group_ref,
                    "display_name": {"type": "string"},
                    "description": {"type": "string"},
                    "visibility": {"type": "string"},
                    "membership_rule": {"type": "string"},
                    "properties": {"type": "object", "description": "Arbitrary additional Graph group properties."},
                },
                "required": ["group_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="delete_entra_group",
            description="Delete an Entra ID group. Microsoft 365 groups go to the 30-day recycle bin; security groups are removed permanently.",
            input_schema={
                "type": "object",
                "properties": {"group_id": group_ref},
                "required": ["group_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_entra_group_members",
            description="List a group's members. Set transitive=true to include members of nested groups.",
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": group_ref,
                    "transitive": {"type": "boolean", "default": False},
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token from a previous call's 'More results' hint.",
                    },
                },
                "required": ["group_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="add_entra_group_member",
            description=(
                "Add a member to a group. member_id is the object id of a user, "
                "group, device, or service principal. Note: dynamic groups manage "
                "membership automatically and reject manual adds."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": group_ref,
                    "member_id": {
                        "type": "string",
                        "description": "Object id (GUID) of the directory object to add.",
                    },
                },
                "required": ["group_id", "member_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="remove_entra_group_member",
            description="Remove a member from a group by the member's object id.",
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": group_ref,
                    "member_id": {"type": "string", "description": "Object id (GUID) of the member to remove."},
                },
                "required": ["group_id", "member_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="manage_entra_group",
            description=(
                "Consolidated group sub-operations. action is one of: 'list_owners', "
                "'add_owner' (needs principal_id), 'remove_owner' (needs "
                "principal_id), 'list_app_role_assignments' (enterprise-app access "
                "granted to the group)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_owners",
                            "add_owner",
                            "remove_owner",
                            "list_app_role_assignments",
                        ],
                    },
                    "group_id": group_ref,
                    "principal_id": {
                        "type": "string",
                        "description": "User object id for add_owner / remove_owner.",
                    },
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token for the list_* actions (from a previous call's 'More results' hint).",
                    },
                },
                "required": ["action", "group_id"],
            },
            source=src,
            priority=1,
        ),
    ]
