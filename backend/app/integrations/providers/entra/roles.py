"""Microsoft Entra ID - directory roles and RBAC (privileged-access governance).

Uses the modern unified RBAC API (``roleManagement/directory``) for role
definitions and assignments, plus the classic ``directoryRoles`` collection
for activated-role membership. This is the surface for auditing who holds
privileged roles (Global Administrator, Privileged Role Administrator, etc.)
and for granting / revoking those roles.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import EntraClient, fmt_list, odata_quote, safe_json, trunc


def _fmt_role_def(r: dict[str, Any]) -> str:
    builtin = "built-in" if r.get("isBuiltIn") else "custom"
    enabled = "enabled" if r.get("isEnabled") else "disabled"
    return (
        f"- {r.get('displayName', 'Unnamed')} ({builtin}, {enabled})\n"
        f"  roleDefinitionId: {r.get('id', '?')} | templateId: "
        f"{r.get('templateId', '-')}\n"
        f"  {trunc(r.get('description'), 160)}"
    )


def _fmt_role(r: dict[str, Any]) -> str:
    return (
        f"- {r.get('displayName', 'Unnamed')} | roleId: {r.get('id', '?')} | "
        f"templateId: {r.get('roleTemplateId', '-')}"
    )


def _fmt_assignment(a: dict[str, Any]) -> str:
    return (
        f"- assignmentId: {a.get('id', '?')}\n"
        f"  principalId: {a.get('principalId', '?')} | roleDefinitionId: "
        f"{a.get('roleDefinitionId', '?')} | scope: "
        f"{a.get('directoryScopeId', '/')}"
    )


def _fmt_principal(p: dict[str, Any]) -> str:
    kind = (p.get("@odata.type") or "").split(".")[-1] or "object"
    label = p.get("displayName") or p.get("userPrincipalName") or "Unknown"
    upn = p.get("userPrincipalName")
    return f"- {label}{f' <{upn}>' if upn else ''} ({kind}) | id: {p.get('id', '?')}"


def register(
    client: EntraClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    src = "integration:entra"

    async def list_entra_directory_roles() -> str:
        data, err = await client.get_json(
            "/directoryRoles", "list activated directory roles"
        )
        if err:
            return err
        return fmt_list(
            (data or {}).get("value", []),
            _fmt_role,
            noun="activated directory role",
            empty_msg="No directory roles are activated in this tenant.",
        )

    async def list_entra_role_definitions(
        odata_filter: str = "", top: int = 50, skiptoken: str = ""
    ) -> str:
        params: dict[str, Any] = {}
        if odata_filter:
            params["$filter"] = odata_filter
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.paged(
            "/roleManagement/directory/roleDefinitions",
            "list role definitions",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items, _fmt_role_def, noun="role definition", skiptoken=next_tok
        )

    async def list_entra_role_assignments(
        principal_id: str = "",
        role_definition_id: str = "",
        top: int = 50,
        skiptoken: str = "",
    ) -> str:
        filters: list[str] = []
        if principal_id:
            filters.append(f"principalId eq '{odata_quote(principal_id)}'")
        if role_definition_id:
            filters.append(
                f"roleDefinitionId eq '{odata_quote(role_definition_id)}'"
            )
        params: dict[str, Any] = {}
        if filters:
            params["$filter"] = " and ".join(filters)
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.paged(
            "/roleManagement/directory/roleAssignments",
            "list role assignments",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_assignment,
            noun="role assignment",
            skiptoken=next_tok,
            empty_msg="No matching role assignments.",
        )

    async def list_entra_role_members(role_id: str) -> str:
        # Accept either a directoryRole id (the primary form from
        # list_entra_directory_roles) or a roleTemplateId. Try the id form
        # first, then transparently fall back to the documented alternate-key
        # form for a roleTemplateId on a benign 400/404. Microsoft Graph only
        # documents the parenthesised key form for GET .../members, i.e.
        # /directoryRoles(roleTemplateId='{id}')/members (the bare
        # /directoryRoles/roleTemplateId={id}/members path is documented for
        # POST .../$ref only).
        resp, err = await client.request(
            "get",
            f"/directoryRoles/{quote(role_id, safe='')}/members",
            "list directory role members",
            expected_statuses={400, 404},
        )
        if resp is not None and resp.status_code in (400, 404):
            resp, err = await client.request(
                "get",
                f"/directoryRoles(roleTemplateId='{odata_quote(role_id)}')/members",
                "list directory role members",
            )
        if err:
            return err
        members = safe_json(resp).get("value", [])
        return fmt_list(
            members,
            _fmt_principal,
            noun="role member",
            empty_msg="No members hold this role (it may not be activated).",
        )

    async def assign_entra_directory_role(
        role_definition_id: str,
        principal_id: str,
        directory_scope_id: str = "/",
    ) -> str:
        _, err = await client.request(
            "post",
            "/roleManagement/directory/roleAssignments",
            "assign directory role",
            json={
                "@odata.type": "#microsoft.graph.unifiedRoleAssignment",
                "roleDefinitionId": role_definition_id,
                "principalId": principal_id,
                "directoryScopeId": directory_scope_id or "/",
            },
        )
        if err:
            return err
        return (
            f"Assigned role {role_definition_id} to principal {principal_id} "
            f"(scope: {directory_scope_id or '/'}). This is a privileged change - "
            f"verify it was intended."
        )

    async def remove_entra_role_assignment(role_assignment_id: str) -> str:
        _, err = await client.request(
            "delete",
            f"/roleManagement/directory/roleAssignments/"
            f"{quote(role_assignment_id, safe='')}",
            "remove role assignment",
        )
        if err:
            return err
        return f"Removed role assignment {role_assignment_id}."

    async def manage_entra_role(
        action: str, role_template_id: str = "", top: int = 50, skiptoken: str = ""
    ) -> str:
        if action == "activate_role_template":
            if not role_template_id:
                return "Error: role_template_id is required to activate a role."
            resp, err = await client.request(
                "post",
                "/directoryRoles",
                "activate directory role",
                json={"roleTemplateId": role_template_id},
            )
            if err:
                return err
            r = safe_json(resp)
            return (
                f"Activated directory role '{r.get('displayName', role_template_id)}' "
                f"(roleId: {r.get('id', '?')}). Members can now be added."
            )
        if action == "list_eligible":
            items, err, next_tok = await client.paged(
                "/roleManagement/directory/roleEligibilitySchedules",
                "list PIM-eligible role assignments",
                top=min(top, 100),
                params={"$skiptoken": skiptoken} if skiptoken else None,
            )
            if err:
                return err
            return fmt_list(
                items,
                _fmt_assignment,
                noun="eligible role assignment",
                skiptoken=next_tok,
                empty_msg="No PIM-eligible role assignments (requires Entra ID P2 / PIM).",
            )
        return (
            "Error: unknown action. Valid actions: activate_role_template, "
            "list_eligible."
        )

    handlers = {
        "list_entra_directory_roles": list_entra_directory_roles,
        "list_entra_role_definitions": list_entra_role_definitions,
        "list_entra_role_assignments": list_entra_role_assignments,
        "list_entra_role_members": list_entra_role_members,
        "assign_entra_directory_role": assign_entra_directory_role,
        "remove_entra_role_assignment": remove_entra_role_assignment,
        "manage_entra_role": manage_entra_role,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    return [
        ToolDefinition(
            name="list_entra_directory_roles",
            description=(
                "List the directory roles currently activated in the tenant "
                "(Global Administrator, Security Administrator, etc.). Use the "
                "returned roleId with list_entra_role_members to see who holds a "
                "role - the core privileged-access audit."
            ),
            input_schema={"type": "object", "properties": {}},
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_role_definitions",
            description=(
                "List all RBAC role definitions (built-in + custom) available in the "
                "directory, with their roleDefinitionId and templateId. Use these ids "
                "with assign_entra_directory_role."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "odata_filter": {
                        "type": "string",
                        "description": "OData $filter, e.g. \"isBuiltIn eq false\" for custom roles.",
                    },
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_role_assignments",
            description=(
                "List unified RBAC role assignments, optionally filtered by "
                "principal_id (who) or role_definition_id (which role). Use to audit "
                "all roles held by a user/service principal, or everyone assigned a "
                "given role."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "principal_id": {
                        "type": "string",
                        "description": "Filter by the assigned principal's object id.",
                    },
                    "role_definition_id": {
                        "type": "string",
                        "description": "Filter by role definition id.",
                    },
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_role_members",
            description=(
                "List the principals (users / service principals) that hold a given "
                "activated directory role. Accepts a directoryRole id or a "
                "roleTemplateId. The fastest way to answer 'who are the Global "
                "Administrators?'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "role_id": {
                        "type": "string",
                        "description": "directoryRole id or roleTemplateId (from list_entra_directory_roles).",
                    },
                },
                "required": ["role_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="assign_entra_directory_role",
            description=(
                "Grant a directory (RBAC) role to a principal. role_definition_id may "
                "be a roleDefinitionId or the role's templateId; principal_id is the "
                "user/group/service-principal object id; directory_scope_id defaults "
                "to '/' (tenant-wide). PRIVILEGED: granting admin roles materially "
                "expands access - confirm intent."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "role_definition_id": {
                        "type": "string",
                        "description": "roleDefinitionId or roleTemplateId to grant.",
                    },
                    "principal_id": {
                        "type": "string",
                        "description": "Object id of the user/group/service principal.",
                    },
                    "directory_scope_id": {
                        "type": "string",
                        "description": "Scope: '/' (tenant), '/administrativeUnits/{id}', or '/{appId}'. Default '/'.",
                        "default": "/",
                    },
                },
                "required": ["role_definition_id", "principal_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="remove_entra_role_assignment",
            description=(
                "Remove a directory role assignment by its assignment id (from "
                "list_entra_role_assignments). PRIVILEGED and permanent - this revokes "
                "the principal's role."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "role_assignment_id": {
                        "type": "string",
                        "description": "The role assignment id to delete.",
                    },
                },
                "required": ["role_assignment_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="manage_entra_role",
            description=(
                "Consolidated role sub-operations. action is one of: "
                "'activate_role_template' (instantiate a built-in role from its "
                "roleTemplateId so members can be added) or 'list_eligible' (PIM "
                "eligible-but-not-active role assignments; requires Entra ID P2)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["activate_role_template", "list_eligible"],
                    },
                    "role_template_id": {
                        "type": "string",
                        "description": "roleTemplateId for activate_role_template.",
                    },
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token for the list_eligible action (from a previous call's 'More results' hint).",
                    },
                },
                "required": ["action"],
            },
            source=src,
            priority=1,
        ),
    ]
