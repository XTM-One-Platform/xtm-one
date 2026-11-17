"""Microsoft Entra ID - app registrations, service principals, and consent."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import EntraClient, build_search_params, dt, fmt_list, safe_json

_APP_SELECT = "id,appId,displayName,signInAudience,createdDateTime,publisherDomain"
_SP_SELECT = (
    "id,appId,displayName,servicePrincipalType,accountEnabled,"
    "appRoleAssignmentRequired,homepage,tags"
)


def _fmt_app(a: dict[str, Any]) -> str:
    return (
        f"- {a.get('displayName', 'Unnamed')}\n"
        f"  objectId: {a.get('id', '?')} | appId: {a.get('appId', '?')} | "
        f"audience: {a.get('signInAudience', '?')}"
    )


def _fmt_sp(s: dict[str, Any]) -> str:
    enabled = s.get("accountEnabled")
    state = "enabled" if enabled else ("disabled" if enabled is False else "?")
    return (
        f"- {s.get('displayName', 'Unnamed')} ({s.get('servicePrincipalType', '?')}, {state})\n"
        f"  objectId: {s.get('id', '?')} | appId: {s.get('appId', '?')} | "
        f"assignment required: {s.get('appRoleAssignmentRequired', False)}"
    )


def _fmt_credential(c: dict[str, Any], kind: str) -> str:
    return (
        f"- [{kind}] {c.get('displayName') or '(no name)'} | keyId: "
        f"{c.get('keyId', '?')}\n"
        f"  valid: {dt(c.get('startDateTime'))} -> {dt(c.get('endDateTime'))}"
    )


def register(
    client: EntraClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    src = "integration:entra"

    async def list_entra_applications(
        search: str = "", odata_filter: str = "", top: int = 25, skiptoken: str = ""
    ) -> str:
        params, extra = build_search_params(
            search=search,
            odata_filter=odata_filter,
            select=_APP_SELECT,
            skiptoken=skiptoken,
        )
        items, err, next_tok = await client.paged(
            "/applications",
            "list applications",
            top=min(top, 100),
            params=params,
            extra_headers=extra,
        )
        if err:
            return err
        return fmt_list(items, _fmt_app, noun="application", skiptoken=next_tok)

    async def get_entra_application(app_id: str) -> str:
        data, err = await client.get_json(
            f"/applications/{quote(app_id, safe='')}", "get application"
        )
        if err:
            return err
        a = data or {}
        lines = [_fmt_app(a)]
        if a.get("createdDateTime"):
            lines.append(f"  created: {dt(a['createdDateTime'])}")
        if a.get("publisherDomain"):
            lines.append(f"  publisher domain: {a['publisherDomain']}")
        pwd = a.get("passwordCredentials", []) or []
        keys = a.get("keyCredentials", []) or []
        if pwd or keys:
            lines.append(f"  credentials: {len(pwd)} secret(s), {len(keys)} certificate(s)")
        return "\n".join(lines)

    async def list_entra_service_principals(
        search: str = "", odata_filter: str = "", top: int = 25, skiptoken: str = ""
    ) -> str:
        params, extra = build_search_params(
            search=search,
            odata_filter=odata_filter,
            select=_SP_SELECT,
            skiptoken=skiptoken,
        )
        items, err, next_tok = await client.paged(
            "/servicePrincipals",
            "list service principals",
            top=min(top, 100),
            params=params,
            extra_headers=extra,
        )
        if err:
            return err
        return fmt_list(items, _fmt_sp, noun="service principal", skiptoken=next_tok)

    async def get_entra_service_principal(sp_id: str) -> str:
        data, err = await client.get_json(
            f"/servicePrincipals/{quote(sp_id, safe='')}", "get service principal"
        )
        if err:
            return err
        s = data or {}
        lines = [_fmt_sp(s)]
        if s.get("homepage"):
            lines.append(f"  homepage: {s['homepage']}")
        tags = s.get("tags", []) or []
        if tags:
            lines.append(f"  tags: {', '.join(tags)}")
        return "\n".join(lines)

    async def list_entra_app_role_assignments(
        sp_id: str, top: int = 50, skiptoken: str = ""
    ) -> str:
        items, err, next_tok = await client.paged(
            f"/servicePrincipals/{quote(sp_id, safe='')}/appRoleAssignedTo",
            "list app role assignments",
            top=min(top, 100),
            params={"$skiptoken": skiptoken} if skiptoken else None,
        )
        if err:
            return err

        def _fmt(a: dict[str, Any]) -> str:
            return (
                f"- {a.get('principalDisplayName', '?')} "
                f"({a.get('principalType', '?')}) -> "
                f"{a.get('resourceDisplayName', '?')} | id: {a.get('id', '?')}"
            )

        return fmt_list(
            items,
            _fmt,
            noun="app role assignment",
            skiptoken=next_tok,
            empty_msg="No principals are assigned to this enterprise application.",
        )

    async def list_entra_oauth2_grants(
        sp_id: str = "", top: int = 50, skiptoken: str = ""
    ) -> str:
        path = (
            f"/servicePrincipals/{quote(sp_id, safe='')}/oauth2PermissionGrants"
            if sp_id
            else "/oauth2PermissionGrants"
        )
        items, err, next_tok = await client.paged(
            path,
            "list OAuth2 permission grants",
            top=min(top, 100),
            params={"$skiptoken": skiptoken} if skiptoken else None,
        )
        if err:
            return err

        def _fmt(g: dict[str, Any]) -> str:
            consent = g.get("consentType", "?")
            return (
                f"- clientId: {g.get('clientId', '?')} -> resourceId: "
                f"{g.get('resourceId', '?')} | consent: {consent}\n"
                f"  scopes: {g.get('scope', '').strip() or '-'}"
            )

        return fmt_list(
            items,
            _fmt,
            noun="OAuth2 permission grant",
            skiptoken=next_tok,
            empty_msg="No delegated OAuth2 permission grants found.",
        )

    async def manage_entra_application(
        action: str,
        app_id: str = "",
        sp_id: str = "",
        display_name: str = "",
        key_id: str = "",
        enabled: bool = True,
        reveal_secret: bool = False,
        skiptoken: str = "",
    ) -> str:
        if action == "list_credentials":
            if not app_id.strip():
                return "Error: app_id is required for list_credentials."
            data, err = await client.get_json(
                f"/applications/{quote(app_id, safe='')}",
                "get application credentials",
                params={"$select": "passwordCredentials,keyCredentials,displayName"},
            )
            if err:
                return err
            a = data or {}
            creds = [
                _fmt_credential(c, "secret")
                for c in a.get("passwordCredentials", []) or []
            ] + [
                _fmt_credential(c, "certificate")
                for c in a.get("keyCredentials", []) or []
            ]
            if not creds:
                return f"Application {app_id} has no secrets or certificates."
            return (
                f"Credentials for '{a.get('displayName', app_id)}' (review expiry "
                f"dates for rotation):\n" + "\n".join(creds)
            )
        if action == "add_password":
            if not app_id.strip():
                return "Error: app_id is required for add_password."
            resp, err = await client.request(
                "post",
                f"/applications/{quote(app_id, safe='')}/addPassword",
                "add application secret",
                json={"passwordCredential": {"displayName": display_name or "added-by-xtm"}},
            )
            if err:
                return err
            c = safe_json(resp)
            base = (
                f"Added secret to application {app_id}. keyId: {c.get('keyId', '?')}, "
                f"expires: {dt(c.get('endDateTime'))}."
            )
            secret = c.get("secretText")
            if not secret:
                return base
            if not reveal_secret:
                return (
                    f"{base} The secret value is hidden by default to avoid leaking "
                    f"it into chat history. Microsoft only returns it once, at "
                    f"creation - re-run with reveal_secret=true to display it, then "
                    f"store it in a secret manager (rotate it if it is exposed)."
                )
            return (
                f"{base} Secret value (shown once - store it securely now): {secret}"
            )
        if action == "remove_password":
            if not app_id.strip():
                return "Error: app_id is required for remove_password."
            if not key_id:
                return "Error: key_id is required to remove a secret."
            _, err = await client.request(
                "post",
                f"/applications/{quote(app_id, safe='')}/removePassword",
                "remove application secret",
                json={"keyId": key_id},
            )
            return err or f"Removed secret {key_id} from application {app_id}."
        if action == "set_sp_enabled":
            if not sp_id.strip():
                return "Error: sp_id is required for set_sp_enabled."
            _, err = await client.request(
                "patch",
                f"/servicePrincipals/{quote(sp_id, safe='')}",
                "enable/disable service principal",
                json={"accountEnabled": enabled},
            )
            if err:
                return err
            verb = "enabled" if enabled else "disabled"
            return f"Service principal {sp_id} {verb} (sign-in to the app is now {verb})."
        if action == "list_owners":
            if not (app_id.strip() or sp_id.strip()):
                return "Error: list_owners requires app_id or sp_id."
            ref = f"/applications/{quote(app_id, safe='')}/owners" if app_id else (
                f"/servicePrincipals/{quote(sp_id, safe='')}/owners"
            )
            items, err, next_tok = await client.paged(
                ref,
                "list owners",
                top=50,
                params={"$skiptoken": skiptoken} if skiptoken else None,
            )
            if err:
                return err

            def _fmt_owner(o: dict[str, Any]) -> str:
                return (
                    f"- {o.get('displayName', 'Unknown')} "
                    f"<{o.get('userPrincipalName', '?')}> | id: {o.get('id', '?')}"
                )

            return fmt_list(items, _fmt_owner, noun="owner", skiptoken=next_tok)
        return (
            "Error: unknown action. Valid actions: list_credentials (needs app_id), "
            "add_password (app_id), remove_password (app_id + key_id), set_sp_enabled "
            "(sp_id + enabled), list_owners (app_id or sp_id)."
        )

    handlers = {
        "list_entra_applications": list_entra_applications,
        "get_entra_application": get_entra_application,
        "list_entra_service_principals": list_entra_service_principals,
        "get_entra_service_principal": get_entra_service_principal,
        "list_entra_app_role_assignments": list_entra_app_role_assignments,
        "list_entra_oauth2_grants": list_entra_oauth2_grants,
        "manage_entra_application": manage_entra_application,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    return [
        ToolDefinition(
            name="list_entra_applications",
            description=(
                "List Entra ID application registrations (the app definitions / "
                "manifests owned by this tenant). Returns objectId, appId (client "
                "id), and sign-in audience. Use get_entra_application or "
                "manage_entra_application (list_credentials) for secret/certificate "
                "expiry hygiene."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "odata_filter": {"type": "string"},
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_entra_application",
            description="Get an app registration by object id, including a credential count summary.",
            input_schema={
                "type": "object",
                "properties": {
                    "app_id": {"type": "string", "description": "Application OBJECT id (not the appId/client id)."}
                },
                "required": ["app_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_service_principals",
            description=(
                "List service principals (enterprise applications) - the local "
                "instances of apps that can sign in or be granted access in this "
                "tenant. Returns objectId, appId, type, and enabled state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "odata_filter": {"type": "string"},
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_entra_service_principal",
            description="Get a service principal (enterprise application) by its object id.",
            input_schema={
                "type": "object",
                "properties": {
                    "sp_id": {"type": "string", "description": "Service principal object id."}
                },
                "required": ["sp_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_app_role_assignments",
            description=(
                "List the users / groups / service principals assigned to an "
                "enterprise application (who can access it). Pass the service "
                "principal's object id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sp_id": {"type": "string", "description": "Service principal object id."},
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token from a previous call's 'More results' hint.",
                    },
                },
                "required": ["sp_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_oauth2_grants",
            description=(
                "List delegated OAuth2 permission (consent) grants - which scopes "
                "apps have been consented to access on behalf of users. Pass sp_id to "
                "scope to one app, or omit it for the whole tenant. Key for consent / "
                "over-permission audits."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sp_id": {"type": "string", "description": "Optional service principal object id to scope the grants."},
                    "top": {"type": "integer", "default": 50},
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
            name="manage_entra_application",
            description=(
                "Consolidated application / service-principal sub-operations. action "
                "is one of: 'list_credentials' (secret + certificate expiry for an "
                "app, needs app_id), 'add_password' (add a client secret to an app; "
                "the secret value is hidden unless reveal_secret=true), "
                "'remove_password' (needs app_id + key_id), 'set_sp_enabled' (enable/"
                "disable sign-in for a service principal, needs sp_id + enabled), "
                "'list_owners' (app_id or sp_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_credentials",
                            "add_password",
                            "remove_password",
                            "set_sp_enabled",
                            "list_owners",
                        ],
                    },
                    "app_id": {"type": "string", "description": "Application object id."},
                    "sp_id": {"type": "string", "description": "Service principal object id."},
                    "display_name": {"type": "string", "description": "Label for a new secret (add_password)."},
                    "key_id": {"type": "string", "description": "Secret keyId to remove (remove_password)."},
                    "enabled": {"type": "boolean", "description": "Target state for set_sp_enabled.", "default": True},
                    "reveal_secret": {
                        "type": "boolean",
                        "description": (
                            "add_password only: reveal the newly created secret value "
                            "in the result. Default false to avoid leaking it into "
                            "chat history; the value is only retrievable once, at "
                            "creation."
                        ),
                        "default": False,
                    },
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token for the list_owners action (from a previous call's 'More results' hint).",
                    },
                },
                "required": ["action"],
            },
            source=src,
            priority=1,
        ),
    ]
