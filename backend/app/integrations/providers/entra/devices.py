"""Microsoft Entra ID - registered / joined directory devices.

Distinct from Microsoft Intune (the dedicated `intune` integration manages MDM
device posture + remote actions): this is the Entra ID device *directory
object* surface - registration state, enable/disable, delete, and ownership.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import EntraClient, build_search_params, dt, fmt_list

_DEVICE_SELECT = (
    "id,deviceId,displayName,operatingSystem,operatingSystemVersion,"
    "accountEnabled,isCompliant,isManaged,trustType,approximateLastSignInDateTime"
)


def _fmt_device(d: dict[str, Any]) -> str:
    enabled = d.get("accountEnabled")
    state = "enabled" if enabled else ("disabled" if enabled is False else "?")
    compliant = d.get("isCompliant")
    comp = "compliant" if compliant else ("non-compliant" if compliant is False else "?")
    return (
        f"- {d.get('displayName', 'Unknown')} ({d.get('operatingSystem', '?')} "
        f"{d.get('operatingSystemVersion', '')})\n"
        f"  objectId: {d.get('id', '?')} | deviceId: {d.get('deviceId', '?')}\n"
        f"  account: {state} | {comp} | managed: {d.get('isManaged', '?')} | "
        f"trust: {d.get('trustType', '?')} | last seen: "
        f"{dt(d.get('approximateLastSignInDateTime'))}"
    )


def _fmt_owner(o: dict[str, Any]) -> str:
    label = o.get("displayName") or o.get("userPrincipalName") or "Unknown"
    upn = o.get("userPrincipalName")
    return f"- {label}{f' <{upn}>' if upn else ''} | id: {o.get('id', '?')}"


def register(
    client: EntraClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    src = "integration:entra"

    async def list_entra_devices(
        search: str = "", odata_filter: str = "", top: int = 25, skiptoken: str = ""
    ) -> str:
        params, extra = build_search_params(
            search=search,
            search_fields=("displayName",),
            odata_filter=odata_filter,
            select=_DEVICE_SELECT,
            skiptoken=skiptoken,
        )
        items, err, next_tok = await client.paged(
            "/devices",
            "list devices",
            top=min(top, 100),
            params=params,
            extra_headers=extra,
        )
        if err:
            return err
        return fmt_list(items, _fmt_device, noun="device", skiptoken=next_tok)

    async def get_entra_device(device_id: str) -> str:
        data, err = await client.get_json(
            f"/devices/{quote(device_id, safe='')}", "get device"
        )
        if err:
            return err
        return _fmt_device(data or {})

    async def manage_entra_device(
        action: str, device_id: str, top: int = 50, skiptoken: str = ""
    ) -> str:
        obj = f"/devices/{quote(device_id, safe='')}"
        if action in ("enable", "disable"):
            _, err = await client.request(
                "patch",
                obj,
                "enable/disable device",
                json={"accountEnabled": action == "enable"},
            )
            if err:
                return err
            return f"Device {device_id} {action}d."
        if action == "delete":
            _, err = await client.request("delete", obj, "delete device")
            return err or f"Deleted device object {device_id} from the directory."
        if action in ("list_registered_owners", "list_registered_users"):
            rel = (
                "registeredOwners"
                if action == "list_registered_owners"
                else "registeredUsers"
            )
            items, err, next_tok = await client.paged(
                f"{obj}/{rel}",
                f"list device {rel}",
                top=min(top, 100),
                params={"$skiptoken": skiptoken} if skiptoken else None,
            )
            if err:
                return err
            return fmt_list(
                items,
                _fmt_owner,
                noun="user",
                skiptoken=next_tok,
                empty_msg=f"Device has no {rel}.",
            )
        return (
            "Error: unknown action. Valid actions: enable, disable, delete, "
            "list_registered_owners, list_registered_users."
        )

    handlers = {
        "list_entra_devices": list_entra_devices,
        "get_entra_device": get_entra_device,
        "manage_entra_device": manage_entra_device,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    device_ref = {"type": "string", "description": "Device OBJECT id (GUID), not the deviceId."}
    return [
        ToolDefinition(
            name="list_entra_devices",
            description=(
                "List devices registered or joined to Entra ID. Returns object id, "
                "deviceId, OS, enabled/compliant state, trust type, and last sign-in. "
                "For MDM posture and remote actions (wipe/lock/sync) use the Microsoft "
                "Intune integration instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "odata_filter": {
                        "type": "string",
                        "description": "OData $filter, e.g. \"operatingSystem eq 'Windows'\" or \"accountEnabled eq false\".",
                    },
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_entra_device",
            description="Get a single Entra ID device object by its object id.",
            input_schema={
                "type": "object",
                "properties": {"device_id": device_ref},
                "required": ["device_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="manage_entra_device",
            description=(
                "Device sub-operations. action is one of: 'enable' / 'disable' (block "
                "the device from authenticating - use 'disable' for a lost/stolen "
                "device), 'delete' (remove the device object from the directory), "
                "'list_registered_owners', 'list_registered_users'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "enable",
                            "disable",
                            "delete",
                            "list_registered_owners",
                            "list_registered_users",
                        ],
                    },
                    "device_id": device_ref,
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token for the list_* actions (from a previous call's 'More results' hint).",
                    },
                },
                "required": ["action", "device_id"],
            },
            source=src,
            priority=1,
        ),
    ]
