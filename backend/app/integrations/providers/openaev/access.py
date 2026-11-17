# SPDX-License-Identifier: Apache-2.0

"""OpenAEV access management tools - organizations, users, groups, roles, profile.

* ``manage_openaev_organization`` - organization CRUD.
* ``manage_openaev_user``         - user CRUD (scope tenant|platform).
* ``manage_openaev_group``        - group CRUD + users/roles/grants
  (scope tenant|platform; grants are tenant-only).
* ``manage_openaev_role``         - role CRUD + capabilities (scope tenant|platform).
* ``manage_openaev_profile``      - the authenticated user (me) + API tokens.

Platform-scoped endpoints require an Enterprise Edition instance.
"""

from __future__ import annotations

import json
import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _search_body

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    # ── Organizations ──────────────────────────────────────────────────
    async def manage_openaev_organization(
        action: str = "",
        organization_id: str = "",
        name: str = "",
        description: str = "",
        tags: list[str] | None = None,
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/organizations/search",
                "search organizations",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No organizations found."
            lines = [f"Organizations ({len(items)}):"]
            for o in items:
                lines.append(
                    f"- {o.get('organization_name', '?')} (id: {o.get('organization_id')})"
                )
            return "\n".join(lines)

        if action in ("create", "update"):
            if action == "create" and not name:
                return "name is required for action 'create'."
            if action == "update" and not organization_id:
                return "organization_id is required for action 'update'."
            body: dict = {}
            if name:
                body["organization_name"] = name
            if description:
                body["organization_description"] = description
            if tags is not None:
                body["organization_tags"] = tags
            if action == "update" and not body:
                return "Provide at least one field to update (name, description, or tags)."
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", "/api/organizations",
                    "create organization", json=body,
                )
            else:
                resp, err = await _api(
                    base_url, headers, "put", f"/api/organizations/{organization_id}",
                    "update organization", json=body,
                )
            if err:
                return err
            o = resp.json()
            verb = "Created" if action == "create" else "Updated"
            return f"{verb} organization '{o.get('organization_name')}' (id: {o.get('organization_id')})."

        if action == "delete":
            if not organization_id:
                return "organization_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/organizations/{organization_id}",
                "delete organization",
            )
            if err:
                return err
            return f"Deleted organization {organization_id}."

        return f"Unknown action '{action}'. Valid actions: list, create, update, delete."

    # ── Users ──────────────────────────────────────────────────────────
    async def manage_openaev_user(
        action: str = "",
        scope: str = "tenant",
        user_id: str = "",
        email: str = "",
        firstname: str = "",
        lastname: str = "",
        password: str = "",
        organization_id: str = "",
        tags: list[str] | None = None,
        admin: bool | None = None,
        tenant_ids: list[str] | None = None,
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()
        scope = (scope or "tenant").strip().lower()
        base = "/api/platform-users" if scope == "platform" else "/api/users"

        def _user_body() -> dict:
            body: dict = {}
            if admin is not None:
                body["user_admin"] = admin
            if email:
                body["user_email"] = email
            if firstname:
                body["user_firstname"] = firstname
            if lastname:
                body["user_lastname"] = lastname
            if password:
                body["user_plain_password"] = password
            if organization_id:
                body["user_organization"] = organization_id
            if tags is not None:
                body["user_tags"] = tags
            if tenant_ids is not None:
                body["user_tenants"] = tenant_ids
            return body

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", f"{base}/search", "search users",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No users found."
            lines = [f"Users ({len(items)}, scope={scope}):"]
            for u in items:
                lines.append(
                    f"- {u.get('user_email', '?')} (id: {u.get('user_id')}) "
                    f"admin={u.get('user_admin')}"
                )
            return "\n".join(lines)

        if action == "get":
            if not user_id:
                return "user_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"{base}/{user_id}", "get user"
            )
            if err:
                return err
            return json.dumps(resp.json(), default=str)[:2000]

        if action == "create":
            if not email:
                return "email is required for action 'create'."
            resp, err = await _api(
                base_url, headers, "post", base, "create user", json=_user_body()
            )
            if err:
                return err
            u = resp.json()
            return f"Created user '{u.get('user_email')}' (id: {u.get('user_id')}, scope={scope})."

        if action == "update":
            if not user_id:
                return "user_id is required for action 'update'."
            body = _user_body()
            if not body:
                return "Provide at least one field to update."
            resp, err = await _api(
                base_url, headers, "put", f"{base}/{user_id}", "update user",
                json=body,
            )
            if err:
                return err
            return f"Updated user {user_id} (scope={scope})."

        if action == "delete":
            if not user_id:
                return "user_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"{base}/{user_id}", "delete user"
            )
            if err:
                return err
            return f"Deleted user {user_id} (scope={scope})."

        return f"Unknown action '{action}'. Valid actions: list, get, create, update, delete."

    # ── Groups ─────────────────────────────────────────────────────────
    async def manage_openaev_group(
        action: str = "",
        scope: str = "tenant",
        group_id: str = "",
        name: str = "",
        description: str = "",
        default_user_assign: bool | None = None,
        user_ids: list[str] | None = None,
        role_ids: list[str] | None = None,
        grant_name: str = "",
        grant_resource_id: str = "",
        grant_resource_type: str = "",
        grant_id: str = "",
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()
        scope = (scope or "tenant").strip().lower()
        platform = scope == "platform"
        base = "/api/platform-groups" if platform else "/api/groups"

        def _group_body() -> dict:
            if platform:
                body: dict = {}
                if default_user_assign is not None:
                    body["group_default_user_assign"] = default_user_assign
                if name:
                    body["platform_group_name"] = name
                if description:
                    body["platform_group_description"] = description
                return body
            body = {}
            if default_user_assign is not None:
                body["group_default_user_assign"] = default_user_assign
            if name:
                body["group_name"] = name
            if description:
                body["group_description"] = description
            return body

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", f"{base}/search", "search groups",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No groups found."
            lines = [f"Groups ({len(items)}, scope={scope}):"]
            for g in items:
                gid = g.get("group_id") or g.get("platform_group_id")
                gname = g.get("group_name") or g.get("platform_group_name", "?")
                lines.append(f"- {gname} (id: {gid})")
            return "\n".join(lines)

        if action == "get":
            if not group_id:
                return "group_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"{base}/{group_id}", "get group"
            )
            if err:
                return err
            return json.dumps(resp.json(), default=str)[:2000]

        if action == "create":
            if not name:
                return "name is required for action 'create'."
            resp, err = await _api(
                base_url, headers, "post", base, "create group", json=_group_body()
            )
            if err:
                return err
            g = resp.json()
            gid = g.get("group_id") or g.get("platform_group_id")
            return f"Created group (id: {gid}, scope={scope})."

        if action == "update":
            if not group_id:
                return "group_id is required for action 'update'."
            body = _group_body()
            if not body:
                return (
                    "Provide at least one field to update (name, description, or "
                    "default_user_assign)."
                )
            path = f"{base}/{group_id}" if platform else f"{base}/{group_id}/information"
            _, err = await _api(
                base_url, headers, "put", path, "update group", json=body
            )
            if err:
                return err
            return f"Updated group {group_id} (scope={scope})."

        if action == "set_users":
            if not group_id or user_ids is None:
                return "group_id and user_ids are required for 'set_users'."
            field = "platform_group_users" if platform else "group_users"
            _, err = await _api(
                base_url, headers, "put", f"{base}/{group_id}/users",
                "set group users", json={field: user_ids},
            )
            if err:
                return err
            return f"Set {len(user_ids)} users on group {group_id}."

        if action == "set_roles":
            if not group_id or role_ids is None:
                return "group_id and role_ids are required for 'set_roles'."
            if platform:
                path = f"{base}/{group_id}/platform-roles"
                body = {"platform_group_platform_roles": role_ids}
            else:
                path = f"{base}/{group_id}/roles"
                body = {"group_roles": role_ids}
            _, err = await _api(
                base_url, headers, "put", path, "set group roles", json=body
            )
            if err:
                return err
            return f"Set {len(role_ids)} roles on group {group_id}."

        if action == "add_grant":
            if platform:
                return "Grants are tenant-scoped; use scope='tenant'."
            if not (group_id and grant_name and grant_resource_id and grant_resource_type):
                return (
                    "group_id, grant_name (e.g. OBSERVER/PLANNER/LAUNCHER), "
                    "grant_resource_id and grant_resource_type (SCENARIO/SIMULATION) "
                    "are required for 'add_grant'."
                )
            _, err = await _api(
                base_url, headers, "post", f"{base}/{group_id}/grants", "add grant",
                json={
                    "grant_name": grant_name,
                    "grant_resource": grant_resource_id,
                    "grant_resource_type": grant_resource_type,
                },
            )
            if err:
                return err
            return f"Added {grant_name} grant on {grant_resource_type} {grant_resource_id} to group {group_id}."

        if action == "remove_grant":
            if platform:
                return "Grants are tenant-scoped; use scope='tenant'."
            if not group_id or not grant_id:
                return "group_id and grant_id are required for 'remove_grant'."
            _, err = await _api(
                base_url, headers, "delete", f"{base}/{group_id}/grants/{grant_id}",
                "remove grant",
            )
            if err:
                return err
            return f"Removed grant {grant_id} from group {group_id}."

        if action == "delete":
            if not group_id:
                return "group_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"{base}/{group_id}", "delete group"
            )
            if err:
                return err
            return f"Deleted group {group_id} (scope={scope})."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "set_users, set_roles, add_grant, remove_grant, delete."
        )

    # ── Roles ──────────────────────────────────────────────────────────
    async def manage_openaev_role(
        action: str = "",
        scope: str = "tenant",
        role_id: str = "",
        name: str = "",
        description: str = "",
        capabilities: list[str] | None = None,
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()
        scope = (scope or "tenant").strip().lower()
        platform = scope == "platform"
        base = "/api/platform-roles" if platform else "/api/roles"
        prefix = "platform_role" if platform else "role"

        def _role_body() -> dict:
            body: dict = {}
            if name:
                body[f"{prefix}_name"] = name
            if description:
                body[f"{prefix}_description"] = description
            if capabilities is not None:
                body[f"{prefix}_capabilities"] = capabilities
            return body

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", f"{base}/search", "search roles",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No roles found."
            lines = [f"Roles ({len(items)}, scope={scope}):"]
            for r in items:
                rid = r.get("role_id") or r.get("platform_role_id")
                rname = r.get("role_name") or r.get("platform_role_name", "?")
                lines.append(f"- {rname} (id: {rid})")
            return "\n".join(lines)

        if action == "get":
            if not role_id:
                return "role_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"{base}/{role_id}", "get role"
            )
            if err:
                return err
            return json.dumps(resp.json(), default=str)[:2000]

        if action in ("create", "update"):
            if action == "create" and not name:
                return "name is required for action 'create'."
            if action == "update" and not role_id:
                return "role_id is required for action 'update'."
            body = _role_body()
            if action == "update" and not body:
                return "Provide at least one field to update."
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", base, "create role", json=body
                )
            else:
                resp, err = await _api(
                    base_url, headers, "put", f"{base}/{role_id}", "update role",
                    json=body,
                )
            if err:
                return err
            r = resp.json()
            rid = r.get("role_id") or r.get("platform_role_id")
            verb = "Created" if action == "create" else "Updated"
            return f"{verb} role (id: {rid}, scope={scope})."

        if action == "delete":
            if not role_id:
                return "role_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"{base}/{role_id}", "delete role"
            )
            if err:
                return err
            return f"Deleted role {role_id} (scope={scope})."

        return f"Unknown action '{action}'. Valid actions: list, get, create, update, delete."

    # ── Profile (me) ───────────────────────────────────────────────────
    async def manage_openaev_profile(
        action: str = "",
        config: str = "",
    ) -> str:
        action = (action or "").strip().lower()

        if action == "get":
            resp, err = await _api(base_url, headers, "get", "/api/me", "get profile")
            if err:
                return err
            me = resp.json()
            return "\n".join(
                [
                    f"{me.get('user_email', '?')} (id: {me.get('user_id')})",
                    f"  name: {me.get('user_firstname') or ''} {me.get('user_lastname') or ''}".rstrip(),
                    f"  admin: {me.get('user_admin')}",
                ]
            )

        if action == "tokens":
            resp, err = await _api(
                base_url, headers, "get", "/api/me/tokens", "list tokens"
            )
            if err:
                return err
            return json.dumps(resp.json(), default=str)[:2000]

        if action == "update":
            if not config:
                return "config (JSON object of profile fields) is required for 'update'."
            try:
                body = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(body, dict):
                return "config must be a JSON object of profile fields."
            _, err = await _api(
                base_url, headers, "put", "/api/me/profile", "update profile",
                json=body,
            )
            if err:
                return err
            return "Updated your profile."

        return f"Unknown action '{action}'. Valid actions: get, tokens, update."

    executor.register("manage_openaev_organization", manage_openaev_organization)
    executor.register("manage_openaev_user", manage_openaev_user)
    executor.register("manage_openaev_group", manage_openaev_group)
    executor.register("manage_openaev_role", manage_openaev_role)
    executor.register("manage_openaev_profile", manage_openaev_profile)

    return [
        ToolDefinition(
            name="manage_openaev_organization",
            description=(
                "Manage OpenAEV organizations. Actions: 'list' (search), 'create' "
                "(name, optional description/tags), 'update' (organization_id + "
                "fields), 'delete' (organization_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "update", "delete"]},
                    "organization_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_user",
            description=(
                "Manage OpenAEV platform users. scope: 'tenant' (/api/users) or "
                "'platform' (/api/platform-users, Enterprise Edition). Actions: 'list' "
                "(search), 'get' (user_id), 'create' (email + optional firstname/"
                "lastname/password/organization_id/tags/admin/tenant_ids), 'update' "
                "(user_id + fields), 'delete' (user_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                    "scope": {"type": "string", "enum": ["tenant", "platform"], "default": "tenant"},
                    "user_id": {"type": "string"},
                    "email": {"type": "string"},
                    "firstname": {"type": "string"},
                    "lastname": {"type": "string"},
                    "password": {"type": "string", "description": "Initial plain password (create)."},
                    "organization_id": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "admin": {"type": "boolean", "description": "Grant admin rights. Omit on update to leave unchanged."},
                    "tenant_ids": {"type": "array", "items": {"type": "string"}, "description": "Tenants to attach the user to."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_group",
            description=(
                "Manage OpenAEV groups. scope: 'tenant' (/api/groups) or 'platform' "
                "(/api/platform-groups, EE). Actions: 'list' (search), 'get' "
                "(group_id), 'create' (name, optional description/default_user_assign), "
                "'update' (group_id + fields), 'set_users' (group_id + user_ids), "
                "'set_roles' (group_id + role_ids), 'delete' (group_id). Tenant-only: "
                "'add_grant' (group_id + grant_name [OBSERVER/PLANNER/LAUNCHER] + "
                "grant_resource_id + grant_resource_type [SCENARIO/SIMULATION]) and "
                "'remove_grant' (group_id + grant_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create", "update", "set_users",
                            "set_roles", "add_grant", "remove_grant", "delete",
                        ],
                    },
                    "scope": {"type": "string", "enum": ["tenant", "platform"], "default": "tenant"},
                    "group_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "default_user_assign": {"type": "boolean", "description": "Auto-assign new users to this group. Omit on update to leave unchanged."},
                    "user_ids": {"type": "array", "items": {"type": "string"}},
                    "role_ids": {"type": "array", "items": {"type": "string"}},
                    "grant_name": {"type": "string", "description": "OBSERVER/PLANNER/LAUNCHER."},
                    "grant_resource_id": {"type": "string"},
                    "grant_resource_type": {"type": "string", "description": "SCENARIO/SIMULATION."},
                    "grant_id": {"type": "string"},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_role",
            description=(
                "Manage OpenAEV roles (capability sets). scope: 'tenant' (/api/roles) "
                "or 'platform' (/api/platform-roles, EE). Actions: 'list' (search), "
                "'get' (role_id), 'create' (name, optional description/capabilities), "
                "'update' (role_id + fields), 'delete' (role_id). capabilities is a "
                "list of capability names."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                    "scope": {"type": "string", "enum": ["tenant", "platform"], "default": "tenant"},
                    "role_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_profile",
            description=(
                "Read or update the authenticated OpenAEV user. Actions: 'get' "
                "(email, name, admin), 'tokens' (list personal API tokens), 'update' "
                "(config = a JSON object of profile fields to PUT to /api/me/profile)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "tokens", "update"]},
                    "config": {"type": "string", "description": "JSON object of profile fields (update)."},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
