# SPDX-License-Identifier: Apache-2.0

"""Microsoft Intune integration – device management, compliance, and security.

Provides tools for managing devices, compliance policies, configuration
profiles, detected applications, and device actions via the Microsoft
Graph API. The configuration, script, app and detected-app tools live in
``intune_management``.  Uses the same OAuth flow as the core Microsoft integration
but with Intune-specific scopes (DeviceManagement*).

Designed as a foundational building block for an agentic SOC: agents
can query device posture, check compliance drift, trigger remote
actions (lock, wipe, sync), and feed findings into incident workflows.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import AutoRefreshClient, format_entity_list, safe_request
from app.integrations.providers.intune_management import management_tools
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Microsoft Intune"
_BASE_URL = "https://graph.microsoft.com/v1.0"


class IntuneIntegration(BaseIntegration):
    provider_name = "intune"
    display_name = "Microsoft Intune"
    icon = "Shield"
    description = "Connect Microsoft Intune for device management, compliance monitoring, and endpoint security."
    auth_types = ["oauth"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",  # noqa: S106
            # OAuth token exchange endpoint URL, not a secret.
            scopes=[
                "openid",
                "profile",
                "email",
                "offline_access",
                "DeviceManagementManagedDevices.Read.All",
                "DeviceManagementManagedDevices.ReadWrite.All",
                "DeviceManagementManagedDevices.PrivilegedOperations.All",
                "DeviceManagementConfiguration.Read.All",
                "DeviceManagementConfiguration.ReadWrite.All",
                "DeviceManagementApps.Read.All",
                "DeviceManagementApps.ReadWrite.All",
                "DeviceManagementScripts.ReadWrite.All",
                "DeviceManagementServiceConfig.Read.All",
                "DeviceManagementRBAC.Read.All",
            ],
        )

    async def validate_connection(self, token: str) -> bool:
        async with egress_client() as client:
            resp = await client.get(
                f"{_BASE_URL}/deviceManagement",
                headers={"Authorization": f"Bearer {token}"},
            )
            return resp.status_code == 200

    async def get_user_info(self, token: str) -> dict:
        async with egress_client() as client:
            resp = await client.get(
                f"{_BASE_URL}/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _fmt_device(d: dict[str, Any]) -> str:
    name = d.get("deviceName", "Unknown")
    os = d.get("operatingSystem", "?")
    os_ver = d.get("osVersion", "?")
    compliance = d.get("complianceState", "unknown")
    mgmt = d.get("managementAgent", "?")
    last_sync = d.get("lastSyncDateTime", "never")
    owner = d.get("userPrincipalName", "unassigned")
    enrolled = d.get("enrolledDateTime", "?")
    model = d.get("model", "?")
    manufacturer = d.get("manufacturer", "?")
    serial = d.get("serialNumber", "?")
    device_id = d.get("id", "?")
    return (
        f"• {name} (ID: {device_id})\n"
        f"  OS: {os} {os_ver} | Model: {manufacturer} {model} | Serial: {serial}\n"
        f"  Compliance: {compliance} | Management: {mgmt}\n"
        f"  Owner: {owner} | Enrolled: {enrolled} | Last sync: {last_sync}"
    )


def _fmt_compliance_policy(p: dict[str, Any]) -> str:
    name = p.get("displayName", "Unnamed")
    pid = p.get("id", "?")
    created = p.get("createdDateTime", "?")
    modified = p.get("lastModifiedDateTime", "?")
    return f"• {name} (ID: {pid}) — Created: {created}, Modified: {modified}"


def _fmt_config_profile(p: dict[str, Any]) -> str:
    name = p.get("displayName", "Unnamed")
    pid = p.get("id", "?")
    platform = p.get("platformType", p.get("@odata.type", "?"))
    created = p.get("createdDateTime", "?")
    return f"• {name} (ID: {pid}) — Platform: {platform}, Created: {created}"


def _fmt_device_category(c: dict[str, Any]) -> str:
    name = c.get("displayName", "Unnamed")
    cid = c.get("id", "?")
    desc = c.get("description", "")
    return f"• {name} (ID: {cid}){f' — {desc}' if desc else ''}"


def _fmt_compliance_status(s: dict[str, Any]) -> str:
    device = s.get("deviceDisplayName", "Unknown")
    user = s.get("userName", "?")
    status = s.get("status", "?")
    last = s.get("lastReportedDateTime", "?")
    return f"• {device} ({user}) — Status: {status}, Last reported: {last}"


def _fmt_role_assignment(r: dict[str, Any]) -> str:
    name = r.get("displayName", "Unnamed")
    rid = r.get("id", "?")
    desc = r.get("description", "")
    return f"• {name} (ID: {rid}){f' — {desc}' if desc else ''}"


# ---------------------------------------------------------------------------
# Device filters
# ---------------------------------------------------------------------------

#: The managedDevices properties a ``$filter`` can name, listed when Graph refuses one.
_FILTERABLE_DEVICE_PROPERTIES = (
    "deviceName",
    "userPrincipalName",
    "serialNumber",
    "operatingSystem",
    "complianceState",
    "managementAgent",
    "azureADDeviceId",
    "lastSyncDateTime",
)


def _odata_quote(value: str) -> str:
    """Escape a value for a single-quoted OData string literal (``'`` doubled)."""
    return value.replace("'", "''")


def _device_search_clause(search: str) -> str:
    """The ``$filter`` clause a free-text device search stands for.

    Graph answers ``$search`` on ``managedDevices`` with HTTP 400, so a search
    is a prefix match on the device name, or an exact match on the owner's UPN
    when the value is an email. Graph compares ``startswith`` case-insensitively,
    so the value is sent as given.
    """
    term = _odata_quote(search)
    if "@" in search:
        return f"userPrincipalName eq '{term}'"
    return f"startswith(deviceName,'{term}')"


def _device_filter(
    *,
    odata_filter: str = "",
    search: str = "",
    device_name: str = "",
    user_principal_name: str = "",
    compliance_state: str = "",
    operating_system: str = "",
) -> str:
    """Every device criterion as one ``$filter`` expression joined with ``and``.

    The caller's ``odata_filter`` is parenthesised when other criteria join it:
    ``and`` binds tighter than ``or``, so an ``or`` inside it would otherwise
    escape them.
    """
    clauses: list[str] = []
    if compliance_state:
        clauses.append(f"complianceState eq '{_odata_quote(compliance_state)}'")
    if operating_system:
        clauses.append(f"operatingSystem eq '{_odata_quote(operating_system)}'")
    if device_name:
        clauses.append(f"deviceName eq '{_odata_quote(device_name)}'")
    if user_principal_name:
        clauses.append(f"userPrincipalName eq '{_odata_quote(user_principal_name)}'")
    if search:
        clauses.append(_device_search_clause(search))
    if odata_filter:
        clauses.insert(0, f"({odata_filter})" if clauses else odata_filter)
    return " and ".join(clauses)


def _graph_error_message(resp: httpx.Response) -> str:
    """Graph's own ``error.message``, else the start of the raw body."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:500]
    return resp.text[:500]


def _device_filter_refusal(resp: httpx.Response, odata_filter: str) -> str:
    """A Graph 400 on managedDevices: Graph's message verbatim, then what can be filtered."""
    sent = f" with $filter={odata_filter}" if odata_filter else ""
    return (
        f"Error: {_PROVIDER} rejected the request to list managed devices{sent} "
        f"(HTTP 400): {_graph_error_message(resp)}\n"
        f"Hint: managedDevices can be filtered on {', '.join(_FILTERABLE_DEVICE_PROPERTIES)}."
    )


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    refresh_fn: Callable[[], Awaitable[str | None]] | None = None,
    **_kwargs,
) -> list[ToolDefinition]:
    # ``**_kwargs`` absorbs request-scoped extras the dispatcher forwards to all
    # providers (e.g. ``scope_headers``); without it those chat sessions raised
    # TypeError and every Intune tool was silently dropped.
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    def _client() -> AutoRefreshClient:
        return AutoRefreshClient(headers, refresh_fn)

    # ── List managed devices ─────────────────────────────────────────────

    async def intune_list_devices(
        top: int = 25,
        filter: str = "",
        search: str = "",
        compliance_state: str = "",
        operating_system: str = "",
        device_name: str = "",
        user_principal_name: str = "",
    ) -> str:
        params: dict[str, Any] = {"$top": max(1, min(top, 100))}
        odata_filter = _device_filter(
            odata_filter=filter.strip(),
            search=search.strip(),
            device_name=device_name.strip(),
            user_principal_name=user_principal_name.strip(),
            compliance_state=compliance_state.strip(),
            operating_system=operating_system.strip(),
        )
        if odata_filter:
            params["$filter"] = odata_filter

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/deviceManagement/managedDevices",
                _PROVIDER,
                "list managed devices",
                expected_statuses=set(),
                params=params,
            )
            if err:
                if resp is not None and resp.status_code == 400:
                    return _device_filter_refusal(resp, odata_filter)
                return err
            data = resp.json()  # type: ignore[union-attr]
            devices = data.get("value", [])
            return format_entity_list(devices, _fmt_device, noun="device")

    # ── Get device details ───────────────────────────────────────────────

    async def intune_get_device(device_id: str) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/deviceManagement/managedDevices/{device_id}",
                _PROVIDER,
                "get device details",
            )
            if err:
                return err
            d = resp.json()  # type: ignore[union-attr]
            return _fmt_device(d)

    # ── Remote actions ───────────────────────────────────────────────────

    async def intune_sync_device(device_id: str) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/deviceManagement/managedDevices/{device_id}/syncDevice",
                _PROVIDER,
                "sync device",
                content=b"",
            )
            if err:
                return err
            return f"Sync initiated for device {device_id}."

    async def intune_reboot_device(device_id: str) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/deviceManagement/managedDevices/{device_id}/rebootNow",
                _PROVIDER,
                "reboot device",
                content=b"",
            )
            if err:
                return err
            return f"Reboot initiated for device {device_id}."

    async def intune_lock_device(device_id: str) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/deviceManagement/managedDevices/{device_id}/remoteLock",
                _PROVIDER,
                "remote lock device",
                content=b"",
            )
            if err:
                return err
            return f"Remote lock initiated for device {device_id}."

    async def intune_retire_device(device_id: str) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/deviceManagement/managedDevices/{device_id}/retire",
                _PROVIDER,
                "retire device",
                content=b"",
            )
            if err:
                return err
            return f"Retire initiated for device {device_id}. Corporate data will be removed."

    async def intune_wipe_device(device_id: str, keep_enrollment: bool = False) -> str:
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/deviceManagement/managedDevices/{device_id}/wipe",
                _PROVIDER,
                "wipe device",
                json={"keepEnrollmentData": keep_enrollment, "keepUserData": False},
            )
            if err:
                return err
            return f"Wipe initiated for device {device_id}. This is irreversible."

    # ── Compliance policies ──────────────────────────────────────────────

    async def intune_list_compliance_policies(top: int = 50) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/deviceManagement/deviceCompliancePolicies",
                _PROVIDER,
                "list compliance policies",
                params={"$top": min(top, 100)},
            )
            if err:
                return err
            data = resp.json()  # type: ignore[union-attr]
            policies = data.get("value", [])
            return format_entity_list(
                policies,
                _fmt_compliance_policy,
                noun="compliance policy",
                empty_msg="No compliance policies found. Compliance policies define the rules devices must meet.",
            )

    async def intune_get_compliance_policy(policy_id: str) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/deviceManagement/deviceCompliancePolicies/{policy_id}",
                _PROVIDER,
                "get compliance policy",
            )
            if err:
                return err
            p = resp.json()  # type: ignore[union-attr]
            import json

            return f"Compliance Policy: {p.get('displayName', 'Unnamed')}\n{json.dumps(p, indent=2, default=str)}"

    async def intune_get_compliance_policy_device_statuses(
        policy_id: str, top: int = 50
    ) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/deviceManagement/deviceCompliancePolicies/{policy_id}/deviceStatuses",
                _PROVIDER,
                "get compliance policy device statuses",
                params={"$top": min(top, 100)},
            )
            if err:
                return err
            data = resp.json()  # type: ignore[union-attr]
            statuses = data.get("value", [])
            return format_entity_list(
                statuses, _fmt_compliance_status, noun="device status"
            )

    # ── Configuration profiles ───────────────────────────────────────────

    async def intune_list_config_profiles(top: int = 50) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/deviceManagement/deviceConfigurations",
                _PROVIDER,
                "list configuration profiles",
                params={"$top": min(top, 100)},
            )
            if err:
                return err
            data = resp.json()  # type: ignore[union-attr]
            profiles = data.get("value", [])
            return format_entity_list(
                profiles, _fmt_config_profile, noun="configuration profile"
            )

    async def intune_get_config_profile(profile_id: str) -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/deviceManagement/deviceConfigurations/{profile_id}",
                _PROVIDER,
                "get configuration profile",
            )
            if err:
                return err
            p = resp.json()  # type: ignore[union-attr]
            import json

            return f"Configuration Profile: {p.get('displayName', 'Unnamed')}\n{json.dumps(p, indent=2, default=str)}"

    # ── Device categories ────────────────────────────────────────────────

    async def intune_list_device_categories() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/deviceManagement/deviceCategories",
                _PROVIDER,
                "list device categories",
            )
            if err:
                return err
            data = resp.json()  # type: ignore[union-attr]
            cats = data.get("value", [])
            return format_entity_list(
                cats, _fmt_device_category, noun="device category"
            )

    # ── Role assignments (RBAC) ──────────────────────────────────────────

    async def intune_list_role_assignments() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/deviceManagement/roleAssignments",
                _PROVIDER,
                "list role assignments",
            )
            if err:
                return err
            data = resp.json()  # type: ignore[union-attr]
            roles = data.get("value", [])
            return format_entity_list(
                roles, _fmt_role_assignment, noun="role assignment"
            )

    # ── Device compliance overview ───────────────────────────────────────

    async def intune_compliance_overview() -> str:
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/deviceManagement/managedDevices",
                _PROVIDER,
                "get compliance overview",
                params={"$top": 999, "$select": "complianceState,operatingSystem"},
            )
            if err:
                return err
            data = resp.json()  # type: ignore[union-attr]
            devices = data.get("value", [])
            total = len(devices)
            if total == 0:
                return "No managed devices found."

            states: dict[str, int] = {}
            os_counts: dict[str, int] = {}
            for d in devices:
                cs = d.get("complianceState", "unknown")
                states[cs] = states.get(cs, 0) + 1
                os_name = d.get("operatingSystem", "Unknown")
                os_counts[os_name] = os_counts.get(os_name, 0) + 1

            lines = [f"Compliance Overview ({total} devices):"]
            lines.append("\nBy compliance state:")
            for state, count in sorted(states.items(), key=lambda x: -x[1]):
                pct = count * 100 / total
                lines.append(f"  • {state}: {count} ({pct:.1f}%)")
            lines.append("\nBy operating system:")
            for os_name, count in sorted(os_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  • {os_name}: {count}")
            return "\n".join(lines)

    # ── Register all tools ───────────────────────────────────────────────

    executor.register("intune_list_devices", intune_list_devices)
    executor.register("intune_get_device", intune_get_device)
    executor.register("intune_sync_device", intune_sync_device)
    executor.register("intune_reboot_device", intune_reboot_device)
    executor.register("intune_lock_device", intune_lock_device)
    executor.register("intune_retire_device", intune_retire_device)
    executor.register("intune_wipe_device", intune_wipe_device)
    executor.register(
        "intune_list_compliance_policies", intune_list_compliance_policies
    )
    executor.register("intune_get_compliance_policy", intune_get_compliance_policy)
    executor.register(
        "intune_get_compliance_policy_device_statuses",
        intune_get_compliance_policy_device_statuses,
    )
    executor.register("intune_list_config_profiles", intune_list_config_profiles)
    executor.register("intune_get_config_profile", intune_get_config_profile)
    executor.register("intune_list_device_categories", intune_list_device_categories)
    executor.register("intune_list_role_assignments", intune_list_role_assignments)
    executor.register("intune_compliance_overview", intune_compliance_overview)

    device_tools = [
        ToolDefinition(
            name="intune_list_devices",
            description="List managed devices in Microsoft Intune. Filter by device name, owner, compliance state, OS, or search. Returns device name, OS, compliance status, owner, and last sync time.",
            input_schema={
                "type": "object",
                "properties": {
                    "top": {
                        "type": "integer",
                        "description": "Max results (1-100, default 25)",
                        "default": 25,
                    },
                    "filter": {
                        "type": "string",
                        "description": (
                            "OData $filter expression, combined with the other criteria using 'and' "
                            f"(e.g. \"managementAgent eq 'mdm'\"). Filterable: {', '.join(_FILTERABLE_DEVICE_PROPERTIES)}"
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": "Matched with startswith on deviceName (or userPrincipalName when the value is an email)",
                    },
                    "device_name": {
                        "type": "string",
                        "description": "Exact device name (deviceName eq)",
                    },
                    "user_principal_name": {
                        "type": "string",
                        "description": "Exact owner UPN, e.g. 'jane.doe@contoso.com' (userPrincipalName eq)",
                    },
                    "compliance_state": {
                        "type": "string",
                        "description": "Filter by compliance state",
                        "enum": [
                            "compliant",
                            "noncompliant",
                            "conflict",
                            "error",
                            "inGracePeriod",
                            "unknown",
                        ],
                    },
                    "operating_system": {
                        "type": "string",
                        "description": "Filter by OS (e.g. 'Windows', 'iOS', 'Android', 'macOS')",
                    },
                },
            },
        ),
        ToolDefinition(
            name="intune_get_device",
            description="Get detailed information about a specific managed device by its ID. Returns full device properties including hardware info, compliance state, and enrollment details.",
            input_schema={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "The Intune managed device ID",
                    },
                },
                "required": ["device_id"],
            },
        ),
        ToolDefinition(
            name="intune_sync_device",
            description="Trigger a sync for a managed device. Forces the device to check in with Intune and apply any pending policies or configurations.",
            input_schema={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "The Intune managed device ID",
                    },
                },
                "required": ["device_id"],
            },
        ),
        ToolDefinition(
            name="intune_reboot_device",
            description="Remotely reboot a managed device. The device will restart immediately.",
            input_schema={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "The Intune managed device ID",
                    },
                },
                "required": ["device_id"],
            },
        ),
        ToolDefinition(
            name="intune_lock_device",
            description="Remotely lock a managed device. Useful for lost or stolen devices. The device screen will lock immediately.",
            input_schema={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "The Intune managed device ID",
                    },
                },
                "required": ["device_id"],
            },
        ),
        ToolDefinition(
            name="intune_retire_device",
            description="Retire a managed device. Removes corporate data while keeping personal data. Use for BYOD devices when an employee leaves.",
            input_schema={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "The Intune managed device ID",
                    },
                },
                "required": ["device_id"],
            },
        ),
        ToolDefinition(
            name="intune_wipe_device",
            description="Factory-reset a managed device. IRREVERSIBLE — erases all data. Use only for lost/stolen corporate devices or severe compromise.",
            input_schema={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "The Intune managed device ID",
                    },
                    "keep_enrollment": {
                        "type": "boolean",
                        "description": "Keep the device enrolled after wipe (default false)",
                        "default": False,
                    },
                },
                "required": ["device_id"],
            },
        ),
        ToolDefinition(
            name="intune_list_compliance_policies",
            description="List all device compliance policies. Compliance policies define rules that devices must meet (password requirements, encryption, OS version, etc.).",
            input_schema={
                "type": "object",
                "properties": {
                    "top": {
                        "type": "integer",
                        "description": "Max results (1-100, default 50)",
                        "default": 50,
                    },
                },
            },
        ),
        ToolDefinition(
            name="intune_get_compliance_policy",
            description="Get full details of a specific compliance policy including all configured rules and settings.",
            input_schema={
                "type": "object",
                "properties": {
                    "policy_id": {
                        "type": "string",
                        "description": "The compliance policy ID",
                    },
                },
                "required": ["policy_id"],
            },
        ),
        ToolDefinition(
            name="intune_get_compliance_policy_device_statuses",
            description="Get the per-device compliance status for a specific policy. Shows which devices are compliant, non-compliant, or in error for this policy.",
            input_schema={
                "type": "object",
                "properties": {
                    "policy_id": {
                        "type": "string",
                        "description": "The compliance policy ID",
                    },
                    "top": {
                        "type": "integer",
                        "description": "Max results (1-100, default 50)",
                        "default": 50,
                    },
                },
                "required": ["policy_id"],
            },
        ),
        ToolDefinition(
            name="intune_list_config_profiles",
            description="List device configuration profiles. These define settings pushed to devices (Wi-Fi, VPN, email, restrictions, etc.).",
            input_schema={
                "type": "object",
                "properties": {
                    "top": {
                        "type": "integer",
                        "description": "Max results (1-100, default 50)",
                        "default": 50,
                    },
                },
            },
        ),
        ToolDefinition(
            name="intune_get_config_profile",
            description="Get full details of a specific device configuration profile including all configured settings.",
            input_schema={
                "type": "object",
                "properties": {
                    "profile_id": {
                        "type": "string",
                        "description": "The configuration profile ID",
                    },
                },
                "required": ["profile_id"],
            },
        ),
        ToolDefinition(
            name="intune_list_device_categories",
            description="List device categories configured in Intune. Categories help organize devices for targeting policies.",
            input_schema={"type": "object", "properties": {}},
        ),
        ToolDefinition(
            name="intune_list_role_assignments",
            description="List Intune RBAC role assignments. Shows who has what administrative permissions in Intune.",
            input_schema={"type": "object", "properties": {}},
        ),
        ToolDefinition(
            name="intune_compliance_overview",
            description="Get a high-level compliance overview across all managed devices. Returns compliance state distribution and OS breakdown — ideal for SOC dashboards and posture reports.",
            input_schema={"type": "object", "properties": {}},
        ),
    ]
    # Configuration policies, scripts, apps and detected apps: intune_management.
    return [*device_tools, *management_tools(_client, executor)]
