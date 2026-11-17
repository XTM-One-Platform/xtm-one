"""Drata assets tools - Asset register."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import (
    _PROVIDER,
    _full_name,
)


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata assets tools and register their callables."""

    async def list_drata_assets(asset_class_type: str = "", asset_type: str = "") -> str:
        """List registered assets in Drata. Optionally filter by class type or asset type."""
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {"expand[]": ["device", "assetClassTypes", "owner", "customFields"]}
            if asset_class_type:
                params["assetClassType"] = asset_class_type
            if asset_type:
                params["assetType"] = asset_type
            items, err = await _common._paginated_fetch(client, f"{base_url}/assets", headers, params)
            if err:
                return err
            return format_entity_list(
                items,
                lambda a: (
                    f"[{a.get('id')}] {a.get('name', 'Unnamed')} — "
                    f"Type: {a.get('assetType', 'N/A')} | "
                    f"Provider: {a.get('assetProvider', 'N/A')} | "
                    f"Reference: {a.get('assetReferenceType', 'N/A')} | "
                    f"Owner: {_full_name(a.get('owner'))}"
                ),
                noun="asset",
            )

    async def get_drata_asset(asset_id: int) -> str:
        """Get full details of a specific asset including device and owner."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/assets/{asset_id}",
                _PROVIDER,
                "get asset",
                headers=headers,
                params={
                    "expand[]": [
                        "device",
                        "assetClassTypes",
                        "owner",
                        "complianceChecks",
                        "customFields",
                    ]
                },
            )
            if err:
                return err
            a = resp.json()
            lines = [
                f"Asset: {a.get('name', 'Unnamed')} (ID: {a.get('id')})",
                f"Description: {a.get('description', 'N/A')}",
                f"Type: {a.get('assetType', 'N/A')}",
                f"Provider: {a.get('assetProvider', 'N/A')}",
                f"Reference Type: {a.get('assetReferenceType', 'N/A')}",
                f"Associated ID: {a.get('associatedId', 'N/A')}",
                f"Notes: {a.get('notes', 'N/A')}",
                f"Owner: {_full_name(a.get('owner'))}",
                f"Created: {a.get('createdAt', 'N/A')}",
                f"Removed: {a.get('removedAt', 'N/A')}",
            ]
            device = a.get("device")
            if device:
                lines.append("\nDevice:")
                lines.append(f"  Model: {device.get('model', 'N/A')}")
                lines.append(f"  Serial: {device.get('serialNumber', 'N/A')}")
                lines.append(f"  OS: {device.get('osVersion', 'N/A')}")
                lines.append(f"  Compliant: {device.get('isDeviceCompliant', 'N/A')}")
                lines.append(
                    f"  Encryption: {device.get('encryptionEnabled', 'N/A')} | "
                    f"Firewall: {device.get('firewallEnabled', 'N/A')} | "
                    f"Antivirus: {device.get('antivirusEnabled', 'N/A')} | "
                    f"Auto Update: {device.get('autoUpdateEnabled', 'N/A')}"
                )
            return "\n".join(lines)

    async def create_drata_asset(
        name: str,
        description: str,
        asset_class_types: str,
        owner_id: int,
        asset_type: str = "PHYSICAL",
        notes: str = "",
        associated_id: str = "",
    ) -> str:
        """Create a new asset.

        ``asset_class_types`` is a CSV of class type enum values
        (HARDWARE / POLICY / DOCUMENT / PERSONNEL / SOFTWARE / CODE /
        CONTAINER / COMPUTE / NETWORKING / DATABASE / STORAGE).
        """
        classes = [c.strip().upper() for c in asset_class_types.split(",") if c.strip()]
        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "assetType": asset_type,
            "assetClassTypes": classes,
            "ownerId": owner_id,
        }
        if notes:
            payload["notes"] = notes
        if associated_id:
            payload["associatedId"] = associated_id
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/assets",
                _PROVIDER,
                "create asset",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            a = resp.json()
            return f"Asset '{name}' created (ID: {a.get('id', '?')})."

    async def update_drata_asset(
        asset_id: int,
        name: str = "",
        description: str = "",
        notes: str = "",
        owner_id: int = 0,
        asset_class_types: str = "",
    ) -> str:
        """Update an existing asset. Only provided fields are changed."""
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        if notes:
            payload["notes"] = notes
        if owner_id:
            payload["ownerId"] = owner_id
        if asset_class_types:
            payload["assetClassTypes"] = [c.strip().upper() for c in asset_class_types.split(",") if c.strip()]
        if not payload:
            return "Error: provide at least one field to update."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "put",
                f"{base_url}/assets/{asset_id}",
                _PROVIDER,
                "update asset",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Asset {asset_id} updated."

    async def remove_drata_asset(asset_id: int) -> str:
        """Remove (delete) a manually-added asset. This is unrecoverable."""
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/assets/{asset_id}",
                _PROVIDER,
                "remove asset",
                headers=headers,
            )
            if err:
                return err
            return f"Asset {asset_id} removed."

    callable_map = {
        "list_drata_assets": list_drata_assets,
        "get_drata_asset": get_drata_asset,
        "create_drata_asset": create_drata_asset,
        "update_drata_asset": update_drata_asset,
        "remove_drata_asset": remove_drata_asset,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="list_drata_assets",
            description="List registered assets in Drata. Optionally filter by asset class type or asset type.",
            input_schema={
                "type": "object",
                "properties": {
                    "asset_class_type": {
                        "type": "string",
                        "enum": [
                            "HARDWARE",
                            "POLICY",
                            "DOCUMENT",
                            "PERSONNEL",
                            "SOFTWARE",
                            "CODE",
                            "CONTAINER",
                            "COMPUTE",
                            "NETWORKING",
                            "DATABASE",
                            "STORAGE",
                        ],
                    },
                    "asset_type": {
                        "type": "string",
                        "enum": ["PHYSICAL", "VIRTUAL"],
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_asset",
            description="Get full details of a specific asset including device and owner.",
            input_schema={
                "type": "object",
                "properties": {
                    "asset_id": {"type": "integer"},
                },
                "required": ["asset_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="create_drata_asset",
            description="Manually create a new asset. asset_class_types is a CSV of class enum values.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "asset_class_types": {
                        "type": "string",
                        "description": "CSV like 'HARDWARE,COMPUTE'",
                    },
                    "owner_id": {"type": "integer"},
                    "asset_type": {
                        "type": "string",
                        "enum": ["PHYSICAL", "VIRTUAL"],
                        "default": "PHYSICAL",
                    },
                    "notes": {"type": "string"},
                    "associated_id": {"type": "string"},
                },
                "required": ["name", "description", "asset_class_types", "owner_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="update_drata_asset",
            description="Update an existing asset. Only provided fields are changed.",
            input_schema={
                "type": "object",
                "properties": {
                    "asset_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "notes": {"type": "string"},
                    "owner_id": {"type": "integer"},
                    "asset_class_types": {"type": "string"},
                },
                "required": ["asset_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="remove_drata_asset",
            description="Remove (delete) a manually-added or virtual asset. This is unrecoverable.",
            input_schema={
                "type": "object",
                "properties": {
                    "asset_id": {"type": "integer"},
                },
                "required": ["asset_id"],
            },
            source="integration:drata",
            priority=2,
        ),
    ]
