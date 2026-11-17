"""Microsoft Entra ID - tenant, domains, licenses, conditional access, recycle
bin, and tenant-wide password protection (Smart Lockout + banned passwords)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import EntraClient, dt, fmt_list, safe_json, trunc

# Fixed id of the "Password Rule Settings" directory-setting template. It holds
# the tenant-wide password protection configuration shown in the Entra admin
# center under Protection > Authentication methods > Password protection:
# the Smart Lockout threshold/duration and the banned-password policy. Until a
# tenant customizes it, NO settings object exists and the template's preset
# defaults (LockoutThreshold=10, LockoutDurationInSeconds=60) are in effect.
PASSWORD_RULE_TEMPLATE_ID = "5cf42378-d67d-4f36-ba46-e8b86229381d"  # noqa: S105


def _yn(value: Any) -> str:
    """Render a Graph string-typed boolean ("True"/"False") as enabled/disabled."""
    s = ("" if value is None else str(value)).strip().lower()
    if s in {"true", "1", "yes", "on"}:
        return "enabled"
    if s in {"false", "0", "no", "off"}:
        return "disabled"
    return str(value) if value not in (None, "") else "?"


def _fmt_password_protection(values: dict[str, Any], source_note: str) -> str:
    """Format the Password Rule Settings name->value map into a readable summary."""
    threshold = values.get("LockoutThreshold", "?")
    duration = values.get("LockoutDurationInSeconds", "?")
    banned_raw = values.get("BannedPasswordList") or ""
    # The custom banned-password list is a tab-delimited string.
    banned_count = len([w for w in str(banned_raw).split("\t") if w]) if banned_raw else 0
    return (
        f"Microsoft Entra password protection ({source_note}):\n"
        f"  Smart Lockout threshold: {threshold} failed sign-in attempts before "
        f"the first lockout\n"
        f"  Smart Lockout duration: {duration} seconds for the initial lockout "
        f"period (it grows on repeated lockouts)\n"
        f"  Cloud banned-password check: {_yn(values.get('EnableBannedPasswordCheck'))}\n"
        f"  On-premises banned-password protection: "
        f"{_yn(values.get('EnableBannedPasswordCheckOnPremises'))} "
        f"(mode: {values.get('BannedPasswordCheckOnPremisesMode', '?')})\n"
        f"  Custom banned-password entries: {banned_count}\n"
        f"  (Source: Password Rule Settings directory-setting template "
        f"{PASSWORD_RULE_TEMPLATE_ID}; requires Directory.Read.All. Smart Lockout "
        f"values map to the Entra 'Password protection' blade.)"
    )


def _fmt_sku(s: dict[str, Any]) -> str:
    consumed = s.get("consumedUnits", 0)
    prepaid = (s.get("prepaidUnits", {}) or {}).get("enabled", 0)
    available = prepaid - consumed
    return (
        f"- {s.get('skuPartNumber', '?')} | skuId: {s.get('skuId', '?')}\n"
        f"  assigned: {consumed}/{prepaid} | available: {available} | status: "
        f"{s.get('capabilityStatus', '?')}"
    )


def _fmt_deleted_user(u: dict[str, Any]) -> str:
    return (
        f"- {u.get('displayName', 'Unknown')} <{u.get('userPrincipalName', '?')}>\n"
        f"  id: {u.get('id', '?')} | deleted: {dt(u.get('deletedDateTime'))}"
    )


def _fmt_deleted_group(g: dict[str, Any]) -> str:
    # Deleted groups carry no userPrincipalName - key off displayName/mail/id.
    return (
        f"- {g.get('displayName', 'Unknown')} | id: {g.get('id', '?')} | "
        f"mail: {g.get('mail') or '-'} | deleted: {dt(g.get('deletedDateTime'))}"
    )


def register(
    client: EntraClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    src = "integration:entra"

    async def get_entra_organization() -> str:
        data, err = await client.get_json("/organization", "get organization")
        if err:
            return err
        orgs = (data or {}).get("value", []) or []
        if not orgs:
            return "No organization (tenant) information returned."
        o = orgs[0]
        domains = [
            d.get("name", "?")
            for d in (o.get("verifiedDomains", []) or [])
            if d.get("isInitial") or d.get("isDefault")
        ]
        return (
            f"Tenant: {o.get('displayName', '?')} (id: {o.get('id', '?')})\n"
            f"  country: {o.get('countryLetterCode', '?')} | created: "
            f"{dt(o.get('createdDateTime'))}\n"
            f"  primary domain(s): {', '.join(domains) or '-'}\n"
            f"  tenant type: {o.get('tenantType', '?')}"
        )

    async def list_entra_domains() -> str:
        data, err = await client.get_json("/domains", "list domains")
        if err:
            return err

        def _fmt(d: dict[str, Any]) -> str:
            flags = []
            if d.get("isDefault"):
                flags.append("default")
            if d.get("isInitial"):
                flags.append("initial")
            if d.get("isVerified"):
                flags.append("verified")
            return (
                f"- {d.get('id', '?')} ({', '.join(flags) or 'unverified'}) | auth: "
                f"{d.get('authenticationType', '?')}"
            )

        return fmt_list((data or {}).get("value", []), _fmt, noun="domain")

    async def list_entra_subscribed_skus() -> str:
        data, err = await client.get_json("/subscribedSkus", "list subscribed SKUs")
        if err:
            return err
        return fmt_list(
            (data or {}).get("value", []),
            _fmt_sku,
            noun="license SKU",
            empty_msg="No subscribed license SKUs found.",
        )

    async def list_entra_conditional_access_policies(
        top: int = 50, skiptoken: str = ""
    ) -> str:
        items, err, next_tok = await client.paged(
            "/identity/conditionalAccess/policies",
            "list conditional access policies",
            top=min(top, 100),
            params={"$skiptoken": skiptoken} if skiptoken else None,
        )
        if err:
            return err

        def _fmt(p: dict[str, Any]) -> str:
            grants = (p.get("grantControls", {}) or {}).get("builtInControls", []) or []
            return (
                f"- {p.get('displayName', 'Unnamed')} ({p.get('state', '?')})\n"
                f"  id: {p.get('id', '?')} | grant controls: "
                f"{', '.join(grants) or '-'}"
            )

        return fmt_list(
            items,
            _fmt,
            noun="conditional access policy",
            skiptoken=next_tok,
            empty_msg="No conditional access policies (requires Policy.Read.All + Entra ID P1/P2).",
        )

    async def list_entra_deleted_users(top: int = 25, skiptoken: str = "") -> str:
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.paged(
            "/directory/deletedItems/microsoft.graph.user",
            "list deleted users",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_deleted_user,
            noun="deleted user",
            skiptoken=next_tok,
            empty_msg="No recently deleted users in the recycle bin.",
        )

    async def _password_rule_template_defaults() -> tuple[dict[str, Any], str | None]:
        # When no tenant-wide Password Rule Settings object exists, the preset
        # template defaults are what's actually in effect. Read them from v1.0
        # (groupSettingTemplates) and fall back to beta (directorySettingTemplates)
        # for tenants where only the beta surface returns the template.
        for path in (
            f"/groupSettingTemplates/{PASSWORD_RULE_TEMPLATE_ID}",
            (
                "https://graph.microsoft.com/beta/directorySettingTemplates/"
                f"{PASSWORD_RULE_TEMPLATE_ID}"
            ),
        ):
            tmpl, terr = await client.get_json(
                path, "get password protection template defaults"
            )
            if terr:
                continue
            vals = {
                v.get("name"): v.get("defaultValue")
                for v in (tmpl or {}).get("values", []) or []
                if v.get("name")
            }
            if vals:
                return vals, None
        return {}, (
            "Error: could not read the Password Rule Settings template defaults "
            "from Microsoft Graph (tried v1.0 and beta). Confirm the integration "
            "has Directory.Read.All."
        )

    async def get_entra_password_protection() -> str:
        data, err = await client.get_json(
            "/groupSettings", "get password protection settings"
        )
        if err:
            return err
        configured = next(
            (
                s
                for s in (data or {}).get("value", []) or []
                if s.get("templateId") == PASSWORD_RULE_TEMPLATE_ID
            ),
            None,
        )
        if configured:
            values = {
                v.get("name"): v.get("value")
                for v in configured.get("values", []) or []
                if v.get("name")
            }
            return _fmt_password_protection(values, "tenant-customized values")
        values, terr = await _password_rule_template_defaults()
        if terr:
            return terr
        return _fmt_password_protection(
            values,
            "tenant DEFAULTS in effect - no custom Password Rule Settings object "
            "has been created, so these preset values apply",
        )

    async def manage_entra_directory(
        action: str, object_id: str = "", top: int = 50, skiptoken: str = ""
    ) -> str:
        if action == "restore_deleted_user":
            if not object_id:
                return "Error: object_id is required to restore a deleted item."
            resp, err = await client.request(
                "post",
                f"/directory/deletedItems/{quote(object_id, safe='')}/restore",
                "restore deleted directory object",
            )
            if err:
                return err
            r = safe_json(resp)
            return (
                f"Restored directory object {object_id} "
                f"({r.get('userPrincipalName') or r.get('displayName') or 'restored'})."
            )
        if action == "permanently_delete":
            if not object_id:
                return "Error: object_id is required to permanently delete an item."
            _, err = await client.request(
                "delete",
                f"/directory/deletedItems/{quote(object_id, safe='')}",
                "permanently delete directory object",
            )
            return err or (
                f"Permanently deleted object {object_id} from the recycle bin. "
                f"This cannot be undone."
            )
        if action == "list_deleted_groups":
            items, err, next_tok = await client.paged(
                "/directory/deletedItems/microsoft.graph.group",
                "list deleted groups",
                top=min(top, 100),
                params={"$skiptoken": skiptoken} if skiptoken else None,
            )
            if err:
                return err
            return fmt_list(
                items,
                _fmt_deleted_group,
                noun="deleted group",
                skiptoken=next_tok,
                empty_msg="No recently deleted groups in the recycle bin.",
            )
        if action == "list_named_locations":
            data, err = await client.get_json(
                "/identity/conditionalAccess/namedLocations",
                "list named locations",
            )
            if err:
                return err

            def _fmt_loc(loc: dict[str, Any]) -> str:
                kind = (loc.get("@odata.type") or "").split(".")[-1]
                return (
                    f"- {loc.get('displayName', 'Unnamed')} ({kind}) | id: "
                    f"{loc.get('id', '?')} | trusted: {loc.get('isTrusted', '?')}"
                )

            return fmt_list(
                (data or {}).get("value", []),
                _fmt_loc,
                noun="named location",
                empty_msg="No conditional-access named locations configured.",
            )
        if action == "get_conditional_access_policy":
            if not object_id:
                return "Error: object_id (policy id) is required."
            data, err = await client.get_json(
                f"/identity/conditionalAccess/policies/{quote(object_id, safe='')}",
                "get conditional access policy",
            )
            if err:
                return err
            return (
                f"Conditional Access Policy '{(data or {}).get('displayName', '?')}':\n"
                + trunc(json.dumps(data, indent=2, default=str), 2000)
            )
        return (
            "Error: unknown action. Valid actions: restore_deleted_user, "
            "permanently_delete, list_deleted_groups, list_named_locations, "
            "get_conditional_access_policy."
        )

    handlers = {
        "get_entra_organization": get_entra_organization,
        "list_entra_domains": list_entra_domains,
        "list_entra_subscribed_skus": list_entra_subscribed_skus,
        "list_entra_conditional_access_policies": (
            list_entra_conditional_access_policies
        ),
        "list_entra_deleted_users": list_entra_deleted_users,
        "get_entra_password_protection": get_entra_password_protection,
        "manage_entra_directory": manage_entra_directory,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    return [
        ToolDefinition(
            name="get_entra_organization",
            description="Get tenant (organization) details: name, tenant id, country, creation date, and primary domains.",
            input_schema={"type": "object", "properties": {}},
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_domains",
            description="List the tenant's domains with verification status and authentication type (federated vs managed).",
            input_schema={"type": "object", "properties": {}},
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_subscribed_skus",
            description=(
                "List the tenant's subscribed license SKUs with assigned vs available "
                "counts. Returns the skuId values needed by manage_entra_user "
                "(assign_license / remove_license). Use for license inventory and "
                "reclamation."
            ),
            input_schema={"type": "object", "properties": {}},
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_conditional_access_policies",
            description=(
                "List Conditional Access policies (name, state enabled/disabled/"
                "report-only, and grant controls like MFA). Requires Policy.Read.All "
                "+ Entra ID P1/P2. Use manage_entra_directory get_conditional_access_"
                "policy for the full policy JSON."
            ),
            input_schema={
                "type": "object",
                "properties": {
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
            name="list_entra_deleted_users",
            description=(
                "List users in the directory recycle bin (soft-deleted, recoverable "
                "for 30 days). Use manage_entra_directory restore_deleted_user to "
                "recover one, or permanently_delete to purge it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_entra_password_protection",
            description=(
                "Get the tenant-wide password protection configuration (the Entra "
                "'Password protection' blade): Smart Lockout threshold (failed "
                "sign-in attempts before lockout) and lockout duration, plus the "
                "banned-password policy (cloud + on-premises check, custom banned "
                "list size). Reads the 'Password Rule Settings' directory setting; "
                "if the tenant never customized it, the preset DEFAULTS in effect "
                "(threshold 10, duration 60s) are returned and clearly labelled as "
                "defaults. Requires Directory.Read.All. Use for identity-security "
                "posture and compliance evidence (e.g. brute-force / account "
                "lockout controls)."
            ),
            input_schema={"type": "object", "properties": {}},
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="manage_entra_directory",
            description=(
                "Consolidated directory/recycle-bin/policy sub-operations. action is "
                "one of: 'restore_deleted_user' (recover a soft-deleted object by id), "
                "'permanently_delete' (purge a recycled object - cannot be undone), "
                "'list_deleted_groups', 'list_named_locations' (Conditional Access "
                "named locations), 'get_conditional_access_policy' (full JSON by "
                "policy id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "restore_deleted_user",
                            "permanently_delete",
                            "list_deleted_groups",
                            "list_named_locations",
                            "get_conditional_access_policy",
                        ],
                    },
                    "object_id": {
                        "type": "string",
                        "description": "Object/policy id for restore, permanently_delete, or get_conditional_access_policy.",
                    },
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token for the list_deleted_groups action (from a previous call's 'More results' hint).",
                    },
                },
                "required": ["action"],
            },
            source=src,
            priority=1,
        ),
    ]
