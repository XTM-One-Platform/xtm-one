"""Microsoft Entra ID - user lifecycle, offboarding, and account governance."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    GRAPH_BASE,
    PASSWORD_METHOD_ID,
    EntraClient,
    build_search_params,
    dt,
    fmt_list,
    safe_json,
)

# Default property projection for user reads - compact but informative.
_USER_SELECT = (
    "id,displayName,userPrincipalName,mail,jobTitle,department,"
    "accountEnabled,userType,officeLocation,mobilePhone,createdDateTime"
)


def _fmt_user(u: dict[str, Any]) -> str:
    enabled = u.get("accountEnabled")
    state = "enabled" if enabled else ("disabled" if enabled is False else "?")
    job = u.get("jobTitle") or "-"
    dept = u.get("department") or "-"
    utype = u.get("userType") or "Member"
    return (
        f"- {u.get('displayName', 'Unknown')} <{u.get('userPrincipalName', '?')}>\n"
        f"  id: {u.get('id', '?')} | account: {state} | type: {utype}\n"
        f"  title: {job} | dept: {dept} | mail: {u.get('mail') or '-'}"
    )


def _fmt_group_ref(g: dict[str, Any]) -> str:
    kind = (g.get("@odata.type") or "").split(".")[-1] or "group"
    return (
        f"- {g.get('displayName', 'Unknown')} ({kind}) | id: {g.get('id', '?')}"
        f"{' | role' if kind == 'directoryRole' else ''}"
    )


def register(
    client: EntraClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    src = "integration:entra"

    # -- List / search ----------------------------------------------------

    async def list_entra_users(
        search: str = "",
        odata_filter: str = "",
        top: int = 25,
        skiptoken: str = "",
    ) -> str:
        params, extra = build_search_params(
            search=search,
            search_fields=("displayName", "mail", "userPrincipalName"),
            odata_filter=odata_filter,
            select=_USER_SELECT,
            skiptoken=skiptoken,
        )
        items, err, next_tok = await client.paged(
            "/users",
            "list users",
            top=min(top, 100),
            params=params,
            extra_headers=extra,
        )
        if err:
            return err
        return fmt_list(items, _fmt_user, noun="user", skiptoken=next_tok)

    async def get_entra_user(user_id: str, include_signin_activity: bool = False) -> str:
        select = _USER_SELECT + ",givenName,surname,usageLocation,companyName,lastPasswordChangeDateTime"
        if include_signin_activity:
            select += ",signInActivity"
        data, err = await client.get_json(
            f"/users/{quote(user_id, safe='')}",
            "get user",
            params={"$select": select},
        )
        if err:
            return err
        u = data or {}
        lines = [_fmt_user(u)]
        if u.get("givenName") or u.get("surname"):
            lines.append(
                f"  name: {u.get('givenName') or ''} {u.get('surname') or ''}".rstrip()
            )
        if u.get("usageLocation"):
            lines.append(f"  usage location: {u['usageLocation']}")
        if u.get("companyName"):
            lines.append(f"  company: {u['companyName']}")
        if u.get("createdDateTime"):
            lines.append(f"  created: {dt(u['createdDateTime'])}")
        if u.get("lastPasswordChangeDateTime"):
            lines.append(f"  last password change: {dt(u['lastPasswordChangeDateTime'])}")
        sia = u.get("signInActivity") or {}
        if sia:
            lines.append(
                f"  last interactive sign-in: {dt(sia.get('lastSignInDateTime'))} | "
                f"last non-interactive: {dt(sia.get('lastNonInteractiveSignInDateTime'))}"
            )
        return "\n".join(lines)

    # -- Create / update / delete ----------------------------------------

    async def create_entra_user(
        display_name: str,
        user_principal_name: str,
        mail_nickname: str,
        password: str,
        account_enabled: bool = True,
        force_change_password: bool = True,
        job_title: str = "",
        department: str = "",
        usage_location: str = "",
    ) -> str:
        payload: dict[str, Any] = {
            "accountEnabled": account_enabled,
            "displayName": display_name,
            "mailNickname": mail_nickname,
            "userPrincipalName": user_principal_name,
            "passwordProfile": {
                "password": password,
                "forceChangePasswordNextSignIn": force_change_password,
            },
        }
        if job_title:
            payload["jobTitle"] = job_title
        if department:
            payload["department"] = department
        if usage_location:
            payload["usageLocation"] = usage_location
        resp, err = await client.request(
            "post", "/users", "create user", json=payload
        )
        if err:
            return err
        created = safe_json(resp)
        return (
            f"Created user {created.get('displayName', display_name)} "
            f"<{created.get('userPrincipalName', user_principal_name)}> "
            f"(id: {created.get('id', '?')})."
        )

    async def update_entra_user(
        user_id: str,
        display_name: str = "",
        job_title: str = "",
        department: str = "",
        office_location: str = "",
        mobile_phone: str = "",
        usage_location: str = "",
        properties: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = dict(properties or {})
        for key, val in (
            ("displayName", display_name),
            ("jobTitle", job_title),
            ("department", department),
            ("officeLocation", office_location),
            ("mobilePhone", mobile_phone),
            ("usageLocation", usage_location),
        ):
            if val:
                payload[key] = val
        if not payload:
            return "Error: No fields provided to update."
        _, err = await client.request(
            "patch",
            f"/users/{quote(user_id, safe='')}",
            "update user",
            json=payload,
        )
        if err:
            return err
        return f"Updated user {user_id} - {', '.join(payload.keys())}."

    async def set_entra_user_account_enabled(user_id: str, enabled: bool) -> str:
        _, err = await client.request(
            "patch",
            f"/users/{quote(user_id, safe='')}",
            "enable/disable user account",
            json={"accountEnabled": enabled},
        )
        if err:
            return err
        verb = "enabled" if enabled else "disabled"
        note = (
            ""
            if enabled
            else " Existing tokens remain valid until expiry - call "
            "revoke_entra_user_sessions to force re-authentication immediately."
        )
        return f"User {user_id} account {verb}.{note}"

    async def delete_entra_user(user_id: str) -> str:
        _, err = await client.request(
            "delete", f"/users/{quote(user_id, safe='')}", "delete user"
        )
        if err:
            return err
        return (
            f"Deleted user {user_id}. The object is recoverable from the directory "
            f"recycle bin for 30 days (see manage_entra_directory restore actions)."
        )

    # -- Offboarding / security actions ----------------------------------

    async def revoke_entra_user_sessions(user_id: str) -> str:
        _, err = await client.request(
            "post",
            f"/users/{quote(user_id, safe='')}/revokeSignInSessions",
            "revoke user sign-in sessions",
        )
        if err:
            return err
        return (
            f"Revoked all refresh tokens / sessions for user {user_id}. They must "
            f"sign in again on every application."
        )

    async def reset_entra_user_password(
        user_id: str, new_password: str = "", reveal_password: bool = False
    ) -> str:
        # The resetPassword action body only carries an optional newPassword;
        # the user is always prompted to change it at next sign-in (admin reset).
        payload: dict[str, Any] = {}
        if new_password:
            payload["newPassword"] = new_password
        resp, err = await client.request(
            "post",
            f"/users/{quote(user_id, safe='')}/authentication/methods/"
            f"{PASSWORD_METHOD_ID}/resetPassword",
            "reset user password",
            json=payload,
        )
        if err:
            return err
        # safe_json never raises on a non-JSON / empty / non-dict success body,
        # matching every other write path in the package.
        generated = safe_json(resp).get("newPassword")
        suffix = (
            " The user must change it at next sign-in; the reset is processed "
            "asynchronously."
        )
        # Only an AUTO-generated password may ever be echoed. When the caller
        # supplied new_password they already have it, and echoing it back (even
        # with reveal_password=true) would leak the caller-supplied secret into
        # stored tool output - so a supplied password is never returned.
        if generated and not new_password:
            if reveal_password:
                return (
                    f"Initiated password reset for user {user_id}. System-generated "
                    f"temporary password (shown once - deliver it to the user over a "
                    f"secure channel): {generated}.{suffix}"
                )
            return (
                f"Initiated password reset for user {user_id}. A temporary password "
                f"was generated but is hidden by default to avoid leaking it into "
                f"chat history - re-run with reveal_password=true to reveal it, or "
                f"pass an explicit new_password.{suffix}"
            )
        return f"Initiated password reset for user {user_id}.{suffix}"

    # -- Relationships ----------------------------------------------------

    async def list_entra_user_groups(
        user_id: str, transitive: bool = False, top: int = 50, skiptoken: str = ""
    ) -> str:
        rel = "transitiveMemberOf" if transitive else "memberOf"
        items, err, next_tok = await client.paged(
            f"/users/{quote(user_id, safe='')}/{rel}",
            "list user group memberships",
            top=min(top, 100),
            params={"$skiptoken": skiptoken} if skiptoken else None,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_group_ref,
            noun="membership",
            skiptoken=next_tok,
            empty_msg="User is not a member of any groups or directory roles.",
        )

    async def get_entra_user_manager(user_id: str) -> str:
        resp, err = await client.request(
            "get",
            f"/users/{quote(user_id, safe='')}/manager",
            "get user manager",
            expected_statuses={404},
        )
        if resp is not None and resp.status_code == 404:
            return f"User {user_id} has no manager assigned."
        if err:
            return err
        m = safe_json(resp)
        return (
            f"Manager of {user_id}: {m.get('displayName', 'Unknown')} "
            f"<{m.get('userPrincipalName', '?')}> (id: {m.get('id', '?')})"
        )

    async def set_entra_user_manager(user_id: str, manager_id: str) -> str:
        _, err = await client.request(
            "put",
            f"/users/{quote(user_id, safe='')}/manager/$ref",
            "set user manager",
            json={"@odata.id": f"{GRAPH_BASE}/users/{quote(manager_id, safe='')}"},
        )
        if err:
            return err
        return f"Set manager of user {user_id} to {manager_id}."

    # -- Consolidated long tail ------------------------------------------

    async def manage_entra_user(
        action: str,
        user_id: str = "",
        sku_id: str = "",
        disabled_plans: list[str] | None = None,
        top: int = 50,
        skiptoken: str = "",
    ) -> str:
        page = min(top, 100)
        page_params = {"$skiptoken": skiptoken} if skiptoken else None
        if action == "list_licenses":
            data, err = await client.get_json(
                f"/users/{quote(user_id, safe='')}/licenseDetails",
                "list user licenses",
            )
            if err:
                return err

            def _fmt_lic(lic: dict[str, Any]) -> str:
                plans = lic.get("servicePlans", []) or []
                active = sum(
                    1 for p in plans if p.get("provisioningStatus") == "Success"
                )
                return (
                    f"- {lic.get('skuPartNumber', '?')} (skuId: {lic.get('skuId', '?')})"
                    f" | {active}/{len(plans)} service plans active"
                )

            return fmt_list(
                (data or {}).get("value", []),
                _fmt_lic,
                noun="license",
                empty_msg="User has no assigned licenses.",
            )
        if action == "assign_license":
            if not sku_id:
                return "Error: sku_id is required to assign a license."
            _, err = await client.request(
                "post",
                f"/users/{quote(user_id, safe='')}/assignLicense",
                "assign user license",
                json={
                    "addLicenses": [
                        {"skuId": sku_id, "disabledPlans": disabled_plans or []}
                    ],
                    "removeLicenses": [],
                },
            )
            return err or f"Assigned license {sku_id} to user {user_id}."
        if action == "remove_license":
            if not sku_id:
                return "Error: sku_id is required to remove a license."
            _, err = await client.request(
                "post",
                f"/users/{quote(user_id, safe='')}/assignLicense",
                "remove user license",
                json={"addLicenses": [], "removeLicenses": [sku_id]},
            )
            return err or f"Removed license {sku_id} from user {user_id}."
        if action == "list_direct_reports":
            items, err, next_tok = await client.paged(
                f"/users/{quote(user_id, safe='')}/directReports",
                "list direct reports",
                top=page,
                params=page_params,
            )
            if err:
                return err
            return fmt_list(
                items, _fmt_user, noun="direct report", skiptoken=next_tok
            )
        if action == "list_registered_devices":
            items, err, next_tok = await client.paged(
                f"/users/{quote(user_id, safe='')}/registeredDevices",
                "list registered devices",
                top=page,
                params=page_params,
            )
            if err:
                return err

            def _fmt_dev(d: dict[str, Any]) -> str:
                return (
                    f"- {d.get('displayName', 'Unknown')} | os: "
                    f"{d.get('operatingSystem', '?')} {d.get('operatingSystemVersion', '')}"
                    f" | id: {d.get('id', '?')}"
                )

            return fmt_list(items, _fmt_dev, noun="device", skiptoken=next_tok)
        if action == "list_app_role_assignments":
            items, err, next_tok = await client.paged(
                f"/users/{quote(user_id, safe='')}/appRoleAssignments",
                "list user app role assignments",
                top=page,
                params=page_params,
            )
            if err:
                return err

            def _fmt_ara(a: dict[str, Any]) -> str:
                return (
                    f"- {a.get('resourceDisplayName', '?')} | principal: "
                    f"{a.get('principalDisplayName', '?')} | id: {a.get('id', '?')}"
                )

            return fmt_list(
                items, _fmt_ara, noun="app role assignment", skiptoken=next_tok
            )
        return (
            "Error: unknown action. Valid actions: list_licenses, assign_license, "
            "remove_license, list_direct_reports, list_registered_devices, "
            "list_app_role_assignments."
        )

    handlers = {
        "list_entra_users": list_entra_users,
        "get_entra_user": get_entra_user,
        "create_entra_user": create_entra_user,
        "update_entra_user": update_entra_user,
        "set_entra_user_account_enabled": set_entra_user_account_enabled,
        "delete_entra_user": delete_entra_user,
        "revoke_entra_user_sessions": revoke_entra_user_sessions,
        "reset_entra_user_password": reset_entra_user_password,
        "list_entra_user_groups": list_entra_user_groups,
        "get_entra_user_manager": get_entra_user_manager,
        "set_entra_user_manager": set_entra_user_manager,
        "manage_entra_user": manage_entra_user,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    user_ref = {
        "type": "string",
        "description": "User object id (GUID) or userPrincipalName (e.g. jane@contoso.com).",
    }
    return [
        ToolDefinition(
            name="list_entra_users",
            description=(
                "List or search Microsoft Entra ID (Azure AD) directory users. "
                "Prefer the structured 'search' (matches display name, mail, UPN) "
                "or an OData 'odata_filter' over scanning. Returns each user's id, "
                "UPN, account enabled/disabled state, type, title, and department. "
                "Use the returned id or UPN with get_entra_user and the user "
                "management tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Free-text search across displayName, mail, and userPrincipalName (or one 'property:value' clause, e.g. 'displayName:jane').",
                    },
                    "odata_filter": {
                        "type": "string",
                        "description": (
                            "OData $filter, e.g. \"accountEnabled eq false\" or "
                            "\"startswith(department,'Finance')\"."
                        ),
                    },
                    "top": {
                        "type": "integer",
                        "description": "Max results per page (1-100, default 25).",
                        "default": 25,
                    },
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token from a previous call's 'More results' hint.",
                    },
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_entra_user",
            description=(
                "Get a single Entra ID user's full profile by id or UPN, with "
                "the last password change time. Set "
                "include_signin_activity to also return last interactive / "
                "non-interactive sign-in times (requires Entra ID P1/P2 + "
                "AuditLog.Read.All) - useful for finding stale or dormant accounts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": user_ref,
                    "include_signin_activity": {
                        "type": "boolean",
                        "description": "Include last sign-in activity timestamps (default false).",
                        "default": False,
                    },
                },
                "required": ["user_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="create_entra_user",
            description=(
                "Create a new Entra ID user (onboarding). Requires a display name, "
                "userPrincipalName (login, e.g. jane@contoso.com), mailNickname, and "
                "an initial password. By default the account is enabled and the user "
                "must change the password at first sign-in. Set usage_location (ISO "
                "country code) before assigning licenses."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "user_principal_name": {
                        "type": "string",
                        "description": "Login name / UPN, e.g. jane.doe@contoso.com.",
                    },
                    "mail_nickname": {
                        "type": "string",
                        "description": "Mail alias (the part before @), e.g. jane.doe.",
                    },
                    "password": {
                        "type": "string",
                        "description": "Initial password (must meet the tenant's complexity policy).",
                    },
                    "account_enabled": {"type": "boolean", "default": True},
                    "force_change_password": {
                        "type": "boolean",
                        "description": "Require a password change at next sign-in (default true).",
                        "default": True,
                    },
                    "job_title": {"type": "string"},
                    "department": {"type": "string"},
                    "usage_location": {
                        "type": "string",
                        "description": "Two-letter ISO country code (e.g. US, FR) - required before license assignment.",
                    },
                },
                "required": [
                    "display_name",
                    "user_principal_name",
                    "mail_nickname",
                    "password",
                ],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_entra_user",
            description=(
                "Update profile attributes on an Entra ID user. Only the fields you "
                "provide are changed. Use the typed fields for common attributes, or "
                "'properties' for any other Graph user property (e.g. "
                "{\"employeeId\": \"1234\"})."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": user_ref,
                    "display_name": {"type": "string"},
                    "job_title": {"type": "string"},
                    "department": {"type": "string"},
                    "office_location": {"type": "string"},
                    "mobile_phone": {"type": "string"},
                    "usage_location": {"type": "string"},
                    "properties": {
                        "type": "object",
                        "description": "Arbitrary additional Graph user properties to set.",
                    },
                },
                "required": ["user_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="set_entra_user_account_enabled",
            description=(
                "Enable or disable an Entra ID user account. Disabling (enabled=false) "
                "is the standard first offboarding step - it blocks new sign-ins. "
                "Existing tokens stay valid until expiry, so also call "
                "revoke_entra_user_sessions to cut access immediately."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": user_ref,
                    "enabled": {
                        "type": "boolean",
                        "description": "true to enable the account, false to disable (block sign-in).",
                    },
                },
                "required": ["user_id", "enabled"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="revoke_entra_user_sessions",
            description=(
                "Revoke all of a user's refresh tokens and browser sessions, forcing "
                "re-authentication on every application. Use during offboarding or "
                "after a suspected account compromise, alongside disabling the account "
                "and/or resetting the password."
            ),
            input_schema={
                "type": "object",
                "properties": {"user_id": user_ref},
                "required": ["user_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="reset_entra_user_password",
            description=(
                "Reset a user's password (administrative reset, not the user's own). "
                "Provide new_password, or omit it to have Entra generate a temporary "
                "one. An auto-generated password is hidden by default (to avoid "
                "leaking it into chat history); pass reveal_password=true to show it. "
                "The user is always required to change it at next sign-in. Requires "
                "UserAuthenticationMethod.ReadWrite.All and an admin role; processed "
                "asynchronously."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": user_ref,
                    "new_password": {
                        "type": "string",
                        "description": "Optional explicit new password; omit to auto-generate one.",
                    },
                    "reveal_password": {
                        "type": "boolean",
                        "description": (
                            "Reveal an auto-generated temporary password in the "
                            "result (default false). Ignored when new_password is "
                            "supplied."
                        ),
                        "default": False,
                    },
                },
                "required": ["user_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="list_entra_user_groups",
            description=(
                "List the groups and directory roles a user belongs to. Set "
                "transitive=true to include nested (inherited) memberships. Use to "
                "audit a user's access before or during offboarding."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": user_ref,
                    "transitive": {
                        "type": "boolean",
                        "description": "Include nested/inherited memberships (default false).",
                        "default": False,
                    },
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token from a previous call's 'More results' hint.",
                    },
                },
                "required": ["user_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_entra_user_manager",
            description="Get a user's assigned manager (display name, UPN, id).",
            input_schema={
                "type": "object",
                "properties": {"user_id": user_ref},
                "required": ["user_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="set_entra_user_manager",
            description="Set (or change) a user's manager by the manager's user id or UPN.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": user_ref,
                    "manager_id": {
                        "type": "string",
                        "description": "The manager's user object id or UPN.",
                    },
                },
                "required": ["user_id", "manager_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="manage_entra_user",
            description=(
                "Consolidated user sub-operations. action is one of: "
                "'list_licenses' (assigned SKUs), 'assign_license' (needs sku_id, "
                "optional disabled_plans), 'remove_license' (needs sku_id), "
                "'list_direct_reports', 'list_registered_devices', "
                "'list_app_role_assignments' (enterprise-app access). Get sku_id "
                "values from list_entra_subscribed_skus."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_licenses",
                            "assign_license",
                            "remove_license",
                            "list_direct_reports",
                            "list_registered_devices",
                            "list_app_role_assignments",
                        ],
                    },
                    "user_id": user_ref,
                    "sku_id": {
                        "type": "string",
                        "description": "License SKU id (for assign_license / remove_license).",
                    },
                    "disabled_plans": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Service-plan ids to disable when assigning a license.",
                    },
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token for the list_* actions (from a previous call's 'More results' hint).",
                    },
                },
                "required": ["action", "user_id"],
            },
            source=src,
            priority=1,
        ),
    ]
